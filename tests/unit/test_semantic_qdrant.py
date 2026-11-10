"""The Qdrant semantic backend, and the dispatch that reaches it (FR-261, FR-282).

The backend is additive: it is reached only when ``DREAMJOB_SEMANTIC_BACKEND``
is ``qdrant``, and the default SQLite path is left exactly as it was (its own
behaviour is pinned in ``test_semantic.py`` / ``test_semantic_consumed.py``).

Everything here runs offline and without the ``qdrant-client`` package: a fake
``qdrant_client`` module is installed into ``sys.modules``, with an in-memory
client whose ``query_points`` ranks by the same ``semantic.cosine`` the SQLite
path uses, so the test can check a similarity means the same thing in both
stores.  A stub embedder keeps the texts deterministic ('data' points one way,
everything else the other).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from dreamjob.config import get_settings
from dreamjob.db import connection as conn_mod
from dreamjob.db.connection import insert_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.pipeline import semantic, semantic_qdrant

# ---------------------------------------------------------------------------
# A fake qdrant_client: an in-memory server shared across client instances
# ---------------------------------------------------------------------------


class _FakePoint:
    def __init__(self, id, vector, payload, score=None):
        self.id = id
        self.vector = vector
        self.payload = payload
        self.score = score


class _FakeResponse:
    def __init__(self, points):
        self.points = points


def _passes(query_filter, payload: dict | None) -> bool:
    payload = payload or {}
    for cond in getattr(query_filter, "must", []):
        value = payload.get(cond.key)
        match = cond.match
        if hasattr(match, "value"):  # MatchValue
            if value != match.value:
                return False
        elif hasattr(match, "any"):  # MatchAny
            if value not in match.any:
                return False
    return True


class _FakeQdrantClient:
    """An in-memory stand-in for QdrantClient.

    Storage is keyed by URL on the class, so a client built for a query sees the
    points a client built for the backfill wrote - the persistence a real server
    gives, which ``semantic_qdrant`` relies on by building a fresh client per
    call.
    """

    _servers: dict[str, dict] = {}

    def __init__(self, url=None, api_key=None):
        self.url = url
        self.api_key = api_key
        self.collections = _FakeQdrantClient._servers.setdefault(url, {})

    @classmethod
    def reset(cls):
        cls._servers.clear()

    def collection_exists(self, collection_name):
        return collection_name in self.collections

    def create_collection(self, collection_name, vectors_config):
        self.collections[collection_name] = {
            "size": vectors_config.size,
            "points": {},
            "payload_schema": {},
            "index_calls": [],
        }

    def get_collection(self, collection_name):
        return types.SimpleNamespace(
            payload_schema=dict(self.collections[collection_name]["payload_schema"])
        )

    def create_payload_index(self, collection_name, field_name, field_schema):
        collection = self.collections[collection_name]
        collection["payload_schema"][field_name] = field_schema
        collection["index_calls"].append(field_name)

    def upsert(self, collection_name, points):
        store = self.collections[collection_name]["points"]
        for p in points:
            store[p.id] = _FakePoint(p.id, list(p.vector), dict(p.payload))

    def retrieve(self, collection_name, ids, with_payload=True, with_vectors=False):
        store = self.collections.get(collection_name, {}).get("points", {})
        out = []
        for i in ids:
            p = store.get(i)
            if p is None:
                continue
            out.append(
                _FakePoint(
                    p.id,
                    list(p.vector) if with_vectors else None,
                    dict(p.payload) if with_payload else None,
                )
            )
        return out

    def query_points(self, collection_name, query, query_filter=None, limit=10, with_payload=True):
        store = self.collections.get(collection_name, {}).get("points", {})
        scored = [
            _FakePoint(
                p.id,
                None,
                dict(p.payload) if with_payload else None,
                score=semantic.cosine(list(query), p.vector),
            )
            for p in store.values()
            if query_filter is None or _passes(query_filter, p.payload)
        ]
        scored.sort(key=lambda pt: pt.score, reverse=True)
        return _FakeResponse(scored[:limit])


def _fake_models() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        VectorParams=lambda size, distance: types.SimpleNamespace(size=size, distance=distance),
        Distance=types.SimpleNamespace(COSINE="Cosine"),
        PayloadSchemaType=types.SimpleNamespace(KEYWORD="keyword", INTEGER="integer"),
        PointStruct=lambda id, vector, payload: types.SimpleNamespace(
            id=id, vector=vector, payload=payload
        ),
        Filter=lambda must: types.SimpleNamespace(must=must),
        FieldCondition=lambda key, match: types.SimpleNamespace(key=key, match=match),
        MatchValue=lambda value: types.SimpleNamespace(value=value),
        MatchAny=lambda any: types.SimpleNamespace(any=any),
    )


class _Stub:
    """Deterministic two-axis embedder: 'data' vs 'sales'."""

    class settings:
        embeddings_model = "stub-model"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "data" in t.lower() else [0.0, 1.0] for t in texts]


class _NoModel:
    class settings:
        embeddings_model = ""

    def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover - never called
        raise AssertionError("embed must not be called without a model")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_qdrant(monkeypatch: pytest.MonkeyPatch):
    """Install a fake ``qdrant_client`` module and return the live server map."""
    _FakeQdrantClient.reset()
    module = types.ModuleType("qdrant_client")
    module.QdrantClient = _FakeQdrantClient
    module.models = _fake_models()
    monkeypatch.setitem(sys.modules, "qdrant_client", module)
    monkeypatch.setitem(sys.modules, "qdrant_client.models", module.models)
    yield _FakeQdrantClient
    _FakeQdrantClient.reset()


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "semantic.db"
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(path))
    monkeypatch.setenv("DREAMJOB_SEMANTIC_BACKEND", "qdrant")
    get_settings.cache_clear()
    migrate(path)
    real = conn_mod.get_connection
    monkeypatch.setattr(conn_mod, "get_connection", lambda db_path=None: real(path))
    yield path
    get_settings.cache_clear()


def _vacancy(title: str, description: str) -> str:
    return insert_row(
        "vacancy",
        {
            "title": title,
            "description": description,
            "source_adapter": "ats.greenhouse",
            "collected_at": utcnow(),
        },
    )


# ---------------------------------------------------------------------------
# The backend itself
# ---------------------------------------------------------------------------


def test_backfill_indexes_and_skips_unchanged_text(db, fake_qdrant):
    _vacancy("Data Engineer", "Build data pipelines")
    _vacancy("Account Executive", "Sell the product")

    first = semantic.backfill("vacancy", llm=_Stub())
    assert first["indexed"] == 2
    assert first["considered"] == 2

    # The collection was created with the real runtime dimension (2), COSINE.
    client = _FakeQdrantClient(url=get_settings().qdrant_url)
    name = f"{get_settings().qdrant_collection_prefix}_vacancy"
    assert client.collections[name]["size"] == 2

    # Second pass embeds nothing: the content hash is already stored.
    assert semantic.backfill("vacancy", llm=_Stub())["indexed"] == 0


def test_backfill_creates_the_filter_payload_indexes_once(db, fake_qdrant):
    # similarities() filters on entity_type and entity_id: both need a KEYWORD
    # payload index so Qdrant can filter inside the HNSW search.
    _vacancy("Data Engineer", "Build data pipelines")
    semantic.backfill("vacancy", llm=_Stub())

    client = _FakeQdrantClient(url=get_settings().qdrant_url)
    name = f"{get_settings().qdrant_collection_prefix}_vacancy"
    collection = client.collections[name]
    assert collection["payload_schema"] == {"entity_type": "keyword", "entity_id": "keyword"}

    # Idempotent: a second backfill with new text does not recreate them.
    _vacancy("Account Executive", "Sell the product")
    assert semantic.backfill("vacancy", llm=_Stub())["indexed"] == 1
    assert sorted(collection["index_calls"]) == ["entity_id", "entity_type"]


def test_a_collection_without_indexes_gets_them_on_the_next_backfill(db, fake_qdrant):
    # A collection created before the indexes existed: only the missing one is added.
    _vacancy("Data Engineer", "Build data pipelines")
    client = _FakeQdrantClient(url=get_settings().qdrant_url)
    name = f"{get_settings().qdrant_collection_prefix}_vacancy"
    client.create_collection(name, types.SimpleNamespace(size=2))
    client.collections[name]["payload_schema"]["entity_type"] = "keyword"

    semantic.backfill("vacancy", llm=_Stub())

    assert client.collections[name]["index_calls"] == ["entity_id"]
    assert set(client.collections[name]["payload_schema"]) == {"entity_type", "entity_id"}


def test_an_unchanged_collection_still_gets_its_missing_index(db, fake_qdrant):
    # Every point is already up to date, so nothing is embedded. The missing
    # entity_id index must still be created.
    _vacancy("Data Engineer", "Build data pipelines")
    assert semantic.backfill("vacancy", llm=_Stub())["indexed"] == 1
    client = _FakeQdrantClient(url=get_settings().qdrant_url)
    name = f"{get_settings().qdrant_collection_prefix}_vacancy"
    collection = client.collections[name]
    del collection["payload_schema"]["entity_id"]
    collection["index_calls"].clear()

    assert semantic.backfill("vacancy", llm=_Stub())["indexed"] == 0

    assert collection["index_calls"] == ["entity_id"]


def test_an_index_failure_does_not_stop_the_backfill(db, fake_qdrant, monkeypatch):
    def _boom(self, **kwargs):
        raise RuntimeError("index refused")

    monkeypatch.setattr(_FakeQdrantClient, "create_payload_index", _boom)
    _vacancy("Data Engineer", "Build data pipelines")
    assert semantic.backfill("vacancy", llm=_Stub())["indexed"] == 1


class _OtherModel(_Stub):
    """The same texts embedded by a different model (same dimension)."""

    class settings:
        embeddings_model = "other-model"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 1.0] if "data" in t.lower() else [1.0, 0.0] for t in texts]


def test_changing_the_model_re_embeds_everything(db, fake_qdrant):
    # The SQLite path keys its content-hash skip by (entity_type, model), so a
    # model switch re-embeds every row. The Qdrant path must do the same, or a
    # switch leaves the old model's vectors in place and stored_vector() for the
    # new model answers None for every row.
    data_id = _vacancy("Data Engineer", "Build data pipelines")
    assert semantic.backfill("vacancy", llm=_Stub())["indexed"] == 1

    assert semantic.backfill("vacancy", llm=_OtherModel())["indexed"] == 1
    assert semantic.stored_vector("vacancy", data_id, "other-model") == [0.0, 1.0]
    assert semantic.stored_vector("vacancy", data_id, "stub-model") is None


def test_similarities_restricts_to_candidates_and_keeps_cosine_meaning(db, fake_qdrant):
    data_id = _vacancy("Data Engineer", "Build data pipelines")
    sales_id = _vacancy("Account Executive", "Sell the product")
    other_id = _vacancy("Data Scientist", "Data models")
    semantic.backfill("vacancy", llm=_Stub())

    # Only the two named candidates are searchable; the third is never scored.
    sims = semantic.similarities("data work", [data_id, sales_id], llm=_Stub())

    assert set(sims) == {data_id, sales_id}
    assert other_id not in sims
    # Cosine keeps its meaning: the data advert is identical direction (1.0),
    # the sales advert orthogonal (0.0) - the same numbers the SQLite path gives.
    assert sims[data_id] == pytest.approx(1.0)
    assert sims[sales_id] == pytest.approx(0.0)


def test_similarities_is_inert_without_a_model(db, fake_qdrant):
    _vacancy("Data Engineer", "Build data pipelines")
    assert semantic.backfill("vacancy", llm=_NoModel())["indexed"] == 0
    assert semantic.similarities("data", [_vacancy("X", "y")], llm=_NoModel()) == {}


def test_similarities_on_an_empty_index_returns_empty_not_an_error(db, fake_qdrant):
    data_id = _vacancy("Data Engineer", "Build data pipelines")
    # Nothing was backfilled, so the collection does not exist yet.
    assert semantic.similarities("data", [data_id], llm=_Stub()) == {}


def test_a_query_failure_is_not_an_error(db, fake_qdrant, monkeypatch):
    data_id = _vacancy("Data Engineer", "Build data pipelines")
    semantic.backfill("vacancy", llm=_Stub())

    def _boom(self, collection_name, **kwargs):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(_FakeQdrantClient, "query_points", _boom)
    assert semantic.similarities("data", [data_id], llm=_Stub()) == {}


def test_stored_vector_round_trips_through_qdrant(db, fake_qdrant):
    data_id = _vacancy("Data Engineer", "Build data pipelines")
    semantic.backfill("vacancy", llm=_Stub())

    vector = semantic.stored_vector("vacancy", data_id, "stub-model")
    assert vector == [1.0, 0.0]
    # A different model name must not match the stored vector.
    assert semantic.stored_vector("vacancy", data_id, "other-model") is None


# ---------------------------------------------------------------------------
# Dispatch and the optional dependency
# ---------------------------------------------------------------------------


def test_default_backend_never_touches_qdrant(tmp_path, monkeypatch):
    # With the default sqlite backend, the dispatch must not reach this module:
    # the calls below go through semantic.* and would hit _fail if it did.
    path = tmp_path / "semantic.db"
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(path))
    monkeypatch.delenv("DREAMJOB_SEMANTIC_BACKEND", raising=False)
    get_settings.cache_clear()
    migrate(path)
    real = conn_mod.get_connection
    monkeypatch.setattr(conn_mod, "get_connection", lambda db_path=None: real(path))

    def _fail(*args, **kwargs):
        raise AssertionError("the sqlite backend must not call semantic_qdrant")

    monkeypatch.setattr(semantic_qdrant, "similarities", _fail)
    monkeypatch.setattr(semantic_qdrant, "backfill", _fail)
    monkeypatch.setattr(semantic_qdrant, "stored_vector", _fail)
    assert semantic._use_qdrant() is False

    data_id = _vacancy("Data Engineer", "Build data pipelines")
    assert semantic.backfill("vacancy", llm=_Stub())["indexed"] == 1
    assert semantic.similarities("data", [data_id], llm=_Stub())[data_id] == pytest.approx(1.0)
    assert semantic.stored_vector("vacancy", data_id, "stub-model") == [1.0, 0.0]
    get_settings.cache_clear()


def test_missing_qdrant_client_raises_a_clear_install_hint(monkeypatch):
    # No fake installed and the package is not in the venv: the hint fires.
    monkeypatch.delitem(sys.modules, "qdrant_client", raising=False)
    with pytest.raises(RuntimeError, match="qdrant-client"):
        semantic_qdrant._require_qdrant()


def test_the_api_key_reaches_the_client_only_when_set(fake_qdrant, monkeypatch):
    module = sys.modules["qdrant_client"]
    monkeypatch.setenv("DREAMJOB_QDRANT_API_KEY", "secret-key")
    get_settings.cache_clear()
    assert semantic_qdrant._client(module, get_settings()).api_key == "secret-key"

    monkeypatch.setenv("DREAMJOB_QDRANT_API_KEY", "")
    get_settings.cache_clear()
    assert semantic_qdrant._client(module, get_settings()).api_key is None
    get_settings.cache_clear()
