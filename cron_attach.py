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
  --profile NAME  只动某个 profile。NAME='default' 匹每个 Hermes 实例的默认
                  home(即不带 |profile: 的那个);省略则全部 home。注意这是
                  per-instance 筛选:每个用户的默认 home 都会被选中。
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

# 整个 ssh 调用的上界,含远端所有 home 的排队等锁时间;家多的节点可调大。
SSH_TIMEOUT = int(os.environ.get("CRON_ATTACH_SSH_TIMEOUT", "90"))
# 远端等锁预算 = SSH_TIMEOUT - margin。margin 要盖住 ssh 建连(ConnectTimeout=8,
# 可能还有 ProxyJump 第二跳)、stdin 写 payload、远端解释器启动和 stdout 回程。
SSH_MARGIN = 35.0

# ---- 远端载荷:枚举 home → 列作业 或 在锁内改 attach_to_session ----
# 通过 argv: python3 - <mode> <params_b64>
# (params 走 base64:command_argv 路径上命令要穿过两层 shell 引号,裸 JSON 会被剥引号)
REMOTE = r'''
import base64, fcntl, json, os, stat, sys, glob, tempfile, time, subprocess

# 拿锁上界(秒)。Hermes 自身是 30s(cron/jobs.py::_JOBS_LOCK_TIMEOUT_SECONDS),多等 5s
# 是为了在本进程排队于 gateway 的 30s 临界区之后仍有机会拿到锁。
LOCK_WAIT_SECONDS = 35.0

mode, params = sys.argv[1], json.loads(base64.b64decode(sys.argv[2]).decode("utf-8"))

# 全局等锁预算(秒,由本地侧传入 = SSH_TIMEOUT - margin)。所有 home 的锁等待
# 共用这一份预算:否则 N 个 home 各等 35s,总和可以越过本地 SSH_TIMEOUT——远端
# 在用户已看到超时后仍然落盘。预算用 time.monotonic() 自减,是相对量:本机与
# 远端时钟有多少漂移都不影响,不会出现远端时钟快导致"deadline 已过"误报。
# budget_s 是显式值:0 就是"别等锁",不能用 or 兜底成 35s。
_budget_s = params.get("budget_s")
_budget_end = time.monotonic() + (float(_budget_s) if _budget_s is not None
                                  else LOCK_WAIT_SECONDS)

def lock_budget():
    """本次等锁还剩多少秒。<=0 即不该再等。"""
    return min(LOCK_WAIT_SECONDS, max(0.0, _budget_end - time.monotonic()))

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
            # 外部工具写的 id-keyed map:值里可能没有 "id" 字段。用 map 的键补上,
            # 否则写回时全挤进 "None" 键,只剩最后一个作业(同 Hermes jobs.py 的
            # 拍平逻辑:{**v, "id": v.get("id") or k})。给每条挂 _map_key 记录原键,
            # 写回时按原键重建,避免把外部工具的键名改写成 id 值。
            fixed = []
            for k, v in j.items():
                if not isinstance(v, dict):
                    continue
                fixed.append({**v, "id": v.get("id") or k, "_map_key": k})
            return fixed, "dict"
        return [], "dict"
    return (data if isinstance(data, list) else []), "list"

def match(job, pats):
    jid = str(job.get("id") or "")
    name = str(job.get("name") or "")
    return any(p and (p == jid or p.lower() in name.lower()) for p in pats)

def profile_matches(label, prof):
    """label 形如 home:USER 或 home:USER|profile:NAME。
    - prof=None(未给 --profile):所有 home 都过,不筛选。
    - prof='default':匹所有无 profile 段的 home——即每个 Hermes 实例(每个用户)
      的默认 home。per-instance 语义,不是只匹 SSH 用户自己:在多用户的机上,
      每个 home 的默认库都会中。Hermes profiles 不允许叫 default,不会撞车。
    - prof='NAME':只匹 |profile:NAME 段,不匹 home 部分
      (避免 'ubuntu' 匹配所有 home:ubuntu*)。
    """
    if prof is None:
        return True
    if "|profile:" not in label:
        return prof == "default"
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

# 子进程:用 home 自带的 Hermes venv 走 update_job。与 gateway 同一条
# 锁+reload+merge+normalize+原子写路径;存法以后改了也跟得上。
# 关键:gateway 的 _acquire_flock 超时返回 False 后会只拿进程内锁继续写
# (为不冻 scheduler 的故意兜底,#60703)——对 gateway 合理,对我们就是
# "锁被占还落盘"的洞。子进程把它 monkey-patch 成严格版:拿不到锁抛
# RuntimeError(非 OSError,逃出 _jobs_lock 的 except,什么都不写)。
# BUDGET 由父进程按剩余锁预算传入,子进程不会拖到本地 ssh 已超时之后才写。
_HERMES_SET = r"""
import json, sys, os, time
agent_dir, home, ids, val, budget = sys.argv[1], sys.argv[2], \
    json.loads(sys.argv[3]), json.loads(sys.argv[4]), float(sys.argv[5])
sys.path.insert(0, agent_dir)
os.environ["HERMES_HOME"] = home  # list_jobs 的 executions.db 按 home 定位,
                                  # 不设会摸到 root 的库
try:
    import cron.jobs as cj
    from cron.jobs import use_cron_store, list_jobs, update_job
except (ImportError, AttributeError) as e:
    # 3 = Hermes API 不兼容,父进程据此回落 raw 写(别靠 stderr 文本匹配)
    print("CA_INCOMPAT: %s" % e, file=sys.stderr)
    sys.exit(3)
if not hasattr(cj, "_acquire_flock"):
    print("CA_INCOMPAT: no _acquire_flock", file=sys.stderr)
    sys.exit(3)

deadline = time.monotonic() + budget  # L7: 整个 child 共享这一个截止点

_orig_acquire = cj._acquire_flock
def _strict_flock(fd, timeout):
    # 等锁最多 (deadline - now) 秒;拿不到就抛——绝不落到 gateway 的
    # "无锁照写"兜底。多个 id 的 update_job 各自调锁,但预算只剩一份:
    # 累计等待不得超过 budget。
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("cron jobs lock budget exhausted")
    if _orig_acquire(fd, min(timeout, remaining)) is not True:
        raise RuntimeError("cron jobs lock busy (gateway 持锁或锁泄漏)")
    return True
cj._acquire_flock = _strict_flock

out = {"ok": True, "changed": [], "skipped": [], "warnings": []}
with use_cron_store(home):
    before = {str(j.get("id")): j.get("attach_to_session")
              for j in list_jobs()}
    for i, jid in enumerate(ids):
        if deadline - time.monotonic() <= 0:
            out["skipped"].extend(ids[i:])
            out["warnings"].append("budget exhausted mid-write")
            break
        if before.get(jid) is val:
            continue  # 已是目标值:不动,也不报 changed(update_job 总会写盘)
        job = update_job(jid, {"attach_to_session": val})
        if job is None:
            out["skipped"].append(jid)
            continue
        out["changed"].append({"id": jid, "name": job.get("name"),
                               "old": before.get(jid), "new": val})
        # 每个 id 落盘即报:父进程超时杀掉子进程时,已完成的改动不至于
        # 被整体误报成"什么都没写"(它们实际已经落盘)。
        print("CA_DONE:" + json.dumps(out["changed"][-1], ensure_ascii=False),
              flush=True)
print("CA_JSON:" + json.dumps(out, ensure_ascii=False))
"""

def hermes_python(home_dir):
    """home 自带的 Hermes venv 解释器;profile home 用父 home 的 venv。
    没有就回 None(走 raw 路径)。返回 (python, agent_dir) 或 (None, None)。"""
    # profile home 是 <home>/profiles/NAME:往上两级才是带 venv 的父 home。
    # 非 profile 时 dirname(home_dir) 已经够用,profiles 情形要多上一层。
    cands = [home_dir, os.path.dirname(home_dir)]
    if os.path.basename(os.path.dirname(home_dir)) == "profiles":
        cands.append(os.path.dirname(os.path.dirname(home_dir)))
    for d in cands:
        py = os.path.join(d, "hermes-agent", "venv", "bin", "python")
        if os.path.isfile(py) and os.access(py, os.X_OK):
            return py, os.path.join(d, "hermes-agent")
    return None, None

def set_via_hermes(label, home_dir, jf, ids, val):
    """用 home 自己的 Hermes 改 attach_to_session。返回 True 走了这条路
    (结果已挂进 result);False 回退到 raw 写。"""
    py, agent_dir = hermes_python(home_dir)
    if not py:
        return False
    if not ids:
        return True
    budget = lock_budget()
    if budget <= 0:
        result["errors"].append(
            "%s: lock fail 全局锁预算已耗尽,跳过(本地即将超时)" % label)
        return True
    # 以 jobs.json 属主跑子进程:锁文件/output/executions.db 若在属主
    # 是 gateway 用户时被我们创成 root:root,gateway 就拿不到跨进程锁了。
    try:
        st = os.stat(jf)
        uid, gid = st.st_uid, st.st_gid
    except OSError:
        uid = gid = None
    preexec = None
    if uid is not None and os.geteuid() == 0 and uid != 0:
        def _drop():
            # initgroups 先按属主重置附属组(setgroups 会带上 root 的组),
            # 再 setgid/setuid——顺序不能反,uid 降下来就没权限改组了。
            try:
                import pwd as _pwd
                name = _pwd.getpwuid(uid).pw_name
                os.initgroups(name, gid)
            except Exception:
                try:
                    os.setgroups([])
                except OSError:
                    pass
            os.setgid(gid); os.setuid(uid)
        preexec = _drop
    env = dict(os.environ)
    env["HERMES_HOME"] = home_dir
    try:
        import pwd
        env["HOME"] = pwd.getpwuid(uid).pw_dir if uid is not None else home_dir
    except Exception:
        env["HOME"] = home_dir
    try:
        r = subprocess.run(
            [py, "-c", _HERMES_SET, agent_dir, home_dir, json.dumps(sorted(ids)),
             json.dumps(val), repr(budget)],
            capture_output=True, text=True, env=env, preexec_fn=preexec,
            cwd=home_dir,  # 别在 root 的 /root 起子进程
            timeout=budget + 15)  # budget 等锁 + ~15s 启动/导入/写盘余量
    except subprocess.TimeoutExpired as e:
        # 子进程可能已经把部分 id 写盘:解析 CA_DONE 行,把已落盘的改动补进
        # changed,再报超时——"超时但什么都没写"会把已落盘的改动说没了。
        # e.stdout 在 capture_output 下是 bytes(或 None),别拿 r 变量——
        # subprocess.run 抛异常时 r 根本没赋值,直接用会 UnboundLocalError。
        out_b = e.stdout or b""
        out_s = out_b.decode("utf-8", "replace") if isinstance(out_b, bytes) else out_b
        done_lines = [l for l in out_s.splitlines()
                      if l.startswith("CA_DONE:")]
        for l in done_lines:
            c = json.loads(l[len("CA_DONE:"):])
            c["home"] = label; c["via"] = "hermes"; c["partial"] = True
            result["changed"].append(c)
        result["errors"].append(
            "%s: hermes update_job 超时(%d 个 id 已落盘,见上)" % (label, len(done_lines)))
        return True
    except Exception as e:
        result["errors"].append("%s: hermes venv 调用失败 %s" % (label, e))
        return True
    line = next((l for l in r.stdout.splitlines() if l.startswith("CA_JSON:")), None)
    if r.returncode == 3:
        # 子进程明确报 Hermes API 不兼容 → 回落 raw(别靠 stderr 文本匹配)
        tail = (r.stderr or "").strip().splitlines()
        msg = tail[-1][:160] if tail else "incompatible"
        result["warnings"].append("%s: hermes 版本过旧(%s),回落 raw 写" % (label, msg))
        return False
    if r.returncode != 0 or not line:
        tail = (r.stderr or "").strip().splitlines()
        msg = tail[-1][:160] if tail else "no output"
        result["errors"].append("%s: hermes update_job fail %s" % (label, msg))
        return True
    out = json.loads(line[len("CA_JSON:"):])
    for c in out.get("changed", []):
        c["home"] = label
        c["via"] = "hermes"
        result["changed"].append(c)
    for s in out.get("skipped", []):
        result["skipped"].append("%s: %s 加锁前已被移除" % (label, s))
    return True

# 第一遍:收集所有匹配,先判全局歧义,再动手写。
selected = []  # [(label, jf, jobs, shape, hits)]
for label, jf in homes():
    prof = params.get("profile")
    if not profile_matches(label, str(prof) if prof is not None else None):
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
    home_dir = os.path.dirname(cron_dir)
    if os.path.islink(jf):
        # 仅 jobs.json 条目本身是 symlink 才拒:gateway 锁的是它所在目录的
        # .jobs.lock(未 resolve),我们把锁建到别处就不是同一把。目录级
        # symlink(~/.hermes 链到数据盘等)不影响——锁路径经同一串链接解析到
        # 同一真实 inode。list 本来就不进这段。via=hermes 也一样:update_job
        # 会照 gateway 逻辑锁同目录的 .jobs.lock,我们这里的拒绝一致适用。
        result["errors"].append(
            "%s: jobs.json 是符号链接(→ %s),锁会落到错的位置,跳过。先固化链接再改。"
            % (label, os.path.realpath(jf)))
        continue
    # 优先走 home 自带 Hermes 的 update_job(via=hermes):同一条锁+reload+merge+
    # normalize+原子写路径,不受我们重实现的 map/dict schema 分支影响。没有
    # venv(外部工具写的库、精简部署)才落到下面的 raw 写。dry-run 不走子进程
    # (否则每个 home 一次 import,太贵),由 raw 路径出 pending。
    if not params.get("dry_run"):
        ids = [str(j.get("id")) for j in hits]
        if set_via_hermes(label, home_dir, jf, ids, params["value"]):
            continue
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
        # LOCK_NB 轮询 + 上界(同 Hermes:它自己的锁等待也是非阻塞轮询)。
        # 阻塞式 LOCK_EX 等待本身可被信号打断;真正的坑在于本地 ssh 被杀时
        # 远端收不到任何信号会继续等锁并落盘——用户看到超时重试,旧改动才生效。
        # 等锁预算取 min(35s, 全局 deadline 剩余):N 个 home 串行时总和仍受
        # SSH_TIMEOUT 约束,不会拖到本地超时之后才写。
        budget = lock_budget()
        if budget <= 0:
            raise TimeoutError("全局锁预算已耗尽,跳过(本地即将超时)")
        deadline = time.monotonic() + budget
        while True:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "等锁 %.0fs 超时(gateway 持锁中或锁泄漏)" % budget)
                time.sleep(0.2)
    except (OSError, TimeoutError) as e:
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
        pending = []
        for j in hits_now:
            old = j.get("attach_to_session")
            pending.append({"home": label, "id": j.get("id"),
                            "name": j.get("name"),
                            "old": old, "new": target})
            if not params.get("dry_run"):
                if old is not target:
                    j["attach_to_session"] = target
                    to_write = True
        if params.get("dry_run") or not to_write:
            result["changed"].extend(pending)  # dry-run:报告计划改动;no-op:照列
            continue  # finally 仍解锁
        payload = jobs_now if shape == "list" else None
        if shape == "dict":
            with open(jf, encoding="utf-8-sig") as f:
                orig = json.load(f)
            if isinstance(orig, dict):
                if isinstance(orig.get("jobs"), dict):
                    # jobs 原本是 dict:保持 schema 与原键名。load() 已给每条挂上
                    # _map_key(原 map 键);gateway 新增的作业没有 _map_key,用 id 兜底。
                    # 原文件里非 dict 的值(注释、垃圾)原样保留——Hermes 的 auto-repair
                    # 会丢,但我们不该替它做清理。
                    orig_jobs = orig.get("jobs")
                    rebuilt = {}
                    for j in jobs_now:
                        k = j.pop("_map_key", None) or str(j.get("id"))
                        rebuilt[k] = j
                    for k, v in orig_jobs.items():
                        if not isinstance(v, dict):
                            # 键若跟某个 job 撞了,job 赢——非 dict 值不许覆盖作业。
                            rebuilt.setdefault(k, v)
                    orig["jobs"] = rebuilt
                else:
                    for j in jobs_now:
                        j.pop("_map_key", None)
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
            result["changed"].extend(pending)  # 写成功才算 changed
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
    # 远端等锁预算(秒)= SSH_TIMEOUT - margin。传相对秒数而不是本地 unix 时间戳:
    # 两端时钟漂移与预算无关(远端 monotonic 自减),远端时钟快不会误报、慢不会
    # 透支。margin 盖 ssh 建连/ProxyJump/写 payload/回程,外加 via=hermes 子进程在
    # 预算耗尽后还能跑的 ~15s(启动/导入/写盘余量)——25s 不够,35s 才罩得住。
    if mode == "set":
        budget = SSH_TIMEOUT - SSH_MARGIN
        if budget < 5.0:
            return {"ok": False, "error":
                    "CRON_ATTACH_SSH_TIMEOUT=%ds 太小:set 需要 ≥%ds 才装得下 "
                    "%.0fs 连接/传输开销 + ≥5s 等锁预算"
                    % (SSH_TIMEOUT, int(SSH_MARGIN) + 5, SSH_MARGIN)}
        params = dict(params)
        params["budget_s"] = budget
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
    # --all-jobs 在 profile=None 时会扫到机器上所有 home(root、/home/*、env)。
    # 多 home 的机器上这是跨 gateway 的全量写,要求显式确认范围:
    #   --profile NAME 限定范围,或 --all-homes 声明"我知道会碰所有 home"。
    # 单 home 的机器上 --all-homes 也只是多写一个 flag,不阻塞。
    if args.all_jobs and not args.profile and not args.all_homes:
        print("✗ --all-jobs 会覆盖该机全部 home(/root、/home/* 上所有 gateway)。")
        print("  缩小范围:加 --profile NAME;确认全量:加 --all-homes。")
        return 2
    params = {"value": args.on, "patterns": args.job or [],
              "all_jobs": args.all_jobs, "force_multi": args.force_multi,
              "dry_run": args.dry_run, "profile": args.profile}
    res = run_remote(node, "set", params)
    if not res.get("ok"):
        print("✗ %s: %s" % (node["name"], res.get("error"))); return 1
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
    p.add_argument("--all-homes", action="store_true",
                   help="确认 --all-jobs 要覆盖机器上所有 home(多 gateway 机必须)")
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
