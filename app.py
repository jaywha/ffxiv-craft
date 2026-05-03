from flask import Flask, jsonify, request, Response
import requests, math

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
    """Convert v2 icon dict -> PNG via asset endpoint."""
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

# Recipe fields — IMPORTANT: subfield filtering (Field.Sub or Field[].Sub) silently
# returns nothing for array/certain relationship fields in v2. Must request bare field
# names and parse nested data ourselves. Confirmed from live debug data.
_RCP_FIELDS = "ItemResult,AmountResult,CraftType,RecipeLevelTable,Ingredient,AmountIngredient"

def find_recipe(item_id):
    """Search Recipe sheet for a recipe producing item_id, return parsed recipe or None."""
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

    # ItemResult: {"value": <id>, "fields": {"Name": ..., "Icon": {...}, ...}}
    result_item = f.get("ItemResult") or {}
    ri_f = result_item.get("fields", {}) if isinstance(result_item, dict) else {}

    # CraftType: {"value": N, "fields": {"Name": "Goldsmithing", ...}}
    craft_type = f.get("CraftType") or {}
    ct_f = craft_type.get("fields", {}) if isinstance(craft_type, dict) else {}

    # RecipeLevelTable: {"value": N, "fields": {"ClassJobLevel": 20, ...}}
    lvl_tbl = f.get("RecipeLevelTable") or {}
    lvl_f = lvl_tbl.get("fields", {}) if isinstance(lvl_tbl, dict) else {}

    # AmountIngredient: plain int list e.g. [1, 1, 0, 0, 0, 0, 1, 1]
    raw_amts = f.get("AmountIngredient") or []

    # Ingredient: list of full Item relationship objects
    # Each: {"value": <item_id>, "fields": {"Name": ..., "Icon": {...}, "ItemUICategory": {...}}}
    raw_ings = f.get("Ingredient") or []

    ingredients = []
    for i, ing in enumerate(raw_ings):
        if not isinstance(ing, dict):
            continue
        ing_id   = ing.get("value")
        ing_f    = ing.get("fields", {})
        ing_name = ing_f.get("Name", "")
        # Skip empty/null slots
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
    """Fetch name, icon, category for a single item."""
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
    """Raw XIVAPI search — use to verify item IDs."""
    try:
        return jsonify(xiv_get("/search", params={
            "sheets": "Item", "query": f'Name~"{query}"', "fields": "Name,Icon", "limit": 5
        }))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/debug/breakdown/<int:item_id>")
def api_debug_breakdown(item_id):
    """Show exactly what find_recipe returns for an item, with raw field dump."""
    try:
        # Step 1: raw recipe row
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
    """Raw recipe search for an item_id — shows exactly what comes back."""
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
            # Fetch with NO field filter to see ALL available field names
            raw = xiv_get(f"/sheet/Recipe/{rid}")
            out["all_field_names"] = sorted(raw.get("fields", {}).keys())
            out["recipe_data_raw"] = raw
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/breakdown", methods=["POST"])
def api_breakdown():
    body       = request.get_json()
    item_id    = int(body["item_id"])
    quantity   = int(body.get("quantity", 1))
    have_items = {str(k): int(v) for k, v in body.get("have_items", {}).items()}

    item_cache   = {}
    recipe_cache = {}
    raw_materials = {}

    def fetch_info(iid):
        if iid not in item_cache:
            try:
                item_cache[iid] = get_item_info(iid)
            except Exception:
                item_cache[iid] = {"id": iid, "name": f"Item {iid}", "icon": None, "ui_category": ""}
        return item_cache[iid]

    def fetch_recipe(iid):
        if iid not in recipe_cache:
            try:
                recipe_cache[iid] = find_recipe(iid)
            except Exception:
                recipe_cache[iid] = None
        return recipe_cache[iid]

    def build(iid, qty_needed, depth=0, ing_name=None, ing_icon=None, ing_cat=""):
        have      = have_items.get(str(iid), 0)
        qty_after = max(0, qty_needed - have)

        recipe     = fetch_recipe(iid)
        has_recipe = bool(recipe and recipe.get("ingredients"))

        # Use ingredient-supplied name/icon when available (avoids extra API call)
        if ing_name:
            name, icon, cat = ing_name, ing_icon, ing_cat
        else:
            info = fetch_info(iid)
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

    # Fetch root item info from the Item sheet directly
    root_info = fetch_info(item_id)
    tree = build(item_id, quantity,
                 ing_name=root_info["name"], ing_icon=root_info["icon"],
                 ing_cat=root_info["ui_category"])

    grouped = {"gathered": [], "crystal": [], "other": []}
    for iid, mat in raw_materials.items():
        src = mat["source"] if mat["source"] in grouped else "other"
        grouped[src].append({"item_id": iid, "name": mat["name"], "icon": mat["icon"],
                              "total_needed": mat["total_needed"],
                              "gather_sources": [], "has_mob_drop": src == "other"})
    for src in grouped:
        grouped[src].sort(key=lambda x: x["name"])

    return jsonify({"tree": tree, "raw_materials": raw_materials, "grouped_materials": grouped})


# ---------------------------------------------------------------------------
# Embedded HTML (no templates/ folder needed)
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
.search-results{padding:0 20px 12px;display:flex;flex-direction:column;gap:4px;max-height:220px;overflow-y:auto}
.sri{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:var(--r);cursor:pointer;border:1px solid transparent;transition:background .1s,border-color .1s}
.sri:hover{background:var(--bg3);border-color:var(--border)}
.sri.sel{background:var(--bg3);border-color:var(--gold-dim)}
.iico{width:32px;height:32px;border-radius:4px;background:var(--bg3);border:1px solid var(--border);object-fit:cover;flex-shrink:0}
.iico-ph{width:32px;height:32px;border-radius:4px;background:var(--bg3);border:1px solid var(--border);flex-shrink:0}
.iname{font-size:13px;color:var(--text)}
.iid{font-size:11px;color:var(--text3)}
.have-panel{padding:16px 20px;border-bottom:1px solid var(--border);flex:1;overflow-y:auto}
.have-row{display:flex;align-items:center;gap:8px;margin-bottom:6px;padding:6px 8px;background:var(--bg3);border-radius:var(--r);border:1px solid var(--border)}
.have-row img{width:24px;height:24px;border-radius:3px}
.have-name{flex:1;font-size:12px;color:var(--text2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.have-row input[type=number]{width:60px;padding:4px 8px;font-size:12px;background:var(--bg2)}
.rbtn{background:var(--red-dim);border-color:var(--red);color:var(--red);font-size:10px;padding:4px 8px;font-family:'Lato',sans-serif}
.rbtn:hover{background:var(--red);color:white}
.action-row{padding:16px 20px;border-top:1px solid var(--border);background:var(--bg2)}
.btn-primary{width:100%;padding:10px;font-size:12px;letter-spacing:.12em;background:linear-gradient(135deg,#3d2c0a,#6b4c10);border-color:var(--gold);color:var(--gold2)}
.btn-primary:hover{background:linear-gradient(135deg,var(--gold-dim),#8a6318)}
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
.ahb{background:transparent;border:1px solid var(--border);color:var(--text3);font-size:10px;padding:2px 7px;font-family:'Lato',sans-serif;letter-spacing:0}
.ahb:hover{border-color:var(--gold-dim);color:var(--gold);background:transparent}
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
    <div class="have-panel">
      <div class="panel-label">Items I Already Have</div>
      <div id="hl"><div class="empty">No items added yet.<br>Click "Have Some?" on tree nodes.</div></div>
    </div>
    <div class="action-row">
      <button class="btn-primary" id="bb" disabled>&#9658; Calculate Breakdown</button>
    </div>
  </aside>
  <main class="main" id="ma">
    <div class="welcome" id="ws">
      <h2>Craft Planner</h2>
      <p>Search for an item, select it, mark what you already have, then calculate the full material breakdown.</p>
      <div class="hint">Recipe &amp; gathering data via Garland Tools · Locations included</div>
    </div>
  </main>
</div>
<script>
const S={sel:null,have:{},loaded:false};
const $=id=>document.getElementById(id);
const stbar=$('stbar');

function setStatus(msg,err){
  if(!msg){stbar.style.display='none';return}
  stbar.style.display='flex';
  stbar.innerHTML=err?`<span class="err">${msg}</span>`:`<span class="spin"></span>${msg}`;
}

$('sb').onclick=$('si').onkeydown=function(e){if(e.type==='click'||e.key==='Enter')doSearch()};

async function doSearch(){
  const q=$('si').value.trim();if(!q)return;
  setStatus('Searching...');
  try{
    const r=await fetch('/api/search?q='+encodeURIComponent(q));
    const d=await r.json();
    setStatus('');
    renderResults(Array.isArray(d)?d:[]);
  }catch(e){setStatus('Search failed — is the server running?',true)}
}

function renderResults(items){
  const el=$('sr');el.innerHTML='';
  if(!items.length){el.innerHTML='<div class="empty">No results found.</div>';return}
  items.forEach(item=>{
    const row=document.createElement('div');
    row.className='sri';
    row.innerHTML=`${item.icon?`<img class="iico" src="${item.icon}" alt="">`:'<div class="iico-ph"></div>'}
      <div><div class="iname">${item.name}</div><div class="iid">ID: ${item.id}</div></div>`;
    row.onclick=()=>{
      document.querySelectorAll('.sri').forEach(r=>r.classList.remove('sel'));
      row.classList.add('sel');S.sel=item;$('bb').disabled=false;
    };
    el.appendChild(row);
  });
}

function addHave(item){
  if(S.have[item.id])return;
  S.have[item.id]={...item,qty:0};renderHave();
}

function renderHave(){
  const el=$('hl');
  const items=Object.values(S.have);
  if(!items.length){el.innerHTML='<div class="empty">No items added yet.<br>Click "Have Some?" on tree nodes.</div>';return}
  el.innerHTML='';
  items.forEach(item=>{
    const row=document.createElement('div');row.className='have-row';
    row.innerHTML=`${item.icon?`<img src="${item.icon}" alt="">`:'<div style="width:24px;height:24px;background:var(--bg3);border-radius:3px"></div>'}
      <span class="have-name">${item.name}</span>
      <input type="number" min="0" value="${item.qty}" data-id="${item.id}" class="hqi">
      <button class="rbtn" data-id="${item.id}">✕</button>`;
    el.appendChild(row);
  });
  el.querySelectorAll('.hqi').forEach(i=>i.onchange=e=>{S.have[e.target.dataset.id].qty=parseInt(e.target.value)||0});
  el.querySelectorAll('.rbtn').forEach(b=>b.onclick=e=>{delete S.have[e.target.dataset.id];renderHave()});
}

$('bb').onclick=async function(){
  if(!S.sel)return;
  const qty=parseInt($('qi').value)||1;
  setStatus('Fetching recipe tree...');$('bb').disabled=true;
  const hm={};Object.values(S.have).forEach(it=>{hm[it.id]=it.qty});
  try{
    const r=await fetch('/api/breakdown',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({item_id:S.sel.id,quantity:qty,have_items:hm})});
    const d=await r.json();
    setStatus('');renderBreakdown(d,S.sel.name,qty);
  }catch(e){setStatus('Breakdown failed.',true)}
  $('bb').disabled=false;
};

function renderBreakdown(data,name,qty){
  const ma=$('ma');
  if($('ws'))$('ws').style.display='none';
  if(!window._ck)window._ck={};
  ma.innerHTML=`
    <div>
      <div class="sec-title">Recipe Tree — ${name} ×${qty}</div>
      <div class="tree-card" id="tc"></div>
    </div>
    <div>
      <div class="sec-title">Gathering &amp; Collection Log</div>
      <div class="tabs">
        <div class="tab active" data-tab="cl">Checklist</div>
        <div class="tab" data-tab="tot">All Totals</div>
      </div>
      <div class="tc active" id="tab-cl"></div>
      <div class="tc" id="tab-tot"></div>
    </div>`;
  ma.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
    ma.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
    ma.querySelectorAll('.tc').forEach(x=>x.classList.remove('active'));
    t.classList.add('active');
    ma.querySelector('#tab-'+t.dataset.tab).classList.add('active');
  });
  renderTree(data.tree,$('tc'),0);
  renderChecklist(data.grouped_materials,$('tab-cl'));
  renderTotals(data.raw_materials,$('tab-tot'));
}

function renderTree(node,container,depth){
  const wrap=document.createElement('div');
  const rw=document.createElement('div');rw.style.cssText='display:flex;align-items:stretch';
  for(let i=0;i<depth;i++){const l=document.createElement('div');l.className='tline';rw.appendChild(l)}
  const row=document.createElement('div');row.className='tnr';row.style.flex='1';
  const sat=node.qty_to_craft_or_gather===0;
  const sb=sat?'<span class="badge b-have">✓ Have</span>':`<span class="badge b-${node.source}">${node.source}</span>`;
  const cm=node.job?`<span>Lv${node.level} ${jobName(node.job)}</span><span>×${node.times_to_craft} craft${node.times_to_craft>1?'s':''}</span>`:'';
  row.innerHTML=`${node.icon?`<img class="nico" src="${node.icon}" alt="">`:'<div class="nico-ph"></div>'}
    <div class="ninfo"><div class="nname">${node.name}</div><div class="nmeta">${sb}${cm?'&nbsp;'+cm:''}</div></div>
    <div style="display:flex;align-items:center;gap:6px;flex-shrink:0">
      <span class="qty${sat?' sat':''}">&times;${node.qty_needed}${node.qty_have>0?` (have ${node.qty_have})`:''}</span>
      ${(!sat&&node.is_leaf)?`<button class="ahb" data-id="${node.item_id}" data-name="${node.name}" data-icon="${node.icon||''}">Have Some?</button>`:''}
    </div>`;
  rw.appendChild(row);wrap.appendChild(rw);
  if(node.children&&node.children.length){
    const cc=document.createElement('div');
    node.children.forEach(c=>renderTree(c,cc,depth+1));
    wrap.appendChild(cc);
  }
  container.appendChild(wrap);
  row.querySelectorAll('.ahb').forEach(b=>b.onclick=()=>addHave({id:b.dataset.id,name:b.dataset.name,icon:b.dataset.icon||null,qty:0}));
}

const JOBS={8:'CRP',9:'BSM',10:'ARM',11:'GSM',12:'LTW',13:'WVR',14:'ALC',15:'CUL'};
function jobName(id){return JOBS[id]||`Job ${id}`}

function renderChecklist(grouped,container){
  const order=['gathered','crystal','mob','vendor','other'];
  const labels={gathered:'Gathered',crystal:'Crystals & Shards',mob:'Mob Drops',vendor:'Vendor / Trade',other:'Other'};
  container.innerHTML='';let any=false;
  order.forEach(src=>{
    const items=grouped[src]||[];if(!items.length)return;any=true;
    const sec=document.createElement('div');sec.className='gs';
    sec.innerHTML=`<div class="gsh"><span class="gsl ${src}">${labels[src]}</span><div class="gsline"></div><span class="gsc">${items.length} item${items.length>1?'s':''}</span></div><div class="checklist"></div>`;
    const list=sec.querySelector('.checklist');
    items.forEach(mat=>{
      const card=document.createElement('div');card.className='ci';
      if(window._ck[mat.item_id])card.classList.add('done');
      const hasLoc=(mat.gather_sources&&mat.gather_sources.length)||mat.has_mob_drop;
      card.innerHTML=`
        <div class="cimain">
          <div class="cbox">${window._ck[mat.item_id]?'✓':''}</div>
          ${mat.icon?`<img class="cico" src="${mat.icon}" alt="">`:'<div class="cico-ph"></div>'}
          <div class="ciinfo"><div class="ciname">${mat.name}</div><div class="ciqty">&times;${mat.total_needed}</div></div>
          ${hasLoc?'<button class="expand-btn">&#9662; Where</button>':''}
        </div>
        ${hasLoc?`<div class="cilocs">${buildLocs(mat,src)}</div>`:''}`;
      card.querySelector('.cimain').onclick=e=>{
        if(e.target.classList.contains('expand-btn')){
          card.classList.toggle('exp');
          e.target.innerHTML=card.classList.contains('exp')?'&#9652; Where':'&#9662; Where';
          return;
        }
        card.classList.toggle('done');
        const box=card.querySelector('.cbox');
        window._ck[mat.item_id]=card.classList.contains('done');
        box.textContent=window._ck[mat.item_id]?'✓':'';
      };
      list.appendChild(card);
    });
    container.appendChild(sec);
  });
  if(!any)container.innerHTML='<div class="empty">No raw materials needed — you already have everything!</div>';
}

function buildLocs(mat,src){
  const rows=[];
  if(mat.gather_sources&&mat.gather_sources.length){
    mat.gather_sources.forEach(gs=>{
      const zone=gs.zone||gs.area||'Unknown zone';
      const coords=gs.coords?`<span class="lcoords">${gs.coords}</span>`:'';
      rows.push(`<div class="lrow"><span class="ltype">${gs.type||'Gathering'} Lv${gs.level}</span><span class="ldetail">${zone} ${coords}</span></div>`);
    });
  }
  if(mat.has_mob_drop)rows.push(`<div class="lrow"><span class="ltype mob">Mob Drop</span><span class="ldetail">Search the enemy list in-game or check Garland Tools for spawn locations.</span></div>`);
  if(src==='vendor')rows.push(`<div class="lrow"><span class="ltype vendor">Vendor</span><span class="ldetail">Available from shop NPCs — check Garland Tools for exact vendor locations.</span></div>`);
  return rows.length?rows.join(''):'<div class="noloc">No location data available.</div>';
}

function renderTotals(raw,container){
  container.innerHTML='';
  const items=Object.values(raw).sort((a,b)=>a.name.localeCompare(b.name));
  if(!items.length){container.innerHTML='<div class="empty">No raw materials needed.</div>';return}
  const card=document.createElement('div');card.className='tot-card';
  items.forEach(mat=>{
    const src=mat.source||'other';
    const row=document.createElement('div');row.className='tot-row';
    row.innerHTML=`${mat.icon?`<img src="${mat.icon}" alt="">`:'<div style="width:22px;height:22px;background:var(--bg3);border-radius:3px"></div>'}
      <span class="tot-name">${mat.name}</span>
      <span class="badge b-${src}" style="margin-right:6px">${src}</span>
      <span class="tot-qty">&times;${mat.total_needed}</span>`;
    card.appendChild(row);
  });
  container.appendChild(card);
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(debug=False, port=5000)