"""tools/solar-deploy against a throwaway repository, with fake systemctl, uv and checker."""
import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "tools/solar-deploy"

FAKE_SYSTEMCTL = """#!/bin/sh
echo "$*" >> "$T/systemctl.log"
case "$1" in
  list-timers) echo "[{\\"next\\": $(cat "$T/next")000000, \\"unit\\": \\"solar-issue-11h.timer\\"}]" ;;
  list-units) cat "$T/jobs" 2>/dev/null ;;
esac
"""
FAKE_UV = """#!/bin/sh
here="$(pwd) $(git rev-parse HEAD)"
grep -qx "$here" "$T/sync_fail" 2>/dev/null && exit 1
s=$(grep "^$here " "$T/sync_sleep" 2>/dev/null | cut -d' ' -f3); [ -n "$s" ] && sleep "$s"
mkdir -p .venv/bin && cp "$T/python" .venv/bin/python && chmod +x .venv/bin/python
"""
FAKE_CHECK = """#!/bin/sh
dir=$(cd "$(dirname "$0")/../.." && pwd)
echo "$dir $(git -C "$dir" rev-parse HEAD)" >> "$T/checks.log"
grep -qx "$dir $(git -C "$dir" rev-parse HEAD)" "$T/check_fail" 2>/dev/null && exit 1
exit 0
"""


def sh(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def lab(tmp_path):
    t = tmp_path
    for name, body in (("bin/systemctl", FAKE_SYSTEMCTL), ("bin/uv", FAKE_UV), ("python", FAKE_CHECK)):
        (t / name).parent.mkdir(exist_ok=True)
        (t / name).write_text(body)
        (t / name).chmod(0o755)
    origin = t / "origin"
    origin.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=origin)
    (origin / "tools").mkdir()
    (origin / "tools/deploy_check.py").write_text("# checker\n")
    commits = []
    for n in ("a", "b"):
        (origin / "release").write_text(n)
        sh("git", "add", "-A", cwd=origin)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", n, cwd=origin)
        commits.append(sh("git", "rev-parse", "HEAD", cwd=origin))
    repo = t / "repo"
    sh("git", "clone", "-q", str(origin), str(repo))
    sh("git", "checkout", "-q", "--detach", commits[0], cwd=repo)
    lab = type("Lab", (), {})()
    lab.t, lab.repo, lab.a, lab.b = t, repo, commits[0], commits[1]
    lab.next = lambda s: (t / "next").write_text(str(int(time.time()) + s))
    lab.add = lambda f, line: open(t / f, "a").write(line + "\n")
    lab.head = lambda: sh("git", "rev-parse", "HEAD", cwd=repo)
    lab.calls = lambda: (t / "systemctl.log").read_text().splitlines() if (t / "systemctl.log").exists() else []
    lab.next(7200)
    return lab


def deploy(lab, commit, **env):
    e = {**os.environ, "T": str(lab.t), "PATH": f"{lab.t / 'bin'}:{os.environ['PATH']}", "AS_SOLAR": "",
         "REPO": str(lab.repo), "STAGE": str(lab.t / "stage"), "BIN": str(lab.t / "sbin"), "UV": str(lab.t / "bin/uv"),
         **{k: str(v) for k, v in env.items()}}
    r = subprocess.run(["sh", str(SCRIPT), commit], env=e, capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


def started_last(lab):
    acts = [c.split()[0] for c in lab.calls() if c.split()[0] in ("stop", "start")]
    return acts[-1:] == ["start"]


def test_deploys_and_starts_the_timers(lab):
    code, out = deploy(lab, lab.b)
    assert code == 0 and lab.head() == lab.b and started_last(lab), out


def test_a_failed_staged_check_activates_nothing(lab):
    lab.add("check_fail", f"{lab.t / 'stage'} {lab.b}")
    code, out = deploy(lab, lab.b)
    assert code == 1 and lab.head() == lab.a and not any(c.startswith("stop") for c in lab.calls()), out


def test_a_sync_failure_after_the_switch_restores_and_checks_the_previous_release(lab):
    lab.add("sync_fail", f"{lab.repo} {lab.b}")
    code, out = deploy(lab, lab.b)
    assert code == 1 and lab.head() == lab.a and started_last(lab), out
    assert (lab.t / "checks.log").read_text().splitlines()[-1] == f"{lab.repo} {lab.a}"


def test_a_failed_restore_leaves_the_timers_stopped(lab):
    lab.add("sync_fail", f"{lab.repo} {lab.b}")
    lab.add("check_fail", f"{lab.repo} {lab.a}")
    code, out = deploy(lab, lab.b)
    assert code == 2 and "RESTORE FAILED" in out and not started_last(lab), out


def test_an_overrun_is_cut_at_the_deadline_and_restored_before_the_next_run(lab):
    lab.next(80)  # deadline 10 s from now, restore by 20 s
    lab.add("sync_sleep", f"{lab.repo} {lab.b} 60")
    t = time.time()
    code, out = deploy(lab, lab.b, WINDOW=0, RESERVE=70, MIN_ACTIVATE=2)
    assert code == 1 and lab.head() == lab.a and started_last(lab), out
    assert time.time() - t < 20 and "ran out of time" in out


def test_too_close_to_a_scheduled_run_activates_nothing(lab):
    lab.next(600)
    code, out = deploy(lab, lab.b)
    assert code == 1 and lab.head() == lab.a and not any(c.startswith("stop") for c in lab.calls()), out


def test_jobs_running_into_the_reserve_abort_before_switching(lab):
    lab.next(80)
    (lab.t / "jobs").write_text("solar-score.service loaded activating\n")
    code, out = deploy(lab, lab.b, WINDOW=0, RESERVE=70, MIN_ACTIVATE=2)
    assert code == 1 and lab.head() == lab.a and started_last(lab) and "nothing activated" in out, out
