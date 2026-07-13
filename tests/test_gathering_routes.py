import math

from app import (
    ZONE_DATA,
    _to_map_coord,
    build_route,
    get_gathering_locations,
    solve_tsp_nearest_neighbor,
    teleport_cost,
    travel_time_minutes,
    zone_distance,
)


# ---------------------------------------------------------------------------
# _to_map_coord (raw world coords -> in-game map coords)
# ---------------------------------------------------------------------------

def test_to_map_coord_default_size_factor():
    # Iron Ore node raw coords -> the familiar Western Thanalan map numbers.
    assert _to_map_coord(300.024) == 27.5
    assert _to_map_coord(-223.742) == 17.0


def test_to_map_coord_respects_size_factor():
    # A larger SizeFactor compresses the map, shifting coords toward center.
    assert _to_map_coord(0, size_factor=100) == 21.5
    assert _to_map_coord(0, size_factor=200) == 11.2


def test_to_map_coord_none_passthrough():
    assert _to_map_coord(None) is None


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


def test_tsp_min_cost_starts_at_most_expensive_zone():
    # Teleport cost is destination-only and the first stop is free, so the
    # cheapest route makes the priciest zone the free start. Western La Noscea
    # (260) is dearer than Eastern (220) and Limsa (0), so it should lead.
    zones = ["Western La Noscea", "Limsa Lominsa", "Eastern La Noscea"]
    path = solve_tsp_nearest_neighbor(zones, teleport_cost)
    assert path[0] == "Western La Noscea"


def test_tsp_min_cost_is_optimal_over_all_starts():
    # Cost = sum of every zone's teleport cost minus the (free) start; the
    # solver must find the global minimum, not just a fixed-start greedy result.
    zones = ["Eastern La Noscea", "Northern Thanalan", "Coerthas Central Highlands"]
    route = build_route({z: [] for z in zones}, teleport_cost, "min cost")
    total = sum(ZONE_DATA[z]["teleport_cost"] for z in zones)
    best = total - max(ZONE_DATA[z]["teleport_cost"] for z in zones)
    assert route["total_cost"] == best  # 480, not the fixed-start 660


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


def test_api_route_consolidates_items_into_a_shared_zone(client, no_network):
    # Item A is in {Central, Western Thanalan}; item B is in {Eastern, Western
    # Thanalan}. Naive "first zone" routing would visit Central + Eastern (2
    # stops); set-cover should recognize Western Thanalan covers both -> 1 stop.
    resp = client.post("/api/route", json={
        "items": [
            {"name": "Ore A", "total_needed": 1,
             "zones": ["Central Thanalan", "Western Thanalan"]},
            {"name": "Ore B", "total_needed": 1,
             "zones": ["Eastern Thanalan", "Western Thanalan"]},
        ]
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["zone_count"] == 1
    assert data["min_cost_route"]["path"] == ["Western Thanalan"]
    # both items land on the single shared stop
    names = {i["name"] for i in data["min_cost_route"]["steps"][0]["items"]}
    assert names == {"Ore A", "Ore B"}


def test_api_route_mixed_known_and_unknown_items(client, no_network):
    resp = client.post("/api/route", json={
        "items": [
            {"name": "Ore A", "total_needed": 1, "zones": ["Limsa Lominsa"]},
            {"name": "Mystery", "total_needed": 1, "zones": ["Nowhere"]},
        ]
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["zone_count"] == 1
    assert [u["name"] for u in data["unknown_items"]] == ["Mystery"]


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


def test_gathering_locations_full_node_detail(iron_ingot_chain):
    # Zone must come from TerritoryType.PlaceName ("Western Thanalan"), NOT the
    # PlaceName landmark ("Horizon's Edge"), and coords come from
    # ExportedGatheringPoint. The two points sharing base 158 dedupe to one.
    locations = get_gathering_locations(101)
    # x/y are converted from raw world coords (300.024, -223.742) to in-game
    # map coords via _to_map_coord with the default SizeFactor of 100.
    assert locations == [
        {
            "zone": "Western Thanalan",
            "pinpoint": "Horizon's Edge",
            "type": "Mining",
            "level": 5,
            "x": 27.5,
            "y": 17.0,
            "radius": 64,
        }
    ]


def test_gathering_locations_zone_is_territory_not_landmark(iron_ingot_chain):
    # Regression guard for the original bug: the PlaceName landmark must never
    # be used as the routable zone (it won't match ZONE_DATA).
    zone = get_gathering_locations(101)[0]["zone"]
    assert zone in ZONE_DATA
    assert zone != "Horizon's Edge"


def test_gathering_locations_missing_coords_degrade_gracefully(monkeypatch):
    import app as app_module

    def fake_xiv_get(path, params=None, timeout=10):
        params = params or {}
        if path == "/search":
            sheets, query = params.get("sheets"), params.get("query", "")
            if sheets == "GatheringItem" and query == "+Item=101":
                return {"results": [{"row_id": 501, "fields": {
                    "GatheringItemLevel": {"fields": {"GatheringItemLevel": 5}}}}]}
            if sheets == "GatheringPoint" and query == "+GatheringPointBase.Item[]=501":
                return {"results": [{"fields": {
                    "PlaceName": {"fields": {"Name": "Horizon's Edge"}},
                    "TerritoryType": {"fields": {"PlaceName": {"fields": {"Name": "Western Thanalan"}}}},
                    "GatheringPointBase": {"value": 158, "fields": {
                        "GatheringType": {"fields": {"Name": "Mining"}}}},
                }}]}
            raise AssertionError(f"Unexpected /search call: {params}")
        if path == "/sheet/ExportedGatheringPoint/158":
            # coord sheet missing for this base -> function must not crash
            raise app_module.requests.HTTPError("404")
        raise AssertionError(f"Unexpected call: {path} {params}")

    monkeypatch.setattr(app_module, "xiv_get", fake_xiv_get)
    loc = get_gathering_locations(101)[0]
    assert loc["zone"] == "Western Thanalan"
    assert loc["x"] is None and loc["y"] is None and loc["radius"] is None


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
