from flask import Flask, render_template, jsonify, request, Response
import json, os, re, threading, time, base64, io
from datetime import datetime, timezone
from difflib import SequenceMatcher
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image

app = Flask(__name__)
DATA_FILE = os.path.join(os.path.dirname(__file__), 'stryk_data.json')

@app.after_request
def add_sw_scope_header(response):
    """A service worker script served from /static/sw.js is, by default, only allowed to control
    pages under /static/ — the browser refuses a broader scope claim (see the frontend's
    { scope: '/' } registration) unless the server explicitly grants it via this header. Root
    cause of push subscriptions hanging forever at serviceWorker.ready: without this, the SW
    registered "successfully" but could never actually control the app's page at '/', so nothing
    was ever there for .ready to resolve to."""
    if request.path == '/static/sw.js':
        response.headers['Service-Worker-Allowed'] = '/'
    return response

# Web Push (see "Push notifications" in CLAUDE.md). Both keys come from a one-time-generated
# VAPID key pair set as Railway env vars — push sending no-ops quietly if they're not set,
# same pattern as GEMINI_API_KEY/get_gemini().
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY')
VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY')
VAPID_CLAIM_EMAIL = os.environ.get('VAPID_CLAIM_EMAIL', 'mailto:admin@example.com')

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

# Whose turn it is to upload — an explicit order set by the group, not derived from
# weeks-uploaded-so-far (that was fragile: deleting a test coupon or an off-cycle upload
# silently shifted whose turn was next). Index 0 = current turn.
DEFAULT_UPLOADER_ROTATION = ['tommy', 'olle', 'david', 'victor', 'gustav', 'martin']

def load_data():
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        fallback = json.load(f)
    db = get_db()
    if db is not None:
        doc = db.stryk_state.find_one({'_id': 'current'})
        if doc:
            doc.pop('_id')
            data = doc
        else:
            db.stryk_state.insert_one({'_id': 'current', **fallback})
            data = fallback
    else:
        data = fallback
    data.setdefault('uploader_rotation', DEFAULT_UPLOADER_ROTATION)
    data.setdefault('uploader_rotation_index', 0)
    return data

def save_data(data):
    db = get_db()
    if db is not None:
        db.stryk_state.replace_one({'_id': 'current'}, {'_id': 'current', **data}, upsert=True)
    else:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

# Coupon photos are stored separately from the main stryk_state document, not embedded in
# week['rows'] etc — that single document is replaced wholesale on every save (including
# frequent, unrelated ones like a manual result tap), so piling image bytes into it would mean
# re-sending every stored photo over the wire on every such save, and risks hitting MongoDB's
# 16MB document cap after a season or two of accumulated coupons. Keyed by week id instead, in
# its own `stryk_images` collection (Mongo) or a local `coupon_images/` folder (JSON fallback).
COUPON_IMAGES_DIR = os.path.join(os.path.dirname(__file__), 'coupon_images')

def store_coupon_image(week_id, raw_bytes):
    """Re-encode to a bounded JPEG before persisting — phone screenshots can be several MB each,
    and this keeps per-image storage small across a season of accumulated coupons. Falls back to
    storing the original bytes if re-encoding fails for any reason (unusual format, corrupt data)
    rather than losing the upload entirely."""
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.load()
        if img.mode != 'RGB':
            img = img.convert('RGB')
        max_w = 1400
        if img.width > max_w:
            img = img.resize((max_w, round(img.height * max_w / img.width)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=82)
        jpeg_bytes = buf.getvalue()
    except Exception as e:
        print(f'store_coupon_image: re-encode failed, storing original bytes ({e})')
        jpeg_bytes = raw_bytes

    db = get_db()
    if db is not None:
        db.stryk_images.replace_one(
            {'_id': week_id},
            {'_id': week_id, 'image_base64': base64.b64encode(jpeg_bytes).decode('ascii'), 'mime_type': 'image/jpeg'},
            upsert=True,
        )
    else:
        os.makedirs(COUPON_IMAGES_DIR, exist_ok=True)
        with open(os.path.join(COUPON_IMAGES_DIR, f'{week_id}.jpg'), 'wb') as f:
            f.write(jpeg_bytes)

def get_coupon_image(week_id):
    """Returns (bytes, mime_type) or None if no photo was ever stored for this week (e.g. a week
    saved before this feature shipped)."""
    db = get_db()
    if db is not None:
        doc = db.stryk_images.find_one({'_id': week_id})
        if not doc:
            return None
        return base64.b64decode(doc['image_base64']), doc.get('mime_type', 'image/jpeg')
    path = os.path.join(COUPON_IMAGES_DIR, f'{week_id}.jpg')
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as f:
        return f.read(), 'image/jpeg'

def delete_coupon_image(week_id):
    db = get_db()
    if db is not None:
        db.stryk_images.delete_one({'_id': week_id})
    else:
        path = os.path.join(COUPON_IMAGES_DIR, f'{week_id}.jpg')
        if os.path.exists(path):
            os.remove(path)

def find_user(data, username):
    username = (username or '').strip().lower()
    for u in data.get('users', []):
        if u.get('username', '').lower() == username:
            return u
    return None

def broadcast_push(data, title, body, tag=None, exclude_username=None):
    """Send a Web Push notification to every user with at least one stored subscription.
    Mutates data['users'][*]['push_subscriptions'] in place to drop subscriptions the push
    service reports as gone (404/410 — the browser unsubscribed, e.g. app uninstalled), so the
    caller should save_data(data) afterward if this returns True. No-ops quietly (returns False)
    if VAPID keys aren't configured, same as decode's get_gemini() pattern for GEMINI_API_KEY."""
    if not (VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY):
        print(f'broadcast_push: skipped "{title}" — VAPID_PRIVATE_KEY/VAPID_PUBLIC_KEY not configured')
        return False
    from pywebpush import webpush, WebPushException
    changed = False
    sent, skipped_no_sub = 0, []
    for user in data.get('users', []):
        if exclude_username and user.get('username', '').lower() == exclude_username.lower():
            continue
        subs = user.get('push_subscriptions') or []
        if not subs:
            skipped_no_sub.append(user.get('username'))
            continue
        keep = []
        for sub in subs:
            try:
                webpush(
                    subscription_info=sub,
                    data=json.dumps({'title': title, 'body': body, 'tag': tag}),
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={'sub': VAPID_CLAIM_EMAIL},
                )
                keep.append(sub)
                sent += 1
            except WebPushException as e:
                status = getattr(e.response, 'status_code', None)
                if status in (404, 410):
                    changed = True  # expired/unregistered subscription — drop it
                    print(f'push subscription gone for {user.get("username")} (status {status}) — dropping it')
                    continue
                print(f'push send failed for {user.get("username")}: {e}')
                keep.append(sub)  # transient error — keep it, don't discard on a whim
        if len(keep) != len(subs):
            user['push_subscriptions'] = keep
    print(f'broadcast_push: "{title}" — sent to {sent} subscription(s), no subscription on file for {skipped_no_sub or "none"}')
    return changed

def check_first_match_notifications(data):
    """Fires a 'first match started' push exactly once per week, the first time we notice its
    earliest kickoff has passed. Checked purely against wall-clock time vs each row's already-
    known match_start — doesn't need live draw data, so it works even if nobody's browser is
    open to poll anything."""
    changed = False
    now = datetime.now(timezone.utc)
    for week in data.get('weeks', []):
        if week.get('first_match_notified'):
            continue
        starts = [r.get('match_start') for r in (week.get('rows') or []) if r.get('match_start')]
        if not starts:
            continue
        try:
            earliest = min(datetime.fromisoformat(s.replace('Z', '+00:00')) for s in starts)
        except Exception:
            continue
        if now >= earliest:
            week['first_match_notified'] = True
            changed = True
            product = week.get('product', 'stryktipset')
            broadcast_push(
                data,
                title='Första matchen har startat',
                body=f'{PRODUCT_LABELS.get(product, product)} v{week.get("draw_number")} har dragit igång',
                tag=f'first-match-{week["id"]}',
            )
    return changed

def check_settlement_notifications(data):
    """Fires a 'coupon fully settled' push exactly once per week. Checked directly against each
    week's own cached draw events (sport_event_status), not the frontend-derived settled_result
    field — that one only gets written when someone's browser is actually open to compute it, so
    relying on it here would mean this notification might never fire if nobody happens to be
    looking at the app when the last match ends."""
    changed = False
    for week in data.get('weeks', []):
        if week.get('fully_settled_notified'):
            continue
        rows = week.get('rows') or []
        if not rows:
            continue
        product = week.get('product', 'stryktipset')
        draw = _draw_cache.get(product, {}).get(week.get('draw_number'))
        if not draw:
            continue
        events_by_id = {ev['match_id']: ev for ev in draw.get('events', []) if ev.get('match_id')}
        all_finished = all(
            (ev := events_by_id.get(r.get('match_id'))) and _looks_finished(ev)
            for r in rows
        )
        if all_finished:
            week['fully_settled_notified'] = True
            changed = True
            broadcast_push(
                data,
                title='Kupongen är avgjord',
                body=f'{PRODUCT_LABELS.get(product, product)} v{week.get("draw_number")} är klar — dags att kolla resultatet!',
                tag=f'settled-{week["id"]}',
            )
    return changed

def public_data(data):
    """Strip password hashes before this ever reaches the client. last_login rides along here
    too — the frontend only ever renders the logged-in user's own value (see renderMyLastLogin
    in index.html), not shown for anyone else, even though it's technically present for all
    users in this response (same trust level as the rest of this friend-app's session model)."""
    return {
        **data,
        'users': [
            {
                'username': u.get('username'),
                'display_name': u.get('display_name'),
                'must_change_password': u.get('must_change_password', False),
                'last_login': u.get('last_login'),
            }
            for u in data.get('users', [])
        ],
    }


# ── SVENSKA SPEL DRAW DATA ──
# Both Stryktipset and Europatipset live on the same API family/shape — just a different
# product slug in the URL — so one fetch_draw() serves both, cached separately per product.

PRODUCTS = ('stryktipset', 'europatipset')

# {product: {draw_number: draw_dict}} — NOT a single "current draw" per product. A product can
# have an older, not-yet-fully-settled week whose draw_number differs from whatever's currently
# open for betting (e.g. Wednesday's Europatipset draw is still being tracked while a new one has
# already opened by the weekend) — each needs its own independently-kept-fresh cache entry, found
# the hard way: once a newer draw opened, the app silently stopped updating the older one's
# results entirely (findEvent() found nothing, so isFinished()/settled_result froze mid-draw).
_draw_cache = {p: {} for p in PRODUCTS}
_current_draw_number = {p: None for p in PRODUCTS}
_last_scraped = {p: None for p in PRODUCTS}

def parse_odds(val):
    if not val:
        return None
    try:
        return float(str(val).replace(',', '.'))
    except Exception:
        return None

# Confirmed against a real live match (Ljungskile-Norrby, SecondHalf): Svenska Spel's draws API
# carries no match-clock/minute field at all — every key extract_live_minute() used to guess at
# (matchClock, clock, minute, etc., plus a nested liveData/live dict) is genuinely absent. Only
# statusId/sportEventStatus (coarse period: NotStarted/FirstHalf/Halftime/SecondHalf/Ended) and
# statusTime (when that status last changed) exist. The frontend now estimates a "80'"-style
# minute client-side from match_start (first-half kickoff) / status_time (second-half kickoff,
# i.e. when sportEventStatus last flipped to SecondHalf) instead — see estimateMinute() in
# templates/index.html. status_time is exposed for that; extract_live_minute() is gone.

def _last_saved_draw_number(product):
    """Cold-start fallback for fetch_draw()'s closed/live-draw case: this process's in-memory
    cache is empty (fresh start or redeploy), so fall back to whatever draw number the most
    recently saved coupon for this product used — reliable since a coupon only ever gets
    uploaded for the currently open/just-closed draw."""
    try:
        weeks = [w for w in load_data().get('weeks', []) if w.get('product', 'stryktipset') == product]
        if not weeks:
            return None
        return max((w.get('draw_number') for w in weeks if w.get('draw_number')), default=None)
    except Exception as e:
        print(f'_last_saved_draw_number error ({product}): {e}')
        return None

def _parse_draw(d, product):
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
            'status_time': m.get('statusTime'),
            'result': m.get('result'),
            'odds_1': parse_odds(odds.get('one')),
            'odds_x': parse_odds(odds.get('x')),
            'odds_2': parse_odds(odds.get('two')),
        })
    events.sort(key=lambda x: x['row_num'] or 0)
    return {
        'product': product,
        'draw_number': d.get('drawNumber'),
        'draw_state': d.get('drawState'),
        'reg_close_time': d.get('regCloseTime'),
        'current_net_sale': d.get('currentNetSale'),
        'events': events,
    }

def fetch_draw(product='stryktipset', draw_number=None):
    """Pull a draw (matches, odds, live status) for the given product from Svenska Spel's public
    draws API — no auth needed. draw_number=None means 'whichever draw is currently open for
    betting' (via the list endpoint) and also updates _current_draw_number[product]; a specific
    draw_number fetches that exact draw directly, which keeps working after it's closed/superseded
    by a newer current draw — needed so an older not-yet-fully-settled week keeps getting fresh
    live/final data instead of freezing the moment a new draw opens."""
    if product not in PRODUCTS:
        product = 'stryktipset'
    import requests as req
    try:
        if draw_number is None:
            r = req.get(f'https://api.spela.svenskaspel.se/draw/1/{product}/draws',
                         params={'numberOfDraws': 1},
                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
            r.raise_for_status()
            draws = (r.json() or {}).get('draws') or []
            if not draws:
                # Confirmed live during a real Europatipset window: this "current draws" list goes
                # empty the moment a draw closes for betting — even while its matches are actively
                # being played. Fall back to whatever we already know is current, or (cold start /
                # after a redeploy wipes the in-memory cache) the most recently saved coupon.
                fallback = _current_draw_number[product] or _last_saved_draw_number(product)
                if not fallback:
                    return None
                return fetch_draw(product, draw_number=fallback)
            parsed = _parse_draw(draws[0], product)
            _current_draw_number[product] = parsed['draw_number']
        else:
            r = req.get(f'https://api.spela.svenskaspel.se/draw/1/{product}/draws/{draw_number}',
                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
            r.raise_for_status()
            d = (r.json() or {}).get('draw')
            if d is None:
                return _draw_cache[product].get(draw_number)
            parsed = _parse_draw(d, product)

        _draw_cache[product][parsed['draw_number']] = parsed
        _last_scraped[product] = datetime.now(timezone.utc)
        print(f"{product}: fetched draw {parsed['draw_number']} with {len(parsed['events'])} matches")
        return parsed
    except Exception as e:
        print(f'Draw fetch error ({product}, draw_number={draw_number}): {e}')
        fallback_number = draw_number if draw_number is not None else _current_draw_number[product]
        return _draw_cache[product].get(fallback_number) if fallback_number else None

def current_draw(product):
    """The draw currently cached as 'open for betting' for this product, fetching fresh if we
    don't have one yet."""
    dn = _current_draw_number.get(product)
    if dn and dn in _draw_cache.get(product, {}):
        return _draw_cache[product][dn]
    return fetch_draw(product)

# {product: {draw_number: result_dict}} — same reasoning as _draw_cache: multiple draws per
# product can be relevant at once (an older still-active week alongside a newer open one).
_result_cache = {p: {} for p in PRODUCTS}

def fetch_result(product, draw_number):
    """Once Svenska Spel fully finalizes a draw (all matches done + verified — this can lag
    kickoff-to-kickoff by hours), a dedicated /result endpoint appears with the official 1/X/2
    outcome per match (no score-guessing needed) plus the real payout distribution (winners +
    kronor per row for 10/11/12/13 rätt). Confirmed against a real past draw (4969): 404s until
    finalized, so a 404 here just means 'not settled yet', not an error."""
    if product not in PRODUCTS or not draw_number:
        return None
    cached = _result_cache[product].get(draw_number)
    import requests as req
    try:
        r = req.get(f'https://api.spela.svenskaspel.se/draw/1/{product}/draws/{draw_number}/result',
                     headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        if r.status_code == 404:
            return cached
        r.raise_for_status()
        result = (r.json() or {}).get('result') or {}

        outcomes = {}
        for ev in result.get('events', []):
            match_id = ev.get('matchId')
            outcome = ev.get('outcome')
            if match_id and outcome:
                outcomes[str(match_id)] = outcome

        distribution = []
        for tier in result.get('distribution', []):
            win_div = tier.get('winDiv')
            if win_div is None:
                continue
            distribution.append({
                'correct': 13 - win_div,  # winDiv 0 == 13 rätt, 1 == 12 rätt, etc. (observed on draw 4969)
                'winners': tier.get('winners'),
                'amount': parse_odds(tier.get('amount')),
            })
        distribution.sort(key=lambda t: -t['correct'])

        if not outcomes and not distribution:
            return cached

        _result_cache[product][draw_number] = {
            'draw_number': draw_number,
            'outcomes': outcomes,
            'distribution': distribution,
        }
        print(f"{product}: fetched final result for draw {draw_number} ({len(outcomes)} outcomes, {len(distribution)} payout tiers)")
    except Exception as e:
        print(f'Result fetch error ({product}, draw {draw_number}): {e}')
        return cached
    return _result_cache[product][draw_number]

LIVE_POLL_SECONDS = 20
IDLE_POLL_SECONDS = 90

def has_live_match(draw):
    """Best-effort: a match counts as 'live' if it has kicked off but neither its
    sportEventStatus nor status string indicates it's finished. Used only to decide
    polling speed, so false positives just mean we poll a bit more than strictly needed."""
    if not draw:
        return False
    now = datetime.now(timezone.utc)
    for ev in draw.get('events', []):
        start = ev.get('match_start')
        if not start:
            continue
        try:
            start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
        except Exception:
            continue
        if start_dt > now:
            continue
        if _looks_finished(ev):
            continue
        return True
    return False

def _looks_finished(ev):
    s = (ev.get('sport_event_status') or '').lower()
    st = (ev.get('status') or '').lower()
    return any(k in s for k in ('end', 'finish', 'final')) or any(k in st for k in ('avslutad', 'slut'))

def _active_draw_numbers(product):
    """Draw numbers for this product's not-yet-fully-settled weeks — kept fresh independently of
    whatever the 'current' open-for-betting draw is. Root cause of the Wednesday-coupon-frozen
    bug: once a newer draw opened, this product's single cache slot got overwritten with it, so
    the older week's match_ids no longer matched anything in drawData and its rows just stopped
    updating (isFinished() never saw fresh data, so settled_result froze permanently)."""
    try:
        weeks = load_data().get('weeks', [])
    except Exception as e:
        print(f'_active_draw_numbers error ({product}): {e}')
        return set()
    numbers = set()
    for w in weeks:
        if w.get('product', 'stryktipset') != product:
            continue
        dn = w.get('draw_number')
        rows = w.get('rows') or []
        if dn and any(not r.get('settled_result') for r in rows):
            numbers.add(dn)
    return numbers

def background_poller():
    time.sleep(5)
    while True:
        any_live = False
        for product in PRODUCTS:
            try:
                draw = fetch_draw(product)
                if has_live_match(draw):
                    any_live = True
                draw_numbers = _active_draw_numbers(product)
                current_dn = (draw or {}).get('draw_number')
                if current_dn:
                    draw_numbers.add(current_dn)
                for dn in draw_numbers:
                    d = draw if dn == current_dn else fetch_draw(product, draw_number=dn)
                    if has_live_match(d):
                        any_live = True
                    if dn not in _result_cache[product]:
                        fetch_result(product, dn)
            except Exception as e:
                print(f'Background poller error ({product}): {e}')
        try:
            data = load_data()
            notif_changed = check_first_match_notifications(data)
            if check_settlement_notifications(data):
                notif_changed = True
            if notif_changed:
                save_data(data)
        except Exception as e:
            print(f'Background poller notification check error: {e}')
        time.sleep(LIVE_POLL_SECONDS if any_live else IDLE_POLL_SECONDS)

threading.Thread(target=background_poller, daemon=True).start()


# ── COUPON DECODE (Gemini vision) ──
# Switched from Claude to Gemini after manual side-by-side testing showed Gemini reading the
# pill grid correctly on the first try — the exact thing 10 rounds of Claude prompt/pipeline
# engineering couldn't reliably fix (see CLAUDE.md decode-flow history v1-v10). Different vision
# encoders clearly have different strengths on this specific fine-grained-grid task.

_gemini_client = None

def get_gemini():
    global _gemini_client
    if _gemini_client is None:
        key = os.environ.get('GEMINI_API_KEY')
        if not key:
            return None
        from google import genai
        _gemini_client = genai.Client(api_key=key)
    return _gemini_client

PRODUCT_LABELS = {'stryktipset': 'Stryktipset', 'europatipset': 'Europatipset'}

def build_describe_prompt(expected_rows):
    # Which product (Stryktipset vs Europatipset) this is gets auto-detected afterward by
    # matching decoded team names against both products' live draws — not known yet here, and
    # not needed: the product name was only ever cosmetic framing text, irrelevant to reading
    # the pill grid itself.
    return (
        f"This is a screenshot of a Svenska Spel Stryktipset- or Europatipset-style football pool betting "
        f"coupon. It has a numbered list of matches (usually {expected_rows}), each with three small pill-shaped buttons in "
        "a fixed left-to-right order: '1', 'X', '2'. A pill is SELECTED if its background is a solid dark navy "
        "blue with white text, and NOT selected if its background is white/very light with a thin gray border "
        "and dark text.\n\n"
        "Some rows ALSO have a small separate square badge with just the letter 'M' in it (dark outline, white "
        "or light fill), positioned between the kickoff time/live score and the three pills — clearly separate "
        "from the '1'/'X'/'2' pills themselves, and only present on some rows, not all. Check for this badge on "
        "every row independently; do not assume it repeats or skips in any pattern.\n\n"
        "Process the rows ONE AT A TIME, in strict order from the first row to the last. Do NOT skip ahead and do "
        "NOT batch multiple rows together — for EVERY row, immediately after analyzing it, output its result line "
        "before moving to the next row.\n\n"
        "Before row 1, output one line: SYSTEM_TYPE: <the label near the top of the coupon, e.g. 'M-system', "
        "'B-system', 'Helsystem', or 'Enkelrad' if none is visible>\n\n"
        "Then for every single row, output exactly this two-part block, in order:\n"
        "Analysis: name the two teams, then describe what you see for the '1', 'X', '2' pills individually — do "
        "not assume a pattern from previous rows, and do not stop looking after finding the first selected pill. "
        "It is common and expected for two pills to be selected on the same row at once (e.g. X and 2 both "
        "selected, 1 empty) — this is a normal 'garderad rad' (system bet row), not an error. Then also state "
        "whether the small 'M' badge is present on this row.\n"
        "ROW <n> | <home team> - <away team> | <kickoff text> | 1=<0 or 1> X=<0 or 1> 2=<0 or 1> M=<0 or 1>\n\n"
        "Use 1 for selected/present, 0 for not, in the ROW line. Example of two complete row blocks:\n"
        "Analysis: The 1 pill is white with a gray border — not selected. The X pill is solid dark navy with "
        "white text — selected. The 2 pill is also solid dark navy with white text — selected. No 'M' badge is "
        "visible on this row.\n"
        "ROW 7 | Cardiff - Sheffield U | Idag 16:00 | 1=0 X=1 2=1 M=0\n\n"
        "Analysis: The 1 pill is solid dark navy with white text — selected. The X and 2 pills are white with a "
        "gray border — not selected. A small square 'M' badge is visible between the kickoff time and the pills.\n"
        "ROW 11 | Sheffield U - Norwich | 17:00 | 1=1 X=0 2=0 M=1\n\n"
        f"Begin now with SYSTEM_TYPE, then Row 1's analysis and ROW line, then Row 2's, continuing strictly in "
        f"order through every row visible on the coupon (expect {expected_rows} rows total). Do not skip any row."
    )

ROW_LINE_RE = re.compile(
    r'ROW\s+(\d+)\s*\|\s*(.+?)\s*-\s*(.+?)\s*\|\s*(.*?)\s*\|\s*1=([01])\s+X=([01])\s+2=([01])(?:\s+M=([01]))?',
    re.IGNORECASE,
)
SYSTEM_TYPE_RE = re.compile(r'SYSTEM_TYPE:\s*(.+)', re.IGNORECASE)

def parse_decode_analysis(text):
    """Deterministically parse every 'ROW <n> | ...' result line out of the model's response
    (interleaved one per row, right after that row's own analysis) — no second LLM call
    involved, so nothing can get lost in an LLM 're-transcribing itself' step."""
    rows = []
    for m in ROW_LINE_RE.finditer(text):
        row_num, home, away, kickoff, one, x, two, marked = m.groups()
        picks = []
        if one == '1': picks.append('1')
        if x == '1': picks.append('X')
        if two == '1': picks.append('2')
        rows.append({
            'row_num': int(row_num),
            'home': home.strip(),
            'away': away.strip(),
            'kickoff_time': kickoff.strip(),
            'picks': picks or ['1'],
            'zero_picks_read': not picks,
            'marked': marked == '1',
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
    client = get_gemini()
    if client is None:
        return jsonify({'status': 'error', 'message': 'GEMINI_API_KEY not configured on the server'}), 500
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

    # Product isn't known yet — fetch both live draws up front so we can auto-detect
    # afterward by matching decoded team names against each one's fixtures.
    draws = {p: current_draw(p) for p in PRODUCTS}
    row_counts = {len((draws[p] or {}).get('events', [])) for p in PRODUCTS if draws[p]}
    row_counts.discard(0)
    valid_row_counts = row_counts or {13}
    expected_rows = max(valid_row_counts)

    try:
        # Single call: the model reasons in plain text first, then ends with a strict
        # machine-readable summary block that we parse with a regex — no second LLM call
        # "transcribing" its own analysis. This is the AI's best-effort first draft; the
        # frontend always shows it as an editable preview (with the original photo visible
        # alongside it) rather than expecting a perfect unassisted read.
        from google.genai import types as genai_types
        resp = client.models.generate_content(
            model='gemini-3.5-flash',
            contents=[
                build_describe_prompt(expected_rows),
                genai_types.Part.from_bytes(data=img_bytes, mime_type=media_type),
            ],
        )
        analysis_text = resp.text or ''
        if not analysis_text.strip():
            return jsonify({'status': 'error', 'message': 'Model returned no analysis'}), 500
        print('--- coupon decode raw analysis ---')
        print(analysis_text)
        print('--- end raw analysis ---')
        system_type, rows = parse_decode_analysis(analysis_text)
        if not rows:
            return jsonify({'status': 'error', 'message': 'Could not parse the model’s analysis — try again'}), 500
        if len(rows) not in valid_row_counts:
            expected_str = '/'.join(str(n) for n in sorted(valid_row_counts))
            print(f'WARNING: parsed {len(rows)} rows, expected one of {expected_str}')
            return jsonify({
                'status': 'error',
                'message': f'Läste {len(rows)} rader, förväntade {expected_str} — försök igen',
            }), 500
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

    # Auto-detect which product this is: fuzzy-match the decoded team names against each
    # product's live draw and pick whichever one actually explains the coupon.
    best_product, best_score, best_matches = None, -1.0, None
    for p in PRODUCTS:
        events = (draws[p] or {}).get('events', [])
        if not events:
            continue
        matches = [fuzzy_match_event(r.get('home', ''), r.get('away', ''), events) for r in rows]
        score = sum(s for _, s in matches)
        print(f'product detection: {p} scored {score:.2f} across {len(rows)} rows')
        if score > best_score:
            best_product, best_score, best_matches = p, score, matches

    if best_product is None:
        best_product, best_matches = 'stryktipset', [(None, 0.0)] * len(rows)

    for row, (match, score) in zip(rows, best_matches):
        if match and score > 0.55:
            row['match_id'] = match['match_id']
            row['match_start'] = match['match_start']
            row['low_confidence'] = row['zero_picks_read']
        else:
            row['match_id'] = None
            row['match_start'] = None
            row['low_confidence'] = True

    draw = draws[best_product]
    return jsonify({
        'status': 'ok',
        'system_type': system_type,
        'rows': rows,
        'draw_number': (draw or {}).get('draw_number'),
        'product': best_product,
    })

@app.route('/api/coupon/save', methods=['POST'])
def save_coupon():
    payload = request.json or {}
    data = load_data()
    weeks = data.setdefault('weeks', [])

    product = payload.get('product', 'stryktipset')
    if product not in PRODUCTS:
        product = 'stryktipset'
    week_id = payload.get('id') or f"week-{product}-{payload.get('draw_number', 'x')}-{int(time.time())}"

    idx = next((i for i, w in enumerate(weeks) if w.get('id') == week_id), None)
    has_image = weeks[idx].get('has_image', False) if idx is not None else False
    image_b64 = payload.get('image_base64')
    if image_b64:
        try:
            store_coupon_image(week_id, base64.b64decode(image_b64))
            has_image = True
        except Exception as e:
            print(f'save_coupon: failed to store image ({e})')

    week = {
        'id': week_id,
        'product': product,
        'draw_number': payload.get('draw_number'),
        'uploaded_by': payload.get('uploaded_by'),
        'system_type': payload.get('system_type', 'Enkelrad'),
        'rows': payload.get('rows', []),
        'has_image': has_image,
        'created_at': datetime.now(timezone.utc).isoformat(),
    }

    is_new_week = idx is None
    if idx is not None:
        weeks[idx] = week
    else:
        weeks.append(week)

    # Only a genuinely new week fires the "new coupon" push — a re-upload correcting an existing
    # one (see "Kupong shows all active coupons" in CLAUDE.md) shouldn't spam the group again.
    if is_new_week:
        broadcast_push(
            data,
            title='Ny kupong uppladdad',
            body=f'{payload.get("uploaded_by") or "Någon"} laddade upp {PRODUCT_LABELS.get(product, product)} v{week.get("draw_number")}',
            tag=f'new-coupon-{week_id}',
            exclude_username=payload.get('uploaded_by_username'),
        )

    save_data(data)
    return jsonify({'status': 'ok', 'week': week})

@app.route('/api/push/vapid-public-key')
def vapid_public_key():
    return jsonify({'key': VAPID_PUBLIC_KEY})

@app.route('/api/push/test', methods=['POST'])
def push_test():
    """Manual one-off test send — same broadcast_push() path as real notifications, but scoped to
    a single user (`username` in the payload) rather than the whole group. Used to confirm actual
    delivery end-to-end after the service-worker-scope bug fix (see CLAUDE.md)."""
    payload = request.json or {}
    username = payload.get('username')
    data = load_data()
    user = find_user(data, username)
    if not user:
        return jsonify({'status': 'error', 'message': 'Unknown user'}), 404
    if broadcast_push({'users': [user]}, title='Testnotis', body='Om du ser detta funkar push-notiser! 🎉', tag='test-push'):
        for u in data.get('users', []):
            if u.get('username') == user.get('username'):
                u['push_subscriptions'] = user.get('push_subscriptions')
        save_data(data)
    return jsonify({'status': 'ok'})

@app.route('/api/push/debug', methods=['POST'])
def push_debug():
    """Diagnostic-only sink for subscribeToPush()'s client-side stages — added specifically
    because every user showed 0 stored push_subscriptions with no way to tell why (mobile
    Safari's console isn't practically reachable without a Mac + USB). Just logs; nothing
    persisted, nothing this can break."""
    payload = request.json or {}
    print(f"push_debug: user={payload.get('username')} stage={payload.get('stage')} "
          f"detail={payload.get('detail')!r} ua={payload.get('ua')!r}")
    return jsonify({'status': 'ok'})

@app.route('/api/push/subscribe', methods=['POST'])
def push_subscribe():
    payload = request.json or {}
    username = payload.get('username')
    subscription = payload.get('subscription')
    if not username or not subscription:
        return jsonify({'status': 'error', 'message': 'Missing username or subscription'}), 400
    data = load_data()
    user = find_user(data, username)
    if not user:
        return jsonify({'status': 'error', 'message': 'Unknown user'}), 404
    subs = user.setdefault('push_subscriptions', [])
    # De-dupe by endpoint — re-logging-in on the same device/browser shouldn't pile up duplicate
    # subscriptions (each of which would otherwise get its own push, i.e. duplicate notifications).
    endpoint = subscription.get('endpoint')
    subs[:] = [s for s in subs if s.get('endpoint') != endpoint]
    subs.append(subscription)
    save_data(data)
    print(f'push_subscribe: stored subscription for {username} ({len(subs)} total for this user)')
    return jsonify({'status': 'ok'})

@app.route('/api/coupon/image/<week_id>', methods=['GET', 'DELETE'])
def coupon_image(week_id):
    if request.method == 'DELETE':
        delete_coupon_image(week_id)
        return jsonify({'status': 'ok'})
    result = get_coupon_image(week_id)
    if result is None:
        return '', 404
    img_bytes, mime_type = result
    return Response(img_bytes, mimetype=mime_type)


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
    # Tracked for lookup only — deliberately not surfaced anywhere in the UI.
    user['last_login'] = datetime.now(timezone.utc).isoformat()
    save_data(data)
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

def _result_for(product, draw):
    """Only hand back a cached result if it actually matches the requested draw."""
    draw_number = (draw or {}).get('draw_number')
    if not draw_number:
        return None
    return _result_cache[product].get(draw_number)

@app.route('/api/draw')
def get_draw():
    product = request.args.get('product', 'stryktipset')
    if product not in PRODUCTS:
        product = 'stryktipset'
    draw_number = request.args.get('draw_number', type=int)
    if draw_number:
        draw = _draw_cache[product].get(draw_number) or fetch_draw(product, draw_number=draw_number)
    else:
        draw = current_draw(product)
    last = _last_scraped[product]
    return jsonify({'draw': draw, 'last_updated': last.isoformat() if last else None, 'has_live': has_live_match(draw), 'result': _result_for(product, draw)})

@app.route('/api/refresh-draw', methods=['POST'])
def refresh_draw():
    product = request.args.get('product', 'stryktipset')
    if product not in PRODUCTS:
        product = 'stryktipset'
    draw_number = request.args.get('draw_number', type=int)
    draw = fetch_draw(product, draw_number=draw_number) if draw_number else fetch_draw(product)
    last = _last_scraped[product]
    if draw and draw.get('draw_number'):
        fetch_result(product, draw['draw_number'])
    return jsonify({'draw': draw, 'last_updated': last.isoformat() if last else None, 'has_live': has_live_match(draw), 'result': _result_for(product, draw)})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5003))
    app.run(debug=True, port=port)
