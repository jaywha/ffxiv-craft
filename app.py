from flask import Flask, jsonify, request, Response
import requests, math, itertools

app = Flask(__name__)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "ffxiv-craft-planner/1.0"})

# ---------------------------------------------------------------------------
# XIVAPI v2 helpers  (base: https://v2.xivapi.com/api)
# ---------------------------------------------------------------------------

XIV_BASE = "https://v2.xivapi.com/api"

def xiv_get(path, params=None, timeout=10):
    r = SESSION.get(f"{XIV_BASE}{path}", params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

def icon_url(icon_field):
    if not icon_field:
        return None
    if isinstance(icon_field, dict):
        path = icon_field.get("path_hr1") or icon_field.get("path") or ""
        if path:
            return f"{XIV_BASE}/asset?path={path.lstrip('/')}&format=png"
    return None

def search_items_xiv(query):
    data = xiv_get("/search", params={
        "sheets": "Item",
        "query": f'Name~"{query}"',
        "fields": "Name,Icon",
        "limit": 12,
    })
    results = []
    for row in data.get("results", []):
        f = row.get("fields", {})
        name = f.get("Name", "")
        if not name:
            continue
        results.append({"id": row["row_id"], "name": name, "icon": icon_url(f.get("Icon"))})
    return results

_RCP_FIELDS = "ItemResult,AmountResult,CraftType,RecipeLevelTable,Ingredient,AmountIngredient"

def find_recipe(item_id):
    search = xiv_get("/search", params={
        "sheets": "Recipe",
        "query": f"+ItemResult={item_id}",
        "fields": "ItemResult",
        "limit": 1,
    })
    results = search.get("results", [])
    if not results:
        return None
    recipe_row = results[0]["row_id"]
    data = xiv_get(f"/sheet/Recipe/{recipe_row}", params={"fields": _RCP_FIELDS})
    return parse_recipe(recipe_row, data)

def parse_recipe(recipe_row, data):
    f = data.get("fields", {})
    result_item = f.get("ItemResult") or {}
    ri_f = result_item.get("fields", {}) if isinstance(result_item, dict) else {}
    craft_type = f.get("CraftType") or {}
    ct_f = craft_type.get("fields", {}) if isinstance(craft_type, dict) else {}
    lvl_tbl = f.get("RecipeLevelTable") or {}
    lvl_f = lvl_tbl.get("fields", {}) if isinstance(lvl_tbl, dict) else {}
    raw_amts = f.get("AmountIngredient") or []
    raw_ings = f.get("Ingredient") or []
    ingredients = []
    for i, ing in enumerate(raw_ings):
        if not isinstance(ing, dict):
            continue
        ing_id   = ing.get("value")
        ing_f    = ing.get("fields", {})
        ing_name = ing_f.get("Name", "")
        if not ing_name or not ing_id:
            continue
        amt = raw_amts[i] if i < len(raw_amts) else 0
        if not amt:
            continue
        ui_cat   = ing_f.get("ItemUICategory") or {}
        ui_cat_f = ui_cat.get("fields", {}) if isinstance(ui_cat, dict) else {}
        ingredients.append({
            "id":          ing_id,
            "name":        ing_name,
            "amount":      amt,
            "icon":        icon_url(ing_f.get("Icon")),
            "ui_category": ui_cat_f.get("Name", ""),
        })
    return {
        "recipe_id":     recipe_row,
        "result_name":   ri_f.get("Name", ""),
        "result_amount": f.get("AmountResult", 1) or 1,
        "job":           ct_f.get("Name", ""),
        "level":         lvl_f.get("ClassJobLevel", "?"),
        "ingredients":   ingredients,
    }

def get_item_info(item_id):
    data = xiv_get(f"/sheet/Item/{item_id}", params={
        "fields": "Name,Icon,ItemUICategory.Name"
    })
    f    = data.get("fields", {})
    cat  = f.get("ItemUICategory") or {}
    cat_f = cat.get("fields", {}) if isinstance(cat, dict) else {}
    return {
        "id":          item_id,
        "name":        f.get("Name", f"Item {item_id}"),
        "icon":        icon_url(f.get("Icon")),
        "ui_category": cat_f.get("Name", ""),
    }

GATHERED_KW = {"mineral","stone","ore","log","lumber","cloth","fiber","reagent",
               "ingredient","seafood","bone","hide","material","gathering","plant",
               "seed","fruit","vegetable","grain","spice","water","sand","soil",
               "leather","pelt"}
CRYSTAL_KW  = {"crystal","shard","cluster"}

def classify(name, ui_category, has_recipe):
    if has_recipe:
        return "crafted"
    nl  = (name or "").lower()
    cat = (ui_category or "").lower()
    if any(k in nl for k in CRYSTAL_KW):
        return "crystal"
    if any(k in cat for k in GATHERED_KW):
        return "gathered"
    return "other"

# ---------------------------------------------------------------------------
# Gathering location lookup via XIVAPI GatheringPoint sheet
# ---------------------------------------------------------------------------

def _extract_exported_point(base_id):
    """Fetch raw node coords/radius from ExportedGatheringPoint, keyed by the
    GatheringPointBase row id. Returns {x, y, radius} or {} if unavailable.

    NOTE: X/Y here are raw world coordinates, not the in-game map coordinates
    shown to players. Conversion to map coords requires the zone's Map
    SizeFactor and is applied downstream."""
    try:
        data = xiv_get(f"/sheet/ExportedGatheringPoint/{base_id}")
        f = data.get("fields", {})
        return {"x": f.get("X"), "y": f.get("Y"), "radius": f.get("Radius")}
    except Exception:
        return {}


def _to_map_coord(raw, size_factor=100, offset=0):
    """Convert a raw world coordinate to the in-game 2D map coordinate
    (the numbers shown on the player map). Returns None if raw is missing."""
    if raw is None:
        return None
    c = (size_factor or 100) / 100.0
    return round((41.0 / c) * (((raw + (offset or 0)) * c + 1024.0) / 2048.0) + 1.0, 1)


def get_gathering_locations(item_id):
    """Return list of gathering nodes for a gathered item, each shaped:
        {zone, pinpoint, type, level, x, y, radius}

    XIVAPI v2 has NO reverse-lookup, so we traverse relationships forward:

      1. GatheringItem  +Item={item_id}
            -> gi_id (GatheringItem row) + node level.

      2. GatheringPoint +GatheringPointBase.Item[]={gi_id}
            -> zone     = TerritoryType.PlaceName.Name  (the actual map, e.g.
                          "Western Thanalan" -- this is what ZONE_DATA keys on)
               pinpoint = PlaceName.Name                (landmark, e.g.
                          "Horizon's Edge")
               job      = GatheringPointBase.GatheringType.Name
               base_id  = GatheringPointBase row id     (keys step 3)

      3. ExportedGatheringPoint/{base_id}
            -> raw X/Y/Radius for the node.

    The plain +Item[]= query (no GatheringPointBase prefix) returns HTTP 400
    from v2 -- it MUST be nested through GatheringPointBase. Zone must come
    from TerritoryType.PlaceName, NOT PlaceName (which is the sub-landmark and
    won't match ZONE_DATA).
    """
    try:
        # Step 1 -- find GatheringItem row(s) for this item
        gi_search = xiv_get("/search", params={
            "sheets": "GatheringItem",
            "query": f"+Item={item_id}",
            "fields": "GatheringItemLevel",
            "limit": 5,
        })
        gi_rows = gi_search.get("results", [])
        if not gi_rows:
            return []

        locations = []
        seen_bases = set()  # dedupe nodes that share a GatheringPointBase

        for gi_row in gi_rows[:3]:
            gi_id = gi_row["row_id"]
            gi_f  = gi_row.get("fields", {})
            lvl_obj    = gi_f.get("GatheringItemLevel") or {}
            lvl_f      = lvl_obj.get("fields", {}) if isinstance(lvl_obj, dict) else {}
            gather_lvl = lvl_f.get("GatheringItemLevel", "?")

            # Step 2 -- nested-array search on GatheringPoint via GatheringPointBase.
            gp_search = xiv_get("/search", params={
                "sheets": "GatheringPoint",
                "query": f"+GatheringPointBase.Item[]={gi_id}",
                "fields": "TerritoryType.PlaceName.Name,PlaceName.Name,"
                          "TerritoryType.Map.SizeFactor,TerritoryType.Map.OffsetX,"
                          "TerritoryType.Map.OffsetY,"
                          "GatheringPointBase.GatheringType.Name",
                "limit": 20,
            })
            for pt_row in gp_search.get("results", []):
                pf = pt_row.get("fields", {})
                # zone comes from TerritoryType.PlaceName (the map), not PlaceName
                tt   = pf.get("TerritoryType") or {}
                tt_f = tt.get("fields", {}) if isinstance(tt, dict) else {}
                tt_pn   = tt_f.get("PlaceName") or {}
                tt_pn_f = tt_pn.get("fields", {}) if isinstance(tt_pn, dict) else {}
                zone = tt_pn_f.get("Name") or ""
                # map scale/offset for converting node coords to map coords
                mp   = tt_f.get("Map") or {}
                mp_f = mp.get("fields", {}) if isinstance(mp, dict) else {}
                size_factor = mp_f.get("SizeFactor") or 100
                off_x = mp_f.get("OffsetX") or 0
                off_y = mp_f.get("OffsetY") or 0
                # pinpoint landmark (nice-to-have, may be blank)
                pn   = pf.get("PlaceName") or {}
                pn_f = pn.get("fields", {}) if isinstance(pn, dict) else {}
                pinpoint = pn_f.get("Name") or ""
                # gathering job + base id (keys the coord lookup)
                gpb   = pf.get("GatheringPointBase") or {}
                gpb_f = gpb.get("fields", {}) if isinstance(gpb, dict) else {}
                base_id = gpb.get("value") if isinstance(gpb, dict) else None
                if base_id is None and isinstance(gpb, dict):
                    base_id = gpb.get("row_id")
                gt   = gpb_f.get("GatheringType") or {}
                gt_f = gt.get("fields", {}) if isinstance(gt, dict) else {}
                gtype = gt_f.get("Name", "Gathering")

                if not zone or base_id is None or base_id in seen_bases:
                    continue
                seen_bases.add(base_id)

                coords = _extract_exported_point(base_id)
                locations.append({
                    "zone":     zone,
                    "pinpoint": pinpoint,
                    "type":     gtype,
                    "level":    gather_lvl,
                    "x":        _to_map_coord(coords.get("x"), size_factor, off_x),
                    "y":        _to_map_coord(coords.get("y"), size_factor, off_y),
                    "radius":   coords.get("radius"),
                })

        return locations
    except Exception:
        return []

# ---------------------------------------------------------------------------
# FFXIV zone graph for TSP (aetheryte network adjacency)
# Teleport costs in gil (approximate base costs), travel times in minutes
# Zones grouped by expansion for context
# ---------------------------------------------------------------------------

# Zone data: name -> {aetheryte, region, expansion, coords_approx}
# coords_approx are abstract map coordinates for distance estimation
ZONE_DATA = {
    # ARR - La Noscea
    "Limsa Lominsa": {"region": "La Noscea", "expansion": "ARR", "x": 0, "y": 0, "is_city": True, "teleport_cost": 0},
    "Middle La Noscea": {"region": "La Noscea", "expansion": "ARR", "x": -1, "y": 1, "is_city": False, "teleport_cost": 150},
    "Lower La Noscea": {"region": "La Noscea", "expansion": "ARR", "x": -2, "y": 2, "is_city": False, "teleport_cost": 180},
    "Eastern La Noscea": {"region": "La Noscea", "expansion": "ARR", "x": 2, "y": 1, "is_city": False, "teleport_cost": 220},
    "Western La Noscea": {"region": "La Noscea", "expansion": "ARR", "x": -3, "y": 0, "is_city": False, "teleport_cost": 260},
    "Upper La Noscea": {"region": "La Noscea", "expansion": "ARR", "x": 1, "y": -1, "is_city": False, "teleport_cost": 300},
    "Outer La Noscea": {"region": "La Noscea", "expansion": "ARR", "x": 3, "y": -2, "is_city": False, "teleport_cost": 340},
    # ARR - Thanalan
    "Ul'dah": {"region": "Thanalan", "expansion": "ARR", "x": 10, "y": 10, "is_city": True, "teleport_cost": 0},
    "Western Thanalan": {"region": "Thanalan", "expansion": "ARR", "x": 8, "y": 11, "is_city": False, "teleport_cost": 150},
    "Central Thanalan": {"region": "Thanalan", "expansion": "ARR", "x": 10, "y": 12, "is_city": False, "teleport_cost": 180},
    "Eastern Thanalan": {"region": "Thanalan", "expansion": "ARR", "x": 13, "y": 11, "is_city": False, "teleport_cost": 220},
    "Southern Thanalan": {"region": "Thanalan", "expansion": "ARR", "x": 11, "y": 15, "is_city": False, "teleport_cost": 300},
    "Northern Thanalan": {"region": "Thanalan", "expansion": "ARR", "x": 10, "y": 8, "is_city": False, "teleport_cost": 260},
    # ARR - Black Shroud
    "Gridania": {"region": "Black Shroud", "expansion": "ARR", "x": 20, "y": 5, "is_city": True, "teleport_cost": 0},
    "Central Shroud": {"region": "Black Shroud", "expansion": "ARR", "x": 20, "y": 7, "is_city": False, "teleport_cost": 150},
    "East Shroud": {"region": "Black Shroud", "expansion": "ARR", "x": 23, "y": 6, "is_city": False, "teleport_cost": 180},
    "South Shroud": {"region": "Black Shroud", "expansion": "ARR", "x": 21, "y": 10, "is_city": False, "teleport_cost": 220},
    "North Shroud": {"region": "Black Shroud", "expansion": "ARR", "x": 19, "y": 3, "is_city": False, "teleport_cost": 260},
    # ARR - Coerthas / Mor Dhona
    "Coerthas Central Highlands": {"region": "Coerthas", "expansion": "ARR", "x": 15, "y": 0, "is_city": False, "teleport_cost": 400},
    "Mor Dhona": {"region": "Mor Dhona", "expansion": "ARR", "x": 18, "y": -1, "is_city": False, "teleport_cost": 420},
    # HW
    "Ishgard": {"region": "Coerthas", "expansion": "HW", "x": 14, "y": -2, "is_city": True, "teleport_cost": 0},
    "Coerthas Western Highlands": {"region": "Coerthas", "expansion": "HW", "x": 12, "y": -4, "is_city": False, "teleport_cost": 400},
    "The Sea of Clouds": {"region": "Abalathia", "expansion": "HW", "x": 10, "y": -6, "is_city": False, "teleport_cost": 450},
    "Azys Lla": {"region": "Abalathia", "expansion": "HW", "x": 8, "y": -8, "is_city": False, "teleport_cost": 500},
    "The Dravanian Forelands": {"region": "Dravania", "expansion": "HW", "x": 20, "y": -5, "is_city": False, "teleport_cost": 460},
    "The Dravanian Hinterlands": {"region": "Dravania", "expansion": "HW", "x": 24, "y": -7, "is_city": False, "teleport_cost": 480},
    "The Churning Mists": {"region": "Dravania", "expansion": "HW", "x": 22, "y": -9, "is_city": False, "teleport_cost": 500},
    # SB
    "Kugane": {"region": "Hingashi", "expansion": "SB", "x": 35, "y": 0, "is_city": True, "teleport_cost": 0},
    "The Ruby Sea": {"region": "Hingashi", "expansion": "SB", "x": 37, "y": 2, "is_city": False, "teleport_cost": 450},
    "Yanxia": {"region": "Othard", "expansion": "SB", "x": 38, "y": -2, "is_city": False, "teleport_cost": 460},
    "The Azim Steppe": {"region": "Othard", "expansion": "SB", "x": 40, "y": -5, "is_city": False, "teleport_cost": 500},
    "The Fringes": {"region": "Gyr Abania", "expansion": "SB", "x": 30, "y": -3, "is_city": False, "teleport_cost": 420},
    "The Peaks": {"region": "Gyr Abania", "expansion": "SB", "x": 28, "y": -6, "is_city": False, "teleport_cost": 440},
    "The Lochs": {"region": "Gyr Abania", "expansion": "SB", "x": 32, "y": -5, "is_city": False, "teleport_cost": 460},
    # ShB
    "The Crystarium": {"region": "Lakeland", "expansion": "ShB", "x": 45, "y": 5, "is_city": True, "teleport_cost": 0},
    "Eulmore": {"region": "Kholusia", "expansion": "ShB", "x": 42, "y": 8, "is_city": True, "teleport_cost": 0},
    "Lakeland": {"region": "Lakeland", "expansion": "ShB", "x": 46, "y": 7, "is_city": False, "teleport_cost": 460},
    "Kholusia": {"region": "Kholusia", "expansion": "ShB", "x": 41, "y": 10, "is_city": False, "teleport_cost": 460},
    "Amh Araeng": {"region": "Amh Araeng", "expansion": "ShB", "x": 48, "y": 12, "is_city": False, "teleport_cost": 480},
    "Il Mheg": {"region": "Il Mheg", "expansion": "ShB", "x": 44, "y": 2, "is_city": False, "teleport_cost": 480},
    "The Rak'tika Greatwood": {"region": "Rak'tika", "expansion": "ShB", "x": 50, "y": 4, "is_city": False, "teleport_cost": 500},
    "The Tempest": {"region": "The Tempest", "expansion": "ShB", "x": 52, "y": 8, "is_city": False, "teleport_cost": 520},
    # EW
    "Old Sharlayan": {"region": "Sharlayan", "expansion": "EW", "x": 55, "y": -5, "is_city": True, "teleport_cost": 0},
    "Raz-at-Han": {"region": "Thavnair", "expansion": "EW", "x": 58, "y": 0, "is_city": True, "teleport_cost": 0},
    "Labyrinthos": {"region": "Sharlayan", "expansion": "EW", "x": 55, "y": -8, "is_city": False, "teleport_cost": 460},
    "Thavnair": {"region": "Thavnair", "expansion": "EW", "x": 59, "y": 3, "is_city": False, "teleport_cost": 480},
    "Garlemald": {"region": "Garlemald", "expansion": "EW", "x": 52, "y": -10, "is_city": False, "teleport_cost": 500},
    "Mare Lamentorum": {"region": "Moon", "expansion": "EW", "x": 60, "y": -6, "is_city": False, "teleport_cost": 520},
    "Elpis": {"region": "Elpis", "expansion": "EW", "x": 62, "y": -2, "is_city": False, "teleport_cost": 540},
    "Ultima Thule": {"region": "Ultima Thule", "expansion": "EW", "x": 65, "y": -8, "is_city": False, "teleport_cost": 560},
    # DT
    "Tuliyollal": {"region": "Tural", "expansion": "DT", "x": 70, "y": 5, "is_city": True, "teleport_cost": 0},
    "Solution Nine": {"region": "Tural", "expansion": "DT", "x": 72, "y": 8, "is_city": True, "teleport_cost": 0},
    "Urqopacha": {"region": "Tural", "expansion": "DT", "x": 68, "y": 3, "is_city": False, "teleport_cost": 480},
    "Kozama'uka": {"region": "Tural", "expansion": "DT", "x": 71, "y": 7, "is_city": False, "teleport_cost": 500},
    "Yak T'el": {"region": "Tural", "expansion": "DT", "x": 73, "y": 5, "is_city": False, "teleport_cost": 510},
    "Shaaloani": {"region": "Tural", "expansion": "DT", "x": 74, "y": 2, "is_city": False, "teleport_cost": 520},
    "Heritage Found": {"region": "Tural", "expansion": "DT", "x": 76, "y": 4, "is_city": False, "teleport_cost": 530},
    "Living Memory": {"region": "Tural", "expansion": "DT", "x": 78, "y": 6, "is_city": False, "teleport_cost": 550},
}

def zone_distance(z1, z2):
    """Euclidean distance between two zones using abstract coords."""
    d1 = ZONE_DATA.get(z1, {})
    d2 = ZONE_DATA.get(z2, {})
    if not d1 or not d2:
        return 999
    dx = d1["x"] - d2["x"]
    dy = d1["y"] - d2["y"]
    return math.sqrt(dx*dx + dy*dy)

def travel_time_minutes(z1, z2):
    """Estimate travel time in minutes between zones.
    Same zone = 2min (run), same region = 5min (chocobo), 
    same expansion = 10min (chocobo+ferry), different expansion = 20min.
    Teleport is always ~1min load time."""
    d1 = ZONE_DATA.get(z1, {})
    d2 = ZONE_DATA.get(z2, {})
    if z1 == z2:
        return 2
    if d1.get("region") == d2.get("region"):
        return 5
    if d1.get("expansion") == d2.get("expansion"):
        return 10
    return 20  # cross-expansion needs teleport or long travel

def teleport_cost(z1, z2):
    """Cost of teleporting between two zones (0 if same zone)."""
    if z1 == z2:
        return 0
    d2 = ZONE_DATA.get(z2, {})
    return d2.get("teleport_cost", 300)

def _route_cost(path, cost_fn):
    """Total cost of a path under cost_fn, with the first stop free (you start
    there). Mirrors how build_route tallies per-step cost."""
    return sum(cost_fn(path[i - 1], path[i]) for i in range(1, len(path)))


def solve_tsp_nearest_neighbor(zones, cost_fn):
    """Repeated greedy nearest-neighbor TSP: run NN from every possible start
    and keep the cheapest complete route under cost_fn.

    Trying all starts matters because the first stop is free. For the min-gil
    objective (teleport cost depends only on the destination) the optimum is to
    make the most expensive zone the free start -- a single fixed start would
    instead leave that pricey teleport in the tally. Routes have only a handful
    of zones, so O(n^3) here is negligible."""
    zlist = list(zones)
    if len(zlist) <= 1:
        return zlist

    best_path, best_cost = None, None
    for start in zlist:
        remaining = [z for z in zlist if z != start]
        path = [start]
        while remaining:
            current = path[-1]
            nxt = min(remaining, key=lambda z: cost_fn(current, z))
            path.append(nxt)
            remaining.remove(nxt)
        cost = _route_cost(path, cost_fn)
        if best_cost is None or cost < best_cost:
            best_path, best_cost = path, cost
    return best_path

def build_route(zones, cost_fn, label):
    """Build route with step-by-step instructions."""
    path = solve_tsp_nearest_neighbor(zones, cost_fn)
    total_cost = 0
    total_time = 0
    steps = []
    
    for i, zone in enumerate(path):
        prev = path[i-1] if i > 0 else None
        step_cost = teleport_cost(prev, zone) if prev else 0
        step_time = travel_time_minutes(prev, zone) if prev else 0
        total_cost += step_cost
        total_time += step_time
        
        zd = ZONE_DATA.get(zone, {})
        steps.append({
            "zone": zone,
            "region": zd.get("region", "?"),
            "expansion": zd.get("expansion", "?"),
            "teleport_cost": step_cost,
            "travel_time": step_time,
            "action": "Start here" if i == 0 else (
                "Teleport" if step_cost > 0 else "Walk/Chocobo"
            ),
            "items": zones[zone],
        })
    
    return {
        "label": label,
        "path": path,
        "steps": steps,
        "total_cost": total_cost,
        "total_time_min": total_time,
    }

# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    try:
        return jsonify(search_items_xiv(q))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/debug/search/<path:query>")
def api_debug_search(query):
    try:
        return jsonify(xiv_get("/search", params={
            "sheets": "Item", "query": f'Name~"{query}"', "fields": "Name,Icon", "limit": 5
        }))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/debug/breakdown/<int:item_id>")
def api_debug_breakdown(item_id):
    try:
        search = xiv_get("/search", params={
            "sheets": "Recipe", "query": f"+ItemResult={item_id}",
            "fields": "ItemResult", "limit": 1,
        })
        results = search.get("results", [])
        if not results:
            return jsonify({"error": "no recipe found", "item_id": item_id})
        recipe_row = results[0]["row_id"]
        raw = xiv_get(f"/sheet/Recipe/{recipe_row}", params={"fields": _RCP_FIELDS})
        f = raw.get("fields", {})
        parsed = parse_recipe(recipe_row, raw)
        return jsonify({
            "recipe_row_id": recipe_row,
            "fields_present": sorted(f.keys()),
            "AmountIngredient": f.get("AmountIngredient"),
            "Ingredient_count": len(f.get("Ingredient") or []),
            "Ingredient_sample": [
                {"value": ing.get("value"), "name": (ing.get("fields") or {}).get("Name")}
                for ing in (f.get("Ingredient") or [])[:5]
            ],
            "CraftType": f.get("CraftType"),
            "RecipeLevelTable": f.get("RecipeLevelTable"),
            "parsed_recipe": parsed,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500

@app.route("/api/debug/recipe/<int:item_id>")
def api_debug_recipe(item_id):
    try:
        step1 = xiv_get("/search", params={
            "sheets": "Recipe", "query": f"+ItemResult={item_id}",
            "fields": "ItemResult", "limit": 3,
        })
        out = {"search_results": step1}
        rows = step1.get("results", [])
        if rows:
            rid = rows[0]["row_id"]
            out["recipe_row_id"] = rid
            out["recipe_data_filtered"] = xiv_get(f"/sheet/Recipe/{rid}", params={"fields": _RCP_FIELDS})
            raw = xiv_get(f"/sheet/Recipe/{rid}")
            out["all_field_names"] = sorted(raw.get("fields", {}).keys())
            out["recipe_data_raw"] = raw
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/debug/gathering/<int:item_id>")
def api_debug_gathering(item_id):
    """Debug: show every step of zone lookup. Usage: /api/debug/gathering/<item_id>
    
    Tests in order:
      1. GatheringItem search for +Item={item_id}
      2a. GatheringPoint search with +Item[]={gi_id}  (array bracket notation)
      2b. Raw GatheringItem row fetch (to see all available fields)
    """
    try:
        import traceback as tb
        out = {"item_id": item_id, "steps": []}

        # Step 1
        gi_search = xiv_get("/search", params={
            "sheets": "GatheringItem", "query": f"+Item={item_id}",
            "fields": "GatheringItemLevel", "limit": 5,
        })
        gi_rows = gi_search.get("results", [])
        out["steps"].append({"step": "1_GatheringItem_search", "result_count": len(gi_rows),
                             "row_ids": [r["row_id"] for r in gi_rows]})
        if not gi_rows:
            out["conclusion"] = "No GatheringItem rows — item is not gathered."
            return jsonify(out)

        gi_id = gi_rows[0]["row_id"]

        # Step 2a -- raw fetch of the GatheringItem row to see ALL fields
        try:
            raw_gi = xiv_get(f"/sheet/GatheringItem/{gi_id}")
            out["steps"].append({
                "step": "2a_GatheringItem_raw_fetch",
                "gi_id": gi_id,
                "all_fields": sorted(raw_gi.get("fields", {}).keys()),
                "fields": raw_gi.get("fields", {}),
            })
        except Exception as e:
            out["steps"].append({"step": "2a_GatheringItem_raw_fetch", "error": str(e)})

        # Step 2b -- try GatheringPoint with array bracket notation +Item[]={gi_id}
        try:
            gp_search = xiv_get("/search", params={
                "sheets": "GatheringPoint",
                "query": f"+Item[]={gi_id}",
                "fields": "TerritoryType,PlaceName,GatheringPointBase",
                "limit": 8,
            })
            gp_rows = gp_search.get("results", [])
            out["steps"].append({
                "step": "2b_GatheringPoint_array_search",
                "query": f"+Item[]={gi_id}",
                "result_count": len(gp_rows),
                "zones": [
                    ((r.get("fields", {}).get("PlaceName") or {}).get("fields", {}) or {}).get("Name")
                    for r in gp_rows
                ],
            })
        except Exception as e:
            out["steps"].append({"step": "2b_GatheringPoint_array_search",
                                 "query": f"+Item[]={gi_id}", "error": str(e)})

        # Step 2c -- try GatheringPoint with dot-bracket: +GatheringPointBase.Item[]={gi_id}
        try:
            gp_search2 = xiv_get("/search", params={
                "sheets": "GatheringPoint",
                "query": f"+GatheringPointBase.Item[]={gi_id}",
                "fields": "TerritoryType,PlaceName",
                "limit": 5,
            })
            gp_rows2 = gp_search2.get("results", [])
            out["steps"].append({
                "step": "2c_GatheringPoint_nested_array_search",
                "query": f"+GatheringPointBase.Item[]={gi_id}",
                "result_count": len(gp_rows2),
                "zones": [
                    ((r.get("fields", {}).get("PlaceName") or {}).get("fields", {}) or {}).get("Name")
                    for r in gp_rows2
                ],
            })
        except Exception as e:
            out["steps"].append({"step": "2c_GatheringPoint_nested_array_search",
                                 "query": f"+GatheringPointBase.Item[]={gi_id}", "error": str(e)})

        # Final result
        out["locations"] = get_gathering_locations(item_id)
        return jsonify(out)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500

def _fetch_info(iid, cache):
    if iid not in cache:
        try:
            cache[iid] = get_item_info(iid)
        except Exception:
            cache[iid] = {"id": iid, "name": f"Item {iid}", "icon": None, "ui_category": ""}
    return cache[iid]


def _fetch_recipe(iid, cache):
    if iid not in cache:
        try:
            cache[iid] = find_recipe(iid)
        except Exception:
            cache[iid] = None
    return cache[iid]


def build_material_tree(item_id, quantity, have_items, item_cache, recipe_cache, raw_materials):
    """Expand item_id into an ingredient tree, accumulating leaf/raw materials
    into raw_materials. raw_materials sums total_needed across calls, so several
    targets can share one dict to aggregate their shared materials."""
    def build(iid, qty_needed, depth=0, ing_name=None, ing_icon=None, ing_cat=""):
        have      = have_items.get(str(iid), 0)
        qty_after = max(0, qty_needed - have)

        recipe     = _fetch_recipe(iid, recipe_cache)
        has_recipe = bool(recipe and recipe.get("ingredients"))

        if ing_name:
            name, icon, cat = ing_name, ing_icon, ing_cat
        else:
            info = _fetch_info(iid, item_cache)
            name, icon, cat = info["name"], info["icon"], info["ui_category"]

        src = classify(name, cat, has_recipe)

        node = {
            "item_id": iid, "name": name, "icon": icon,
            "qty_needed": qty_needed, "qty_have": have,
            "qty_to_craft_or_gather": qty_after,
            "source": src, "depth": depth,
            "children": [], "is_leaf": False,
        }

        if qty_after == 0:
            node["is_leaf"] = True
            return node

        if not has_recipe:
            node["is_leaf"] = True
            if iid not in raw_materials:
                raw_materials[iid] = {"name": name, "icon": icon, "total_needed": 0,
                                      "source": src, "ui_category": cat}
            raw_materials[iid]["total_needed"] += qty_after
            return node

        result_amt     = recipe.get("result_amount", 1) or 1
        times_to_craft = math.ceil(qty_after / result_amt)
        node.update({"job": recipe.get("job"), "level": recipe.get("level"),
                     "times_to_craft": times_to_craft, "result_amount": result_amt})

        for ing in recipe["ingredients"]:
            child = build(ing["id"], ing["amount"] * times_to_craft, depth + 1,
                          ing_name=ing["name"], ing_icon=ing["icon"],
                          ing_cat=ing.get("ui_category", ""))
            node["children"].append(child)

        return node

    root_info = _fetch_info(item_id, item_cache)
    return build(item_id, quantity,
                 ing_name=root_info["name"], ing_icon=root_info["icon"],
                 ing_cat=root_info["ui_category"])


def group_materials(raw_materials):
    """Bucket accumulated raw materials into gathered / crystal / other."""
    grouped = {"gathered": [], "crystal": [], "other": []}
    for iid, mat in raw_materials.items():
        src = mat["source"] if mat["source"] in grouped else "other"
        grouped[src].append({"item_id": iid, "name": mat["name"], "icon": mat["icon"],
                              "total_needed": mat["total_needed"],
                              "gather_sources": [], "has_mob_drop": src == "other"})
    for src in grouped:
        grouped[src].sort(key=lambda x: x["name"])
    return grouped


@app.route("/api/breakdown", methods=["POST"])
def api_breakdown():
    body       = request.get_json()
    item_id    = int(body["item_id"])
    quantity   = int(body.get("quantity", 1))
    have_items = {str(k): int(v) for k, v in body.get("have_items", {}).items()}

    item_cache, recipe_cache, raw_materials = {}, {}, {}
    tree = build_material_tree(item_id, quantity, have_items,
                               item_cache, recipe_cache, raw_materials)
    grouped = group_materials(raw_materials)
    return jsonify({"tree": tree, "raw_materials": raw_materials, "grouped_materials": grouped})


@app.route("/api/breakdown_multi", methods=["POST"])
def api_breakdown_multi():
    """Break down several target items at once and aggregate their materials.

    Body: { targets: [{item_id, quantity}, ...], have_items?: {id: qty} }
    Returns per-target trees plus raw_materials / grouped_materials summed
    across every target, so shared sub-materials add up into one list."""
    body = request.get_json()
    targets = body.get("targets", [])
    have_items = {str(k): int(v) for k, v in body.get("have_items", {}).items()}

    item_cache, recipe_cache, raw_materials = {}, {}, {}
    out_targets = []
    for t in targets:
        try:
            iid = int(t["item_id"])
            qty = int(t.get("quantity", 1))
        except (KeyError, TypeError, ValueError):
            continue
        if qty < 1:
            continue
        tree = build_material_tree(iid, qty, have_items,
                                   item_cache, recipe_cache, raw_materials)
        info = _fetch_info(iid, item_cache)
        out_targets.append({"item_id": iid, "name": info["name"],
                            "icon": info["icon"], "quantity": qty, "tree": tree})

    grouped = group_materials(raw_materials)
    return jsonify({"targets": out_targets, "raw_materials": raw_materials,
                    "grouped_materials": grouped})


def _candidate_zones(zone_hints):
    """All ZONE_DATA keys an item could be gathered in, given its zone hints.
    Exact matches first, then fuzzy substring matches; de-duplicated, stable."""
    cands = []
    for z in zone_hints or []:
        if z in ZONE_DATA and z not in cands:
            cands.append(z)
    for z in zone_hints or []:
        if z in ZONE_DATA:
            continue
        for k in ZONE_DATA:
            if (z.lower() in k.lower() or k.lower() in z.lower()) and k not in cands:
                cands.append(k)
    return cands


def _make_entry(item, target=None):
    """Route-step entry for an item, enriched with the node detail for the
    chosen zone (pinpoint/job/level/coords) when available."""
    entry = {
        "name": item["name"],
        "qty": item.get("total_needed", 1),
        "icon": item.get("icon"),
    }
    node = next((n for n in item.get("nodes", []) if n.get("zone") == target), None)
    if node:
        entry.update({
            "pinpoint": node.get("pinpoint"),
            "gtype": node.get("type"),
            "level": node.get("level"),
            "x": node.get("x"),
            "y": node.get("y"),
        })
    return entry


def _greedy_set_cover(entries, indices):
    """Pick a small set of zones covering every item in `indices`, greedily
    taking the zone that covers the most still-uncovered items."""
    chosen = set()
    unassigned = list(indices)
    while unassigned:
        counts = {}
        for idx in unassigned:
            for z in entries[idx][1]:
                counts[z] = counts.get(z, 0) + 1
        max_cov = max(counts.values())
        # tie-break: prefer cheaper teleport, then name, for determinism
        best = min(
            (z for z in counts if counts[z] == max_cov),
            key=lambda z: (ZONE_DATA.get(z, {}).get("teleport_cost", 999), z),
        )
        chosen.add(best)
        unassigned = [idx for idx in unassigned if best not in entries[idx][1]]
    return chosen


def _assign_items_to_zones(items):
    """Assign items to gathering zones. Returns (zone_items, unknown, flexible).

    Two phases so crystals never force a detour:
      1. Set-cover the "anchor" items (ores, logs, reagents) into the fewest
         zones -- these are what actually justify travel.
      2. Fold crystals in ONLY where one of their (many) zones is already being
         visited. Crystals that fit nowhere go to `flexible` (gather passively /
         buy on the Market Board) rather than adding a dedicated stop.

    Items whose hints match no known zone go to `unknown` (mob drops, vendor).
    """
    entries = []  # (item, candidate_zones, is_crystal)
    for item in items:
        cands = _candidate_zones(item.get("zones", []))
        entries.append((item, cands, item.get("source") == "crystal"))

    # Phase 1: set-cover the anchor (non-crystal) items that have zones.
    anchor_idx = [i for i, (_, c, is_cry) in enumerate(entries) if c and not is_cry]
    chosen_set = _greedy_set_cover(entries, anchor_idx)

    zone_items = {}
    unknown, flexible = [], []
    for item, cands, is_crystal in entries:
        if not cands:
            # crystals with no node data are still "buy/passive", not errors
            (flexible if is_crystal else unknown).append(_make_entry(item))
            continue
        target = next((z for z in cands if z in chosen_set), None)
        if is_crystal and target is None:
            # fold-in only: no already-visited zone -> do not add a stop
            flexible.append(_make_entry(item))
            continue
        if target is None:
            # anchor item somehow uncovered; give it its own stop
            target = cands[0]
            chosen_set.add(target)
        zone_items.setdefault(target, []).append(_make_entry(item, target))
    return zone_items, unknown, flexible


@app.route("/api/route", methods=["POST"])
def api_route():
    """
    Given a list of gathering items with zone hints, compute TSP routes.
    Body: { items: [{name, icon, total_needed, zones: ["Zone Name", ...]}, ...] }
    Returns two routes: min_cost and min_time.
    """
    body = request.get_json()
    items = body.get("items", [])
    
    # Consolidate anchor items into as few stops as possible and fold crystals
    # into those stops where they fit (see _assign_items_to_zones).
    known_zones, unknown_items, flexible_items = _assign_items_to_zones(items)

    if not known_zones:
        return jsonify({
            "min_cost_route": None,
            "min_time_route": None,
            "unknown_items": unknown_items,
            "flexible_items": flexible_items,
            "zone_count": 0,
            "message": "No zone data available for any items. Try gathering location lookup first."
        })
    
    min_cost_route = build_route(known_zones, teleport_cost, "Cheapest Route (Min Gil)")
    min_time_route = build_route(known_zones, travel_time_minutes, "Fastest Route (Min Time)")
    
    return jsonify({
        "min_cost_route": min_cost_route,
        "min_time_route": min_time_route,
        "unknown_items": unknown_items,
        "flexible_items": flexible_items,
        "zone_count": len(known_zones),
    })


@app.route("/api/lookup_zones", methods=["POST"])
def api_lookup_zones():
    """
    Look up gathering zone data for a list of items from XIVAPI.
    Body: { items: [{item_id, name, icon, total_needed, source}, ...] }
    Returns items with zones filled in.
    """
    body = request.get_json()
    items = body.get("items", [])
    results = []
    
    for item in items:
        iid = item.get("item_id")
        source = item.get("source", "other")
        zones = []
        nodes = []
        # Crystals/shards are gathered too (as node yields), and each is
        # available in many zones -- so they usually fold into a stop already
        # on the route. Mob drops / "other" have no gathering nodes; skip them.
        if source in ("gathered", "crystal") and iid:
            try:
                nodes = get_gathering_locations(iid)
                zones = [n["zone"] for n in nodes if n.get("zone") and n["zone"] != "Unknown"]
                zones = list(dict.fromkeys(zones))  # deduplicate
            except Exception:
                zones, nodes = [], []
        results.append({**item, "zones": zones, "nodes": nodes})
    
    return jsonify(results)


# ---------------------------------------------------------------------------
# Embedded HTML
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FFXIV Craft Planner</title>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;600;700&family=Lato:wght@300;400;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#0d0f14;--bg2:#13161e;--bg3:#1a1e2a;--border:#2a2f42;--border2:#3a4060;--gold:#c8a84b;--gold2:#e8c870;--gold-dim:#6b5820;--teal:#4ecdc4;--teal-dim:#1a4f4c;--red:#e05555;--red-dim:#4a1f1f;--green:#5ec96c;--green-dim:#1a4022;--purple:#9b7fe8;--purple-dim:#2e2060;--blue:#5b9bd5;--blue-dim:#1a2d4a;--orange:#e8934a;--orange-dim:#4a2d10;--text:#d4cfc4;--text2:#8a8678;--text3:#5a5650;--r:6px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Lato',sans-serif;font-weight:300;min-height:100vh;font-size:14px;line-height:1.6}
header{border-bottom:1px solid var(--border);padding:18px 32px;display:flex;align-items:center;gap:16px;background:var(--bg2)}
.logo{font-family:'Cinzel',serif;font-size:20px;font-weight:700;color:var(--gold);letter-spacing:.05em}
.logo span{color:var(--gold2)}
.sub{font-size:12px;color:var(--text3);letter-spacing:.1em;text-transform:uppercase}
.layout{display:grid;grid-template-columns:360px 1fr;height:calc(100vh - 63px)}
.sidebar{background:var(--bg2);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
.main{overflow-y:auto;padding:24px;display:flex;flex-direction:column;gap:24px}
.search-panel{padding:20px;border-bottom:1px solid var(--border)}
.panel-label{font-family:'Cinzel',serif;font-size:11px;letter-spacing:.15em;color:var(--gold-dim);text-transform:uppercase;margin-bottom:10px}
.search-row{display:flex;gap:8px}
input[type=text],input[type=number]{background:var(--bg3);border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-family:'Lato',sans-serif;font-size:13px;padding:8px 12px;outline:none;transition:border-color .15s}
input[type=text]:focus,input[type=number]:focus{border-color:var(--gold-dim)}
input[type=text]{flex:1}
input[type=number]{width:70px}
button{background:var(--gold-dim);border:1px solid var(--gold);border-radius:var(--r);color:var(--gold2);cursor:pointer;font-family:'Cinzel',serif;font-size:11px;letter-spacing:.08em;padding:8px 14px;transition:background .15s,color .15s;white-space:nowrap}
button:hover{background:var(--gold);color:var(--bg)}
button:disabled{opacity:.4;cursor:not-allowed}
.search-results{padding:0 20px 12px;display:flex;flex-direction:column;gap:4px;max-height:200px;overflow-y:auto}
.sri{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:var(--r);cursor:pointer;border:1px solid transparent;transition:background .1s,border-color .1s}
.sri:hover{background:var(--bg3);border-color:var(--border)}
.sri.sel{background:var(--bg3);border-color:var(--gold-dim)}
.iico{width:32px;height:32px;border-radius:4px;background:var(--bg3);border:1px solid var(--border);object-fit:cover;flex-shrink:0}
.iico-ph{width:32px;height:32px;border-radius:4px;background:var(--bg3);border:1px solid var(--border);flex-shrink:0}
.iname{font-size:13px;color:var(--text)}
.iid{font-size:11px;color:var(--text3)}

/* ---- Needed Item List (formerly "Items I Already Have") ---- */
.needed-panel{padding:16px 20px;border-bottom:1px solid var(--border);flex:1;overflow-y:auto}
.needed-panel .panel-label{color:var(--teal);border-bottom:1px solid var(--teal-dim);padding-bottom:6px;margin-bottom:10px}
.nil-hint{font-size:11px;color:var(--text3);margin-bottom:8px;line-height:1.5}
.ni-row{display:flex;align-items:center;gap:8px;margin-bottom:6px;padding:6px 8px;background:var(--bg3);border-radius:var(--r);border:1px solid var(--border);position:relative}
.ni-row.collected{border-color:var(--green-dim);opacity:.5}
.ni-row img{width:24px;height:24px;border-radius:3px;flex-shrink:0}
.ni-img-ph{width:24px;height:24px;background:var(--bg);border-radius:3px;flex-shrink:0}
.ni-name{flex:1;font-size:12px;color:var(--text2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ni-qty{font-family:'Cinzel',serif;font-size:12px;color:var(--gold2);white-space:nowrap}
.ni-check{width:18px;height:18px;border:1px solid var(--border2);border-radius:3px;display:flex;align-items:center;justify-content:center;font-size:11px;cursor:pointer;flex-shrink:0;transition:background .15s,border-color .15s;color:var(--green)}
.ni-row.collected .ni-check{background:var(--green-dim);border-color:var(--green)}
.rbtn{background:var(--red-dim);border-color:var(--red);color:var(--red);font-size:10px;padding:4px 8px;font-family:'Lato',sans-serif}
.ni-row .tgt-qty{width:52px;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--gold2);font-family:'Cinzel',serif;font-size:12px;padding:3px 6px;text-align:center}
.tgt-head{display:flex;align-items:center;gap:8px;cursor:pointer;padding-bottom:8px;margin-bottom:8px;border-bottom:1px solid var(--border)}
.tgt-title{flex:1;font-family:'Cinzel',serif;font-size:13px;color:var(--gold2)}
.tgt-toggle{color:var(--text3);font-size:12px}
.rbtn:hover{background:var(--red);color:white}
.action-row{padding:16px 20px;border-top:1px solid var(--border);background:var(--bg2);display:flex;flex-direction:column;gap:8px}
.btn-primary{width:100%;padding:10px;font-size:12px;letter-spacing:.12em;background:linear-gradient(135deg,#3d2c0a,#6b4c10);border-color:var(--gold);color:var(--gold2)}
.btn-primary:hover{background:linear-gradient(135deg,var(--gold-dim),#8a6318)}
.btn-route{width:100%;padding:8px;font-size:11px;letter-spacing:.1em;background:linear-gradient(135deg,var(--teal-dim),#1a6b66);border-color:var(--teal);color:var(--teal)}
.btn-route:hover{background:var(--teal);color:var(--bg)}
.empty{text-align:center;color:var(--text3);font-size:12px;padding:16px;font-style:italic}
.status-bar{font-size:12px;color:var(--text3);display:flex;align-items:center;padding:8px 20px;border-bottom:1px solid var(--border);min-height:36px;background:var(--bg2)}
.err{color:var(--red)}
.spin{width:16px;height:16px;border:2px solid var(--border);border-top-color:var(--gold);border-radius:50%;animation:spin .6s linear infinite;display:inline-block;vertical-align:middle;margin-right:8px;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}
.sec-title{font-family:'Cinzel',serif;font-size:13px;letter-spacing:.12em;color:var(--gold);text-transform:uppercase;border-bottom:1px solid var(--border);padding-bottom:10px;margin-bottom:14px}
.tree-card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);padding:16px}
.tnr{display:flex;align-items:center;gap:8px;padding:5px 0}
.tline{width:20px;flex-shrink:0;border-left:1px solid var(--border2);margin-left:10px;position:relative}
.tline::after{content:'';position:absolute;top:50%;left:0;width:12px;height:1px;background:var(--border2)}
.nico{width:26px;height:26px;border-radius:3px;object-fit:cover;border:1px solid var(--border);flex-shrink:0}
.nico-ph{width:26px;height:26px;border-radius:3px;background:var(--bg3);border:1px solid var(--border);flex-shrink:0}
.ninfo{flex:1;min-width:0}
.nname{font-size:13px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nmeta{font-size:11px;color:var(--text3);display:flex;gap:6px;flex-wrap:wrap;margin-top:1px}
.badge{display:inline-flex;align-items:center;padding:2px 6px;border-radius:3px;font-size:10px;letter-spacing:.04em;white-space:nowrap}
.b-crafted{background:var(--purple-dim);color:var(--purple);border:1px solid var(--purple)}
.b-gathered{background:var(--green-dim);color:var(--green);border:1px solid var(--green)}
.b-crystal{background:var(--teal-dim);color:var(--teal);border:1px solid var(--teal)}
.b-mob{background:var(--red-dim);color:var(--red);border:1px solid var(--red)}
.b-vendor{background:var(--blue-dim);color:var(--blue);border:1px solid var(--blue)}
.b-other{background:var(--orange-dim);color:var(--orange);border:1px solid var(--orange)}
.b-have{background:#1a2e1a;color:var(--green);border:1px solid #3a6a3a}
.qty{font-family:'Cinzel',serif;font-size:12px;color:var(--gold2);background:var(--bg3);border:1px solid var(--gold-dim);border-radius:4px;padding:2px 7px;white-space:nowrap;flex-shrink:0}
.qty.sat{color:var(--text3);border-color:var(--border);background:transparent;text-decoration:line-through}
.ahb{background:transparent;border:1px solid var(--teal-dim);color:var(--teal);font-size:10px;padding:2px 7px;font-family:'Lato',sans-serif;letter-spacing:0}
.ahb:hover{border-color:var(--teal);color:var(--teal);background:var(--teal-dim)}
.tabs{display:flex;gap:0;margin-bottom:16px;border-bottom:1px solid var(--border)}
.tab{padding:8px 16px;font-family:'Cinzel',serif;font-size:11px;letter-spacing:.1em;color:var(--text3);cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;text-transform:uppercase;transition:color .15s,border-color .15s}
.tab:hover{color:var(--text2)}
.tab.active{color:var(--gold);border-bottom-color:var(--gold)}
.tc{display:none}
.tc.active{display:block}
.gs{margin-bottom:20px}
.gsh{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.gsl{font-family:'Cinzel',serif;font-size:11px;letter-spacing:.1em;text-transform:uppercase}
.gsl.gathered{color:var(--green)}.gsl.crystal{color:var(--teal)}.gsl.mob{color:var(--red)}.gsl.vendor{color:var(--blue)}.gsl.other{color:var(--orange)}
.gsline{flex:1;height:1px;background:var(--border)}
.gsc{font-size:11px;color:var(--text3)}
.checklist{display:flex;flex-direction:column;gap:8px}
.ci{background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;transition:border-color .15s}
.ci:hover{border-color:var(--border2)}
.ci.done{opacity:.45}
.cimain{display:flex;align-items:center;gap:10px;padding:10px 12px;cursor:pointer}
.cbox{width:18px;height:18px;border:1px solid var(--border2);border-radius:3px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:11px;color:var(--green);transition:background .15s,border-color .15s}
.ci.done .cbox{background:var(--green-dim);border-color:var(--green)}
.cico{width:28px;height:28px;border-radius:3px;object-fit:cover;border:1px solid var(--border);flex-shrink:0}
.cico-ph{width:28px;height:28px;border-radius:3px;background:var(--bg3);border:1px solid var(--border);flex-shrink:0}
.ciinfo{flex:1;min-width:0}
.ciname{font-size:13px;color:var(--text)}
.ciqty{font-family:'Cinzel',serif;font-size:14px;color:var(--gold2)}
.expand-btn{font-size:11px;color:var(--text3);padding:2px 8px;background:transparent;border:1px solid var(--border);letter-spacing:0;font-family:'Lato',sans-serif}
.expand-btn:hover{color:var(--text);border-color:var(--border2);background:transparent}
.cilocs{border-top:1px solid var(--border);background:var(--bg3);padding:8px 12px 10px 50px;display:none;flex-direction:column;gap:5px}
.ci.exp .cilocs{display:flex}
.lrow{display:flex;align-items:baseline;gap:8px;font-size:12px;color:var(--text2)}
.ltype{color:var(--green);min-width:90px;flex-shrink:0;font-size:11px}
.ltype.mob{color:var(--red)}.ltype.vendor{color:var(--blue)}
.ldetail{color:var(--text2);line-height:1.5}
.lcoords{color:var(--text3);font-size:11px}
.noloc{color:var(--text3);font-style:italic;font-size:12px}
.tot-card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);padding:12px 16px}
.tot-row{display:flex;align-items:center;gap:10px;padding:5px 0;border-bottom:1px solid var(--border);font-size:13px}
.tot-row:last-child{border-bottom:none}
.tot-row img{width:22px;height:22px;border-radius:3px}
.tot-name{flex:1;color:var(--text)}
.tot-qty{font-family:'Cinzel',serif;font-size:14px;color:var(--gold2)}

/* ---- Route Planner ---- */
.route-section{background:var(--bg2);border:1px solid var(--teal-dim);border-radius:var(--r);padding:20px}
.route-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.route-title{font-family:'Cinzel',serif;font-size:13px;letter-spacing:.12em;color:var(--teal);text-transform:uppercase}
.route-tabs{display:flex;gap:0;margin-bottom:16px;border-bottom:1px solid var(--border)}
.route-tab{padding:7px 14px;font-family:'Cinzel',serif;font-size:10px;letter-spacing:.1em;color:var(--text3);cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;text-transform:uppercase;transition:color .15s,border-color .15s}
.route-tab.active{color:var(--teal);border-bottom-color:var(--teal)}
.route-tab:hover{color:var(--text2)}
.route-tc{display:none}
.route-tc.active{display:block}
.route-summary{display:flex;gap:16px;margin-bottom:16px;padding:12px;background:var(--bg3);border-radius:var(--r);border:1px solid var(--border)}
.route-stat{display:flex;flex-direction:column;align-items:center;flex:1}
.route-stat-val{font-family:'Cinzel',serif;font-size:20px;color:var(--teal)}
.route-stat-lbl{font-size:10px;color:var(--text3);letter-spacing:.08em;text-transform:uppercase;margin-top:2px}
.route-steps{display:flex;flex-direction:column;gap:0}
.route-step{display:flex;align-items:stretch;gap:0;position:relative}
.route-step-connector{display:flex;flex-direction:column;align-items:center;width:32px;flex-shrink:0}
.step-dot{width:12px;height:12px;border-radius:50%;border:2px solid var(--teal);background:var(--bg);flex-shrink:0;margin-top:14px;z-index:1}
.step-dot.start{background:var(--teal)}
.step-line{width:2px;background:var(--border2);flex:1;margin:0 auto}
.route-step:last-child .step-line{display:none}
.step-card{flex:1;margin:6px 0 6px 8px;padding:12px 14px;background:var(--bg3);border:1px solid var(--border);border-radius:var(--r);transition:border-color .15s}
.step-card:hover{border-color:var(--border2)}
.step-zone{font-family:'Cinzel',serif;font-size:13px;color:var(--text);font-weight:600}
.step-region{font-size:11px;color:var(--text3);margin-bottom:6px}
.step-action-row{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.step-action{font-size:11px;padding:2px 8px;border-radius:3px}
.step-action.teleport{background:var(--purple-dim);color:var(--purple);border:1px solid var(--purple)}
.step-action.walk{background:var(--green-dim);color:var(--green);border:1px solid var(--green)}
.step-action.start{background:var(--teal-dim);color:var(--teal);border:1px solid var(--teal)}
.step-cost{font-size:11px;color:var(--gold2);font-family:'Cinzel',serif}
.step-time{font-size:11px;color:var(--blue)}
.step-items{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;padding-top:8px;border-top:1px solid var(--border)}
.step-item{display:flex;flex-direction:column;gap:2px;background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:5px 8px;font-size:11px;color:var(--text2)}
.step-item-head{display:flex;align-items:center;gap:5px}
.step-item-name{color:var(--text)}
.step-item-meta{color:var(--green);font-size:10px;letter-spacing:.02em}
.step-item-coords{color:var(--gold2);font-family:'Cinzel',serif;font-size:10px}
.step-item img{width:16px;height:16px;border-radius:2px}
.step-item-qty{color:var(--gold2);font-family:'Cinzel',serif;font-size:11px}
.route-unknown{margin-top:12px;padding:10px 14px;background:var(--orange-dim);border:1px solid var(--orange);border-radius:var(--r);font-size:12px;color:var(--orange)}
.route-flex{margin-top:12px;padding:10px 14px;background:var(--teal-dim);border:1px solid var(--teal);border-radius:var(--r);font-size:12px;color:var(--teal)}
.route-flex strong{display:block;margin-bottom:4px;font-family:'Cinzel',serif;letter-spacing:.08em}
.route-unknown strong{display:block;margin-bottom:4px;font-family:'Cinzel',serif;letter-spacing:.08em}
.zone-badge{display:inline-flex;align-items:center;gap:4px;padding:1px 6px;border-radius:3px;font-size:10px;background:var(--bg3);border:1px solid var(--border);color:var(--text3);cursor:pointer;transition:border-color .15s}
.zone-badge:hover{border-color:var(--teal-dim);color:var(--teal)}
.zone-edit-row{display:flex;align-items:center;gap:6px;margin-top:6px;flex-wrap:wrap}
.zone-select{background:var(--bg3);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:11px;padding:3px 6px;font-family:'Lato',sans-serif;cursor:pointer}
.route-loading{text-align:center;padding:30px;color:var(--text3)}
.spin-teal{width:20px;height:20px;border:2px solid var(--border);border-top-color:var(--teal);border-radius:50%;animation:spin .6s linear infinite;display:inline-block;vertical-align:middle;margin-right:8px}

.welcome{display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;padding:60px;text-align:center;gap:12px}
.welcome h2{font-family:'Cinzel',serif;font-size:22px;color:var(--gold);font-weight:400}
.welcome p{color:var(--text3);font-size:13px;max-width:340px;line-height:1.8}
.welcome .hint{font-size:11px;color:var(--text3);border:1px solid var(--border);border-radius:var(--r);padding:8px 14px;margin-top:8px}
</style>
</head>
<body>
<header>
  <div>
    <div class="logo">FFXIV <span>Craft Planner</span></div>
    <div class="sub">Grand Company Supply &amp; Provision Helper</div>
  </div>
</header>
<div class="layout">
  <aside class="sidebar">
    <div class="search-panel">
      <div class="panel-label">Search Item</div>
      <div class="search-row">
        <input type="text" id="si" placeholder="Item name..." autocomplete="off">
        <input type="number" id="qi" value="1" min="1" title="Quantity needed">
        <button id="sb">Search</button>
      </div>
    </div>
    <div id="stbar" class="status-bar" style="display:none"></div>
    <div id="sr" class="search-results"></div>

    <!-- Items to Craft (targets) -->
    <div class="needed-panel">
      <div class="panel-label">&#9654; Items to Craft</div>
      <div class="nil-hint">Search above and click a result to add it here. Set a quantity per item, then calculate.</div>
      <div id="nil"><div class="empty">No target items yet.<br>Search and click a result to add it.</div></div>
    </div>

    <div class="action-row">
      <button class="btn-primary" id="bb" disabled>&#9658; Calculate Materials</button>
      <button class="btn-route" id="brb" disabled>&#9650; Plan Gathering Route</button>
    </div>
  </aside>

  <main class="main" id="ma">
    <div class="welcome" id="ws">
      <h2>Craft Planner</h2>
      <p>Search for the items you want to craft and add each to your list with a quantity. Calculate to see the combined materials, then plan one optimal gathering route.</p>
      <div class="hint">Recipe &amp; gathering data via XIVAPI · TSP route optimization included</div>
    </div>
  </main>
</div>

<script>
// ---- State ----
const S = {
  sel: null,
  targets: [], // [{id, name, icon, qty}]
  neededItems: {}, // id -> {id, name, icon, qty_needed, source, collected, zones}
  loaded: false,
  lastGrouped: null,
  lastRaw: null,
};

const $ = id => document.getElementById(id);
const stbar = $('stbar');

function setStatus(msg, err) {
  if (!msg) { stbar.style.display = 'none'; return; }
  stbar.style.display = 'flex';
  stbar.innerHTML = err
    ? `<span class="err">${msg}</span>`
    : `<span class="spin"></span>${msg}`;
}

// ---- Search ----
$('sb').onclick = $('si').onkeydown = function(e) {
  if (e.type === 'click' || e.key === 'Enter') doSearch();
};

async function doSearch() {
  const q = $('si').value.trim();
  if (!q) return;
  setStatus('Searching...');
  try {
    const r = await fetch('/api/search?q=' + encodeURIComponent(q));
    const d = await r.json();
    setStatus('');
    renderResults(Array.isArray(d) ? d : []);
  } catch(e) { setStatus('Search failed — is the server running?', true); }
}

function renderResults(items) {
  const el = $('sr'); el.innerHTML = '';
  if (!items.length) { el.innerHTML = '<div class="empty">No results found.</div>'; return; }
  items.forEach(item => {
    const row = document.createElement('div');
    row.className = 'sri';
    row.innerHTML = `${item.icon ? `<img class="iico" src="${item.icon}" alt="">` : '<div class="iico-ph"></div>'}
      <div><div class="iname">${item.name}</div><div class="iid">ID: ${item.id}</div></div>`;
    row.onclick = () => {
      addTarget(item);
      row.classList.add('sel');
      setTimeout(() => row.classList.remove('sel'), 250);
    };
    el.appendChild(row);
  });
}

// ---- Items to Craft (targets) ----
function addTarget(item) {
  const key = String(item.id);
  if (S.targets.some(t => String(t.id) === key)) return; // already added
  const qty = Math.max(1, parseInt($('qi').value) || 1);
  S.targets.push({ id: item.id, name: item.name, icon: item.icon || null, qty });
  renderTargets();
}

function renderTargets() {
  const el = $('nil');
  if (!S.targets.length) {
    el.innerHTML = '<div class="empty">No target items yet.<br>Search and click a result to add it.</div>';
    $('bb').disabled = true;
    return;
  }
  el.innerHTML = '';
  S.targets.forEach(t => {
    const row = document.createElement('div');
    row.className = 'ni-row';
    row.innerHTML = `
      ${t.icon ? `<img src="${t.icon}" alt="">` : '<div class="ni-img-ph"></div>'}
      <span class="ni-name">${t.name}</span>
      <input type="number" class="tgt-qty" min="1" value="${t.qty}" data-id="${t.id}" title="Quantity to craft">
      <button class="rbtn" data-id="${t.id}" title="Remove">✕</button>`;
    row.querySelector('.tgt-qty').onchange = e => {
      const tt = S.targets.find(x => String(x.id) === String(e.currentTarget.dataset.id));
      if (tt) tt.qty = Math.max(1, parseInt(e.currentTarget.value) || 1);
      e.currentTarget.value = tt ? tt.qty : 1;
    };
    row.querySelector('.rbtn').onclick = e => {
      S.targets = S.targets.filter(x => String(x.id) !== String(e.currentTarget.dataset.id));
      renderTargets();
    };
    el.appendChild(row);
  });
  $('bb').disabled = false;
}

// Derive the gathering-route input set from aggregated gathered + crystal mats.
function setRouteItems(grouped) {
  S.neededItems = {};
  ['gathered', 'crystal'].forEach(src => {
    (grouped[src] || []).forEach(m => {
      S.neededItems[String(m.item_id)] = {
        id: m.item_id, name: m.name, icon: m.icon || null,
        qty_needed: m.total_needed, source: src, collected: false, zones: [],
      };
    });
  });
  $('brb').disabled = Object.keys(S.neededItems).length === 0;
}

// ---- Calculate (multi-target breakdown) ----
$('bb').onclick = async function() {
  if (!S.targets.length) return;
  setStatus('Fetching recipe trees...');
  $('bb').disabled = true;
  try {
    const r = await fetch('/api/breakdown_multi', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({targets: S.targets.map(t => ({item_id: t.id, quantity: t.qty}))})
    });
    const d = await r.json();
    S.lastGrouped = d.grouped_materials;
    S.lastRaw = d.raw_materials;
    setStatus('');
    renderMultiBreakdown(d);
    setRouteItems(d.grouped_materials);
  } catch(e) { setStatus('Breakdown failed.', true); }
  $('bb').disabled = false;
};

function renderMultiBreakdown(data) {
  const ma = $('ma');
  if ($('ws')) $('ws').style.display = 'none';
  if (!window._ck) window._ck = {};
  const targets = data.targets || [];
  const title = targets.length === 1
    ? `Recipe Tree — ${targets[0].name} ×${targets[0].quantity}`
    : `Recipe Trees — ${targets.length} Targets`;
  ma.innerHTML = `
    <div>
      <div class="sec-title">${title}</div>
      <div id="trees"></div>
    </div>
    <div>
      <div class="sec-title">Aggregated Gathering &amp; Collection Log</div>
      <div class="tabs">
        <div class="tab active" data-tab="cl">Checklist</div>
        <div class="tab" data-tab="tot">All Totals</div>
      </div>
      <div class="tc active" id="tab-cl"></div>
      <div class="tc" id="tab-tot"></div>
    </div>
    <div id="route-section-container"></div>`;

  ma.querySelectorAll('.tab').forEach(t => t.onclick = () => {
    ma.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    ma.querySelectorAll('.tc').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    ma.querySelector('#tab-' + t.dataset.tab).classList.add('active');
  });

  const treesEl = $('trees');
  const multi = targets.length > 1;
  targets.forEach(tg => {
    const card = document.createElement('div');
    card.className = 'tree-card';
    card.style.marginBottom = '12px';
    const body = document.createElement('div');
    if (multi) {
      const head = document.createElement('div');
      head.className = 'tgt-head';
      head.innerHTML = `${tg.icon ? `<img class="nico" src="${tg.icon}" alt="">` : '<div class="nico-ph"></div>'}`
        + `<span class="tgt-title">${tg.name} ×${tg.quantity}</span><span class="tgt-toggle">▾</span>`;
      head.onclick = () => {
        const hidden = body.style.display === 'none';
        body.style.display = hidden ? '' : 'none';
        head.querySelector('.tgt-toggle').textContent = hidden ? '▾' : '▸';
      };
      card.appendChild(head);
    }
    renderTree(tg.tree, body, 0);
    card.appendChild(body);
    treesEl.appendChild(card);
  });

  renderChecklist(data.grouped_materials, $('tab-cl'));
  renderTotals(data.raw_materials, $('tab-tot'));
}

function renderTree(node, container, depth) {
  const wrap = document.createElement('div');
  const rw = document.createElement('div'); rw.style.cssText = 'display:flex;align-items:stretch';
  for (let i = 0; i < depth; i++) { const l = document.createElement('div'); l.className = 'tline'; rw.appendChild(l); }
  const row = document.createElement('div'); row.className = 'tnr'; row.style.flex = '1';
  const sat = node.qty_to_craft_or_gather === 0;
  const sb2 = sat ? '<span class="badge b-have">✓ Have</span>' : `<span class="badge b-${node.source}">${node.source}</span>`;
  const cm = node.job ? `<span>Lv${node.level} ${node.job}</span><span>×${node.times_to_craft} craft${node.times_to_craft > 1 ? 's' : ''}</span>` : '';
  const isLeafItem = !sat && node.is_leaf;
  row.innerHTML = `${node.icon ? `<img class="nico" src="${node.icon}" alt="">` : '<div class="nico-ph"></div>'}
    <div class="ninfo"><div class="nname">${node.name}</div><div class="nmeta">${sb2}${cm ? '&nbsp;' + cm : ''}</div></div>
    <div style="display:flex;align-items:center;gap:6px;flex-shrink:0">
      <span class="qty${sat ? ' sat' : ''}">×${node.qty_needed}</span>
    </div>`;
  rw.appendChild(row); wrap.appendChild(rw);
  if (node.children && node.children.length) {
    const cc = document.createElement('div');
    node.children.forEach(c => renderTree(c, cc, depth + 1));
    wrap.appendChild(cc);
  }
  container.appendChild(wrap);
}

function renderChecklist(grouped, container) {
  const order = ['gathered','crystal','mob','vendor','other'];
  const labels = {gathered:'Gathered',crystal:'Crystals & Shards',mob:'Mob Drops',vendor:'Vendor / Trade',other:'Other'};
  container.innerHTML = ''; let any = false;
  order.forEach(src => {
    const items = grouped[src] || []; if (!items.length) return; any = true;
    const sec = document.createElement('div'); sec.className = 'gs';
    sec.innerHTML = `<div class="gsh"><span class="gsl ${src}">${labels[src]}</span><div class="gsline"></div><span class="gsc">${items.length} item${items.length>1?'s':''}</span></div><div class="checklist"></div>`;
    const list = sec.querySelector('.checklist');
    items.forEach(mat => {
      const card = document.createElement('div'); card.className = 'ci';
      if (window._ck[mat.item_id]) card.classList.add('done');
      card.innerHTML = `
        <div class="cimain">
          <div class="cbox">${window._ck[mat.item_id] ? '✓' : ''}</div>
          ${mat.icon ? `<img class="cico" src="${mat.icon}" alt="">` : '<div class="cico-ph"></div>'}
          <div class="ciinfo"><div class="ciname">${mat.name}</div><div class="ciqty">×${mat.total_needed}</div></div>
        </div>`;
      card.querySelector('.cimain').onclick = () => {
        card.classList.toggle('done');
        const box = card.querySelector('.cbox');
        window._ck[mat.item_id] = card.classList.contains('done');
        box.textContent = window._ck[mat.item_id] ? '✓' : '';
      };
      list.appendChild(card);
    });
    container.appendChild(sec);
  });
  if (!any) container.innerHTML = '<div class="empty">No raw materials needed.</div>';
}

function renderTotals(raw, container) {
  container.innerHTML = '';
  const items = Object.values(raw).sort((a,b) => a.name.localeCompare(b.name));
  if (!items.length) { container.innerHTML = '<div class="empty">No raw materials needed.</div>'; return; }
  const card = document.createElement('div'); card.className = 'tot-card';
  items.forEach(mat => {
    const src = mat.source || 'other';
    const row = document.createElement('div'); row.className = 'tot-row';
    row.innerHTML = `${mat.icon ? `<img src="${mat.icon}" alt="">` : '<div style="width:22px;height:22px;background:var(--bg3);border-radius:3px"></div>'}
      <span class="tot-name">${mat.name}</span>
      <span class="badge b-${src}" style="margin-right:6px">${src}</span>
      <span class="tot-qty">×${mat.total_needed}</span>`;
    card.appendChild(row);
  });
  container.appendChild(card);
}

// ---- Route Planner ----
$('brb').onclick = async function() {
  if (!Object.keys(S.neededItems).length) return;

  // Find or create route section
  let rsc = $('route-section-container');
  if (!rsc) {
    // If breakdown hasn't been run, inject at end of main
    const ma = $('ma');
    if ($('ws')) $('ws').style.display = 'none';
    const div = document.createElement('div');
    div.id = 'route-section-container';
    ma.appendChild(div);
    rsc = div;
  }

  rsc.innerHTML = `
    <div class="route-section">
      <div class="route-header">
        <div class="route-title">&#9650; Gathering Route Planner</div>
      </div>
      <div class="route-loading"><span class="spin-teal"></span>Looking up gathering zones…</div>
    </div>`;
  rsc.scrollIntoView({behavior:'smooth'});

  try {
    // Step 1: look up zones for gathered items
    const itemsToLookup = Object.values(S.neededItems).map(it => ({
      item_id: it.id,
      name: it.name,
      icon: it.icon,
      total_needed: it.qty_needed,
      source: it.source,
    }));

    const lookupR = await fetch('/api/lookup_zones', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({items: itemsToLookup})
    });
    const itemsWithZones = await lookupR.json();

    // Update neededItems with zone data
    itemsWithZones.forEach(it => {
      if (S.neededItems[String(it.item_id)]) {
        S.neededItems[String(it.item_id)].zones = it.zones || [];
      }
    });

    // Step 2: ask for routes (only gathered/crystal items with zones, 
    // plus "other" items as unknown)
    const routeItems = itemsWithZones.filter(it => 
      it.source === 'gathered' || it.source === 'crystal' || it.source === 'other'
    );

    const routeR = await fetch('/api/route', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({items: routeItems})
    });
    const routeData = await routeR.json();

    renderRouteSection(rsc, routeData, itemsWithZones);
  } catch(e) {
    rsc.innerHTML = `<div class="route-section"><div class="err" style="padding:16px">Route planning failed: ${e.message}</div></div>`;
  }
};

function renderRouteSection(container, data, allItems) {
  const minC = data.min_cost_route;
  const minT = data.min_time_route;
  const unknown = data.unknown_items || [];
  const flexible = data.flexible_items || [];

  let html = `<div class="route-section">
    <div class="route-header">
      <div class="route-title">&#9650; Gathering Route — ${data.zone_count || 0} Zone${(data.zone_count||0)!==1?'s':''}</div>
    </div>`;

  if (!minC && !minT) {
    html += `<div style="color:var(--text3);font-size:13px;padding:8px">${data.message || 'No route data available.'}</div>`;
  } else {
    html += `<div class="route-tabs">
      <div class="route-tab active" data-rtab="cost">&#9830; Min Cost</div>
      <div class="route-tab" data-rtab="time">&#9672; Min Time</div>
    </div>
    <div class="route-tc active" id="rtab-cost">${renderRoute(minC)}</div>
    <div class="route-tc" id="rtab-time">${renderRoute(minT)}</div>`;
  }

  if (flexible.length) {
    html += `<div class="route-flex"><strong>&#9670; Crystals & Shards</strong>
      Gather these passively while you route, or buy them cheaply on the Market Board — no detour needed:<br>
      ${flexible.map(u => `<span style="color:var(--text2)">${u.name} ×${u.qty}</span>`).join(' · ')}
    </div>`;
  }

  if (unknown.length) {
    html += `<div class="route-unknown"><strong>&#9888; Items Without Zone Data</strong>
      These aren't gathered (mob drops, vendor, etc.) — check Garland Tools for sources:<br>
      ${unknown.map(u => `<span style="color:var(--text2)">${u.name} ×${u.qty}</span>`).join(' · ')}
    </div>`;
  }

  html += '</div>';
  container.innerHTML = html;

  // Tab switching
  container.querySelectorAll('.route-tab').forEach(t => t.onclick = () => {
    container.querySelectorAll('.route-tab').forEach(x => x.classList.remove('active'));
    container.querySelectorAll('.route-tc').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    container.querySelector('#rtab-' + t.dataset.rtab).classList.add('active');
  });
}

function renderRoute(route) {
  if (!route) return '<div class="empty">No route available.</div>';
  const steps = route.steps || [];

  let html = `<div class="route-summary">
    <div class="route-stat">
      <div class="route-stat-val">${route.total_cost.toLocaleString()}</div>
      <div class="route-stat-lbl">Gil (Teleport)</div>
    </div>
    <div class="route-stat">
      <div class="route-stat-val">~${route.total_time_min}</div>
      <div class="route-stat-lbl">Minutes</div>
    </div>
    <div class="route-stat">
      <div class="route-stat-val">${steps.length}</div>
      <div class="route-stat-lbl">Zones</div>
    </div>
  </div>
  <div class="route-steps">`;

  steps.forEach((step, i) => {
    const isStart = i === 0;
    const actionClass = isStart ? 'start' : (step.teleport_cost > 0 ? 'teleport' : 'walk');
    const actionLabel = isStart ? 'Start Here' : (step.teleport_cost > 0 ? `Teleport · ${step.teleport_cost.toLocaleString()} gil` : 'Walk / Chocobo');
    const timeLabel = step.travel_time > 0 ? `~${step.travel_time} min travel` : '';

    html += `<div class="route-step">
      <div class="route-step-connector">
        <div class="step-dot${isStart?' start':''}"></div>
        <div class="step-line"></div>
      </div>
      <div class="step-card">
        <div class="step-zone">${step.zone}</div>
        <div class="step-region">${step.region} · ${step.expansion}</div>
        <div class="step-action-row">
          <span class="step-action ${actionClass}">${actionLabel}</span>
          ${timeLabel ? `<span class="step-time">${timeLabel}</span>` : ''}
        </div>
        <div class="step-items">
          ${(step.items||[]).map(it => {
            const coords = (it.x != null && it.y != null) ? `X: ${it.x}, Y: ${it.y}` : '';
            const meta = [it.gtype, (it.level!=null && it.level!=='?') ? `Lv ${it.level}` : '', it.pinpoint]
              .filter(Boolean).join(' · ');
            return `
            <div class="step-item">
              <div class="step-item-head">
                ${it.icon ? `<img src="${it.icon}" alt="">` : ''}
                <span class="step-item-name">${it.name}</span>
                <span class="step-item-qty">×${it.qty}</span>
              </div>
              ${meta ? `<div class="step-item-meta">${meta}</div>` : ''}
              ${coords ? `<div class="step-item-coords">${coords}</div>` : ''}
            </div>`;}).join('')}
        </div>
      </div>
    </div>`;
  });

  html += '</div>';
  return html;
}
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(debug=False, port=5000)