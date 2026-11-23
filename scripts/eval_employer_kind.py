#!/usr/bin/env python3
"""Score the employer-kind resolver against the 43 labelled employers.

    python scripts/eval_employer_kind.py              # data/dreamjob.db
    python scripts/eval_employer_kind.py --db other.db
    python scripts/eval_employer_kind.py --json       # machine-readable

Run it after a resolver pass (``run_resolve_many`` or the coverage panel's
button) to get the figures ``docs/Employer_Kind_Results.md`` reports: precision
and recall of the stored verdicts, the same for the signal bands, the direct
employers wrongly tagged as agencies (named, because one is one too many), and
how much of the corpus has a verdict at all.

Read-only: it opens the database, never writes, and makes no request. Labels
come from ``tests/fixtures/agency_labels.json`` and companies are matched on
the same normalised name the pipeline stores, so a labelled employer missing
from the database is listed rather than counted as a miss.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from dreamjob.pipeline.dedup import normalise_company_name  # noqa: E402

LABELS = ROOT / "tests" / "fixtures" / "agency_labels.json"
DEFAULT_DB = ROOT / "data" / "dreamjob.db"
#: Bands from most to least sure; "probable and above" means the first two.
BANDS = ("certain", "probable", "possible", "unknown", "direct_likely")


def load_labels(path: Path = LABELS) -> list[dict[str, str]]:
    """Name and label (``agency`` or ``direct``) of each labelled employer."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return [{"name": e["name"], "label": e["label"]} for e in data["employers"]]


def read_rows(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Verdict and signal band per normalised company name."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT co.normalised_name AS nname, k.kind AS kind, s.tier AS tier
        FROM company co
        LEFT JOIN company_employer_kind k ON k.company_id = co.id
        LEFT JOIN employer_resolution_signal s ON s.company_id = co.id
        """
    ).fetchall()
    by_name: dict[str, dict[str, Any]] = {}
    for row in rows:
        current = by_name.setdefault(row["nname"], {"kind": None, "tier": None})
        # Two rows can share a normalised name; keep whichever has a verdict.
        if current["kind"] is None and row["kind"]:
            current["kind"] = row["kind"]
        if current["tier"] is None and row["tier"]:
            current["tier"] = row["tier"]
    return by_name


def _pr(flagged: list[dict[str, Any]], agencies: int) -> dict[str, Any]:
    hits = sum(1 for e in flagged if e["label"] == "agency")
    return {
        "flagged": len(flagged),
        "precision": round(hits / len(flagged), 2) if flagged else None,
        "recall": round(hits / agencies, 2) if agencies else None,
        "direct_flagged": sorted(e["name"] for e in flagged if e["label"] == "direct"),
    }


def evaluate(labels: list[dict[str, str]], by_name: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Precision and recall for the verdicts and for two band cut-offs."""
    found, missing = [], []
    for entry in labels:
        row = by_name.get(normalise_company_name(entry["name"]))
        if row is None:
            missing.append(entry["name"])
        else:
            found.append({**entry, **row})
    agencies = sum(1 for e in found if e["label"] == "agency")
    answered = [e for e in found if e["kind"] in ("agency", "employer")]

    def in_bands(cut: int) -> list[dict[str, Any]]:
        return [e for e in found if e["tier"] in BANDS[:cut]]

    return {
        "labelled": len(labels),
        "found_in_db": len(found),
        "missing_from_db": missing,
        "agencies": agencies,
        "with_verdict": len(answered),
        "cannot_tell_or_none": len(found) - len(answered),
        "verdict": _pr([e for e in answered if e["kind"] == "agency"], agencies),
        "probable_and_above": _pr(in_bands(2), agencies),
        "possible_and_above": _pr(in_bands(3), agencies),
    }


def corpus_coverage(conn: sqlite3.Connection) -> dict[str, int]:
    """Employers with at least one vacancy, and how many of them have a verdict."""
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT co.id) AS employers,
               COUNT(DISTINCT CASE WHEN k.kind IN ('agency', 'employer')
                                   THEN co.id END) AS decided
        FROM company co
        JOIN vacancy v ON v.company_id = co.id
        LEFT JOIN company_employer_kind k ON k.company_id = co.id
        """
    ).fetchone()
    return {"employers_with_vacancies": row[0], "decided": row[1]}


def render(result: dict[str, Any]) -> str:
    lines = [
        f"Labelled employers: {result['labelled']}, found in the database: "
        f"{result['found_in_db']} ({result['agencies']} agencies)",
    ]
    if result["missing_from_db"]:
        lines.append("Not in the database: " + ", ".join(result["missing_from_db"]))
    lines.append(
        f"With a verdict: {result['with_verdict']}, cannot tell or not researched: "
        f"{result['cannot_tell_or_none']}"
    )
    for key, title in (
        ("verdict", "Stored verdict 'agency'"),
        ("probable_and_above", "Band probable and above"),
        ("possible_and_above", "Band possible and above"),
    ):
        r = result[key]
        lines.append(
            f"{title}: flagged {r['flagged']}, precision {r['precision']}, recall {r['recall']}"
        )
        if r["direct_flagged"]:
            lines.append("  Direct employers flagged as agency: " + ", ".join(r["direct_flagged"]))
    cov = result.get("coverage")
    if cov:
        lines.append(
            f"Corpus: {cov['decided']} of {cov['employers_with_vacancies']} employers "
            "with vacancies have a verdict"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.db.exists():
        print(f"No database at {args.db}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        result = evaluate(load_labels(), read_rows(conn))
        result["coverage"] = corpus_coverage(conn)
    finally:
        conn.close()
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
