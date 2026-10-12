"""France - the State's open company search, recherche-entreprises.api.gouv.fr (FR-241, DR-101).

The French register is split across INSEE (Sirene: identity, activity code,
headcount band) and the INPI (the RNE and the deposited accounts).  The
``recherche-entreprises`` API run by the State's digital service (DINUM) joins
both behind one free endpoint: no key, no account, open data under the
Licence Ouverte.  For each company it gives the SIREN, the names, the NAF
activity code, the head office, the salaried headcount band and - for
companies whose accounts are published - the revenue (``ca``) and net result
(``resultat_net``) of the last closed years.

What it does not give is a balance sheet: no equity, cash, debt or personnel
costs, so the FR-244 ability-to-pay components that need them stay unknown,
and a company whose accounts are confidential (an option open to small
companies) comes back with identity only.  That is still the authoritative
figure where it exists, so the years it does give are written as registry
figures, not estimates, and a company with none is marked estimated (FR-245).

Matching by name is strict: a hit is accepted only when its legal or trading
name normalises to the same key as the company asked about, because the search
is full-text and its first hit for a common word is somebody else's company.
Directors are part of the response and are deliberately not stored: they are
personal data the product has no use for here.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import quote

from dreamjob.adapters.base import AccessMethod, AdapterCapabilities, SourceType, register_adapter
from dreamjob.adapters.registries.common import (
    DEFAULT_YEARS,
    RegistryAdapter,
    RegistryResult,
    identity_record,
    stated_none,
)
from dreamjob.pipeline import filing_extract as fx
from dreamjob.pipeline.dedup import normalise_company_name

log = logging.getLogger(__name__)

API_BASE = "https://recherche-entreprises.api.gouv.fr"
SEARCH_URL = API_BASE + "/search?q={query}&page=1&per_page={per_page}"
#: The public page a reader can open to check what was stored.
COMPANY_PAGE = "https://annuaire-entreprises.data.gouv.fr/entreprise/{siren}"

#: INSEE ``tranche_effectif_salarie`` codes -> the band's lower bound.  The
#: lower bound, not a midpoint: a band of 1,000-1,999 says at least 1,000, and
#: an invented midpoint would be read as a measured headcount.
_HEADCOUNT_FLOOR = {
    "00": 0, "01": 1, "02": 3, "03": 6, "11": 10, "12": 20, "21": 50, "22": 100,
    "31": 200, "32": 250, "41": 500, "42": 1000, "51": 2000, "52": 5000, "53": 10000,
}

_NON_DIGITS = re.compile(r"\D+")


@register_adapter
class RechercheEntreprisesAdapter(RegistryAdapter):
    """French register: identity from Sirene/RNE, revenue and net result where published."""

    key = "registry.recherche_entreprises"
    display_name = "Recherche d'entreprises (FR)"
    source_type = SourceType.REGISTRY
    access_method = AccessMethod.API
    coverage_countries = ["FR"]
    jurisdictions = ["FR"]
    key_required = False
    reporting_standard = "FR-GAAP"
    default_currency = "EUR"
    # The service allows 7 requests a second per IP; a registry pass needs
    # one or two per company, so two a second leaves room for everyone else.
    rate_limit_rps = 2.0
    capabilities = AdapterCapabilities(
        keyword_search=True,
        company_lookup=True,
        pagination=True,
        max_results_per_query=25,
    )
    legal_notes = (
        "Free public API of the French State (DINUM), no key; data under the Licence "
        "Ouverte 2.0 from INSEE Sirene and the INPI RNE. Only revenue and net result are "
        "published, and only for companies whose accounts are not confidential."
    )

    @staticmethod
    def siren(company: dict) -> str | None:
        """The 9-digit SIREN of a company row, from a SIREN, a SIRET or a French VAT number."""
        for key in ("legal_id", "siren", "vat_number"):
            raw = str(company.get(key) or "").strip().upper()
            if not raw:
                continue
            if raw.startswith("FR") and key == "vat_number":
                raw = raw[4:]  # FR + two check characters + SIREN
            digits = _NON_DIGITS.sub("", raw)
            if len(digits) in (9, 14):  # a SIRET is the SIREN plus a 5-digit establishment
                return digits[:9]
        return None

    # -- registry contract --------------------------------------------------
    async def collect(
        self,
        company: dict,
        *,
        years: int = DEFAULT_YEARS,
        egress: Any = None,
        llm: Any = None,
    ) -> RegistryResult:
        result = RegistryResult(adapter_key=self.key)
        async with self.session(egress) as client:
            number = self.siren(company)
            if number:
                hit = await self.lookup(number, egress=client)
            elif company.get("name"):
                hit = await self.search(str(company["name"]), egress=client)
            else:
                hit = None
        if hit is None:
            result.estimated = True
            result.note("No French company could be matched in the register")
            return result

        siren = str(hit["siren"])
        result.identity = self.to_identity(hit, company)
        result.facts = self.to_facts(hit, siren, years)
        if not result.facts:
            result.estimated = True
            result.note(
                f"SIREN {siren}: no published accounts (confidential or not filed); the "
                "financial profile is built from secondary signals and marked estimated"
            )
        return result

    # -- API calls ----------------------------------------------------------
    async def _json(self, url: str, *, egress: Any) -> Any:
        try:
            response = await egress.fetch(url, headers={"Accept": "application/json"})
        except Exception as exc:  # noqa: BLE001 - registry outages degrade
            log.info("[%s] %s unavailable (%s)", self.key, url, exc)
            return None
        if not response.ok:
            log.info("[%s] %s returned %s", self.key, url, response.status_code)
            return None
        try:
            return json.loads(response.text)
        except ValueError:
            return None

    async def lookup(self, siren: str, *, egress: Any) -> dict | None:
        """The register's record for one SIREN, or ``None``."""
        payload = await self._json(SEARCH_URL.format(query=siren, per_page=1), egress=egress)
        if not isinstance(payload, dict):
            return None
        for item in payload.get("results") or []:
            if isinstance(item, dict) and str(item.get("siren")) == siren:
                return item
        if stated_none(payload, listing="results", total="total_results"):
            self.record_stated_empty()
        return None

    async def search(self, name: str, *, egress: Any) -> dict | None:
        """The one active company whose name is ``name``, or ``None``.

        Full-text search ranks a namesake first as readily as the company, so a
        hit is accepted only on an exact normalised name; two such hits that
        are both active are ambiguous and neither is taken.
        """
        payload = await self._json(
            SEARCH_URL.format(query=quote(name, safe=""), per_page=10), egress=egress
        )
        if not isinstance(payload, dict):
            # An outage, a non-2xx and a body that is not JSON are not the
            # register saying it holds nothing (FR-181, NFR-403).
            return None
        wanted = normalise_company_name(name)
        results = payload.get("results")
        readable = [r for r in results if isinstance(r, dict) and r.get("siren")] if isinstance(
            results, list
        ) else []
        matches = [
            item for item in readable
            if wanted and wanted in {normalise_company_name(n) for n in _names(item)}
        ]
        active = [m for m in matches if m.get("etat_administratif") == "A"] or matches
        if len(active) == 1:
            return active[0]
        if len(active) > 1:
            log.info("[%s] %d companies named %r; none taken", self.key, len(active), name)
        elif stated_none(payload, listing="results", total="total_results") or (
            readable and len(readable) == len(results)
        ):
            # Either the register counted zero hits, or every hit it listed was
            # readable and none bears this name: both are the register
            # answering that it holds no such company.  Hits that cannot be
            # read are a breakage and stay one (FR-181, NFR-403).
            self.record_stated_empty()
        return None

    # -- mapping ------------------------------------------------------------
    def to_identity(self, hit: dict, company: dict) -> dict[str, Any]:
        siren = str(hit["siren"])
        siege = hit.get("siege") or {}
        naf = hit.get("activite_principale") or siege.get("activite_principale")
        headcount = _HEADCOUNT_FLOOR.get(str(hit.get("tranche_effectif_salarie") or ""))
        vat = next((v for v in hit.get("tva") or [] if isinstance(v, str) and v), None)
        return identity_record(
            company,
            name=str(hit.get("nom_raison_sociale") or hit.get("nom_complet") or ""),
            legal_id=siren,
            legal_id_type="siren",
            country="FR",
            source=COMPANY_PAGE.format(siren=siren),
            vat_number=vat,
            sector_codes=[str(naf)] if naf else None,
            size_fte=headcount,
            locations=(
                [{"kind": "siege", "address": siege["adresse"]}] if siege.get("adresse") else None
            ),
        )

    def to_facts(self, hit: dict, siren: str, years: int) -> list[fx.FilingFacts]:
        """The published years, most recent ``years`` of them, oldest first."""
        out: list[fx.FilingFacts] = []
        finances = hit.get("finances")
        if not isinstance(finances, dict):
            return out
        for year_text, figures in finances.items():
            if not (str(year_text).isdigit() and isinstance(figures, dict)):
                continue
            revenue = _number(figures.get("ca"))
            net = _number(figures.get("resultat_net"))
            if revenue is None and net is None:
                continue
            out.append(
                fx.FilingFacts(
                    fiscal_year=int(year_text),
                    currency="EUR",
                    reporting_standard=self.reporting_standard,
                    revenue=revenue,
                    net_result=net,
                    source=COMPANY_PAGE.format(siren=siren),
                )
            )
        out.sort(key=lambda f: f.fiscal_year)
        return out[-years:] if years > 0 else out


def _names(hit: dict) -> list[str]:
    """Every name the register knows the company by."""
    names = [hit.get("nom_raison_sociale"), hit.get("nom_complet"), hit.get("sigle")]
    names.extend((hit.get("siege") or {}).get("liste_enseignes") or [])
    return [str(n) for n in names if n]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
