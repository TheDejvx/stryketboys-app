from flask import Flask, render_template, jsonify, request
import json, os, re, threading, time, base64, io
from datetime import datetime, timezone
from difflib import SequenceMatcher
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image

app = Flask(__name__)
DATA_FILE = os.path.join(os.path.dirname(__file__), 'stryk_data.json')

_db = None

def get_db():
    global _db
    if _db is not None:
        return _db
    uri = os.environ.get('MONGODB_URI')
    if not uri:
        return None
    try:
        from pymongo import MongoClient
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        client.admin.command('ping')
        _db = client.get_default_database()
        print('MongoDB connected')
    except Exception as e:
        print(f'MongoDB error: {e}')
    return _db

def load_data():
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        fallback = json.load(f)
    db = get_db()
    if db is not None:
        doc = db.stryk_state.find_one({'_id': 'current'})
        if doc:
            doc.pop('_id')
            return doc
        db.stryk_state.insert_one({'_id': 'current', **fallback})
    return fallback

def save_data(data):
    db = get_db()
    if db is not None:
        db.stryk_state.replace_one({'_id': 'current'}, {'_id': 'current', **data}, upsert=True)
    else:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def find_user(data, username):
    username = (username or '').strip().lower()
    for u in data.get('users', []):
        if u.get('username', '').lower() == username:
            return u
    return None

def public_data(data):
    """Strip password hashes before this ever reaches the client."""
    return {
        **data,
        'users': [
            {
                'username': u.get('username'),
                'display_name': u.get('display_name'),
                'must_change_password': u.get('must_change_password', False),
            }
            for u in data.get('users', [])
        ],
    }


# ── SVENSKA SPEL DRAW DATA ──

_draw_cache = None
_last_scraped = None

def parse_odds(val):
    if not val:
        return None
    try:
        return float(str(val).replace(',', '.'))
    except Exception:
        return None

def fetch_draw():
    """Pull the current Stryktipset draw (matches, odds, live status) from
    Svenska Spel's public draws API — no auth, no scraping needed."""
    global _draw_cache, _last_scraped
    import requests as req
    try:
        r = req.get('https://api.spela.svenskaspel.se/draw/1/stryktipset/draws',
                     params={'numberOfDraws': 1},
                     headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        r.raise_for_status()
        payload = r.json()
        draws = payload.get('draws') or []
        if not draws:
            return _draw_cache
        d = draws[0]

        events = []
        for ev in d.get('drawEvents', []):
            m = ev.get('match', {}) or {}
            participants = m.get('participants', []) or []
            home = next((p.get('name', '') for p in participants if p.get('type') == 'home'), '')
            away = next((p.get('name', '') for p in participants if p.get('type') == 'away'), '')
            odds = ev.get('odds', {}) or {}
            events.append({
                'row_num': ev.get('eventNumber'),
                'match_id': m.get('matchId'),
                'home': home,
                'away': away,
                'league': (m.get('league') or {}).get('name', ''),
                'match_start': m.get('matchStart'),
                'status': m.get('status'),
                'sport_event_status': m.get('sportEventStatus'),
                'result': m.get('result'),
                'odds_1': parse_odds(odds.get('one')),
                'odds_x': parse_odds(odds.get('x')),
                'odds_2': parse_odds(odds.get('two')),
            })
        events.sort(key=lambda x: x['row_num'] or 0)

        _draw_cache = {
            'draw_number': d.get('drawNumber'),
            'draw_state': d.get('drawState'),
            'reg_close_time': d.get('regCloseTime'),
            'current_net_sale': d.get('currentNetSale'),
            'events': events,
        }
        _last_scraped = datetime.now(timezone.utc)
        print(f"Stryktipset: fetched draw {_draw_cache['draw_number']} with {len(events)} matches")
    except Exception as e:
        print(f'Draw fetch error: {e}')
    return _draw_cache

def background_poller():
    time.sleep(5)
    while True:
        try:
            fetch_draw()
        except Exception as e:
            print(f'Background poller error: {e}')
        time.sleep(90)

threading.Thread(target=background_poller, daemon=True).start()


# ── COUPON DECODE (Claude vision) ──

_anthropic_client = None

def get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        key = os.environ.get('ANTHROPIC_API_KEY')
        if not key:
            return None
        from anthropic import Anthropic
        _anthropic_client = Anthropic(api_key=key)
    return _anthropic_client

DESCRIBE_PROMPT = (
    "This is a screenshot of a Svenska Spel Stryktipset betting coupon (Swedish football pool betting). It "
    "has a numbered list of matches (usually 13), each with three small pill-shaped buttons in a fixed "
    "left-to-right order: '1', 'X', '2'. A pill is SELECTED if its background is a solid dark navy blue with "
    "white text, and NOT selected if its background is white/very light with a thin gray border and dark "
    "text.\n\n"
    "Process the rows ONE AT A TIME, in strict order from the first row to the last. Do NOT skip ahead and do "
    "NOT batch multiple rows together — for EVERY row, immediately after analyzing it, output its result line "
    "before moving to the next row.\n\n"
    "Before row 1, output one line: SYSTEM_TYPE: <the label near the top of the coupon, e.g. 'M-system', "
    "'B-system', 'Helsystem', or 'Enkelrad' if none is visible>\n\n"
    "Then for every single row, output exactly this two-part block, in order:\n"
    "Analysis: name the two teams, then describe what you see for the '1', 'X', '2' pills individually — do "
    "not assume a pattern from previous rows, and do not stop looking after finding the first selected pill. "
    "It is common and expected for two pills to be selected on the same row at once (e.g. X and 2 both "
    "selected, 1 empty) — this is a normal 'garderad rad' (system bet row), not an error.\n"
    "ROW <n> | <home team> - <away team> | <kickoff text> | Y=<0.00-1.00> | 1=<0 or 1> X=<0 or 1> 2=<0 or 1>\n\n"
    "Y is the vertical position of THIS row's own text within the full image, as a fraction of total image "
    "height (0.00 = very top of the image, 1.00 = very bottom) — a rough estimate of where you're reading "
    "this row's team names from is fine, it doesn't need pixel precision. Use 1 for selected, 0 for not "
    "selected in the ROW line. Example of one complete row's block:\n"
    "Analysis: The 1 pill is white with a gray border — not selected. The X pill is solid dark navy with "
    "white text — selected. The 2 pill is also solid dark navy with white text — selected.\n"
    "ROW 7 | Cardiff - Sheffield U | Idag 16:00 | Y=0.53 | 1=0 X=1 2=1\n\n"
    "Begin now with SYSTEM_TYPE, then Row 1's analysis and ROW line, then Row 2's, continuing strictly in "
    "order through every row visible on the coupon. Do not skip any row."
)

ROW_LINE_RE = re.compile(
    r'ROW\s+(\d+)\s*\|\s*(.+?)\s*-\s*(.+?)\s*\|\s*(.*?)\s*\|\s*Y=([\d.]+)\s*\|\s*1=([01])\s+X=([01])\s+2=([01])',
    re.IGNORECASE,
)
SYSTEM_TYPE_RE = re.compile(r'SYSTEM_TYPE:\s*(.+)', re.IGNORECASE)

def parse_decode_analysis(text):
    """Deterministically parse every 'ROW <n> | ...' result line out of the model's response
    (interleaved one per row, right after that row's own analysis) — no second LLM call
    involved, so nothing can get lost in an LLM 're-transcribing itself' step. Each row reports
    its OWN vertical text position (Y) rather than interpolating from two endpoints across all
    13 rows — team-name text is something the model reads reliably, unlike small pill colors, and
    per-row anchoring avoids the compounding drift a 2-point interpolation produced."""
    rows = []
    for m in ROW_LINE_RE.finditer(text):
        row_num, home, away, kickoff, y, one, x, two = m.groups()
        picks = []
        if one == '1': picks.append('1')
        if x == '1': picks.append('X')
        if two == '1': picks.append('2')
        y = float(y)
        rows.append({
            'row_num': int(row_num),
            'home': home.strip(),
            'away': away.strip(),
            'kickoff_time': kickoff.strip(),
            'y': y if 0.0 <= y <= 1.0 else None,
            'picks': picks or ['1'],
            'zero_picks_read': not picks,
        })
    sys_match = SYSTEM_TYPE_RE.search(text)
    system_type = sys_match.group(1).strip() if sys_match else 'Enkelrad'
    return system_type, rows

def fuzzy_match_event(home, away, events):
    query = f'{home} {away}'.lower().strip()
    best, best_score = None, 0.0
    for ev in events:
        candidate = f"{ev['home']} {ev['away']}".lower().strip()
        score = SequenceMatcher(None, query, candidate).ratio()
        if score > best_score:
            best, best_score = ev, score
    return best, best_score

@app.route('/api/coupon/decode', methods=['POST'])
def decode_coupon():
    client = get_anthropic()
    if client is None:
        return jsonify({'status': 'error', 'message': 'ANTHROPIC_API_KEY not configured on the server'}), 500
    if 'image' not in request.files:
        return jsonify({'status': 'error', 'message': 'No image uploaded'}), 400

    img = request.files['image']
    img_bytes = img.read()
    media_type = img.mimetype or 'image/jpeg'

    try:
        pil_img = Image.open(io.BytesIO(img_bytes))
        pil_img.load()
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Kunde inte läsa bildformatet: {e}'}), 400

    b64 = base64.b64encode(img_bytes).decode('utf-8')
    image_block = {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': b64}}

    try:
        # Single call: the model reasons in plain text first, then ends with a strict
        # machine-readable summary block that we parse with a regex — no second LLM call
        # "transcribing" its own analysis. This is the AI's best-effort first draft; the
        # frontend always shows it as an editable preview (with the original photo visible
        # alongside it) rather than expecting a perfect unassisted read.
        resp = client.messages.create(
            model='claude-sonnet-5',
            max_tokens=4000,
            messages=[{
                'role': 'user',
                'content': [image_block, {'type': 'text', 'text': DESCRIBE_PROMPT}],
            }],
        )
        analysis_text = ''.join(b.text for b in resp.content if b.type == 'text')
        if not analysis_text.strip():
            return jsonify({'status': 'error', 'message': 'Model returned no analysis'}), 500
        print('--- coupon decode raw analysis ---')
        print(analysis_text)
        print('--- end raw analysis ---')
        system_type, rows = parse_decode_analysis(analysis_text)
        if not rows:
            return jsonify({'status': 'error', 'message': 'Could not parse the model’s analysis — try again'}), 500
        if len(rows) != 13:
            missing = sorted(set(range(1, 14)) - {r['row_num'] for r in rows})
            print(f'WARNING: parsed {len(rows)} rows, expected 13 — missing row(s): {missing}')
            return jsonify({
                'status': 'error',
                'message': f'Läste bara {len(rows)} av 13 rader (saknar rad {", ".join(map(str, missing))}) — försök igen',
            }), 500
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

    draw = _draw_cache or fetch_draw()
    events = (draw or {}).get('events', [])

    for row in rows:
        match, score = fuzzy_match_event(row.get('home', ''), row.get('away', ''), events)
        if match and score > 0.55:
            row['match_id'] = match['match_id']
            row['match_start'] = match['match_start']
            row['low_confidence'] = row['zero_picks_read']
        else:
            row['match_id'] = None
            row['match_start'] = None
            row['low_confidence'] = True

    return jsonify({
        'status': 'ok',
        'system_type': system_type,
        'rows': rows,
        'draw_number': (draw or {}).get('draw_number'),
    })

@app.route('/api/coupon/save', methods=['POST'])
def save_coupon():
    payload = request.json or {}
    data = load_data()
    weeks = data.setdefault('weeks', [])

    week_id = payload.get('id') or f"week-{payload.get('draw_number', 'x')}-{int(time.time())}"
    week = {
        'id': week_id,
        'draw_number': payload.get('draw_number'),
        'uploaded_by': payload.get('uploaded_by'),
        'system_type': payload.get('system_type', 'Enkelrad'),
        'rows': payload.get('rows', []),
        'created_at': datetime.now(timezone.utc).isoformat(),
    }

    idx = next((i for i, w in enumerate(weeks) if w.get('id') == week_id), None)
    if idx is not None:
        weeks[idx] = week
    else:
        weeks.append(week)

    save_data(data)
    return jsonify({'status': 'ok', 'week': week})


# ── ROUTES ──

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/data')
def get_data():
    return jsonify(public_data(load_data()))

@app.route('/api/save', methods=['POST'])
def save():
    try:
        data = request.json
        # never let a client-supplied blob overwrite stored password hashes —
        # merge incoming users by username, keeping the existing hash unless
        # /api/change-password explicitly updates it.
        existing = load_data()
        existing_users = {u['username']: u for u in existing.get('users', [])}
        for u in data.get('users', []):
            prior = existing_users.get(u.get('username'))
            if prior and 'password_hash' not in u:
                u['password_hash'] = prior['password_hash']
        save_data(data)
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/login', methods=['POST'])
def login():
    payload = request.json or {}
    data = load_data()
    user = find_user(data, payload.get('username'))
    if not user or not check_password_hash(user.get('password_hash', ''), payload.get('password', '')):
        return jsonify({'status': 'error', 'message': 'Fel användarnamn eller lösenord'}), 401
    return jsonify({
        'status': 'ok',
        'username': user['username'],
        'display_name': user['display_name'],
        'must_change_password': user.get('must_change_password', False),
    })

@app.route('/api/change-password', methods=['POST'])
def change_password():
    payload = request.json or {}
    data = load_data()
    user = find_user(data, payload.get('username'))
    if not user or not check_password_hash(user.get('password_hash', ''), payload.get('current_password', '')):
        return jsonify({'status': 'error', 'message': 'Fel nuvarande lösenord'}), 401
    new_password = (payload.get('new_password') or '').strip()
    if len(new_password) < 4:
        return jsonify({'status': 'error', 'message': 'Nytt lösenord måste vara minst 4 tecken'}), 400
    user['password_hash'] = generate_password_hash(new_password)
    user['must_change_password'] = False
    save_data(data)
    return jsonify({'status': 'ok'})

@app.route('/api/draw')
def get_draw():
    draw = _draw_cache or fetch_draw()
    return jsonify({'draw': draw, 'last_updated': _last_scraped.isoformat() if _last_scraped else None})

@app.route('/api/refresh-draw', methods=['POST'])
def refresh_draw():
    draw = fetch_draw()
    return jsonify({'draw': draw, 'last_updated': _last_scraped.isoformat() if _last_scraped else None})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5003))
    app.run(debug=True, port=port)
