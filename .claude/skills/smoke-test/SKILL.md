---
name: smoke-test
description: >-
  End-to-end smoke test for the ffxiv-craft app. Use this whenever you've
  changed app.py — its Flask routes, the recipe-breakdown logic, the gathering
  route/zone code, or the embedded HTML/CSS/JS — and want to confirm the whole
  flow still works before calling a change done, or whenever asked to run,
  verify, screenshot, or "check the app works" in the browser. Drives the real
  UI (search → add targets → calculate materials → plan route) and checks for
  errors. Prefer this over ad-hoc manual checking; it encodes the flow and the
  known gotchas (port conflicts, screenshot timeouts, CRLF editing).
---

# ffxiv-craft smoke test

The app is a single-file Flask app (`app.py`) with vanilla JS embedded as a
Python string; data comes live from XIVAPI. See `CLAUDE.md` for the full
architecture. This skill verifies a change end-to-end.

## Fastest check first: backend via test client

For pure logic changes (breakdown, routing, zone assignment), you don't need the
browser. Exercise the endpoints directly — it's seconds, not a UI drive:

```python
import app
c = app.app.test_client()
bd = c.post("/api/breakdown_multi", json={"targets": [
    {"item_id": 5056, "quantity": 2},   # Bronze Ingot
    {"item_id": 2341, "quantity": 1},   # Bronze Cross-pein Hammer
]}).get_json()
# ... build lookup_zones input from bd["grouped_materials"], POST /api/route, print
```

Always also run `pytest` (`pip install -r requirements-dev.txt` first). Only go to
the browser when the change touches the rendered UI or you want visual proof.

## Browser drive

Use the in-app browser tools (`mcp__Claude_Browser__*`).

1. **Launch:** `preview_start` with `name: "ffxiv-craft"` (from `.claude/launch.json`).
   - If it reports **port 5000 in use** by a leftover process, free it (Windows):
     `Stop-Process -Id <PID> -Force`, confirm with
     `Get-NetTCPConnection -LocalPort 5000 -State Listen`. Then retry.

2. **Get element refs:** `read_page` with `filter: "interactive"`. The search box
   is the item-name textbox, plus a quantity number field, a Search button, and
   the two action buttons ("Calculate Materials", "Plan Gathering Route").

3. **Add two targets** (multi-target is the main path — test more than one):
   - Click the item textbox, `type` an item name (e.g. `Bronze Ingot`), click Search.
   - `read_page` (filter `all`) to find the result rows; click one to add it to
     "Items to Craft".
   - Set quantity with `form_input` on the qty field; add a second item the same way.

4. **Calculate:** click "Calculate Materials", then `wait` ~2s.
   - `read_console_messages` with `onlyErrors: true` → must be empty (no JS errors).
   - `get_page_text` → confirm per-target recipe trees render and the
     **Aggregated Gathering & Collection Log** shows summed quantities (a shared
     material like Copper Ore should be the sum across targets).

5. **Route:** click "Plan Gathering Route", then `wait` ~3s (it calls XIVAPI live).
   - `get_page_text` → confirm the route renders with zones, gil/time totals, and
     per-item node detail (job · level · landmark · `X:.., Y:..`), plus the
     "Crystals & Shards" fold-in note.
   - `read_console_messages` (`onlyErrors: true`) again → still empty.

6. **Stop** the server with `preview_stop` when done.

## Known gotchas

- **Screenshots time out.** The page loads an external Google Fonts stylesheet
  that stalls the screenshot renderer; `computer` screenshot actions reliably
  hang (~30s). Don't rely on them — verify with `read_page`, `get_page_text`, and
  `read_console_messages`, which all work fine. The page IS functional even when a
  screenshot fails.
- **Leftover server on :5000.** A previous `python app.py` or preview can hold the
  port; kill it (see step 1) rather than assuming the preview failed.

## Editing app.py (CRLF)

`app.py` uses CRLF line endings, so multi-line `Edit` calls fail ("String to
replace not found"). Single-line edits are fine. For multi-line changes, write a
Python splice script: read with `open(path, "r", newline="")`, `str.replace` a
`"\r\n".join([...])` block (or splice by index on `src.split("\r\n")`), write back
with `newline=""`, then verify: `python -c "import ast; ast.parse(open('app.py').read())"`.
Files under `tests/` are LF and edit normally. (Also in `CLAUDE.md`.)
