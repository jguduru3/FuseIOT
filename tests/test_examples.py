"""Every script in examples/ runs to completion against simulated devices."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted((ROOT / "examples").glob("*.py"))


@pytest.mark.slow
@pytest.mark.parametrize("script", EXAMPLES, ids=[p.name for p in EXAMPLES])
def test_example_runs(script, tmp_path):
    env = dict(os.environ)
    env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    env = {k: v for k, v in env.items() if not k.startswith("FUSEIOT_")}
    proc = subprocess.run([sys.executable, str(script)], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert proc.stdout.strip(), "examples should print what they do"


def test_examples_exist():
    assert {p.name for p in EXAMPLES} >= {"basic_relay.py", "async_batch.py", "smart_home.py",
                                          "industrial_control.py", "custom_capability.py"}
