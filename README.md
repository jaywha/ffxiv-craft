# FFXIV Craft Planner

A local web app for planning crafting and gathering in Final Fantasy XIV. Add the
items you want to craft, see every material they break down into, and get an
optimal route for gathering it all.

## Requirements

- Python 3.10 or newer — https://python.org
- Internet connection (uses [XIVAPI](https://xivapi.com) for live recipe & gathering data — free, no API key)
- (Optional) A Google account to sign in and save routes across devices — or run with `AUTH_DISABLED=1` for a single local profile (see Development)

## Quick Start

### Windows
Double-click `launch.bat`. It checks Python, installs Flask + Requests if needed,
and opens http://localhost:5000.

### Mac / Linux
```bash
chmod +x launch.sh
./launch.sh
```

### Manually
```bash
pip install -r requirements.txt
python app.py
```
Then open http://localhost:5000.

## How to Use

1. **Search** for an item by name.
2. **Click a result** to add it to your **Items to Craft** list. Set a
   **quantity** for each item; add as many different items as you like.
3. **Calculate Materials** — expands every target into its full recipe tree and
   shows a single **aggregated** gathering log (shared materials are summed
   across all your targets).
4. **Plan Gathering Route** — computes an optimal route across the FFXIV zones to
   gather everything, in two flavors you can toggle between:
   - **Min Cost** — fewest gil spent on teleports
   - **Min Time** — fastest travel

Each stop lists the materials found there with the gathering job, node level,
in-zone landmark, and map coordinates (e.g. `X: 26.7, Y: 25.7`).

## Sign in & your Library

Sign in with Google (top-right) to keep a **Library** in the sidebar: your
recent searches and any routes you save. Click **★ Save Route** on a computed
route to store its targets and results; reopen or delete saved routes from the
Library.

Running locally without setting up Google sign-in? Start with `AUTH_DISABLED=1`
(see Development) and the app uses a single local profile — the Library still works.

## About the route

- **Fewest stops.** Most materials can be gathered in several zones; the planner
  consolidates them into the fewest shared zones to minimise teleports and travel.
- **Crystals & Shards** are handled separately. They're gatherable almost
  everywhere, so they're folded into stops you're already making — and any that
  don't fit are listed under a note to gather passively or buy on the Market
  Board, rather than sending you on a detour.
- **Items Without Zone Data** — anything that isn't gathered (mob drops, vendor
  items) is listed here to source elsewhere.

## Notes

- Recipe, gathering, and coordinate data all come live from XIVAPI v2.
- Source classification (gathered / crystal / crafted / other) is a best-effort
  guess from the item's category and may occasionally be off.
- Teleport costs and travel times are approximate — good for choosing *which*
  zones to visit; treat the gil/minute totals as estimates.
- The app runs entirely locally; the only network calls are to XIVAPI.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

**Optional — Google sign-in.** Set `FLASK_SECRET_KEY`, `GOOGLE_CLIENT_ID`, and
`GOOGLE_CLIENT_SECRET` (a Google Cloud OAuth *Web* client, redirect URI
`http://localhost:5000/auth/callback`) to enable per-user saved routes across
devices. Or set `AUTH_DISABLED=1` to skip OAuth entirely and use a single local
profile. History and saved routes live in a local `ffxiv_craft.db` (SQLite).

Tests mock all XIVAPI calls, so they run offline. CI runs the suite on Python
3.10–3.12 (`.github/workflows/ci.yml`). See `CLAUDE.md` for architecture notes.
