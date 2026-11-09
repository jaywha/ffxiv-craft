"""XIVAPI layer robustness: retry/backoff, caching, and the breakdown path's
refusal to turn a transient outage into a silently-wrong material tree.

These tests drive the REAL xiv_get (unlike the rest of the suite, which mocks
it wholesale) by faking SESSION.get, so they exercise the retry/cache logic
itself. time.sleep is stubbed so backoff never actually waits.
"""
import pytest
import requests

import app as app_module
from app import XIVUnavailable, xiv_get


class FakeResp:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {"ok": True}
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


@pytest.fixture(autouse=True)
def fresh_cache_and_no_sleep(monkeypatch):
    """Each test starts with an empty response cache and an instant backoff."""
    app_module._xiv_cache_clear()
    slept = []
    monkeypatch.setattr(app_module.time, "sleep", lambda s: slept.append(s))
    yield slept
    app_module._xiv_cache_clear()


def _fake_session(monkeypatch, responses):
    """Install a SESSION.get that returns/raises from `responses` in order.
    A FakeResp is returned; an Exception instance is raised. Records call count
    on the returned list object via `.calls`."""
    seq = list(responses)
    state = {"i": 0}

    def fake_get(url, params=None, timeout=None):
        idx = min(state["i"], len(seq) - 1)
        state["i"] += 1
        item = seq[idx]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(app_module.SESSION, "get", fake_get)
    return state


# ---------------------------------------------------------------------------
# retry / backoff
# ---------------------------------------------------------------------------

def test_retries_transient_5xx_then_succeeds(monkeypatch):
    state = _fake_session(monkeypatch, [
        FakeResp(503),
        FakeResp(500),
        FakeResp(200, {"value": 42}),
    ])
    assert xiv_get("/sheet/Item/1") == {"value": 42}
    assert state["i"] == 3  # two failures + one success


def test_retries_connection_error_then_succeeds(monkeypatch):
    state = _fake_session(monkeypatch, [
        requests.ConnectionError("reset"),
        requests.Timeout("slow"),
        FakeResp(200, {"value": 7}),
    ])
    assert xiv_get("/sheet/Item/2") == {"value": 7}
    assert state["i"] == 3


def test_exhausted_retries_raise_xivunavailable(monkeypatch):
    _fake_session(monkeypatch, [FakeResp(503)])  # always 503
    with pytest.raises(XIVUnavailable):
        xiv_get("/sheet/Item/3")


def test_4xx_raises_immediately_without_retry(monkeypatch):
    # The documented +Item[]= 400 and missing rows are deterministic: never retry.
    state = _fake_session(monkeypatch, [FakeResp(400)])
    with pytest.raises(requests.HTTPError):
        xiv_get("/search", params={"query": "+Item[]=1"})
    assert state["i"] == 1  # exactly one attempt


def test_429_honors_retry_after_header(monkeypatch, fresh_cache_and_no_sleep):
    slept = fresh_cache_and_no_sleep
    _fake_session(monkeypatch, [
        FakeResp(429, headers={"Retry-After": "2"}),
        FakeResp(200, {"value": 1}),
    ])
    assert xiv_get("/sheet/Item/4") == {"value": 1}
    assert slept and slept[0] == 2.0  # used the header, not exponential backoff


def test_retry_after_is_clamped(monkeypatch, fresh_cache_and_no_sleep):
    slept = fresh_cache_and_no_sleep
    _fake_session(monkeypatch, [
        FakeResp(429, headers={"Retry-After": "9999"}),
        FakeResp(200, {"value": 1}),
    ])
    xiv_get("/sheet/Item/5")
    assert slept[0] == 10.0  # clamped to the 10s ceiling


# ---------------------------------------------------------------------------
# caching
# ---------------------------------------------------------------------------

def test_successful_response_is_cached(monkeypatch):
    state = _fake_session(monkeypatch, [FakeResp(200, {"value": "cached"})])
    a = xiv_get("/sheet/Item/10", params={"fields": "Name"})
    b = xiv_get("/sheet/Item/10", params={"fields": "Name"})
    assert a == b == {"value": "cached"}
    assert state["i"] == 1  # second call served from cache, no network


def test_cache_key_distinguishes_params(monkeypatch):
    state = _fake_session(monkeypatch, [
        FakeResp(200, {"value": "a"}),
        FakeResp(200, {"value": "b"}),
    ])
    first = xiv_get("/sheet/Item/11", params={"fields": "Name"})
    second = xiv_get("/sheet/Item/11", params={"fields": "Icon"})
    assert first == {"value": "a"}
    assert second == {"value": "b"}
    assert state["i"] == 2  # different params -> distinct cache entries


def test_cache_can_be_disabled(monkeypatch):
    monkeypatch.setattr(app_module, "XIV_CACHE_ENABLED", False)
    state = _fake_session(monkeypatch, [
        FakeResp(200, {"value": 1}),
        FakeResp(200, {"value": 2}),
    ])
    xiv_get("/sheet/Item/12")
    xiv_get("/sheet/Item/12")
    assert state["i"] == 2  # no caching -> both hit the network


# ---------------------------------------------------------------------------
# breakdown correctness under a partial outage
# ---------------------------------------------------------------------------

def _fake_xiv_for_partial_outage():
    """Root item 100 is craftable from child 101; the child's recipe lookup
    hits an outage. Everything else resolves normally."""
    def _icon():
        return {"path_hr1": "icon/1.png", "path": "icon/1.png"}

    def fake_xiv_get(path, params=None, timeout=None):
        params = params or {}
        if path == "/search" and params.get("sheets") == "Recipe":
            q = params.get("query")
            if q == "+ItemResult=100":
                return {"results": [{"row_id": 1}]}
            if q == "+ItemResult=101":
                raise XIVUnavailable("child recipe lookup failed")
            return {"results": []}
        if path == "/sheet/Recipe/1":
            return {"fields": {
                "ItemResult": {"fields": {"Name": "Iron Ingot"}},
                "AmountResult": 1,
                "CraftType": {"fields": {"Name": "Blacksmith"}},
                "RecipeLevelTable": {"fields": {"ClassJobLevel": 10}},
                "Ingredient": [
                    {"value": 101, "fields": {
                        "Name": "Iron Ore", "Icon": _icon(),
                        "ItemUICategory": {"fields": {"Name": "Ore"}}}},
                ],
                "AmountIngredient": [2],
            }}
        if path == "/sheet/Item/100":
            return {"fields": {"Name": "Iron Ingot", "Icon": _icon(),
                               "ItemUICategory": {"fields": {"Name": "Metal"}}}}
        raise AssertionError(f"unexpected call: {path} {params}")

    return fake_xiv_get


def test_breakdown_flags_partial_instead_of_silently_wrong(client, monkeypatch):
    monkeypatch.setattr(app_module, "xiv_get", _fake_xiv_for_partial_outage())
    resp = client.post("/api/breakdown_multi",
                       json={"targets": [{"item_id": 100, "quantity": 1}]})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["partial"] is True
    assert any(w["item_id"] == 101 for w in data["warnings"])
    # The child must NOT have silently become a raw material to gather/buy.
    assert "101" not in data["raw_materials"] and 101 not in data["raw_materials"]
    # ...and it is marked degraded in the tree.
    child = data["targets"][0]["tree"]["children"][0]
    assert child["degraded"] is True and child["source"] == "unknown"


def test_breakdown_returns_503_when_xivapi_fully_unreachable(client, monkeypatch):
    def always_down(path, params=None, timeout=None):
        raise XIVUnavailable("down")
    monkeypatch.setattr(app_module, "xiv_get", always_down)
    resp = client.post("/api/breakdown_multi",
                       json={"targets": [{"item_id": 100, "quantity": 1}]})
    assert resp.status_code == 503
    assert "error" in resp.get_json()


def test_single_breakdown_returns_503_when_unreachable(client, monkeypatch):
    def always_down(path, params=None, timeout=None):
        raise XIVUnavailable("down")
    monkeypatch.setattr(app_module, "xiv_get", always_down)
    resp = client.post("/api/breakdown", json={"item_id": 100, "quantity": 1})
    assert resp.status_code == 503
