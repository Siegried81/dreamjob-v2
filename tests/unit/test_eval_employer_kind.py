"""scripts/eval_employer_kind.py scores stored verdicts against the labelled employers."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "eval_employer_kind.py"
spec = importlib.util.spec_from_file_location("eval_employer_kind", SCRIPT)
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)

LABELS = [
    {"name": "NOEL FRANKLIN BV", "label": "agency"},
    {"name": "FORUM JOBS NV", "label": "agency"},
    {"name": "GitLab", "label": "direct"},
    {"name": "SMALS VZW", "label": "direct"},
    {"name": "Not In The Db", "label": "agency"},
]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE company (id TEXT PRIMARY KEY, normalised_name TEXT NOT NULL);
        CREATE TABLE company_employer_kind (company_id TEXT PRIMARY KEY, kind TEXT);
        CREATE TABLE employer_resolution_signal (company_id TEXT PRIMARY KEY, tier TEXT);
        CREATE TABLE vacancy (id TEXT PRIMARY KEY, company_id TEXT);
        """
    )
    rows = [
        # id, name, verdict, band
        ("c1", "NOEL FRANKLIN BV", "agency", "certain"),
        ("c2", "FORUM JOBS NV", "cannot_tell", "possible"),
        ("c3", "GitLab", "employer", "direct_likely"),
        ("c4", "SMALS VZW", "agency", "probable"),
    ]
    for cid, name, kind, tier in rows:
        conn.execute("INSERT INTO company VALUES (?, ?)", (cid, ev.normalise_company_name(name)))
        conn.execute("INSERT INTO company_employer_kind VALUES (?, ?)", (cid, kind))
        conn.execute("INSERT INTO employer_resolution_signal VALUES (?, ?)", (cid, tier))
        conn.execute("INSERT INTO vacancy VALUES (?, ?)", (f"v{cid}", cid))
    conn.commit()
    conn.close()
    return path


def test_precision_recall_and_named_mistakes(db: Path) -> None:
    conn = sqlite3.connect(db)
    result = ev.evaluate(LABELS, ev.read_rows(conn))
    assert result["found_in_db"] == 4
    assert result["missing_from_db"] == ["Not In The Db"]
    assert result["with_verdict"] == 3
    # Two flagged as agency, one of them a direct employer.
    assert result["verdict"] == {
        "flagged": 2, "precision": 0.5, "recall": 0.5, "direct_flagged": ["SMALS VZW"],
    }
    assert result["probable_and_above"]["flagged"] == 2
    assert result["possible_and_above"] == {
        "flagged": 3, "precision": 0.67, "recall": 1.0, "direct_flagged": ["SMALS VZW"],
    }
    assert ev.corpus_coverage(conn) == {"employers_with_vacancies": 4, "decided": 3}
    conn.close()


def test_the_real_label_file_loads() -> None:
    labels = ev.load_labels()
    assert len(labels) == 43
    assert {e["label"] for e in labels} == {"agency", "direct"}


def test_cli_reads_without_writing(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = db.read_bytes()
    assert ev.main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "Direct employers flagged as agency: SMALS VZW" in out
    assert db.read_bytes() == before


def test_missing_database_is_an_error(tmp_path: Path) -> None:
    assert ev.main(["--db", str(tmp_path / "none.db")]) == 1
