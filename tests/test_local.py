"""Local-side tests for cron_attach.py — the parts that build ssh argv and
inject the lock budget into params."""

import base64
import importlib.util
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_mod():
    spec = importlib.util.spec_from_file_location(
        "cron_attach", os.path.join(REPO, "cron_attach.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def captured_params(mod, monkeypatch):
    """Stub subprocess.run so we can see the params b64 the CLI would send."""
    seen = {}

    def fake_run(argv, input=None, capture_output=None, timeout=None):
        seen["params"] = json.loads(base64.b64decode(argv[-1]))
        class R:
            returncode = 0
            stdout = b'{"ok": true, "homes": [], "changed": [], "errors": []}'
            stderr = b""
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    return seen


NODE = {"name": "n1", "ip": "127.0.0.1", "ssh": {"local": True}}


def test_set_sends_relative_budget_not_deadline(monkeypatch):
    mod = load_mod()
    seen = captured_params(mod, monkeypatch)
    res = mod.run_remote(NODE, "set", {"value": True})
    p = seen["params"]
    assert "budget_s" in p and "deadline" not in p
    assert abs(p["budget_s"] - (mod.SSH_TIMEOUT - mod.SSH_MARGIN)) < 0.01
    assert res["ok"]


def test_list_sends_no_budget(monkeypatch):
    mod = load_mod()
    seen = captured_params(mod, monkeypatch)
    mod.run_remote(NODE, "list", {"profile": None})
    assert "budget_s" not in seen["params"] and "deadline" not in seen["params"]


def test_tiny_ssh_timeout_refused(monkeypatch):
    """CRON_ATTACH_SSH_TIMEOUT too small to fit margin+5s → refuse locally with
    an actionable error instead of handing the remote a uselessly small budget."""
    mod = load_mod()
    monkeypatch.setattr(mod, "SSH_TIMEOUT", 28)  # margin 25 → budget 3 < 5
    res = mod.run_remote(NODE, "set", {"value": True})
    assert not res["ok"]
    assert "CRON_ATTACH_SSH_TIMEOUT" in res["error"]


def test_boundary_ssh_timeout_ok(monkeypatch):
    mod = load_mod()
    seen = captured_params(mod, monkeypatch)
    monkeypatch.setattr(mod, "SSH_TIMEOUT", 30)  # budget exactly 5 → allowed
    res = mod.run_remote(NODE, "set", {"value": True})
    assert res["ok"] and abs(seen["params"]["budget_s"] - 5.0) < 0.01


# ---------- --all-homes gate ----------

def _args(**kw):
    import argparse
    base = dict(host="local", node=None, nodes=None, user=None, port=22,
                key=None, jump=None, job=None, all_jobs=False, all_homes=False,
                profile=None, on=True, dry_run=False, force_multi=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_all_jobs_without_scope_refused(capsys):
    """--all-jobs with no --profile and no --all-homes would rewrite every
    gateway's jobs.json on a multi-home box — must be refused up front."""
    mod = load_mod()
    rc = mod.cmd_set(_args(all_jobs=True))
    assert rc == 2
    assert "--all-homes" in capsys.readouterr().out


def test_all_jobs_with_all_homes_allowed(monkeypatch):
    mod = load_mod()
    seen = captured_params(mod, monkeypatch)
    rc = mod.cmd_set(_args(all_jobs=True, all_homes=True))
    assert rc == 0 and seen["params"]["all_jobs"]


def test_all_jobs_with_profile_allowed(monkeypatch):
    mod = load_mod()
    seen = captured_params(mod, monkeypatch)
    rc = mod.cmd_set(_args(all_jobs=True, profile="work"))
    assert rc == 0 and seen["params"]["profile"] == "work"
