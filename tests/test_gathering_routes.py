import math

from app import (
    ZONE_DATA,
    build_route,
    get_gathering_locations,
    solve_tsp_nearest_neighbor,
    teleport_cost,
    travel_time_minutes,
    zone_distance,
)


# ---------------------------------------------------------------------------
# Pure zone-graph math (no network involved)
# ---------------------------------------------------------------------------

def test_zone_distance_same_zone_is_zero():
    assert zone_distance("Limsa Lominsa", "Limsa Lominsa") == 0


def test_zone_distance_matches_euclidean_formula():
    d1 = ZONE_DATA["Limsa Lominsa"]
    d2 = ZONE_DATA["Western La Noscea"]
    expected = math.sqrt((d1["x"] - d2["x"]) ** 2 + (d1["y"] - d2["y"]) ** 2)
    assert zone_distance("Limsa Lominsa", "Western La Noscea") == expected


def test_zone_distance_unknown_zone_returns_sentinel():
    assert zone_distance("Nowhere", "Limsa Lominsa") == 999


def test_travel_time_same_zone():
    assert travel_time_minutes("Limsa Lominsa", "Limsa Lominsa") == 2


def test_travel_time_same_region():
    # Limsa Lominsa and Western La Noscea are both region "La Noscea"
    assert travel_time_minutes("Limsa Lominsa", "Western La Noscea") == 5


def test_travel_time_same_expansion_different_region():
    # Limsa Lominsa (La Noscea, ARR) vs Ul'dah (Thanalan, ARR)
    assert travel_time_minutes("Limsa Lominsa", "Ul'dah") == 10


def test_travel_time_cross_expansion():
    # Limsa Lominsa (ARR) vs Ishgard (HW)
    assert travel_time_minutes("Limsa Lominsa", "Ishgard") == 20


def test_teleport_cost_same_zone_is_free():
    assert teleport_cost("Limsa Lominsa", "Limsa Lominsa") == 0


def test_teleport_cost_uses_destination_zone_cost():
    assert teleport_cost("Limsa Lominsa", "Western La Noscea") == \
        ZONE_DATA["Western La Noscea"]["teleport_cost"]


# ---------------------------------------------------------------------------
# solve_tsp_nearest_neighbor
# ---------------------------------------------------------------------------

def test_tsp_empty_input():
    assert solve_tsp_nearest_neighbor([], teleport_cost) == []


def test_tsp_single_zone():
    assert solve_tsp_nearest_neighbor(["Ul'dah"], teleport_cost) == ["Ul'dah"]


def test_tsp_visits_every_zone_exactly_once():
    zones = ["Western La Noscea", "Eastern La Noscea", "Limsa Lominsa", "Upper La Noscea"]
    path = solve_tsp_nearest_neighbor(zones, teleport_cost)
    assert sorted(path) == sorted(zones)
    assert len(path) == len(zones)


def test_tsp_starts_from_cheapest_to_reach_zone():
    # Limsa Lominsa is a city with teleport_cost 0, so it should be the start
    # regardless of input order.
    zones = ["Western La Noscea", "Limsa Lominsa", "Eastern La Noscea"]
    path = solve_tsp_nearest_neighbor(zones, teleport_cost)
    assert path[0] == "Limsa Lominsa"


# ---------------------------------------------------------------------------
# build_route
# ---------------------------------------------------------------------------

def test_build_route_shape_and_first_step():
    zone_items = {
        "Limsa Lominsa": [{"name": "Item A", "qty": 1}],
        "Western La Noscea": [{"name": "Item B", "qty": 2}],
    }
    route = build_route(zone_items, teleport_cost, "Cheapest Route")

    assert route["label"] == "Cheapest Route"
    assert len(route["path"]) == 2
    assert len(route["steps"]) == 2

    first = route["steps"][0]
    assert first["zone"] == route["path"][0]
    assert first["action"] == "Start here"
    assert first["teleport_cost"] == 0
    assert first["travel_time"] == 0
    assert first["items"] == zone_items[first["zone"]]


def test_build_route_totals_sum_step_costs():
    zone_items = {
        "Limsa Lominsa": [{"name": "Item A", "qty": 1}],
        "Western La Noscea": [{"name": "Item B", "qty": 2}],
    }
    route = build_route(zone_items, teleport_cost, "Cheapest Route")
    assert route["total_cost"] == sum(s["teleport_cost"] for s in route["steps"])
    assert route["total_time_min"] == sum(s["travel_time"] for s in route["steps"])


# ---------------------------------------------------------------------------
# /api/route endpoint (pure ZONE_DATA lookups, no XIVAPI calls)
# ---------------------------------------------------------------------------

def test_api_route_exact_zone_match(client, no_network):
    resp = client.post("/api/route", json={
        "items": [
            {"name": "Iron Ore", "total_needed": 6, "zones": ["Limsa Lominsa"]},
        ]
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["zone_count"] == 1
    assert data["unknown_items"] == []
    assert data["min_cost_route"]["path"] == ["Limsa Lominsa"]
    assert data["min_time_route"]["path"] == ["Limsa Lominsa"]


def test_api_route_fuzzy_matches_partial_zone_name(client, no_network):
    # "Thanalan" is a substring of "Western Thanalan" (first Thanalan zone
    # declared in ZONE_DATA), so it should fuzzy-match rather than fall to Unknown.
    resp = client.post("/api/route", json={
        "items": [
            {"name": "Iron Ore", "total_needed": 6, "zones": ["Thanalan"]},
        ]
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["unknown_items"] == []
    assert data["min_cost_route"]["path"] == ["Western Thanalan"]


def test_api_route_unmatched_zone_goes_to_unknown(client, no_network):
    resp = client.post("/api/route", json={
        "items": [
            {"name": "Mystery Mineral", "total_needed": 1, "zones": ["Nowhere Land"]},
        ]
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["min_cost_route"] is None
    assert data["min_time_route"] is None
    assert len(data["unknown_items"]) == 1
    assert data["unknown_items"][0]["name"] == "Mystery Mineral"


def test_api_route_item_with_no_zones_goes_to_unknown(client, no_network):
    resp = client.post("/api/route", json={
        "items": [{"name": "Mystery Mineral", "total_needed": 1}]
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["min_cost_route"] is None
    assert len(data["unknown_items"]) == 1


# ---------------------------------------------------------------------------
# get_gathering_locations — the fragile multi-step XIVAPI lookup chain
# ---------------------------------------------------------------------------

def test_gathering_locations_no_gathering_item_found(monkeypatch):
    import app as app_module

    def fake_xiv_get(path, params=None, timeout=10):
        assert params["sheets"] == "GatheringItem"
        return {"results": []}

    monkeypatch.setattr(app_module, "xiv_get", fake_xiv_get)
    assert get_gathering_locations(999) == []


def test_gathering_locations_happy_path_array_bracket_search(iron_ingot_chain):
    locations = get_gathering_locations(101)
    assert locations == [
        {"zone": "Western Thanalan", "x": None, "y": None, "type": "Mining", "level": 5}
    ]


def test_gathering_locations_falls_back_to_paginated_scan(monkeypatch):
    import app as app_module

    def fake_xiv_get(path, params=None, timeout=10):
        params = params or {}

        if path == "/search":
            sheets = params.get("sheets")
            query = params.get("query", "")

            if sheets == "GatheringItem" and query == "+Item=101":
                return {
                    "results": [
                        {"row_id": 501, "fields": {
                            "GatheringItemLevel": {"fields": {"GatheringItemLevel": 5}}
                        }}
                    ]
                }

            if sheets == "GatheringPoint" and query == "+Item[]=501":
                # array-bracket search finds nothing -> should fall back to scan
                return {"results": []}

            if sheets == "GatheringPoint" and query == "+GatheringPointBase=42":
                return {
                    "results": [
                        {"fields": {
                            "PlaceName": {"fields": {"Name": "East Shroud"}},
                            "TerritoryType": {"fields": {"Name": "East Shroud"}},
                        }}
                    ]
                }

            raise AssertionError(f"Unexpected /search call: {params}")

        if path == "/sheet/GatheringPointBase":
            if params.get("after") not in (None,):
                # only the first page has data in this test
                return {"rows": []}
            return {
                "rows": [
                    {
                        "row_id": 42,
                        "fields": {
                            "GatheringType": {"fields": {"Name": "Logging"}},
                            "Item": [{"value": 501}],
                        },
                    }
                ]
            }

        raise AssertionError(f"Unexpected xiv_get call: path={path!r} params={params!r}")

    monkeypatch.setattr(app_module, "xiv_get", fake_xiv_get)

    locations = get_gathering_locations(101)
    assert locations == [
        {"zone": "East Shroud", "x": None, "y": None, "type": "Logging", "level": 5}
    ]


def test_gathering_locations_swallows_top_level_exception(monkeypatch):
    import app as app_module

    def fake_xiv_get(path, params=None, timeout=10):
        raise ConnectionError("XIVAPI is down")

    monkeypatch.setattr(app_module, "xiv_get", fake_xiv_get)
    assert get_gathering_locations(101) == []


# ---------------------------------------------------------------------------
# /api/lookup_zones
# ---------------------------------------------------------------------------

def test_lookup_zones_fills_zones_for_gathered_items(client, iron_ingot_chain):
    resp = client.post("/api/lookup_zones", json={
        "items": [{"item_id": 101, "name": "Iron Ore", "source": "gathered"}]
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data[0]["zones"] == ["Western Thanalan"]


def test_lookup_zones_skips_non_gathered_sources(client, no_network):
    resp = client.post("/api/lookup_zones", json={
        "items": [{"item_id": 102, "name": "Fire Shard", "source": "crystal"}]
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data[0]["zones"] == []
