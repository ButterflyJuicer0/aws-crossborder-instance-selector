import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "find_best_instance.sh")


def test_wrapper_dry_run():
    env = {**os.environ, "PYTHON_BIN": sys.executable, "PYTHONPATH": ROOT}
    r = subprocess.run(["bash", SCRIPT, "ap-east-1", "4", "2", "1", "--dry-run"], capture_output=True, text=True, env=env, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert "DRY-RUN" in r.stdout and "4 x t3.nano" in r.stdout and "max_rounds=2" in r.stdout


def test_wrapper_usage_on_no_args():
    r = subprocess.run(["bash", SCRIPT], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 2 and "usage" in (r.stdout + r.stderr).lower()
