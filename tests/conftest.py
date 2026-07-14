import pytest

import app as app_module


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def auth_disabled_by_default(monkeypatch):
    """Run the suite with AUTH_DISABLED on by default so endpoint tests exercise
    the persistence/route logic without an OAuth session (get_current_user_id
    falls back to the 'local' user). Auth-specific tests opt back into real auth
    via the `auth_enabled` fixture, which sets the flag False after this one."""
    monkeypatch.setattr(app_module, "AUTH_DISABLED", True)


@pytest.fixture
def auth_enabled(monkeypatch):
    """Turn real auth back on for a test: get_current_user_id() then reads the
    session and unauthenticated requests get 401. Runs after the autouse
    default, so its False wins."""
    monkeypatch.setattr(app_module, "AUTH_DISABLED", False)
    return monkeypatch


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the app at a throwaway SQLite file for the duration of a test.

    db() reads app.config["DB_PATH"] on every connection and runs
    CREATE TABLE IF NOT EXISTS, so setting the path is all that's needed --
    each test gets a fresh, isolated database with no leftover state.
    """
    db_file = tmp_path / "test.db"
    monkeypatch.setitem(app_module.app.config, "DB_PATH", str(db_file))
    return db_file


@pytest.fixture
def no_network(monkeypatch):
    """Replace xiv_get with a stub that fails loudly on any unmocked call."""

    def _boom(path, params=None, timeout=10):
        raise AssertionError(f"Unmocked xiv_get call: path={path!r} params={params!r}")

    monkeypatch.setattr(app_module, "xiv_get", _boom)
    return monkeypatch


def _icon(path="icon/1.png"):
    return {"path_hr1": path, "path": path}


@pytest.fixture
def iron_ingot_chain(monkeypatch):
    """Mocked XIVAPI responses for a small craft tree:

    Iron Ingot (100, crafted, 1 per craft)
      <- Iron Ore (101, gathered) x2
      <- Fire Shard (102, crystal) x1

    Iron Ore has one known gathering location, resolved via the nested
    GatheringPointBase.Item[] search + ExportedGatheringPoint coord lookup
    (the real chain used by get_gathering_locations). Response shapes mirror
    live XIVAPI v2 captures.
    """

    def fake_xiv_get(path, params=None, timeout=10):
        params = params or {}

        if path == "/search":
            sheets = params.get("sheets")
            query = params.get("query", "")

            if sheets == "Recipe":
                if query == "+ItemResult=100":
                    return {"results": [{"row_id": 1}]}
                return {"results": []}

            if sheets == "GatheringItem":
                if query == "+Item=101":
                    return {
                        "results": [
                            {
                                "row_id": 501,
                                "fields": {
                                    "GatheringItemLevel": {"fields": {"GatheringItemLevel": 5}}
                                },
                            }
                        ]
                    }
                return {"results": []}

            if sheets == "GatheringPoint":
                if query == "+GatheringPointBase.Item[]=501":
                    # Two GatheringPoints sharing one base (id 158) -> the
                    # function should dedupe to a single node.
                    pt = {
                        "fields": {
                            "PlaceName": {"fields": {"Name": "Horizon's Edge"}},
                            "TerritoryType": {
                                "fields": {"PlaceName": {"fields": {"Name": "Western Thanalan"}}}
                            },
                            "GatheringPointBase": {
                                "value": 158,
                                "row_id": 158,
                                "fields": {"GatheringType": {"fields": {"Name": "Mining"}}},
                            },
                        }
                    }
                    return {"results": [pt, pt]}
                return {"results": []}

            raise AssertionError(f"Unexpected /search call: {params}")

        if path == "/sheet/ExportedGatheringPoint/158":
            return {"fields": {"X": 300.024, "Y": -223.742, "Radius": 64}}

        if path == "/sheet/Recipe/1":
            return {
                "fields": {
                    "ItemResult": {"fields": {"Name": "Iron Ingot"}},
                    "AmountResult": 1,
                    "CraftType": {"fields": {"Name": "Blacksmith"}},
                    "RecipeLevelTable": {"fields": {"ClassJobLevel": 10}},
                    "Ingredient": [
                        {
                            "value": 101,
                            "fields": {
                                "Name": "Iron Ore",
                                "Icon": _icon(),
                                "ItemUICategory": {"fields": {"Name": "Ore"}},
                            },
                        },
                        {
                            "value": 102,
                            "fields": {
                                "Name": "Fire Shard",
                                "Icon": _icon(),
                                "ItemUICategory": {"fields": {"Name": "Crystal"}},
                            },
                        },
                    ],
                    "AmountIngredient": [2, 1],
                }
            }

        if path == "/sheet/Item/100":
            return {
                "fields": {
                    "Name": "Iron Ingot",
                    "Icon": _icon(),
                    "ItemUICategory": {"fields": {"Name": "Metal"}},
                }
            }
        if path == "/sheet/Item/101":
            return {
                "fields": {
                    "Name": "Iron Ore",
                    "Icon": _icon(),
                    "ItemUICategory": {"fields": {"Name": "Ore"}},
                }
            }
        if path == "/sheet/Item/102":
            return {
                "fields": {
                    "Name": "Fire Shard",
                    "Icon": _icon(),
                    "ItemUICategory": {"fields": {"Name": "Crystal"}},
                }
            }

        raise AssertionError(f"Unexpected xiv_get call: path={path!r} params={params!r}")

    monkeypatch.setattr(app_module, "xiv_get", fake_xiv_get)
    return fake_xiv_get
