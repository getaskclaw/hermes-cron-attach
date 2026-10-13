"""Tests for the REMOTE payload in cron_attach.py.

Every case runs the payload in a subprocess exactly like the local CLI does —
`python3 - <mode> <params_b64>` with the REMOTE source on stdin — against a
sandboxed HERMES_HOME. HOME is also redirected so the payload's ~/.hermes and
/home/* scans can't see real state.
"""

import base64
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REMOTE_SRC = re.search(
    r"REMOTE = r'''(.*?)'''",
    open(os.path.join(REPO, "cron_attach.py"), encoding="utf-8").read(),
    re.S,
).group(1)

# The payload hard-codes /root/.hermes and /home/*/.hermes in homes(); on a dev
# box those are real gateways' stores. Tests retarget them into the sandbox so
# nothing outside tmp_path is touched.
FAKE_ROOT = "/tmp/__ca_test_root__"
FAKE_HOME_GLOB = "/tmp/__ca_test_home__/*"


def _sandbox_src(remote_src, home):
    """Retarget the payload's hard-coded home paths into the sandbox.

    Each replacement MUST match exactly once — a silent no-op here means the
    payload would scan (and --all-jobs would rewrite) the real gateways'
    jobs.json on this VPS. Assert loudly instead of relying on the literal
    strings in homes() staying the same forever.
    """
    src = remote_src or REMOTE_SRC
    for literal, fake in (('"/root/.hermes"', '"%s/.hermes"' % FAKE_ROOT),
                          ('"/home/*/.hermes"', '"%s/.hermes"' % FAKE_HOME_GLOB)):
        assert src.count(literal) == 1, (
            "payload no longer contains %s — homes() changed, update the "
            "sandbox retargeting or tests will touch REAL jobs.json" % literal)
        src = src.replace(literal, fake)
    return src


def run(mode, params, home, remote_src=None):
    pb64 = base64.b64encode(json.dumps(params).encode()).decode()
    env = {
        "PATH": os.environ["PATH"],
        "HERMES_HOME": home,
        "HOME": home,  # payload's ~ scan must not see real state
    }
    r = subprocess.run(
        [sys.executable, "-", mode, pb64],
        input=_sandbox_src(remote_src, home).encode(),
        capture_output=True, timeout=60, env=env,
    )
    assert not r.stderr, r.stderr.decode()
    return json.loads(r.stdout)


def write_jobs(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f)


def read_jobs(path):
    return json.load(open(path, encoding="utf-8-sig"))


@pytest.fixture
def home(tmp_path):
    h = str(tmp_path / ".hermes")
    os.makedirs(h + "/cron")
    return h


P = {"value": True, "patterns": [], "all_jobs": True,
     "force_multi": False, "dry_run": False, "profile": "default"}


# ---------- id-keyed map handling (the original critical bug) ----------

def test_map_without_inline_ids_keeps_all_jobs(home):
    """{k1: {name}, k2: {name}} must not collapse into a single "None" key."""
    jf = home + "/cron/jobs.json"
    write_jobs(jf, {"jobs": {"k1": {"name": "alpha"}, "k2": {"name": "beta"}}})
    res = run("set", P, home)
    after = read_jobs(jf)
    assert sorted(after["jobs"].keys()) == ["k1", "k2"]
    assert all(v.get("attach_to_session") is True for v in after["jobs"].values())
    assert {c["id"] for c in res["changed"]} == {"k1", "k2"}


def test_map_with_inline_ids_preserves_keys(home):
    """When map values carry their own id, the map keys are kept (k1 ≠ id)."""
    jf = home + "/cron/jobs.json"
    write_jobs(jf, {"jobs": {"k1": {"id": "abc", "name": "a"},
                             "k2": {"id": "def", "name": "b"}}})
    run("set", P, home)
    after = read_jobs(jf)
    assert sorted(after["jobs"].keys()) == ["k1", "k2"]
    assert all(v.get("attach_to_session") is True for v in after["jobs"].values())


def test_list_shape_unchanged(home):
    jf = home + "/cron/jobs.json"
    write_jobs(jf, {"jobs": [{"id": "a1"}, {"id": "a2"}]})
    run("set", P, home)
    after = read_jobs(jf)
    assert isinstance(after["jobs"], list) and len(after["jobs"]) == 2


# ---------- --profile selection ----------

def test_profile_default_hits_default_home_only(home):
    jf = home + "/cron/jobs.json"
    pjf = home + "/profiles/work/cron/jobs.json"
    write_jobs(jf, {"jobs": [{"id": "d1"}]})
    write_jobs(pjf, {"jobs": [{"id": "w1"}]})
    res = run("set", P, home)
    assert all(c["home"] == "env" for c in res["changed"])
    assert read_jobs(pjf)["jobs"][0].get("attach_to_session") is None


def test_named_profile_hits_profile_only(home):
    jf = home + "/cron/jobs.json"
    pjf = home + "/profiles/work/cron/jobs.json"
    write_jobs(jf, {"jobs": [{"id": "d1"}]})
    write_jobs(pjf, {"jobs": [{"id": "w1"}]})
    res = run("set", {**P, "profile": "work"}, home)
    assert [(c["home"], c["id"]) for c in res["changed"]] == [("env|profile:work", "w1")]
    assert read_jobs(jf)["jobs"][0].get("attach_to_session") is None


def test_no_profile_reaches_every_home(home):
    pjf = home + "/profiles/work/cron/jobs.json"
    write_jobs(home + "/cron/jobs.json", {"jobs": [{"id": "d1"}]})
    write_jobs(pjf, {"jobs": [{"id": "w1"}]})
    res = run("set", {**P, "profile": None, "dry_run": True}, home)
    assert {c["home"] for c in res["changed"]} == {"env", "env|profile:work"}


# ---------- symlinked jobs.json ----------

def test_symlinked_jobs_file_refused(home):
    jf = home + "/cron/jobs.json"
    real = home + "/real_jobs.json"
    write_jobs(real, {"jobs": [{"id": "s1"}]})
    os.symlink(real, jf)
    res = run("set", P, home)
    assert "符号链接" in res["errors"][0]


def test_symlinked_home_dir_not_refused(tmp_path):
    """~/.hermes living behind a directory symlink must still work — the
    gateway's lock path resolves through the same links to the same inode."""
    real = str(tmp_path / "real_hermes")
    os.makedirs(real + "/cron")
    write_jobs(real + "/cron/jobs.json", {"jobs": [{"id": "a1"}]})
    link = str(tmp_path / ".hermes")
    os.symlink(real, link)
    res = run("set", P, link)
    assert not res["errors"]
    assert any(c["home"] == "env" for c in res["changed"])


# ---------- lock budget ----------

def test_lock_wait_bounded_by_budget(home):
    """A held lock must abort when the shared budget runs out, not wait 35s."""
    jf = home + "/cron/jobs.json"
    write_jobs(jf, {"jobs": [{"id": "a1"}]})
    lf = open(home + "/cron/.jobs.lock", "a+")
    fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
    try:
        t0 = time.monotonic()
        res = run("set", {**P, "budget_s": 2}, home)
        assert time.monotonic() - t0 < 6
        assert "lock fail" in res["errors"][0]
    finally:
        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        lf.close()


def test_clock_skew_does_not_eat_budget(home):
    """Regression for the cross-clock deadline bug: the old code passed a unix
    timestamp, so a remote clock >80s ahead made every set fail instantly while
    a clock behind let writes land after the local timeout. budget_s is a
    relative count — wall-clock skew can't touch it. (Same machine here, so we
    just verify a sane budget succeeds and reports a changed job.)"""
    jf = home + "/cron/jobs.json"
    write_jobs(jf, {"jobs": [{"id": "a1"}]})
    res = run("set", {**P, "budget_s": 5}, home)
    assert not res["errors"] and res["changed"]


def test_zero_budget_skips_immediately(home):
    """budget_s<=0 → the home is skipped before even trying flock (negative: a
    small positive value could still be alive when the free lock is grabbed)."""
    jf = home + "/cron/jobs.json"
    write_jobs(jf, {"jobs": [{"id": "a1"}]})
    res = run("set", {**P, "budget_s": -1}, home)
    assert not res["changed"]
    assert "预算" in res["errors"][0] or "lock fail" in res["errors"][0]


def test_second_home_gets_no_fresh_35s(tmp_path):
    """The budget is global: two locked homes in one run share it, they don't
    get 35s each."""
    home = str(tmp_path / ".hermes")
    os.makedirs(home + "/cron")
    write_jobs(home + "/cron/jobs.json", {"jobs": [{"id": "d1"}]})
    write_jobs(home + "/profiles/x/cron/jobs.json", {"jobs": [{"id": "x1"}]})
    locks = []
    for p in (home + "/cron/.jobs.lock", home + "/profiles/x/cron/.jobs.lock"):
        lf = open(p, "a+")
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        locks.append(lf)
    try:
        t0 = time.monotonic()
        res = run("set", {"value": True, "patterns": [], "all_jobs": True,
                          "force_multi": False, "dry_run": False,
                          "profile": None, "budget_s": 3}, home)
        el = time.monotonic() - t0
        # without the shared budget this would take ~70s (35s per home)
        assert el < 10
        assert len(res["errors"]) == 2
    finally:
        for lf in locks:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            lf.close()


# ---------- via=hermes (real venv) ----------

REAL_VENV = "/root/.hermes/hermes-agent"
REAL_PY = os.path.join(REAL_VENV, "venv", "bin", "python")


@pytest.mark.skipif(not os.path.isfile(REAL_PY),
                    reason="needs a real Hermes venv on this box")
def test_via_hermes_runs_update_job(home, tmp_path):
    """A home carrying its own hermes-agent/venv must be written through
    update_job, not our raw path — proof: normalized fields appear."""
    # fake hermes-agent dir: real venv symlink + real cron package + top modules
    src = os.path.join(home, "hermes-agent")
    os.makedirs(src)
    os.symlink(os.path.join(REAL_VENV, "venv"), src + "/venv")
    for name in ("cron", "agent", "hermes_constants.py", "utils.py"):
        os.symlink(os.path.join(REAL_VENV, name), src + "/" + name)
    jf = home + "/cron/jobs.json"
    write_jobs(jf, {"jobs": [{"id": "v1", "name": "via-venv",
                              "schedule": {"kind": "every", "every_s": 60}}]})
    res = run("set", {**P, "profile": "default"}, home)
    assert not res["errors"], res
    assert res["changed"][0].get("via") == "hermes"
    after = read_jobs(jf)["jobs"][0]
    assert after["attach_to_session"] is True
    # update_job normalized these; the raw writer would never add them
    assert "updated_at" in read_jobs(jf)
    # R1 regression: a successful via=hermes write must report each change
    # exactly once — not once via CA_DONE (partial:true) + once via CA_JSON.
    assert len(res["changed"]) == 1
    assert res["changed"][0]["id"] == "v1"
    assert "partial" not in res["changed"][0]


def test_raw_fallback_without_venv(home):
    """No hermes-agent/venv → raw lock+write path still works (pre-#1 behavior)."""
    jf = home + "/cron/jobs.json"
    write_jobs(jf, {"jobs": [{"id": "r1"}]})
    res = run("set", P, home)
    assert not res["errors"]
    assert read_jobs(jf)["jobs"][0]["attach_to_session"] is True


@pytest.mark.skipif(not os.path.isfile(REAL_PY),
                    reason="needs a real Hermes venv on this box")
def test_via_hermes_refuses_when_lock_held(home):
    """Regression for the step-0 hole: gateway's _acquire_flock returns False on
    timeout and _jobs_lock proceeds with in-process locking only — so update_job
    used to write while the lock was held. The strict patch must abort instead."""
    src = os.path.join(home, "hermes-agent")
    os.makedirs(src)
    os.symlink(os.path.join(REAL_VENV, "venv"), src + "/venv")
    for name in ("cron", "agent", "hermes_constants.py", "utils.py"):
        os.symlink(os.path.join(REAL_VENV, name), src + "/" + name)
    jf = home + "/cron/jobs.json"
    write_jobs(jf, {"jobs": [{"id": "v2", "name": "held-lock",
                              "schedule": {"kind": "every", "every_s": 60}}]})
    lf = open(home + "/cron/.jobs.lock", "a+")
    fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
    try:
        res = run("set", {**P, "budget_s": 3}, home)
        assert res["errors"], res  # must report, not write through
        assert read_jobs(jf)["jobs"][0].get("attach_to_session") is None
    finally:
        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        lf.close()


@pytest.mark.skipif(not os.path.isfile(REAL_PY),
                    reason="needs a real Hermes venv on this box")
def test_profile_home_uses_parent_venv(home):
    """L1: <home>/profiles/NAME must find <home>/hermes-agent/venv (two levels
    up), not stop at <home>/profiles/hermes-agent. Before the fix the lookup
    ended one level short, so profiles always fell back to the raw path."""
    src = os.path.join(home, "hermes-agent")
    os.makedirs(src)
    os.symlink(os.path.join(REAL_VENV, "venv"), src + "/venv")
    for name in ("cron", "agent", "hermes_constants.py", "utils.py"):
        os.symlink(os.path.join(REAL_VENV, name), src + "/" + name)
    pjf = home + "/profiles/work/cron/jobs.json"
    write_jobs(pjf, {"jobs": [{"id": "w1",
                               "schedule": {"kind": "every", "every_s": 60}}]})
    res = run("set", {**P, "profile": "work"}, home)
    assert not res["errors"], res
    assert res["changed"][0].get("via") == "hermes"
    assert len(res["changed"]) == 1  # R1: no double report
    assert "partial" not in res["changed"][0]
    assert "updated_at" in read_jobs(pjf)


def test_timeout_reports_partial_writes(home):
    """H1+T1: a timed-out child that already emitted CA_DONE lines must have
    them reported as changed (partial:true), not crash on unbound `r` or on
    bytes-vs-str. Portable: the shim venv python needs no real Hermes."""
    # venv/bin/python shim: print one CA_DONE for job 't1', then hang so the
    # parent's subprocess timeout fires with partial stdout in the exception.
    shim_dir = os.path.join(home, "hermes-agent", "venv", "bin")
    os.makedirs(shim_dir)
    shim = os.path.join(shim_dir, "python")
    done = json.dumps({"id": "t1", "name": "t1", "old": None, "new": True})
    with open(shim, "w") as f:
        f.write("#!/bin/sh\n"
                "printf 'CA_DONE:%s\\n'\n"
                "exec sleep 30\n" % done.replace("'", "'\\''"))
    os.chmod(shim, 0o755)
    jf = home + "/cron/jobs.json"
    write_jobs(jf, {"jobs": [{"id": "t1",
                              "schedule": {"kind": "every", "every_s": 60}}]})
    # tighten the child timeout so the test doesn't actually wait ~16s
    remote_fast = REMOTE_SRC.replace("timeout=budget + 15", "timeout=budget + 1")
    assert remote_fast != REMOTE_SRC, "timeout replace failed — pattern drifted"
    res = run("set", {**P, "budget_s": 1}, home, remote_src=remote_fast)
    assert res["errors"], res
    assert "超时" in res["errors"][0]
    # the partial write must be surfaced, not swallowed
    assert res["changed"], res
    assert res["changed"][0]["id"] == "t1"
    assert res["changed"][0]["partial"] is True
    assert all("UnboundLocalError" not in e for e in res["errors"])


def test_nonzero_exit_reports_partial_writes(home):
    """M1: a child that exits non-zero may still have written some ids.
    Parse CA_DONE from stdout on the error path too, not only on timeout."""
    shim_dir = os.path.join(home, "hermes-agent", "venv", "bin")
    os.makedirs(shim_dir)
    shim = os.path.join(shim_dir, "python")
    done = json.dumps({"id": "a", "name": "a", "old": None, "new": True})
    with open(shim, "w") as f:
        f.write("#!/bin/sh\n"
                "printf 'CA_DONE:%s\\n'\n"
                "echo 'RuntimeError: cron jobs lock budget exhausted' >&2\n"
                "exit 1\n" % done.replace("'", "'\\''"))
    os.chmod(shim, 0o755)
    jf = home + "/cron/jobs.json"
    write_jobs(jf, {"jobs": [{"id": "a",
                              "schedule": {"kind": "every", "every_s": 60}}]})
    res = run("set", {**P, "budget_s": 30}, home)
    assert res["errors"], res
    assert "fail" in res["errors"][0] or "锁预算" in res["errors"][0]
    # the partial write must be surfaced
    assert res["changed"], res
    assert res["changed"][0]["id"] == "a"
    assert res["changed"][0]["partial"] is True


def test_child_unwritten_and_warnings_propagate(home):
    """L8: child-side unwritten ids (budget exhausted) and warnings must reach
    the parent's result — not be rendered as 'removed' or dropped."""
    shim_dir = os.path.join(home, "hermes-agent", "venv", "bin")
    os.makedirs(shim_dir)
    shim = os.path.join(shim_dir, "python")
    payload = json.dumps({
        "ok": True, "changed": [],
        "skipped": [], "unwritten": ["b"],
        "warnings": ["budget exhausted mid-write"]})
    with open(shim, "w") as f:
        f.write("#!/bin/sh\n"
                "printf 'CA_JSON:%s\\n'\n"
                "exit 0\n" % payload.replace("'", "'\\''"))
    os.chmod(shim, 0o755)
    jf = home + "/cron/jobs.json"
    write_jobs(jf, {"jobs": [{"id": "a",
                              "schedule": {"kind": "every", "every_s": 60}}]})
    res = run("set", {**P, "budget_s": 30}, home)
    # unwritten must surface as an error, not a "removed" skip
    assert res["errors"], res
    assert "未写入" in res["errors"][0] or "锁预算" in res["errors"][0]
    assert any("budget exhausted" in w for w in res["warnings"])
    # nothing should be marked "加锁前已被移除" for an unwritten id
    assert all("已被移除" not in s for s in res["skipped"])


@pytest.mark.skipif(not os.path.isfile(REAL_PY),
                    reason="needs a real Hermes venv on this box")
def test_concurrent_save_jobs_no_lost_jobs(home):
    """Plan step 4: 50 `set` runs racing a background thread that hammers
    Hermes' own save_jobs on the same store. Assert no run errors out and
    no job is lost from the file."""
    src = os.path.join(home, "hermes-agent")
    os.makedirs(src)
    os.symlink(os.path.join(REAL_VENV, "venv"), src + "/venv")
    for name in ("cron", "agent", "hermes_constants.py", "utils.py"):
        os.symlink(os.path.join(REAL_VENV, name), src + "/" + name)
    jf = home + "/cron/jobs.json"
    write_jobs(jf, {"jobs": [
        {"id": "j%d" % i, "schedule": {"kind": "every", "every_s": 60}}
        for i in range(4)]})

    churn_errors = []
    churn_done = threading.Event()

    def churn():
        # real Hermes save_jobs from its own venv, hammering the same file
        script = (
            "import sys, os, time\n"
            "sys.path.insert(0, %r)\n"
            "os.environ['HERMES_HOME'] = %r\n"
            "import cron.jobs as cj\n"
            "t0 = time.time()\n"
            "while time.time() - t0 < 30:\n"
            "    try: cj.save_jobs(cj.load_jobs())\n"
            "    except Exception as e: print('SAVEERR', e)\n"
            "    time.sleep(0.05)\n"
        ) % (REAL_VENV, home)
        r = subprocess.run(
            [os.path.join(REAL_VENV, "venv", "bin", "python"), "-c", script],
            capture_output=True, text=True, timeout=60)
        churn_errors.extend(l for l in r.stdout.splitlines()
                            if l.startswith("SAVEERR"))
        churn_done.set()

    t = threading.Thread(target=churn, daemon=True)
    t.start()
    time.sleep(0.3)  # let the churn get going before the first set

    errs = []
    for i in range(50):
        res = run("set", {**P, "value": (i % 2 == 0)}, home)
        if res["errors"]:
            errs.append((i, res["errors"]))

    churn_done.wait(timeout=45)
    t.join(timeout=10)

    final = read_jobs(jf)
    job_ids = {j.get("id") for j in final.get("jobs", [])}
    assert job_ids == {"j0", "j1", "j2", "j3"}, \
        "jobs lost during concurrent writes: %s" % (job_ids,)
    assert not churn_errors, "background save_jobs errored: %s" % churn_errors[:3]
    assert not errs, "set runs errored: %s" % errs[:3]


def test_hermes_incompatible_falls_back_via_exit_code(home):
    """L4: a venv python exists but can't import the Hermes API → the child
    exits 3 and the parent falls back to the raw writer (not a stderr-text
    match, and not an error)."""
    # a venv whose python is the bare system python3 — cron.jobs won't import,
    # child exits 3, parent must raw-write
    src = os.path.join(home, "hermes-agent", "venv", "bin")
    os.makedirs(src)
    shim = os.path.join(src, "python")
    with open(shim, "w") as f:
        f.write("#!/bin/sh\nexec /usr/bin/python3 \"$@\"\n")
    os.chmod(shim, 0o755)
    jf = home + "/cron/jobs.json"
    write_jobs(jf, {"jobs": [{"id": "f1",
                              "schedule": {"kind": "every", "every_s": 60}}]})
    res = run("set", P, home)
    assert res["changed"], res
    assert any("回落" in w or "raw" in w for w in res.get("warnings", []))
    assert read_jobs(jf)["jobs"][0]["attach_to_session"] is True


# ---------- list mode ----------

def test_list_reports_jobs(home):
    write_jobs(home + "/cron/jobs.json",
               {"jobs": [{"id": "a1", "name": "x", "attach_to_session": True}]})
    res = run("list", {"profile": "default"}, home)
    assert res["homes"][0]["jobs"][0]["attach"] is True
