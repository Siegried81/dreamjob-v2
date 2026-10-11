"""A process that never ran the API's start-up still finds its adapters.

The scheduled ``--once`` pass imports neither ``main.py`` nor ``load_all``, so
the registry was empty and every board refresh failed with ``Known: []``.
Run in a subprocess: the registry is process-global, and the test session has
already imported the adapters through other tests.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(code: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "backend")}
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


def test_get_adapter_discovers_the_adapters_in_a_fresh_process():
    out = _run(
        "from dreamjob.adapters import base\n"
        "assert base._REGISTRY == {}, 'precondition: nothing imported yet'\n"
        "adapter = base.get_adapter('ats.greenhouse')\n"
        "print(type(adapter).__name__, len(base._REGISTRY))\n"
    )
    assert out.returncode == 0, out.stderr[-2000:]
    _name, count = out.stdout.split()
    assert int(count) > 1


def test_an_unknown_key_still_raises_after_discovery():
    out = _run(
        "from dreamjob.adapters import base\n"
        "try:\n"
        "    base.get_adapter('ats.does-not-exist')\n"
        "except KeyError as exc:\n"
        "    print('KeyError', 'ats.greenhouse' in str(exc))\n"
    )
    assert out.stdout.strip() == "KeyError True", out.stderr[-2000:]
