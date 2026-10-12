"""The French register adapter on recherche-entreprises.api.gouv.fr (FR-241, DR-101).

No network: a stub egress answers with bodies shaped like the live API's (a
real response was read once to write them, trimmed to the fields used).  What
is pinned is what goes wrong quietly with a full-text register - a namesake
taken for the company, an outage read as "no such company", and an estimate
written where published figures exist (or the reverse).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from dreamjob.adapters.registries.recherche_entreprises import RechercheEntreprisesAdapter
from dreamjob.pipeline import financial


class StubResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class StubEgress:
    def __init__(self, body: Any, status_code: int = 200, *, raises: bool = False) -> None:
        self.body = body if isinstance(body, str) else json.dumps(body)
        self.status_code = status_code
        self.raises = raises
        self.urls: list[str] = []

    async def fetch(self, url: str, **_kwargs: Any) -> StubResponse:
        self.urls.append(url)
        if self.raises:
            raise ConnectionError("register unreachable")
        return StubResponse(self.body, self.status_code)


def _hit(siren: str = "552032534", name: str = "DANONE", *, active: bool = True,
         finances: Any = None) -> dict:
    return {
        "siren": siren,
        "nom_complet": name,
        "nom_raison_sociale": name,
        "sigle": None,
        "activite_principale": "70.10Z",
        "tranche_effectif_salarie": "42",
        "etat_administratif": "A" if active else "C",
        "tva": [f"FR27{siren}"],
        "siege": {"adresse": "17 BOULEVARD HAUSSMANN 75009 PARIS", "liste_enseignes": None},
        "finances": finances if finances is not None else {
            "2023": {"ca": 27_619_000_000, "resultat_net": 881_000_000},
            "2024": {"ca": 27_376_000_000, "resultat_net": 2_100_000_000},
        },
        "dirigeants": [{"nom": "SOMEONE", "prenoms": "PRIVATE", "annee_de_naissance": "1964"}],
    }


def _body(*hits: dict) -> dict:
    return {"results": list(hits), "total_results": len(hits), "page": 1, "per_page": 10}


async def _collect(company: dict, egress: StubEgress, years: int = 5):
    adapter = RechercheEntreprisesAdapter(egress=egress)
    return adapter, await adapter.collect(company, years=years, egress=egress)


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("company", "expected"),
    [
        ({"legal_id": "552 032 534"}, "552032534"),
        ({"legal_id": "55203253400703"}, "552032534"),          # a SIRET
        ({"vat_number": "FR27552032534"}, "552032534"),
        ({"legal_id": "1234"}, None),
        ({"name": "Danone"}, None),
    ],
)
def test_the_siren_is_read_however_it_is_written(company: dict, expected: str | None) -> None:
    assert RechercheEntreprisesAdapter.siren(company) == expected


# ---------------------------------------------------------------------------
# collect()
# ---------------------------------------------------------------------------


async def test_a_siren_lookup_gives_identity_and_the_published_years() -> None:
    egress = StubEgress(_body(_hit()))

    _adapter, result = await _collect({"legal_id": "552032534", "country": "FR"}, egress)

    assert egress.urls == [
        "https://recherche-entreprises.api.gouv.fr/search?q=552032534&page=1&per_page=1"
    ]
    assert result.identity["legal_id"] == "552032534"
    assert result.identity["legal_id_type"] == "siren"
    assert result.identity["country"] == "FR"
    assert result.identity["sector_codes"] == ["70.10Z"]
    assert result.identity["size_fte"] == 1000, "the lower bound of INSEE band 42"
    assert result.identity["vat_number"] == "FR27552032534"
    assert [f.fiscal_year for f in result.facts] == [2023, 2024]
    assert result.facts[-1].revenue == 27_376_000_000.0
    assert result.facts[-1].net_result == 2_100_000_000.0
    assert result.facts[-1].currency == "EUR"
    assert result.estimated is False, "published registry figures are not an estimate"


async def test_directors_are_never_stored() -> None:
    _adapter, result = await _collect({"legal_id": "552032534"}, StubEgress(_body(_hit())))

    assert "PRIVATE" not in json.dumps(result.identity)


async def test_years_are_capped_to_the_most_recent() -> None:
    finances = {str(y): {"ca": 1000 * y, "resultat_net": y} for y in range(2017, 2025)}
    egress = StubEgress(_body(_hit(finances=finances)))

    _adapter, result = await _collect({"legal_id": "552032534"}, egress, years=3)

    assert [f.fiscal_year for f in result.facts] == [2022, 2023, 2024]


async def test_confidential_accounts_give_identity_and_an_estimate() -> None:
    egress = StubEgress(_body(_hit(finances={})))

    _adapter, result = await _collect({"legal_id": "552032534"}, egress)

    assert result.identity is not None
    assert result.facts == []
    assert result.estimated is True
    assert "no published accounts" in result.notes[-1]


async def test_a_name_is_matched_exactly_and_a_namesake_is_refused() -> None:
    namesake = _hit("111111111", "DANONE NUTRICIA RESEARCH")
    egress = StubEgress(_body(namesake, _hit()))

    _adapter, result = await _collect({"name": "Danone", "country": "FR"}, egress)

    assert result.identity["legal_id"] == "552032534"


async def test_two_active_companies_of_one_name_are_ambiguous_not_guessed() -> None:
    egress = StubEgress(_body(_hit("111111111", "ACME"), _hit("222222222", "ACME")))

    adapter, result = await _collect({"name": "Acme"}, egress)

    assert result.identity is None
    assert result.estimated is True
    assert adapter.stated_empty == 0, "an ambiguous answer is not 'no such company'"


async def test_a_closed_namesake_yields_to_the_active_company() -> None:
    egress = StubEgress(_body(_hit("111111111", "ACME", active=False), _hit("222222222", "ACME")))

    _adapter, result = await _collect({"name": "Acme"}, egress)

    assert result.identity["legal_id"] == "222222222"


async def test_only_namesakes_is_the_register_answering_no() -> None:
    egress = StubEgress(_body(_hit("111111111", "SOMETHING ELSE")))

    adapter, result = await _collect({"name": "Acme"}, egress)

    assert result.identity is None
    assert adapter.stated_empty == 1


@pytest.mark.parametrize(
    "egress",
    [
        StubEgress({}, raises=True),
        StubEgress("<html>Too Many Requests</html>", 429),
        StubEgress("not json"),
    ],
    ids=["outage", "http_429", "not_json"],
)
async def test_an_outage_is_never_read_as_no_such_company(egress: StubEgress) -> None:
    adapter, result = await _collect({"name": "Danone"}, egress)

    assert result.identity is None
    assert result.estimated is True
    assert adapter.stated_empty == 0


async def test_a_siren_the_register_does_not_return_is_not_taken() -> None:
    egress = StubEgress(_body(_hit("999999999")))

    _adapter, result = await _collect({"legal_id": "552032534"}, egress)

    assert result.identity is None


async def test_the_adapter_needs_no_key() -> None:
    assert RechercheEntreprisesAdapter().available() is True


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_french_companies_are_routed_to_the_french_register() -> None:
    assert financial.registries_for({"country": "FR"})[0] == "registry.recherche_entreprises"
    assert "registry.recherche_entreprises" not in financial.registries_for({"country": "DE"})
