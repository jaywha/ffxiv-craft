# CLAUDE.md

Guidance for Claude Code (and future me) working in this repo.

## What this is

A **single-file Flask app** (`app.py`) that plans FFXIV crafting: search an item,
break its recipe into a full material tree, and compute an optimal gathering
route across in-game zones. Recipe/gathering data comes **live from XIVAPI v2**
(`https://v2.xivapi.com/api`) — there is no database. The entire frontend
(HTML + CSS + vanilla JS) is embedded as one big triple-quoted `HTML` string
near the bottom of `app.py`; there is no bundler, framework, or build step.

Dependencies: `flask`, `requests`, `authlib` (runtime, all pip). `pytest`
(dev). Persistence is stdlib `sqlite3` — no extra dependency.

## Running

- App: `python app.py` → http://localhost:5000 (or `launch.bat` / `launch.sh`).
- Preview/verify: `.claude/launch.json` defines the `ffxiv-craft` server for the
  browser preview tools. Prefer that over spawning `python app.py` by hand.
- Needs internet (calls XIVAPI).
- **Auth/persistence env vars** (all optional locally): `FLASK_SECRET_KEY`,
  `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`. Set `AUTH_DISABLED=1` to run
  without a Google OAuth client — the app then uses a single `local` user
  and the Library/history work offline. See "Persistence & auth" below.

## Testing & CI

- `pip install -r requirements-dev.txt && pytest` (config in `pytest.ini`).
- Tests live in `tests/`; **all XIVAPI calls are mocked** through the single
  `xiv_get()` seam (see `tests/conftest.py`). Never hit the live API in tests.
- CI: `.github/workflows/ci.yml` runs pytest on Python 3.10–3.12.
- Persistence tests use the `temp_db` fixture (a throwaway SQLite file per
  test). The suite defaults to `AUTH_DISABLED` (autouse fixture); auth-gating
  tests opt back in with `auth_enabled` and stamp the Flask session directly
  — no live Google calls.

## ⚠️ Editing `app.py` — CRLF line endings

`app.py` uses **CRLF** line terminators. The `Edit` tool matches bytes exactly,
so any **multi-line** `old_string` fails with "String to replace not found".

- **Single-line** edits work fine.
- For **multi-line** edits, write a small Python splice script: read with
  `open(path, "r", newline="")` (preserves CRLF), `str.replace` a CRLF-joined
  block (or splice by line index via `src.split("\r\n")`), write back with
  `newline=""`. Then verify: `python -c "import ast; ast.parse(open('app.py').read())"`.
- Files under `tests/` are LF — the `Edit` tool works on them normally.

## Code map (`app.py`, top to bottom)

- **XIVAPI helpers**: `xiv_get` (the one network seam — mock this), `icon_url`,
  `search_items_xiv`, `find_recipe`, `parse_recipe`, `get_item_info`,
  `classify` (keyword heuristic → gathered / crystal / crafted / other).
- **Gathering lookup**: `get_gathering_locations` + `_extract_exported_point` +
  `_to_map_coord`. See XIVAPI quirks below.
- **Zone graph / routing**: `ZONE_DATA` (hardcoded ~60 zones with approximate
  teleport costs & abstract coords), `zone_distance`, `travel_time_minutes`,
  `teleport_cost`, `solve_tsp_nearest_neighbor` (repeated nearest-neighbor —
  tries every start), `build_route`.
- **Assignment**: `_candidate_zones`, `_greedy_set_cover`, `_make_entry`,
  `_assign_items_to_zones` (two-phase: set-cover anchor items, then fold
  crystals into already-visited zones only).
- **Breakdown**: `build_material_tree` (recursive, accumulates into a shared
  `raw_materials` dict so multiple targets aggregate), `group_materials`,
  `_fetch_info` / `_fetch_recipe` (cached).
- **Endpoints**: `/`, `/api/search`, `/api/breakdown` (single target),
  `/api/breakdown_multi` (list of targets, aggregated), `/api/route`,
  `/api/lookup_zones`, and several `/api/debug/*` helpers.
- **Persistence + auth**: `db()` (SQLite conn context-manager, lazy schema),
  `get_or_create_user`, `get_current_user_id` (the ONE auth seam — returns
  the session user, or the `local` placeholder when `AUTH_DISABLED`),
  `record_search`/`recent_searches`, `save_route`/`list_saved_routes`/
  `get_saved_route`/`delete_saved_route`. Endpoints: `/api/me`, `/login`,
  `/auth/callback`, `/logout`, `/api/history`, `/api/routes` (+ `/<id>`).
- **Embedded HTML/JS**: the `HTML` string. Frontend state is the `S` object
  (`S.targets` = items to craft; `S.neededItems` = derived route input).
  Key JS: `addTarget`/`renderTargets`, `setRouteItems`, `renderMultiBreakdown`,
  `renderTree`, `renderChecklist`/`renderTotals`, `renderRouteSection`/`renderRoute`;
  `initAuth`/`renderAuth` (header control + Library gating),
  `loadSavedRoutes`/`loadHistory`/`saveCurrentRoute`/`loadRoute` (Library panel).

## XIVAPI v2 quirks (hard-won — don't regress these)

- **No reverse lookup.** To find where an item is gathered:
  1. `GatheringItem` search `+Item={item_id}` → `gi_id` + node level.
  2. `GatheringPoint` search `+GatheringPointBase.Item[]={gi_id}`. The plain
     `+Item[]={gi_id}` form returns **HTTP 400** — it MUST be nested through
     `GatheringPointBase`.
  3. Zone name = `TerritoryType.PlaceName.Name` (the map). **Not** `PlaceName`
     — that's the sub-landmark (e.g. "Horizon's Edge") and won't match `ZONE_DATA`.
  4. Coords = `ExportedGatheringPoint/{base_id}` (keyed by the GatheringPointBase
     row id) → raw `X`/`Y`; convert with `_to_map_coord` using the zone's
     `Map.SizeFactor`/offsets.
- `classify()` is a keyword heuristic on item category; it can miscategorize
  (crystals/shards/gems especially). Crystals ARE gatherable but are handled via
  fold-in (they don't force route stops).
- `GatheringPointBase.Item[]` mixes `GatheringItem` and `SpearfishingItem` rows
  — filter by the row's `sheet`/value when scanning.

## Conventions

- Mock `xiv_get` in tests; assert on plain dict shapes (no schema classes).
- Keep the app single-file and dependency-light unless a change warrants otherwise.
- Match the surrounding vanilla-JS style in the `HTML` string (no framework).

## Persistence & auth

Per-user **search history** and **saved routes** persist in a local SQLite
file (`ffxiv_craft.db`, stdlib `sqlite3`, created lazily on first run,
gitignored). Everything is keyed by an internal user id via
`get_current_user_id()` — the single seam:

- With `AUTH_DISABLED=1` (local dev / offline / tests) it returns one `local`
  placeholder user, so the Library works without any OAuth setup.
- Otherwise it reads the Flask session set by **Authlib "Sign in with
  Google"** (`/login` → Google → `/auth/callback` stores `google:{sub}` as
  the user's `external_id`). Persistence endpoints return **401** when nobody
  is signed in.

To enable real sign-in, set `FLASK_SECRET_KEY`, `GOOGLE_CLIENT_ID`, and
`GOOGLE_CLIENT_SECRET` (a Google Cloud OAuth *Web* client; redirect URI
`<host>/auth/callback`). Without those the server still runs but `/login`
returns 503. When adding auth-aware code, keep `get_current_user_id()` the
only place identity is resolved.

## Direction

Next up for **hosted, multi-user**: pick a host and a production datastore.
SQLite is fine for local/low-scale; move to managed Postgres (e.g. Supabase,
which could also replace the auth layer) if/when scale or hosting warrants.
Firebase remains the heaviest fit — the frontend is vanilla JS in a Python
string with no build step.
