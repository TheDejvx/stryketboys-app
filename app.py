from flask import Flask, render_template, jsonify, request
import json, os, threading, time, base64
from datetime import datetime, timezone
from difflib import SequenceMatcher
from werkzeug.security import generate_password_hash, check_password_hash

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

DECODE_TOOL = {
    'name': 'record_coupon',
    'description': 'Record the decoded Stryktipset coupon rows read from the screenshot.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'system_type': {
                'type': 'string',
                'description': "The system label shown on the coupon, e.g. 'M-system', 'B-system', 'Helsystem'. Use 'Enkelrad' if no such label is visible (a plain single-sign coupon)."
            },
            'rows': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'row_num': {'type': 'integer', 'description': 'Row number 1-13 as printed on the coupon'},
                        'home': {'type': 'string'},
                        'away': {'type': 'string'},
                        'kickoff_time': {'type': 'string', 'description': "Text as shown, e.g. 'Idag 18:30'"},
                        'picks': {
                            'type': 'array',
                            'items': {'type': 'string', 'enum': ['1', 'X', '2']},
                            'minItems': 1,
                            'maxItems': 3,
                            'description': 'Which of 1/X/2 are selected on this row (solid dark-navy fill with white text = selected).'
                        }
                    },
                    'required': ['row_num', 'home', 'away', 'picks']
                }
            }
        },
        'required': ['rows']
    }
}

DECODE_PROMPT = (
    "This is a screenshot of a Svenska Spel Stryktipset betting coupon (Swedish football pool betting, "
    "normally 13 rows). Each row has three small pill-shaped buttons labeled 1, X, 2 in that order.\n\n"
    "A button is SELECTED if it has a solid dark navy blue fill with white text.\n"
    "A button is NOT selected if it has a white/light background with a thin gray border and dark text.\n\n"
    "For each row, read: the home and away team names, the kickoff time text exactly as shown (e.g. 'Idag 18:30'), "
    "and exactly which of 1/X/2 are selected — a row can have 1, 2, or 3 signs selected (system bets). "
    "Also read the system type label shown near the top of the coupon if any (e.g. 'M-system', 'B-system', 'Helsystem'); "
    "use 'Enkelrad' if none is visible.\n\n"
    "Call record_coupon with the full structured result for every row visible on the coupon."
)

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
    b64 = base64.b64encode(img_bytes).decode('utf-8')

    try:
        resp = client.messages.create(
            model='claude-sonnet-5',
            max_tokens=2048,
            tools=[DECODE_TOOL],
            tool_choice={'type': 'tool', 'name': 'record_coupon'},
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': b64}},
                    {'type': 'text', 'text': DECODE_PROMPT},
                ],
            }],
        )
        tool_use = next((b for b in resp.content if b.type == 'tool_use'), None)
        if not tool_use:
            return jsonify({'status': 'error', 'message': 'Model did not return structured data'}), 500
        decoded = tool_use.input
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

    draw = _draw_cache or fetch_draw()
    events = (draw or {}).get('events', [])

    rows = decoded.get('rows', [])
    for row in rows:
        match, score = fuzzy_match_event(row.get('home', ''), row.get('away', ''), events)
        if match and score > 0.55:
            row['match_id'] = match['match_id']
            row['match_start'] = match['match_start']
            row['low_confidence'] = False
        else:
            row['match_id'] = None
            row['match_start'] = None
            row['low_confidence'] = True

    return jsonify({
        'status': 'ok',
        'system_type': decoded.get('system_type', 'Enkelrad'),
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
