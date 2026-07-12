from app import classify, parse_recipe


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------

def test_classify_crafted_wins_regardless_of_name_or_category():
    assert classify("Fire Shard", "Crystal", has_recipe=True) == "crafted"


def test_classify_crystal_by_name_keyword():
    assert classify("Fire Shard", "Materia", has_recipe=False) == "crystal"
    assert classify("Ice Crystal", "", has_recipe=False) == "crystal"


def test_classify_gathered_by_category_keyword():
    assert classify("Copper Ore", "Ore", has_recipe=False) == "gathered"
    assert classify("Mythril Ore", "Metal Ore", has_recipe=False) == "gathered"


def test_classify_other_when_no_keyword_matches():
    assert classify("Aldgoat Skin", "Miscellany", has_recipe=False) == "other"


# ---------------------------------------------------------------------------
# parse_recipe()
# ---------------------------------------------------------------------------

def test_parse_recipe_extracts_ingredients_and_metadata():
    raw = {
        "fields": {
            "ItemResult": {"fields": {"Name": "Iron Ingot"}},
            "AmountResult": 3,
            "CraftType": {"fields": {"Name": "Blacksmith"}},
            "RecipeLevelTable": {"fields": {"ClassJobLevel": 10}},
            "Ingredient": [
                {"value": 101, "fields": {"Name": "Iron Ore", "Icon": None,
                                           "ItemUICategory": {"fields": {"Name": "Ore"}}}},
                {"value": 102, "fields": {"Name": "Fire Shard", "Icon": None,
                                           "ItemUICategory": {"fields": {"Name": "Crystal"}}}},
            ],
            "AmountIngredient": [2, 1],
        }
    }

    parsed = parse_recipe(recipe_row=1, data=raw)

    assert parsed["recipe_id"] == 1
    assert parsed["result_name"] == "Iron Ingot"
    assert parsed["result_amount"] == 3
    assert parsed["job"] == "Blacksmith"
    assert parsed["level"] == 10
    assert [i["name"] for i in parsed["ingredients"]] == ["Iron Ore", "Fire Shard"]
    assert [i["amount"] for i in parsed["ingredients"]] == [2, 1]
    assert parsed["ingredients"][0]["ui_category"] == "Ore"


def test_parse_recipe_skips_ingredients_with_zero_amount_or_missing_name():
    raw = {
        "fields": {
            "ItemResult": {"fields": {"Name": "Widget"}},
            "AmountResult": 1,
            "CraftType": {},
            "RecipeLevelTable": {},
            "Ingredient": [
                {"value": 101, "fields": {"Name": "Iron Ore"}},
                {"value": 102, "fields": {"Name": ""}},
                {"value": None, "fields": {"Name": "No Id Item"}},
            ],
            "AmountIngredient": [0, 5, 5],
        }
    }

    parsed = parse_recipe(recipe_row=2, data=raw)

    # first ingredient has amount 0 -> skipped; second has no name -> skipped;
    # third has no id -> skipped. Nothing should survive.
    assert parsed["ingredients"] == []


# ---------------------------------------------------------------------------
# /api/breakdown (full recursive tree build, XIVAPI calls mocked)
# ---------------------------------------------------------------------------

def test_breakdown_builds_full_tree_with_correct_quantities(client, iron_ingot_chain):
    resp = client.post("/api/breakdown", json={"item_id": 100, "quantity": 3})
    assert resp.status_code == 200
    data = resp.get_json()

    tree = data["tree"]
    assert tree["item_id"] == 100
    assert tree["qty_needed"] == 3
    assert tree["source"] == "crafted"
    assert tree["times_to_craft"] == 3  # ceil(3/1)

    children_by_id = {c["item_id"]: c for c in tree["children"]}
    assert children_by_id[101]["qty_needed"] == 6  # 2 per craft * 3 crafts
    assert children_by_id[101]["source"] == "gathered"
    assert children_by_id[101]["is_leaf"] is True
    assert children_by_id[102]["qty_needed"] == 3  # 1 per craft * 3 crafts
    assert children_by_id[102]["source"] == "crystal"

    raw = data["raw_materials"]
    assert raw["101"]["total_needed"] == 6
    assert raw["102"]["total_needed"] == 3

    grouped = data["grouped_materials"]
    assert [m["item_id"] for m in grouped["gathered"]] == [101]
    assert [m["item_id"] for m in grouped["crystal"]] == [102]
    assert grouped["other"] == []


def test_breakdown_have_items_reduces_or_eliminates_need(client, iron_ingot_chain):
    # Already have enough Iron Ore (need 6, have 10) -> leaf, nothing left to gather.
    resp = client.post("/api/breakdown", json={
        "item_id": 100,
        "quantity": 3,
        "have_items": {"101": 10},
    })
    assert resp.status_code == 200
    data = resp.get_json()

    children_by_id = {c["item_id"]: c for c in data["tree"]["children"]}
    ore_node = children_by_id[101]
    assert ore_node["qty_have"] == 10
    assert ore_node["qty_to_craft_or_gather"] == 0
    assert ore_node["is_leaf"] is True

    # fully-satisfied raw materials are not added to raw_materials/grouped_materials
    assert "101" not in data["raw_materials"]
    assert all(m["item_id"] != 101 for m in data["grouped_materials"]["gathered"])
