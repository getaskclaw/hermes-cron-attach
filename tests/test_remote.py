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
    src = remote_src or REMOTE_SRC
    src = src.replace('"/root/.hermes"', '"%s/.hermes"' % FAKE_ROOT)
    src = src.replace('"/home/*/.hermes"', '"%s/.hermes"' % FAKE_HOME_GLOB)
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


# ---------- lock deadline ----------

def test_lock_wait_bounded_by_deadline(home):
    """A held lock must abort before the passed-in deadline, not wait forever."""
    jf = home + "/cron/jobs.json"
    write_jobs(jf, {"jobs": [{"id": "a1"}]})
    lf = open(home + "/cron/.jobs.lock", "a+")
    fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
    try:
        t0 = time.time()
        res = run("set", {**P, "deadline": time.time() + 2}, home)
        assert time.time() - t0 < 6
        assert "lock fail" in res["errors"][0]
    finally:
        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        lf.close()


def test_expired_deadline_skips_immediately(home):
    jf = home + "/cron/jobs.json"
    write_jobs(jf, {"jobs": [{"id": "a1"}]})
    t0 = time.time()
    res = run("set", {**P, "deadline": time.time() - 1}, home)
    assert time.time() - t0 < 1
    assert "deadline" in res["errors"][0] or "lock fail" in res["errors"][0]


def test_second_home_gets_no_fresh_35s(tmp_path):
    """The deadline is global: two locked homes in one run share it, they don't
    get 35s each."""
    for name in ("h1", "h2"):
        write_jobs(str(tmp_path / name / "cron/jobs.json"),
                   {"jobs": [{"id": name}]})
    # point payload at both homes via two profile dirs of one home
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
        t0 = time.time()
        res = run("set", {"value": True, "patterns": [], "all_jobs": True,
                          "force_multi": False, "dry_run": False,
                          "profile": None, "deadline": time.time() + 3}, home)
        el = time.time() - t0
        # without the shared deadline this would take ~70s (35s per home)
        assert el < 10
        assert len(res["errors"]) == 2
    finally:
        for lf in locks:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            lf.close()


# ---------- list mode ----------

def test_list_reports_jobs(home):
    write_jobs(home + "/cron/jobs.json",
               {"jobs": [{"id": "a1", "name": "x", "attach_to_session": True}]})
    res = run("list", {"profile": "default"}, home)
    assert res["homes"][0]["jobs"][0]["attach"] is True
