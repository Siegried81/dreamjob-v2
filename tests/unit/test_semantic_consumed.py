"""The semantic index, consumed: an API query and an opt-in scoring term (FR-261).

Two promises are pinned here.  The API answers a meaning-based query over the
seeker's own opportunities, and only theirs.  And scoring is unchanged unless
``DREAMJOB_SEMANTIC_SCORING`` is switched on: with the setting off the
embedder is never called and every number is the one the two-part dream fit
already produced.

A stub embedder keeps everything offline: texts mentioning "data" point one
way, everything else the other.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, to_json, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.llm import client as llm_mod

_CALLS: list[list[str]] = []


def _stub_embed(self, texts: list[str]) -> list[list[float]]:
    _CALLS.append(list(texts))
    return [[1.0, 0.0] if "data" in t.lower() else [0.0, 1.0] for t in texts]


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "semantic.db"))
    monkeypatch.setenv("DREAMJOB_EMBEDDINGS_MODEL", "stub-model")
    monkeypatch.delenv("DREAMJOB_SEMANTIC_SCORING", raising=False)
    get_settings.cache_clear()
    get_settings().ensure_dirs()
    migrate()
    monkeypatch.setattr(llm_mod.LLMClient, "embed", _stub_embed)
    _CALLS.clear()

    now = utcnow()

    def seeker(email: str) -> str:
        return insert_row(
            "job_seeker",
            {"email": email, "display_name": email, "locale": "en",
             "created_at": now, "updated_at": now},
        )

    me, other = seeker("me@example.test"), seeker("other@example.test")
    insert_row(
        "consent_record",
        {"job_seeker_id": me, "kind": "llm_transfer", "granted": 1, "granted_at": now},
    )
    profile = insert_row(
        "profile_version",
        {"job_seeker_id": me, "version": 1, "sections": "{}", "created_at": now},
    )
    dream = insert_row(
        "dream_job_model",
        {"job_seeker_id": me, "version": 1,
         "statement": "Build data platforms for a data-driven company",
         "target_roles": to_json([{"title": "Data Engineer", "priority": 1}]),
         "created_at": now},
    )

    def directives(owner: str) -> str:
        return insert_row(
            "directive_set",
            {"job_seeker_id": owner, "name": "d", "created_at": now},
        )

    campaign = insert_row(
        "campaign",
        {"job_seeker_id": me, "directive_set_id": directives(me),
         "profile_version_id": profile, "dream_job_model_id": dream,
         "name": "c", "created_at": now},
    )
    other_campaign = insert_row(
        "campaign",
        {"job_seeker_id": other, "directive_set_id": directives(other),
         "profile_version_id": insert_row(
             "profile_version",
             {"job_seeker_id": other, "version": 1, "sections": "{}", "created_at": now},
         ),
         "name": "o", "created_at": now},
    )

    def opening(owner: str, campaign_id: str, title: str, description: str) -> tuple[str, str]:
        vacancy = insert_row(
            "vacancy",
            {"title": title, "description": description, "source_adapter": "ats.greenhouse",
             "collected_at": now},
        )
        opportunity = insert_row(
            "opportunity",
            {"job_seeker_id": owner, "campaign_id": campaign_id, "vacancy_id": vacancy,
             "kind": "vacancy", "title": title, "description": description,
             "language": "en", "created_at": now, "updated_at": now},
        )
        return vacancy, opportunity

    data_v, data_o = opening(me, campaign, "Data Engineer", "Build data pipelines")
    sales_v, sales_o = opening(me, campaign, "Account Executive", "Sell the product")
    _foreign_v, foreign_o = opening(other, other_campaign, "Data Analyst", "Data dashboards")
    yield {
        "me": me, "other": other, "campaign": campaign,
        "data_vacancy": data_v, "data_opportunity": data_o,
        "sales_vacancy": sales_v, "sales_opportunity": sales_o,
        "foreign_opportunity": foreign_o,
    }
    get_settings.cache_clear()


def _index() -> None:
    from dreamjob.pipeline import semantic

    semantic.backfill("vacancy", llm=llm_mod.LLMClient())
    _CALLS.clear()


@pytest.fixture()
def client(world: dict):
    from dreamjob.api.deps import CurrentSeeker, current_seeker
    from dreamjob.api.routers import opportunities as router_module
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/opportunities")
    seeker = CurrentSeeker(
        id=world["me"], email="me@example.test", display_name="me", is_admin=False, locale="en"
    )
    app.dependency_overrides[current_seeker] = lambda: seeker
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


def test_semantic_endpoint_ranks_the_seekers_own_rows_by_meaning(client, world):
    _index()

    body = client.get("/api/opportunities/semantic", params={"q": "data work"}).json()

    ids = [item["id"] for item in body["items"]]
    assert body["available"] is True
    assert ids[0] == world["data_opportunity"]
    assert body["items"][0]["similarity"] == pytest.approx(1.0)
    assert world["foreign_opportunity"] not in ids, "another seeker's row is never a candidate"
    assert body["candidates"] == 2
    assert body["indexed"] == 2


def test_semantic_endpoint_without_a_model_is_empty_not_an_error(client, world, monkeypatch):
    monkeypatch.setenv("DREAMJOB_EMBEDDINGS_MODEL", "")
    get_settings.cache_clear()

    response = client.get("/api/opportunities/semantic", params={"q": "data"})

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["items"] == []
    assert _CALLS == []


def test_semantic_endpoint_says_when_the_index_is_empty(client, world):
    body = client.get("/api/opportunities/semantic", params={"q": "data"}).json()

    assert body["items"] == []
    assert "index" in body["note"]


def test_semantic_endpoint_requires_a_query(client):
    assert client.get("/api/opportunities/semantic").status_code == 422


# ---------------------------------------------------------------------------
# Scoring: off by default, opt-in changes only the dream fit
# ---------------------------------------------------------------------------


def _score(world: dict, opportunity_id: str) -> dict:
    from dreamjob.db.repositories import campaigns as campaign_repo
    from dreamjob.db.repositories import opportunities as repo
    from dreamjob.pipeline import scoring

    campaign = campaign_repo.get_campaign(world["campaign"], world["me"])
    ctx = scoring.build_context(campaign)
    row = repo.get_opportunity(opportunity_id, world["me"])
    return scoring.score_opportunity(row, ctx)


def test_scoring_is_unchanged_and_never_embeds_by_default(world):
    _index()

    columns = _score(world, world["data_opportunity"])

    dream = columns["score_detail"]["components"]["dream_fit"]
    assert "embedding_similarity" not in dream["detail"]
    assert _CALLS == [], "with the setting off the embedder is not called"


def test_opting_in_moves_only_the_dream_fit_and_says_so(world, monkeypatch):
    _index()
    before = _score(world, world["sales_opportunity"])

    monkeypatch.setenv("DREAMJOB_SEMANTIC_SCORING", "true")
    get_settings.cache_clear()
    after = _score(world, world["sales_opportunity"])

    dream_before = before["score_detail"]["components"]["dream_fit"]
    dream_after = after["score_detail"]["components"]["dream_fit"]
    # The sales advert points away from a data dream job: similarity 0 now
    # weighs a fifth of the mechanical dream fit.
    assert dream_after["detail"]["embedding_similarity"] == pytest.approx(0.0)
    assert dream_after["value"] == pytest.approx(dream_before["value"] * 0.8, abs=1e-3)
    for name in ("profile_fit", "directive_fit", "compensation", "plausibility"):
        assert (
            after["score_detail"]["components"][name]
            == before["score_detail"]["components"][name]
        )
    assert len(_CALLS) == 1, "the dream job is embedded once per run, not per row"


def test_opting_in_with_an_unindexed_vacancy_changes_nothing(world, monkeypatch):
    before = _score(world, world["data_opportunity"])

    monkeypatch.setenv("DREAMJOB_SEMANTIC_SCORING", "true")
    get_settings.cache_clear()
    after = _score(world, world["data_opportunity"])

    assert after["score"] == before["score"]
    assert after["score_detail"]["components"] == before["score_detail"]["components"]


def test_opting_in_without_consent_sends_nothing(world, monkeypatch):
    from dreamjob.db.connection import get_connection

    _index()
    with get_connection() as conn:
        conn.execute("DELETE FROM consent_record")
    monkeypatch.setenv("DREAMJOB_SEMANTIC_SCORING", "true")
    get_settings.cache_clear()

    columns = _score(world, world["data_opportunity"])

    assert "embedding_similarity" not in (
        columns["score_detail"]["components"]["dream_fit"]["detail"]
    )
    assert _CALLS == []
