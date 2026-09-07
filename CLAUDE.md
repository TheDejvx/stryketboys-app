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
- `users` drives both login accounts and the "vem laddar upp" rotation hint (`nextUploader()` in the frontend). Each user: `{username, display_name, password_hash, must_change_password}`.

## Known caveat: live result schema unverified
`fetch_draw()` in `app.py` exposes each event's raw `result` field from Svenska Spel's API as-is; `deriveResultSign()` in the frontend guesses common score-object shapes (`homeScore`/`awayScore` etc.) to compute the 1/X/2 outcome once a match is finished. This was never confirmed against a real finished/live match (built before a live window). If auto-derivation comes back wrong or empty during a real Saturday, the manual override pills next to any finished-but-unsettled row are the correct fix path — check the actual `result` shape via `/api/draw` during a live match and tighten `deriveResultSign()` accordingly.

## Coupon decode flow
This went through several iterations to fix garderade (multi-sign, e.g. X2) rows getting under-read — worth knowing the history if it regresses again:
- **v1**: asked the model for a `picks` array directly via a forced tool call. It applied a "radio button" bias and frequently dropped the second sign.
- **v2**: split picks into three independent booleans (`one_selected`/`x_selected`/`two_selected`) in the same forced-tool-call schema. Marginal improvement at best — forcing `tool_choice` from the first token gives the model zero room to actually reason before answering.
- **v3**: two API calls — a free-text row-by-row reasoning pass, then a second call asking the model to transcribe that analysis into the structured tool schema. Only fixed some rows; the second call (an LLM re-summarizing its own prior output) was itself a plausible new place to lose detail.
- **v4**: single API call, free text row-by-row reasoning ending in a batched machine-readable `SUMMARY:` block (`ROW <n> | ...` per row) parsed by plain regex — no second LLM call, so nothing gets lost in transcription. Better, but real server logs (see below) caught it still failing on specific rows: the model's free-text analysis **silently skipped some rows entirely** (e.g. no "Row 5" write-up anywhere) and went out of order, yet the batched SUMMARY block at the end still confidently emitted a value for every row regardless — including ones it never actually analyzed. Batching the machine-readable output at the end let the final answer detach from the actual reasoning.
- **v5**: same single-call, no-second-LLM-call design, but the per-row `ROW <n> | ...` line must now be emitted **immediately after that row's own analysis, interleaved row-by-row**, instead of batched into one block at the end. This structurally fixed the v4 skipped-row bug — real server logs confirmed every row got its own analysis, in order, with its ROW line matching that analysis. But logs *also* showed the remaining failures were a different problem entirely: the model's own written analysis explicitly, confidently stated the wrong color for specific pills (e.g. "the 2 pill is white — not selected" for a pill that was actually navy-filled). That's a genuine vision-perception error, not a reasoning-structure problem — no amount of "think before you answer" prompting fixes a wrong observation.
- **v6 (current)**: stopped trusting the model's own selected/not-selected color judgment at all. It now reports the **pixel coordinates** (as width/height fractions) of each pill's center instead — a much easier task than fine color discrimination — and `pill_is_selected()` in `app.py` samples the actual image pixels at those coordinates server-side with Pillow, classifying by brightness (dark navy fill vs white/light background is a wide, unambiguous contrast — well clear of the 140/255 threshold either way). The model still writes a free-text "Analysis" note per row (kept for team-name accuracy and to preserve the v5 anti-skipping row-by-row discipline) but that note's own selected/not-selected claim is discarded, not used for `picks`.
- The raw analysis text (and, as of v6, each row's pixel-sample results) is printed to server logs (`railway logs`) on every decode call specifically so a bad read can be diagnosed against real data instead of guessing blind — this is exactly how the v4 and v5 root causes were actually found. `PYTHONUNBUFFERED=1` is set in the `Dockerfile` — without it, `print()` output never reached Railway's log capture at all.
- If a row parses with zero picks, it's flagged `low_confidence` (every real coupon row has ≥1 pick, so an empty read is itself a decode error).
- Backend fuzzy-matches each decoded row's team names against the current live draw (`difflib.SequenceMatcher`) to attach `match_id`; rows that don't clear a 0.55 similarity threshold are also flagged `low_confidence` and highlighted in the review UI.
- Frontend shows the decode as an **editable preview** (never auto-saved) — teams are editable text inputs, picks are toggleable pills — before `POST /api/coupon/save` persists it.

## Endpoints
- `GET /api/data` / `POST /api/save` — load/save the whole state blob (Mongo w/ JSON fallback). `GET` strips `password_hash` via `public_data()`; `POST` re-merges each user's existing hash by username if the client didn't send one, so a stale client blob can never wipe a password.
- `GET /api/draw` / `POST /api/refresh-draw` — cached current Stryktipset draw (matches, odds, live status), background-polled every 90s
- `POST /api/coupon/decode` — image → structured preview (not persisted)
- `POST /api/coupon/save` — persist a confirmed week
- `POST /api/login` — `{username, password}` → `{status, username, display_name, must_change_password}`. Username match is case-insensitive.
- `POST /api/change-password` — `{username, current_password, new_password}`, requires the current password to verify, clears `must_change_password`.

## Accounts
Six accounts, seeded via `seed_users.py` (rerun it to reset everyone's password — it overwrites the whole `users` list, preserving `weeks`): username = first name, initial password = surname (lowercase). Every account starts with `must_change_password: true`; the frontend's login modal forces a password-change step before granting a session — it withholds `setSession()` until that completes, so there's no way to end up logged in while still on the seed password. Sessions live in `localStorage` (`stryk_session`) for 30 days. Viewing the app (Kupong/Historik/Statistik) needs no login; uploading a coupon or manually setting a `settled_result` does, and attributes the action to whoever's logged in — no more free-text uploader picker.

## PWA install
Unlike the sibling `vm-app` (whose service worker is never registered and has no manifest — install there is just a bookmark), this app ships a real `manifest.json` + registered `static/sw.js` + `apple-touch-icon`, so "Add to Home Screen" produces an actual installed app icon on both iOS and Android. Icons are generated via `gen_icons.py` (Pillow) — rerun it if the icon design changes.

## Edit-lock
Superseded by real per-user accounts (see Accounts above) — unlike `vm-app`/`gc-app`'s single shared hardcoded password, this app has individually distinguishable, server-verified logins. Still friend-app-grade security, not enterprise auth: sessions are just a client-trusted `{username, display_name}` blob in `localStorage`, not a signed token, so a motivated group member could forge who an action is attributed to. Good enough for a trusted friend group; would need real session tokens if that trust model ever changes.
