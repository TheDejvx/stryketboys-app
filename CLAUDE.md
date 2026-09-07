# StryketBoys – Stryktipset coupon tracker

## Project
Flask + vanilla JS app for tracking the group's weekly Stryktipset coupon. A different "boy" uploads a screenshot of the coupon each week; the app decodes it into structured picks, tracks live results, and installs to the homescreen on iOS/Android.
- **Local:** `C:\Users\David\Desktop\Stryktipset\stryketboys-app\`
- **Run locally:** `python app.py` → http://localhost:5003

## Stack
- Backend: Python/Flask, served with Waitress on Railway
- Frontend: Single-page HTML/JS (`templates/index.html`), no build step
- Data: MongoDB Atlas (`stryk_app` database, `stryk_state` collection), fallback to `stryk_data.json`
- Live draw data: `https://api.spela.svenskaspel.se/draw/1/stryktipset/draws` (public, no auth) — no scraping needed
- Coupon decode: Anthropic API (Claude vision), forced structured tool-call output
- Deploy: Dockerfile + `railway.json`, no Playwright/browser deps needed

## Data model (`stryk_data.json`)
```json
{
  "boys": ["Name1", "Name2", ...],
  "weeks": [
    {
      "id": "week-4970-...",
      "draw_number": 4970,
      "uploaded_by": "Name1",
      "system_type": "M-system",
      "rows": [
        {
          "row_num": 1, "home": "Sunderland", "away": "Arsenal",
          "match_id": 93093, "match_start": "2026-09-12T21:00:00+02:00",
          "kickoff_time": "Idag 21:00",
          "picks": ["1", "2"],
          "settled_result": null
        }
      ],
      "created_at": "..."
    }
  ]
}
```
- `picks` is always an array — length 1 for straight bets, 2-3 for garderade/system rows (M-system, B-system, etc). A row counts as correct if `settled_result` is present in `picks`.
- `settled_result` is filled in once a match finishes — either auto-derived client-side from the live draw API's `match.result` field (schema unverified — see caveat below), or manually tapped in by whoever's checking (finished rows always show a manual 1/X/2 override picker as a safety net).
- `boys` drives the "vem laddar upp" rotation (`nextUploader()` in the frontend: `boys[weeks.length % boys.length]`).

## Known caveat: live result schema unverified
`fetch_draw()` in `app.py` exposes each event's raw `result` field from Svenska Spel's API as-is; `deriveResultSign()` in the frontend guesses common score-object shapes (`homeScore`/`awayScore` etc.) to compute the 1/X/2 outcome once a match is finished. This was never confirmed against a real finished/live match (built before a live window). If auto-derivation comes back wrong or empty during a real Saturday, the manual override pills next to any finished-but-unsettled row are the correct fix path — check the actual `result` shape via `/api/draw` during a live match and tighten `deriveResultSign()` accordingly.

## Coupon decode flow
1. `POST /api/coupon/decode` (multipart image) → Claude vision reads all rows via a forced tool-call schema (`record_coupon` in `app.py`), extracting home/away/kickoff/picks/system_type per row.
2. Backend fuzzy-matches each decoded row's team names against the current live draw (`difflib.SequenceMatcher`) to attach `match_id`; rows that don't clear a 0.55 similarity threshold are flagged `low_confidence` and highlighted in the review UI.
3. Frontend shows the decode as an **editable preview** (never auto-saved) — teams are editable text inputs, picks are toggleable pills — before `POST /api/coupon/save` persists it.

## Endpoints
- `GET /api/data` / `POST /api/save` — load/save the whole state blob (Mongo w/ JSON fallback)
- `GET /api/draw` / `POST /api/refresh-draw` — cached current Stryktipset draw (matches, odds, live status), background-polled every 90s
- `POST /api/coupon/decode` — image → structured preview (not persisted)
- `POST /api/coupon/save` — persist a confirmed week

## PWA install
Unlike the sibling `vm-app` (whose service worker is never registered and has no manifest — install there is just a bookmark), this app ships a real `manifest.json` + registered `static/sw.js` + `apple-touch-icon`, so "Add to Home Screen" produces an actual installed app icon on both iOS and Android. Icons are generated via `gen_icons.py` (Pillow) — rerun it if the icon design changes.

## Edit-lock
Same lightweight pattern as `vm-app`/`gc-app`: a hardcoded password (`stryktips1337` in `templates/index.html`) gates editing via `localStorage` with a 12h TTL. Not real auth — just friction against accidental edits from a shared link.
