"""Importing one mail backend first must not hide the others.

The loader used "is the registry empty?" as its guard, so a backend imported
directly before the first lookup left every other backend unregistered. Run in
a subprocess: the registry is process-global and the test session has already
imported every backend.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(("first", "other"), [("resend_backend", "gmail_oauth"), ("gmail", "resend")])
def test_an_early_import_of_one_backend_still_finds_the_other(first, other):
    code = (
        f"import dreamjob.mail.{first}\n"
        "from dreamjob.mail import base\n"
        f"base.get_backend({other!r})\n"
        "print(sorted(base._REGISTRY))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "backend")}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-1500:]
    assert other in out.stdout
