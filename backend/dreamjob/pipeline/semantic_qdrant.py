"""A Qdrant-backed semantic index (FR-261, FR-282).

The alternative to the pure-Python store in :mod:`semantic`. It is reached only
through the dispatch in that module, and only when ``DREAMJOB_SEMANTIC_BACKEND``
is ``qdrant``. The SQLite path stays the default and isn't touched. ``backfill``,
``similarities`` and ``stored_vector`` are dispatched; ``search`` isn't, and
nothing in the app calls it.

Same contract as the SQLite path: the same embeddings model, the same
content-hash skip keyed by model (a model switch re-embeds everything), and the
same "no model or empty index gives an empty result, not an error".

The collection uses ``Distance.COSINE``, so Qdrant's score is the cosine
similarity in ``-1..1``, the same number :func:`semantic.cosine` gives on the
SQLite path, rounded to 4 places the same way. A similarity means the same thing
under both backends.

``qdrant-client`` is imported lazily, so an installation left on SQLite never
has to install it. The dimension isn't hardcoded: the collection takes the size
of the first vector the model returns, the way the SQLite path stores each row's
``dim``. One collection holds one model's vectors. Switching to a model with a
different dimension needs the collection dropped first; Qdrant refuses the
upsert otherwise.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from dreamjob.config import Settings, get_settings
from dreamjob.db.connection import query_all
from dreamjob.llm.client import LLMClient
from dreamjob.pipeline import semantic

log = logging.getLogger(__name__)

_INSTALL_HINT = (
    "The Qdrant semantic backend needs the 'qdrant-client' package, which is "
    "not installed.  Install it with `pip install qdrant-client`, or set "
    "DREAMJOB_SEMANTIC_BACKEND=sqlite to use the built-in store."
)


def _require_qdrant():
    """Import ``qdrant_client`` lazily, with a clear hint when it is missing.

    Lazy so the package stays optional: it is only needed on the installations
    that opt into this backend, and importing at module load would make every
    installation carry the dependency.
    """
    try:
        import qdrant_client  # noqa: PLC0415 - deliberately lazy, see docstring
        from qdrant_client import models  # noqa: PLC0415
    except ImportError as exc:  # the test runs this for real: the package isn't in the venv
        raise RuntimeError(_INSTALL_HINT) from exc
    return qdrant_client, models


def _client(qdrant_client, settings: Settings):
    """A Qdrant client for the configured URL."""
    return qdrant_client.QdrantClient(url=settings.qdrant_url)


def _collection(settings: Settings, entity_type: str) -> str:
    return f"{settings.qdrant_collection_prefix}_{entity_type}"


def _point_id(entity_id: str) -> str:
    """A stable Qdrant point id for an entity id.

    Qdrant point ids must be an unsigned integer or a UUID.  The repo's ids are
    UUID4 hex without dashes (``connection.new_id``); the canonical UUID form is
    derived from them so upserting the same entity twice overwrites rather than
    duplicates.  Anything that is not a UUID falls back to a deterministic
    UUID5, so the function never raises on an unexpected id shape.
    """
    try:
        return str(uuid.UUID(entity_id))
    except (ValueError, AttributeError):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, str(entity_id)))


def _collection_exists(client, name: str) -> bool:
    try:
        return client.collection_exists(collection_name=name)
    except Exception:  # noqa: BLE001 - treat any probe failure as "not there yet"
        return False


def _ensure_collection(qdrant_models, client, name: str, dim: int) -> None:
    """Create the collection with the real runtime dimension if it is absent.

    COSINE is what makes the query score a cosine similarity, same as the
    SQLite path.
    """
    if _collection_exists(client, name):
        return
    client.create_collection(
        collection_name=name,
        vectors_config=qdrant_models.VectorParams(
            size=dim, distance=qdrant_models.Distance.COSINE
        ),
    )


def _known_hashes(client, name: str, entity_ids: list[str], model: str) -> dict[str, str]:
    """``{entity_id: content_hash}`` already stored by ``model``, for the skip.

    Empty when the collection does not exist yet (the first backfill), so the
    first run embeds everything and later runs skip unchanged text. A hash
    written by another model doesn't count, so a model switch re-embeds every
    row. That is what the SQLite path does with its
    ``WHERE entity_type = ? AND model = ?``.
    """
    if not entity_ids or not _collection_exists(client, name):
        return {}
    known: dict[str, str] = {}
    for start in range(0, len(entity_ids), 500):
        chunk = entity_ids[start : start + 500]
        try:
            points = client.retrieve(
                collection_name=name,
                ids=[_point_id(eid) for eid in chunk],
                with_payload=True,
                with_vectors=False,
            )
        except Exception:  # noqa: BLE001 - a read failure just means "re-embed"
            log.warning("Qdrant retrieve failed; treating ids as unindexed", exc_info=True)
            continue
        for point in points:
            payload = point.payload or {}
            eid = payload.get("entity_id")
            digest = payload.get("content_hash")
            if eid is not None and digest is not None and str(payload.get("model")) == model:
                known[str(eid)] = str(digest)
    return known


def backfill(
    entity_type: str = "vacancy",
    *,
    llm: LLMClient | None = None,
    limit: int | None = None,
    batch: int = 64,
) -> dict[str, Any]:
    """Embed the entities that have no vector, or whose text has changed.

    Mirrors :func:`semantic.backfill`: same text builders, same content-hash
    skip, same return shape, so the dispatch in :mod:`semantic` can call either
    store and the script and tests read one result dict.
    """
    builder = semantic._TEXT_BUILDERS.get(entity_type)
    if builder is None:
        raise ValueError(f"No text builder for entity_type {entity_type!r}")
    client_llm = llm or LLMClient()
    model = (client_llm.settings.embeddings_model or "").strip()
    if not model:
        return {"indexed": 0, "skipped": "no embeddings model configured", "entity_type": entity_type}

    qdrant_client, qdrant_models = _require_qdrant()
    settings = get_settings()
    client = _client(qdrant_client, settings)
    name = _collection(settings, entity_type)

    sql = f"SELECT * FROM {entity_type}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = query_all(sql)

    candidates: list[tuple[str, str, str]] = []
    for row in rows:
        text = builder(row)
        if not text:
            continue
        candidates.append((row["id"], text, semantic._content_hash(text)))

    known = _known_hashes(client, name, [eid for eid, _, _ in candidates], model)
    pending = [c for c in candidates if known.get(c[0]) != c[2]]

    indexed = 0
    for start in range(0, len(pending), batch):
        chunk = pending[start : start + batch]
        vectors = client_llm.embed([text for _, text, _ in chunk])
        if not vectors:
            continue
        _ensure_collection(qdrant_models, client, name, len(vectors[0]))
        points = [
            qdrant_models.PointStruct(
                id=_point_id(entity_id),
                vector=list(vector),
                payload={
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "model": model,
                    "content_hash": digest,
                },
            )
            for (entity_id, _text, digest), vector in zip(chunk, vectors, strict=False)
        ]
        client.upsert(collection_name=name, points=points)
        indexed += len(points)
    return {
        "indexed": indexed,
        "considered": len(rows),
        "model": model,
        "entity_type": entity_type,
    }


def similarities(
    query: str,
    entity_ids: list[str],
    *,
    llm: LLMClient | None = None,
    entity_type: str = "vacancy",
) -> dict[str, float]:
    """Cosine similarity of ``query`` to each named entity that has a vector.

    The Qdrant counterpart of :func:`semantic.similarities`: the candidate ids
    are the only rows searched (a payload filter restricts the query to them),
    so a seeker's query never reaches a vacancy they were not shown.  An id with
    no stored vector is simply absent from the result, never scored zero.  Inert
    (an empty dict) without a model, and on any embedding or query failure.

    The number is Qdrant's COSINE score, the same cosine similarity in ``-1..1``
    the SQLite path returns, rounded to 4 places.
    """
    wanted = [str(i) for i in entity_ids if i]
    if not query.strip() or not wanted:
        return {}
    client_llm = llm or LLMClient()
    model = (client_llm.settings.embeddings_model or "").strip()
    if not model:
        return {}

    qdrant_client, qdrant_models = _require_qdrant()
    settings = get_settings()
    client = _client(qdrant_client, settings)
    name = _collection(settings, entity_type)
    if not _collection_exists(client, name):
        return {}

    try:
        query_vector = client_llm.embed([query])[0]
    except Exception:  # noqa: BLE001 - search is an enhancement, never a failure
        log.warning("Semantic search unavailable", exc_info=True)
        return {}

    wanted_set = set(wanted)
    query_filter = qdrant_models.Filter(
        must=[
            qdrant_models.FieldCondition(
                key="entity_type",
                match=qdrant_models.MatchValue(value=entity_type),
            ),
            qdrant_models.FieldCondition(
                key="entity_id",
                match=qdrant_models.MatchAny(any=wanted),
            ),
        ]
    )
    try:
        response = client.query_points(
            collection_name=name,
            query=query_vector,
            query_filter=query_filter,
            limit=len(wanted),
            with_payload=True,
        )
    except Exception:  # noqa: BLE001 - a store failure degrades to no result, not an error
        log.warning("Qdrant query failed; semantic search unavailable", exc_info=True)
        return {}

    out: dict[str, float] = {}
    for point in response.points:
        eid = (point.payload or {}).get("entity_id")
        if eid is not None and str(eid) in wanted_set:
            out[str(eid)] = round(float(point.score), 4)
    return out


def stored_vector(entity_type: str, entity_id: str, model: str) -> list[float] | None:
    """One entity's stored vector for ``model``, or ``None`` when it has none.

    The Qdrant counterpart of :func:`semantic.stored_vector`, read by the opt-in
    scoring term so the dream-fit sub-score works under either backend. The
    payload ``model`` is checked so a vector embedded with a different model is
    not mistaken for this one. Qdrant stores a COSINE collection's vectors
    normalised, so this returns a unit vector where SQLite returns the raw
    embedding; the only reader feeds it to ``semantic.cosine``, which doesn't
    care about scale.
    """
    qdrant_client, _qdrant_models = _require_qdrant()
    settings = get_settings()
    client = _client(qdrant_client, settings)
    name = _collection(settings, entity_type)
    if not _collection_exists(client, name):
        return None
    try:
        points = client.retrieve(
            collection_name=name,
            ids=[_point_id(entity_id)],
            with_payload=True,
            with_vectors=True,
        )
    except Exception:  # noqa: BLE001 - a read failure means "no usable vector"
        log.warning("Qdrant retrieve failed; no stored vector", exc_info=True)
        return None
    for point in points:
        payload = point.payload or {}
        if str(payload.get("model")) != model:
            continue
        vector = point.vector
        if isinstance(vector, list) and vector:
            return [float(x) for x in vector]
    return None
