"""Auth layer (Authlib Google OAuth).

The suite defaults to AUTH_DISABLED (see conftest `auth_disabled_by_default`);
these tests use the `auth_enabled` fixture to exercise the real gating. No live
Google calls are made -- the OAuth client is never registered in tests (no
GOOGLE_CLIENT_ID/SECRET), so /login and /auth/callback report "not configured",
and signed-in requests are simulated by stamping the Flask session directly.
"""

from app import get_or_create_user


# ---------------------------------------------------------------------------
# /api/me
# ---------------------------------------------------------------------------

def test_me_reports_local_when_auth_disabled(client, temp_db):
    me = client.get("/api/me").get_json()
    assert me["authenticated"] is True
    assert me.get("auth_disabled") is True


def test_me_anonymous_when_auth_enabled(client, temp_db, auth_enabled):
    me = client.get("/api/me").get_json()
    assert me["authenticated"] is False


def test_me_reports_display_name_when_signed_in(client, temp_db, auth_enabled):
    uid = get_or_create_user("google:abc", display_name="Warrior of Light")
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["display_name"] = "Warrior of Light"
    me = client.get("/api/me").get_json()
    assert me["authenticated"] is True
    assert me["display_name"] == "Warrior of Light"


# ---------------------------------------------------------------------------
# gating: 401 when unauthenticated
# ---------------------------------------------------------------------------

def test_persistence_endpoints_401_when_unauthenticated(client, temp_db, auth_enabled):
    assert client.get("/api/history").status_code == 401
    assert client.post("/api/history", json={"query": "x"}).status_code == 401
    assert client.get("/api/routes").status_code == 401
    assert client.post(
        "/api/routes", json={"targets": [{"id": 1, "name": "X", "qty": 1}]}
    ).status_code == 401
    assert client.get("/api/routes/1").status_code == 401
    assert client.delete("/api/routes/1").status_code == 401


def test_search_still_works_logged_out_but_records_nothing(client, temp_db, auth_enabled, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "search_items_xiv", lambda q: [])
    # search itself succeeds without auth...
    assert client.get("/api/search?q=iron").status_code == 200
    # ...but history stays empty for an anonymous user, and /api/history is gated
    assert client.get("/api/history").status_code == 401


# ---------------------------------------------------------------------------
# signed-in behaviour + per-user isolation
# ---------------------------------------------------------------------------

def test_endpoints_work_for_signed_in_user(client, temp_db, auth_enabled):
    uid = get_or_create_user("google:abc")
    with client.session_transaction() as sess:
        sess["user_id"] = uid
    rid = client.post("/api/routes", json={
        "name": "R", "targets": [{"id": 1, "name": "X", "qty": 1}],
    }).get_json()["id"]
    listed = client.get("/api/routes").get_json()
    assert [x["id"] for x in listed] == [rid]


def test_saved_routes_isolated_between_google_users(client, temp_db, auth_enabled):
    u1 = get_or_create_user("google:one")
    u2 = get_or_create_user("google:two")
    with client.session_transaction() as sess:
        sess["user_id"] = u1
    client.post("/api/routes", json={"name": "u1", "targets": [{"id": 1, "name": "X", "qty": 1}]})
    with client.session_transaction() as sess:
        sess["user_id"] = u2
    assert client.get("/api/routes").get_json() == []


def test_get_or_create_maps_google_sub_stably(temp_db):
    a = get_or_create_user("google:same-sub")
    b = get_or_create_user("google:same-sub")
    assert a == b


# ---------------------------------------------------------------------------
# login / callback / logout
# ---------------------------------------------------------------------------

def test_login_503_when_google_not_configured(client, auth_enabled):
    assert client.get("/login").status_code == 503


def test_callback_503_when_google_not_configured(client, auth_enabled):
    assert client.get("/auth/callback").status_code == 503


def test_logout_clears_session(client, temp_db, auth_enabled):
    uid = get_or_create_user("google:abc")
    with client.session_transaction() as sess:
        sess["user_id"] = uid
    assert client.get("/api/me").get_json()["authenticated"] is True
    assert client.post("/logout").status_code == 200
    assert client.get("/api/me").get_json()["authenticated"] is False
