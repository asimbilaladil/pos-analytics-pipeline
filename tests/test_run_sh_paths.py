#!/usr/bin/env python3
"""run.sh runs the code of the checkout it lives in.

Run:  venv/bin/python tests/test_run_sh_paths.py     (exit 0 = pass)

run.sh used to hardcode APP_DIR=/root/pos-analytics-pipeline, so the nightly
sync executed the development checkout even when started from the release
checkout -- and the 2026-10 items/watermark fix never reached production.
This executes a copy of run.sh against stub python/venv in a temp dir (no
database, no Revel, no real log) and records every interpreter call.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "run.sh")
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def run(env_extra, fail_pipeline=False):
    d = tempfile.mkdtemp()
    app, venv, logs = os.path.join(d, "app"), os.path.join(d, "sharedvenv"), os.path.join(d, "logs")
    os.makedirs(os.path.join(venv, "bin")); os.makedirs(app); os.makedirs(logs)
    shutil.copy(SRC, os.path.join(app, "run.sh"))
    open(os.path.join(app, ".env"), "w").write("X_FROM_ENV=1\n")
    calls = os.path.join(d, "calls")
    stub = os.path.join(venv, "bin", "python3")
    with open(stub, "w") as f:
        f.write("#!/bin/bash\necho \"$PWD|$*\" >> %s\n" % calls
                + ("case \"$1\" in */pipeline.py) exit 1;; esac\n" if fail_pipeline else "")
                + "exit 0\n")
    os.chmod(stub, 0o755)
    env = {"PATH": os.environ["PATH"], "LAYNES_VENV": venv, "LAYNES_LOG_DIR": logs, **env_extra}
    rc = subprocess.run(["bash", os.path.join(app, "run.sh")], cwd=d, env=env,
                        capture_output=True, text=True).returncode
    out = open(calls).read().splitlines() if os.path.exists(calls) else []
    shutil.rmtree(d)
    return rc, out, app


rc, calls, app = run({})
check("exits 0 with all steps succeeding", rc == 0, str(rc))
check("pipeline.py comes from the script's own checkout", any(f"|{app}/pipeline.py" in c for c in calls), str(calls[:1]))
check("no call reaches the hardcoded development path",
      not any("/root/pos-analytics-pipeline/" in c for c in calls))
check("interpreter is LAYNES_VENV's", len(calls) >= 4)
check("weather step runs inside the script's checkout", any(c.startswith(app + "|-m weather_analysis.cli") for c in calls))
check("step 1b skipped when PRICE_HISTORY_REFRESH unset", not any("refresh" in c for c in calls))

rc, calls, app = run({"PRICE_HISTORY_REFRESH": "/elsewhere/refresh_price_history.py"})
idx = [i for i, c in enumerate(calls) if "/elsewhere/refresh_price_history.py" in c]
pi = [i for i, c in enumerate(calls) if c.endswith("/pipeline.py")]
check("step 1b runs the configured script", len(idx) == 1)
check("step 1b runs after the sync and before aggregation",
      idx and pi and pi[0] < idx[0] and "aggregate_features_v2.py" in calls[idx[0] + 1])

rc, calls, app = run({"PRICE_HISTORY_REFRESH": "/elsewhere/refresh_price_history.py"}, fail_pipeline=True)
check("a failed sync exits 1 and never runs step 1b", rc == 1 and not any("refresh" in c for c in calls), str((rc, calls)))

passed = sum(1 for _, ok, _ in results if ok)
failed = len(results) - passed
print(f"\n{'='*66}\nrun.sh paths: {passed} passed, {failed} failed, {len(results)} total")
sys.exit(1 if failed else 0)
