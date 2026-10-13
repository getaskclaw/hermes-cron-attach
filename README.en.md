# hermes-cron-attach

[简体中文](README.md) · English

Toggle `attach_to_session` on Hermes Agent cron jobs — locally or across a fleet over SSH.

When `attach_to_session` is on, a cron job's report is delivered into a **fresh, continuable Telegram thread** (a DM forum topic) instead of a one-shot broadcast message. Replies in that thread reach the agent with the job's context. This tool just flips the flag; delivery and threading are the Hermes gateway's job.

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

- **Same lock as the gateway.** Remote payload opens `<cron dir>/.jobs.lock` and `flock(LOCK_EX)` before reading. The gateway's own read-modify-write sections use the identical lock, so this can't interleave with a scheduler save.
- **Atomic write.** Serialize to a `mkstemp` file in the cron dir, `fsync`, `os.replace` onto `jobs.json`. A crash mid-write leaves the old file.
- **One field only.** Everything else in the job record (schedule, deliver, monitor state, next_run_at, …) is preserved verbatim. Top-level keys of the file (`updated_at`, etc.) are kept too.
- **The gateway's shrink-merge can't eat it.** That merge only resurrects job ids *absent* from a save payload; a same-id job's field is decided by whoever holds the lock — which is this tool during its write.
- **Home discovery** scans `$HERMES_HOME`, the SSH user's `~/.hermes`, `/root/.hermes`, every `/home/*/.hermes`, and all their `profiles/*`, deduped by realpath — because the SSH user and the Hermes user are often different (e.g. root in, Hermes under `/home/deploy`).

## What it does NOT do

- **No message sending.** Delivery stays with the Hermes gateway. This is a flag toggler, not a reporter.
- **No session seeding.** The continuable-thread magic (replies routing to an agent with context) is gateway state. Turning the flag on makes Hermes do it; this tool doesn't reimplement it.
- **No remote installs.** The payload streams over SSH stdin (`ssh host 'python3 -'`), nothing is written to the target filesystem except the jobs.json edit.

## License

MIT — see [LICENSE](LICENSE).
