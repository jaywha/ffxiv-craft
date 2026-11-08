"""Persistence layer: SQLite datastore + history/saved-route endpoints.

Every test runs against a throwaway DB via the `temp_db` fixture. No XIVAPI
calls happen here except the /api/search history test, which mocks xiv_get.
"""

import app as app_module
from app import (
    delete_saved_route,
    get_current_user_id,
    get_or_create_user,
    get_saved_route,
    list_saved_routes,
    recent_searches,
    record_search,
    save_route,
)


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

def test_get_or_create_user_is_idempotent(temp_db):
    uid1 = get_or_create_user("local")
    uid2 = get_or_create_user("local")
    assert uid1 == uid2


def test_distinct_external_ids_get_distinct_users(temp_db):
    a = get_or_create_user("google:abc")
    b = get_or_create_user("google:xyz")
    assert a != b


def test_current_user_is_the_local_placeholder(temp_db):
    assert get_current_user_id() == get_or_create_user("local")


# ---------------------------------------------------------------------------
# search history (data layer)
# ---------------------------------------------------------------------------

def test_record_and_read_recent_searches_newest_first(temp_db):
    uid = get_current_user_id()
    record_search(uid, query="iron")
    record_search(uid, item_id=101, item_name="Iron Ore")
    rows = recent_searches(uid)
    assert len(rows) == 2
    # newest first
    assert rows[0]["item_name"] == "Iron Ore"
    assert rows[0]["item_id"] == 101
    assert rows[1]["query"] == "iron"


def test_recent_searches_respects_limit(temp_db):
    uid = get_current_user_id()
    for i in range(5):
        record_search(uid, query=f"q{i}")
    assert len(recent_searches(uid, limit=3)) == 3


def test_history_is_scoped_per_user(temp_db):
    u1 = get_or_create_user("user-1")
    u2 = get_or_create_user("user-2")
    record_search(u1, query="only-u1")
    assert [r["query"] for r in recent_searches(u1)] == ["only-u1"]
    assert recent_searches(u2) == []


# ---------------------------------------------------------------------------
# saved routes (data layer)
# ---------------------------------------------------------------------------

def test_save_list_get_delete_route_roundtrip(temp_db):
    uid = get_current_user_id()
    targets = [{"id": 100, "name": "Iron Ingot", "qty": 3}]
    route = {"min_cost_route": {"path": ["Western Thanalan"]}, "zone_count": 1}

    rid = save_route(uid, "My Route", targets, route)

    listed = list_saved_routes(uid)
    assert len(listed) == 1
    assert listed[0]["id"] == rid
    assert listed[0]["name"] == "My Route"
    assert listed[0]["targets"] == targets  # parsed back from JSON

    full = get_saved_route(uid, rid)
    assert full["targets"] == targets
    assert full["route"] == route  # full payload preserved

    assert delete_saved_route(uid, rid) is True
    assert list_saved_routes(uid) == []
    assert get_saved_route(uid, rid) is None


def test_save_route_allows_null_route_payload(temp_db):
    uid = get_current_user_id()
    rid = save_route(uid, None, [{"id": 1, "name": "X", "qty": 1}], None)
    full = get_saved_route(uid, rid)
    assert full["route"] is None
    assert full["name"] is None


def test_routes_are_scoped_per_user(temp_db):
    u1 = get_or_create_user("user-1")
    u2 = get_or_create_user("user-2")
    rid = save_route(u1, "u1 route", [{"id": 1, "name": "X", "qty": 1}], None)
    # u2 cannot see or delete u1's route
    assert list_saved_routes(u2) == []
    assert get_saved_route(u2, rid) is None
    assert delete_saved_route(u2, rid) is False
    # u1 still has it
    assert len(list_saved_routes(u1)) == 1


# ---------------------------------------------------------------------------
# /api/history endpoint
# ---------------------------------------------------------------------------

def test_api_history_post_then_get(client, temp_db):
    r = client.post("/api/history", json={"item_id": 101, "item_name": "Iron Ore"})
    assert r.status_code == 201
    assert "id" in r.get_json()

    rows = client.get("/api/history").get_json()
    assert rows[0]["item_name"] == "Iron Ore"
    assert rows[0]["item_id"] == 101


def test_api_history_post_rejects_empty(client, temp_db):
    r = client.post("/api/history", json={})
    assert r.status_code == 400
    assert client.get("/api/history").get_json() == []


def test_api_history_get_respects_limit(client, temp_db):
    for i in range(4):
        client.post("/api/history", json={"query": f"q{i}"})
    rows = client.get("/api/history?limit=2").get_json()
    assert len(rows) == 2


def test_api_search_records_query_history(client, temp_db, monkeypatch):
    # /api/search should record the query as history (best-effort) after a
    # successful lookup. Mock xiv_get so no live network call happens.
    monkeypatch.setattr(app_module, "search_items_xiv", lambda q: [])
    resp = client.get("/api/search?q=mythril")
    assert resp.status_code == 200
    rows = client.get("/api/history").get_json()
    assert rows[0]["query"] == "mythril"


# ---------------------------------------------------------------------------
# /api/routes endpoints
# ---------------------------------------------------------------------------

def test_api_routes_save_list_load_delete(client, temp_db):
    targets = [{"id": 100, "name": "Iron Ingot", "qty": 3}]
    route = {"zone_count": 1, "min_cost_route": {"path": ["Western Thanalan"]}}

    r = client.post("/api/routes", json={"name": "Route A", "targets": targets, "route": route})
    assert r.status_code == 201
    rid = r.get_json()["id"]

    listed = client.get("/api/routes").get_json()
    assert [x["id"] for x in listed] == [rid]
    assert listed[0]["name"] == "Route A"
    assert listed[0]["targets"] == targets

    full = client.get(f"/api/routes/{rid}").get_json()
    assert full["targets"] == targets
    assert full["route"] == route

    assert client.delete(f"/api/routes/{rid}").status_code == 200
    assert client.get("/api/routes").get_json() == []


def test_api_routes_post_requires_targets(client, temp_db):
    assert client.post("/api/routes", json={"name": "x", "targets": []}).status_code == 400
    assert client.post("/api/routes", json={"name": "x"}).status_code == 400


def test_api_routes_get_missing_is_404(client, temp_db):
    assert client.get("/api/routes/9999").status_code == 404


def test_api_routes_delete_missing_is_404(client, temp_db):
    assert client.delete("/api/routes/9999").status_code == 404


def test_api_routes_listed_newest_first(client, temp_db):
    ids = []
    for name in ("first", "second", "third"):
        rid = client.post("/api/routes", json={
            "name": name, "targets": [{"id": 1, "name": "X", "qty": 1}],
        }).get_json()["id"]
        ids.append(rid)
    listed = client.get("/api/routes").get_json()
    assert [x["name"] for x in listed] == ["third", "second", "first"]
