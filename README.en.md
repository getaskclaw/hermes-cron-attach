# hermes-cron-attach

[简体中文](README.md) · English

Toggle `attach_to_session` on Hermes Agent cron jobs — locally or across a fleet over SSH.

When `attach_to_session` is on, a cron job's report is delivered into a **fresh, continuable Telegram thread** (a DM forum topic) instead of a one-shot broadcast message. Replies in that thread reach the agent with the job's context. This tool just flips the flag; delivery and threading are the Hermes gateway's job.

Note: Telegram threads need the bot's Threaded Mode enabled (BotFather → Bot Settings → Threads Settings). Without it the flag still works, reports just fall back to the plain DM.

## Why

`hermes cron edit` has no `--attach` flag. The only way to change `attach_to_session` on an existing job is editing `cron/jobs.json` — which the live gateway owns. This tool does that edit **safely**: it takes the same `<cron>/.jobs.lock` flock the gateway and CLI use, changes only that one field, and writes back via `mkstemp` + `os.replace` (atomic).

## Install

Single file, stdlib only. Python 3.8+.

```bash
curl -O https://raw.githubusercontent.com/getaskclaw/hermes-cron-attach/main/cron_attach.py
chmod +x cron_attach.py
```

## Usage

```bash
# this machine, no SSH
./cron_attach.py list --host local

# a remote box
./cron_attach.py list --host user@203.0.113.5
./cron_attach.py list --host 203.0.113.5 --user root --port 22 --key ~/.ssh/id_ed25519

# a fleet, from a roster JSON (see "Roster format")
./cron_attach.py list --nodes nodes.json --node all
./cron_attach.py list --nodes nodes.json --node web-1 --node db-2

# flip the flag
./cron_attach.py set --host local --job "daily report" --on --dry-run
./cron_attach.py set --host local --job 68fb7aa1d644 --on
./cron_attach.py set --node web-1 --nodes nodes.json --all-jobs --off
./cron_attach.py set --host local --job backup --on --profile myprofile
```

`--job` accepts an exact job id or a case-insensitive substring of the job name. If it matches more than one job the run aborts and lists candidates (use `--force-multi` to allow). Non-matching homes are reported as `skipped`, not errors — machines with several profiles are normal.

## Roster format

Optional; only needed for `--node`. A JSON object with a `nodes` map:

```json
{
  "nodes": {
    "web-1": {"ip": "203.0.113.5", "ssh": {"user": "root", "port": 22}},
    "db-2":  {"ip": "203.0.113.9", "ssh": {"user": "ubuntu", "port": 60302, "key": "~/.ssh/db_key"}},
    "me":    {"ip": "127.0.0.1",    "ssh": {"local": true}},
    "jumped": {"ip": "10.0.0.7", "ssh": {"command_argv": ["ssh", "bastion", "ssh -o BatchMode=yes 10.0.0.7 'python3 -'"]}}
  }
}
```

`command_argv` is the escape hatch for keyholder jump-chains: its last element is the remote command; the tool appends the mode + a base64-encoded params blob to it.

## How it stays safe

- **Same lock as the gateway.** `set` opens `<cron dir>/.jobs.lock` and `flock(LOCK_EX)` before writing; `list` reads lock-free. The gateway's own read-modify-write sections use the identical lock, so this can't interleave with a scheduler save.
- **Atomic write.** Serialize to a `mkstemp` file in the cron dir, `fsync`, `os.replace` onto `jobs.json`, then `fsync` the directory. A crash mid-write leaves the old file.
- **Ownership and mode preserved.** Before replacing, the temp file is `fchmod`/`fchown`ed to the original's mode and uid/gid — SSHing in as root against a Hermes home under another user can't leave `jobs.json` root-owned 600 and lock the gateway out. `.jobs.lock` is created with the cron dir's owner, never root:root inside a user dir.
- **Symlinks handled.** If `jobs.json` is a symlink the write targets its realpath — the link is never replaced by a regular file.
- **One field only.** Everything else in the job record (schedule, deliver, monitor state, next_run_at, …) is preserved verbatim; a `jobs` map that was a dict is written back keyed by id — schema unchanged.
- **The gateway's shrink-merge can't eat it.** That merge only resurrects job ids *absent* from a save payload; a same-id job's field is decided by whoever holds the lock — which is this tool during its write.
- **Exact profile match.** `--profile` compares the profile segment exactly — `--profile prod` can't hit `prod2`, and a name that happens to match a *username* can't pull in every `home:USER` default home.
- **Global ambiguity check.** A `--job` substring matching jobs across any number of homes refuses as a whole (not one-per-file); `--force-multi` overrides.
- **No-op safe.** Jobs already at the target value are listed in `changed` but don't trigger a rewrite.
- **Home discovery** scans `$HERMES_HOME`, the SSH user's `~/.hermes`, `/root/.hermes`, every `/home/*/.hermes`, and all their `profiles/*`, deduped by jobs.json realpath — because the SSH user and the Hermes user are often different (e.g. root in, Hermes under `/home/deploy`).
- **Per-home failure isolation.** A write failure in one home lands in `errors` and the rest still apply — partially-applied results are reported, not swallowed by a bare traceback.
- **TOFU caveat.** `--host` uses `StrictHostKeyChecking=accept-new`: MITM on *first* connect to a new node is unmitigated; ssh manually once first in sensitive environments.
- **`command_argv` caveat.** Jump-chain args are appended to the remote command's last element — works for `ssh`; other wrappers (e.g. a bare `sudo` exec) must pass trailing args through to `python3 -`.

## What it does NOT do

- **No message sending.** Delivery stays with the Hermes gateway. This is a flag toggler, not a reporter.
- **No session seeding.** The continuable-thread magic (replies routing to an agent with context) is gateway state. Turning the flag on makes Hermes do it; this tool doesn't reimplement it.
- **No remote installs.** The payload streams over SSH stdin (`ssh host 'python3 -'`); besides the `jobs.json` write it only leaves `.jobs.lock` (the lock it shares with the gateway) and self-cleaning temp files.

## License

MIT — see [LICENSE](LICENSE).
