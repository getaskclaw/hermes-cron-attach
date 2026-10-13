#!/usr/bin/env python3
"""fleet_cron_attach.py — 机队级 cron 作业 attach_to_session 开关.

用途:给任意节点的 Hermes cron 作业开/关 attach_to_session,让作业报告在
Telegram 落到独立可续聊的线程(新会话),而不是一次性广播。

用法:
  list  : 列出节点各 home 的 cron 作业及 attach/deliver 状态
      python3 cron_attach.py list --node all
      python3 cron_attach.py list --node to6429

  set   : 修改 attach_to_session
      python3 cron_attach.py set --node six32 --job 磁盘 --on
      python3 cron_attach.py set --node MyServer-1 --job 68fb7aa1d644 --off
      python3 cron_attach.py set --node to6429 --all-jobs --on --dry-run

选择器:
  --job PATTERN   精确匹配 job id,或大小写不敏感的 name 子串。
                  若匹配多个作业则拒绝执行并列出候选(可加 --force-multi 放行)。
  --all-jobs      选中目标 home 的全部作业。
  --profile NAME  只动某个 profile(default = 默认 home;省略则全部 home)。
  --dry-run       只打印将要改什么,不落盘。

目标指定(二选一):
  --node NAME     名册节点名(可重复;all = 全部)。名册路径见 --nodes / FLEET_NODES。
  --host H        直连:[user@]ip,或 local 指本机(无 SSH)。配合 --user/--port/--key/--jump。

安全说明:
  - 远端脚本在 <cron>/.jobs.lock 上 flock 后读写 jobs.json,与 gateway/CLI 的
    写路径同一把跨进程锁;写采用 tmp+os.replace 原子替换。
  - 只改 attach_to_session 一个字段,其余字段原样保留。
  - hermes cron edit 目前无 --attach 旗标,直接改库是唯一路径;本工具在锁内
    完成读-改-写,不会被 gateway 的 shrink-merge 吃掉同 id 字段(merge 只恢复
    payload 中缺失的 job,同 id 以持锁方 payload 为准)。
  - 名单与 SSH 参数复用 fleet-reachability/nodes.json,和 fleet_hermes.py 一致。
"""

import argparse
import base64
import json
import os
import shlex
import subprocess
import sys
import time

NODES_PATH = os.environ.get("FLEET_NODES", "")

SSH_TIMEOUT = 30

# ---- 远端载荷:枚举 home → 列作业 或 在锁内改 attach_to_session ----
# 通过 argv: python3 - <mode> <params_b64>
# (params 走 base64:command_argv 路径上命令要穿过两层 shell 引号,裸 JSON 会被剥引号)
REMOTE = r'''
import base64, fcntl, json, os, stat, sys, glob, tempfile

mode, params = sys.argv[1], json.loads(base64.b64decode(sys.argv[2]).decode("utf-8"))

def homes():
    # 覆盖三种常见布局: $HERMES_HOME、SSH 用户的 ~/.hermes、以及 root 登录时
    # Hermes 实际跑在别的用户家目录(如 /home/computebox/.hermes)。
    cands = []
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        cands.append(("env", env_home))
    ssh_home = os.path.expanduser("~/.hermes")
    try:
        import pwd
        user_name = pwd.getpwuid(os.stat(os.path.dirname(ssh_home) or "/").st_uid).pw_name
    except Exception:
        user_name = os.environ.get("USER") or os.environ.get("LOGNAME") or "?"
    cands.append(("home:" + user_name, ssh_home))
    for base in ("/root/.hermes",):
        cands.append(("home:root", base))
    for p in sorted(glob.glob("/home/*/.hermes")):
        cands.append(("home:" + p.split("/")[2], p))
    out, seen = [], set()
    def _add(label, jf):
        real = os.path.realpath(jf)
        if real in seen or not os.path.isfile(jf):
            return
        seen.add(real)
        out.append((label, jf))
    for label, d in cands:
        if not os.path.isdir(d):
            continue
        _add(label, os.path.join(d, "cron", "jobs.json"))
        for prof in sorted(glob.glob(d + "/profiles/*")):
            if os.path.isdir(prof):
                _add(label + "|profile:" + os.path.basename(prof),
                     os.path.join(prof, "cron", "jobs.json"))
    return out

def load(jf):
    with open(jf, encoding="utf-8-sig") as f:
        data = json.load(f)
    if isinstance(data, dict):
        j = data.get("jobs")
        if isinstance(j, list):
            return j, "dict"
        if isinstance(j, dict):
            return list(j.values()), "dict"
        return [], "dict"
    return (data if isinstance(data, list) else []), "list"

def match(job, pats):
    jid = str(job.get("id") or "")
    name = str(job.get("name") or "")
    return any(p and (p == jid or p.lower() in name.lower()) for p in pats)

def profile_matches(label, prof):
    """label 形如 home:USER 或 home:USER|profile:NAME;精确匹配 profile 名。
    给了 --profile 就只中 profile 段,不匹 home 部分(避免 'ubuntu' 匹配所有 home:ubuntu*).
    """
    if "|profile:" not in label:
        return False
    return label.rsplit("|profile:", 1)[1] == prof

def preserve(src_path, tmp_fd):
    """把 src 的 mode/owner 拷到 tmp fd 上。owner 拷贝失败在非 root 下正常,
    mode 拷贝是底线。返回告警串列表。"""
    warns = []
    try:
        st = os.stat(src_path)
    except OSError as e:
        return ["stat %s fail %s" % (src_path, e)]
    try:
        os.fchmod(tmp_fd, stat.S_IMODE(st.st_mode))
    except OSError as e:
        warns.append("chmod fail %s" % e)
    try:
        os.fchown(tmp_fd, st.st_uid, st.st_gid)
    except (OSError, AttributeError) as e:
        warns.append("chown fail %s(非属主时正常)" % e)
    return warns

result = {"ok": True, "homes": [], "changed": [], "errors": [], "skipped": [],
          "warnings": []}

# 第一遍:收集所有匹配,先判全局歧义,再动手写。
selected = []  # [(label, jf, jobs, shape, hits)]
for label, jf in homes():
    if params.get("profile") and not profile_matches(label, str(params["profile"])):
        continue
    try:
        jobs, shape = load(jf)
    except Exception as e:
        result["errors"].append("%s: load fail %s" % (label, e))
        continue
    if mode == "list":
        result["homes"].append({"home": label, "jobs_file": jf, "jobs": [
            {"id": j.get("id"), "name": j.get("name"),
             "attach": j.get("attach_to_session"),
             "deliver": j.get("deliver"), "no_agent": j.get("no_agent"),
             "enabled": j.get("enabled", True),
             "next_run_at": j.get("next_run_at")}
            for j in jobs if isinstance(j, dict)]})
        continue
    pats = params.get("patterns") or []
    hits = [j for j in jobs if isinstance(j, dict)
            and (params.get("all_jobs") or match(j, pats))]
    if hits:
        selected.append((label, jf, jobs, shape, hits))
    else:
        result["skipped"].append("%s: 无匹配作业" % label)

if mode == "set" and not params.get("all_jobs") and not params.get("force_multi"):
    total = sum(len(h) for _, _, _, _, h in selected)
    if total > 1:
        detail = ", ".join("%s:%s" % (lb, ",".join(str(j.get("id")) for j in h))
                           for lb, _, _, _, h in selected)
        result["errors"].append(
            "跨 %d 个文件共匹配 %d 个作业,拒绝执行: %s" % (len(selected), total, detail))
        print(json.dumps(result, ensure_ascii=False)); sys.exit(0)

# 第二遍:对 selected 里的每个 home 上锁改写。
for label, jf, jobs, shape, hits in selected:
    cron_dir = os.path.dirname(jf)
    real_jf = os.path.realpath(jf)
    if real_jf != jf:
        result["warnings"].append("%s: jobs.json 是符号链接,写真实路径 %s" % (label, real_jf))
        jf = real_jf
        cron_dir = os.path.dirname(jf)
    lock_path = os.path.join(cron_dir, ".jobs.lock")
    try:
        # 目录属主优先:别让锁文件被建成 root:root 而 gateway 开不了
        try:
            dir_st = os.stat(cron_dir)
            if not os.path.exists(lock_path):
                fd0 = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
                try:
                    os.fchown(fd0, dir_st.st_uid, dir_st.st_gid)
                except (OSError, AttributeError):
                    pass
                os.close(fd0)
        except OSError:
            pass
        lf = open(lock_path, "a+")
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
    except OSError as e:
        result["errors"].append("%s: lock fail %s" % (label, e))
        continue
    try:
        target = params["value"]
        hit_ids = {str(j.get("id")) for j in hits}
        if params.get("dry_run"):
            jobs_now, hits_now = jobs, hits  # 干跑用 pass-1 快照即可
        else:
            # 锁内重读:pass-1 的快照可能已被 gateway 更新过(next_run_at/增删作业)
            try:
                jobs_now, _shape_now = load(jf)
            except Exception as e:
                result["errors"].append("%s: reload fail %s" % (label, e))
                continue  # finally 仍解锁
            hits_now = [j for j in jobs_now if isinstance(j, dict)
                        and str(j.get("id")) in hit_ids]
            gone = hit_ids - {str(j.get("id")) for j in hits_now}
            if gone:
                result["warnings"].append(
                    "%s: %d 个作业在加锁前已被移除: %s" % (label, len(gone), ",".join(sorted(gone))))
            if not hits_now:
                result["skipped"].append("%s: 匹配作业已不存在" % label)
                continue
        to_write = False
        for j in hits_now:
            old = j.get("attach_to_session")
            result["changed"].append({"home": label, "id": j.get("id"),
                                      "name": j.get("name"),
                                      "old": old, "new": target})
            if not params.get("dry_run"):
                if old is not target:
                    j["attach_to_session"] = target
                    to_write = True
        if params.get("dry_run") or not to_write:
            continue  # finally 仍解锁
        payload = jobs_now if shape == "list" else None
        if shape == "dict":
            with open(jf, encoding="utf-8-sig") as f:
                orig = json.load(f)
            if isinstance(orig, dict):
                if isinstance(orig.get("jobs"), dict):
                    # jobs 原本是 dict:按 id 键回写,保持 schema 不变
                    orig["jobs"] = {str(j.get("id")): j for j in jobs_now}
                else:
                    orig["jobs"] = jobs_now
                payload = orig
        fd, tmp = tempfile.mkstemp(dir=cron_dir, prefix=".jobs_", suffix=".tmp")
        try:
            for w in preserve(jf, fd):
                result["warnings"].append("%s: %s" % (label, w))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush(); os.fsync(f.fileno())
            os.replace(tmp, jf)
            try:
                dfd = os.open(cron_dir, os.O_DIRECTORY | os.O_RDONLY)
                os.fsync(dfd); os.close(dfd)
            except OSError:
                pass
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        result["errors"].append("%s: write fail %s" % (label, e))
    finally:
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN); lf.close()
        except Exception:
            pass

print(json.dumps(result, ensure_ascii=False))
'''


def load_nodes(path=None):
    path = path or NODES_PATH
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    nodes = data.get("nodes") if isinstance(data, dict) else None
    if not isinstance(nodes, dict):
        raise SystemExit("名册格式错误:%s 缺 'nodes' 键" % path)
    return nodes


def node_from_host(args):
    """--host 直连:拼一个和名册同构的 node dict。"""
    host = args.host
    if host == "local":
        return {"name": "local", "ip": "127.0.0.1", "ssh": {"local": True}}
    user = args.user
    if "@" in host:
        user, host = host.rsplit("@", 1)
    return {"name": args.host, "ip": host,
            "ssh": {"user": user or "root",
                    "port": args.port,
                    "key": args.key,
                    "jump": args.jump}}


def ssh_argv(node, remote_args):
    ssh = node.get("ssh") or {}
    if ssh.get("local"):
        return ["python3", "-"] + remote_args
    if "command_argv" in ssh:
        argv = list(ssh["command_argv"])
        if remote_args:
            argv[-1] = argv[-1] + " " + " ".join(shlex.quote(a) for a in remote_args)
        return argv
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15"]
    if ssh.get("key"):
        argv += ["-i", os.path.expanduser(ssh["key"])]
    if ssh.get("jump"):
        argv += ["-o", "ProxyJump=" + str(ssh["jump"])]
    if str(ssh.get("port", 22)) != "22":
        argv += ["-p", str(ssh["port"])]
    argv += ["%s@%s" % (ssh.get("user", "root"), node["ip"]),
             " ".join(shlex.quote(a) for a in ["python3", "-"] + remote_args)]
    return argv


def run_remote(node, mode, params):
    pb64 = base64.b64encode(json.dumps(params, ensure_ascii=False).encode()).decode()
    argv = ssh_argv(node, [mode, pb64])
    try:
        done = subprocess.run(argv, input=REMOTE.encode("utf-8"),
                              capture_output=True, timeout=SSH_TIMEOUT)
        out = done.stdout.decode("utf-8", errors="replace")
        err = done.stderr.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ssh 超时 %ds" % SSH_TIMEOUT}
    except Exception as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
    if done.returncode != 0:
        lines = err.strip().splitlines()
        return {"ok": False, "error": "exit=%s %s" % (done.returncode, lines[-1][:160] if lines else "")}
    try:
        return json.loads(out)
    except Exception as exc:
        return {"ok": False, "error": "输出不是 JSON: %s %s" % (exc, out[:120])}


def fmt_attach(v):
    return {True: "on", False: "off"}.get(v, "—")


def cmd_list(args):
    nodes = {} if args.host else load_nodes(args.nodes)
    if args.host:
        nodes[args.host] = node_from_host(args)
    names = list(nodes) if (args.node and "all" in args.node) or args.host else args.node
    rc = 0
    for name in names:
        node = nodes.get(name)
        if not node:
            print("✗ %s: 不在名册" % name); rc = 1; continue
        t0 = time.time()
        res = run_remote(node, "list", {"profile": args.profile})
        if not res.get("ok"):
            print("✗ %s: %s" % (name, res.get("error"))); rc = 1; continue
        took = time.time() - t0
        n_jobs = sum(len(h["jobs"]) for h in res["homes"])
        print("● %s (%.1fs, %d home, %d 作业)" % (name, took, len(res["homes"]), n_jobs))
        for h in res["homes"]:
            for j in h["jobs"]:
                flag = "" if j.get("enabled") else " [paused]"
                print("   %-28s %-12s attach=%-3s deliver=%-24s %s%s" % (
                    h["home"], j["id"], fmt_attach(j["attach"]),
                    str(j["deliver"])[:24], (j["name"] or "")[:36], flag))
        for e in res.get("errors") or []:
            print("   ⚠ %s" % e)
    return rc


def cmd_set(args):
    if args.host:
        node = node_from_host(args)
    else:
        node = load_nodes(args.nodes).get(args.node)
        if not node:
            print("✗ %s: 不在名册" % args.node); return 1
    if not args.all_jobs and not args.job:
        print("✗ set 需要 --job PATTERN 或 --all-jobs"); return 2
    if args.job and not all(args.job):
        print("✗ --job 不能是空串(会匹配所有作业;要全开用 --all-jobs)"); return 2
    params = {"value": args.on, "patterns": args.job or [],
              "all_jobs": args.all_jobs, "force_multi": args.force_multi,
              "dry_run": args.dry_run, "profile": args.profile}
    res = run_remote(node, "set", params)
    if not res.get("ok"):
        print("✗ %s: %s" % (args.node, res.get("error"))); return 1
    tag = "[dry-run] " if args.dry_run else ""
    for c in res["changed"]:
        print("%s%s %s: %s → %s  (%s)" % (
            tag, c["home"], c["id"], fmt_attach(c["old"]),
            fmt_attach(c["new"]), (c["name"] or "")[:40]))
    for e in res.get("skipped") or []:
        print("  · %s" % e)
    for e in res.get("warnings") or []:
        print("  ⚠ %s" % e)
    for e in res.get("errors") or []:
        print("⚠ %s" % e)
    if not res["changed"] and not res.get("errors"):
        print("无改动")
    return 1 if res.get("errors") else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出作业 attach/deliver 状态")
    tgt = p.add_mutually_exclusive_group(required=True)
    tgt.add_argument("--node", action="append",
                     help="名册节点名,可重复;all = 全部")
    tgt.add_argument("--host", help="直连 [user@]ip 或 local;不读名册")
    p.add_argument("--nodes", help="名册 nodes.json 路径(默认 $FLEET_NODES)")
    p.add_argument("--user", help="--host 的 SSH 用户(默认 root)")
    p.add_argument("--port", type=int, default=22, help="--host 的 SSH 端口")
    p.add_argument("--key", help="--host 的私钥路径")
    p.add_argument("--jump", help="--host 的 ProxyJump")
    p.add_argument("--profile", help="只看某个 profile 名")

    p = sub.add_parser("set", help="开关 attach_to_session")
    tgt = p.add_mutually_exclusive_group(required=True)
    tgt.add_argument("--node", help="单个名册节点名")
    tgt.add_argument("--host", help="直连 [user@]ip 或 local;不读名册")
    p.add_argument("--nodes", help="名册 nodes.json 路径(默认 $FLEET_NODES)")
    p.add_argument("--user", help="--host 的 SSH 用户(默认 root)")
    p.add_argument("--port", type=int, default=22, help="--host 的 SSH 端口")
    p.add_argument("--key", help="--host 的私钥路径")
    p.add_argument("--jump", help="--host 的 ProxyJump")
    p.add_argument("--job", action="append", help="job id 精确匹配或 name 子串,可重复")
    p.add_argument("--all-jobs", action="store_true", help="选中该 home 全部作业")
    p.add_argument("--profile", help="只动某个 profile")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", dest="on", action="store_true")
    g.add_argument("--off", dest="on", action="store_false")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force-multi", action="store_true",
                   help="允许 --job 子串匹配到多个作业时仍执行")

    args = ap.parse_args()
    if args.cmd == "list":
        sys.exit(cmd_list(args))
    sys.exit(cmd_set(args))


if __name__ == "__main__":
    main()
