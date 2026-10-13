# Hermes 定时作业线程开关
(English: hermes-cron-attach)

[English](README.en.md) · 简体中文

开关 Hermes Agent cron 作业的 `attach_to_session`,本机或跨机队都行。

开着 = 作业报告落到 Telegram 独立可续聊的线程(新会话);关掉 = 一次性广播。本工具只负责改开关,投递和线程由 Hermes gateway 完成。

注:Telegram 线程要 bot 开 Threaded Mode(BotFather → Bot Settings → Threads Settings)。没开也能用,报告退回普通私聊,只是不在新会话里。

## 为什么需要它

`hermes cron edit` 没有 `--attach` 旗标;Hermes 的 `cronjob` agent 工具能改 `attach_to_session`,但那要给作业一次 agent 回合。本工具直接改 `cron/jobs.json`——首选每个 home 自带 `hermes-agent/venv` 的 `update_job`(via=hermes,和 gateway 同一条写路径),没有 venv 的库回落到远端 flock + `mkstemp`+`os.replace` 原子写。

## 一眼看懂

**开关开和关,区别是什么:**

```plantuml
@startuml
skinparam DefaultFontColor #1f2937
skinparam ArrowColor #5b6b8c
skinparam ArrowFontColor #1f2937
skinparam RectangleBackgroundColor #eef2fb
skinparam RectangleBorderColor #5b6b8c
skinparam RectangleFontColor #1f2937
skinparam NoteBackgroundColor #dfe5fb
skinparam NoteBorderColor #5b6b8c
skinparam NoteFontColor #1f2937
title attach_to_session 决定报告怎么到你手里

start
:cron 作业到点触发;
if (attach_to_session?) then (on)
  :Gateway 建一个\nTelegram 新线程;
  :报告发进该线程;
  note right
    你在里面回复,
    agent 带作业上下文接话
  end note
else (off / 未设)
  :报告一次性发到\n你的普通聊天;
  note right
    回复不会进作业上下文,
    纯广播
  end note
endif
stop
@enduml
```

**一次 `set` 在远端实际做了什么(为什么不会撞坏 gateway):**

```plantuml
@startuml
skinparam DefaultFontColor #1f2937
skinparam ArrowColor #5b6b8c
skinparam ArrowFontColor #1f2937
skinparam ParticipantBackgroundColor #eef2fb
skinparam ParticipantBorderColor #5b6b8c
skinparam ParticipantFontColor #1f2937
skinparam DatabaseBackgroundColor #eef2fb
skinparam DatabaseBorderColor #5b6b8c
skinparam DatabaseFontColor #1f2937
skinparam NoteBackgroundColor #dfe5fb
skinparam NoteBorderColor #5b6b8c
skinparam NoteFontColor #1f2937
autonumber
title cron_attach.py set —— 一次远端改旗标的全过程

actor "你" as you
participant "cron_attach.py\n(本机)" as cli
participant "远端 python3 -" as remote
participant "Hermes gateway\n(活着)" as gw
database "cron/jobs.json" as jf

you -> cli : set --host user@box --job 日报 --on
cli -> remote : ssh 传入整个 payload(stdin)
activate remote
remote -> remote : flock(<cron>/.jobs.lock, LOCK_EX)
note right of remote
  和 gateway 用同一把锁:
  它写的时候我等,我写的时候它等
end note
remote -> jf : 读全部作业
remote -> remote : 只改 attach_to_session
remote -> jf : mkstemp → fsync → os.replace
note right of remote
  先写临时文件再原子改名,
  中途崩了旧文件还在
end note
remote -> remote : 释放锁,输出 JSON 结果
deactivate remote
remote --> cli : {"changed":[...]}
cli --> you : env 68fb…: off → on (每日流量+健康日报)

gw -> jf : 下个 tick 正常读
note over gw, jf
  gateway 只在持锁时读写;
  我的写落在它两次临界区之间
end note
@enduml
```

## 安装

单文件,只用标准库。Python 3.8+。

```bash
curl -O https://raw.githubusercontent.com/getaskclaw/hermes-cron-attach/main/cron_attach.py
chmod +x cron_attach.py
```

## 用法

```bash
./cron_attach.py list --host local                    # 本机,不走 SSH
./cron_attach.py list --host user@1.2.3.4             # 直连远端
./cron_attach.py list --host 1.2.3.4 --user root --port 22 --key ~/.ssh/id_ed25519

./cron_attach.py set --host local --job "日报" --on --dry-run   # 先看不改
./cron_attach.py set --host local --job 68fb7aa1d644 --on
./cron_attach.py set --host user@1.2.3.4 --all-jobs --all-homes --off   # 机队级全量
./cron_attach.py set --host local --all-jobs --profile myprofile --off  # 或收窄到单 profile
./cron_attach.py set --host local --job backup --on --profile myprofile

./cron_attach.py list --nodes nodes.json --node all   # 名册模式
./cron_attach.py set --node web-1 --nodes nodes.json --job 巡检 --on
```

`--job` 传精确 job id 或名字子串(大小写不敏感)。匹配多个默认拒绝并列出候选,`--force-multi` 放行。无匹配的 home 只记 `skipped`,不算错——多 profile 的机器很正常。

`--all-jobs` 在无 `--profile` 时会扫到该机**所有** home(`$HERMES_HOME`、SSH 用户、`/root/.hermes`、`/home/*/.hermes` 及各自 `profiles/*`)——多 gateway 的机器上这是跨库全量写。此时必须二选一:加 `--profile NAME` 收窄,或加 `--all-homes` 显式确认。

## 安全要点

- 写优先走每个 home 自带 `hermes-agent/venv` 的 `update_job`(via=hermes):同一条锁+reload+merge+normalize+原子写路径,`attach_to_session` 之外的字段由 Hermes 自己归一;报告里 `via` 字段区分 `hermes`/`raw`。
- home 里没有 `hermes-agent/venv`(外部工具写的库、精简部署)才回落到远端 flock + 原子写;`--dry-run` 不起子进程,统一走 raw 出计划。
- 远端拿 `<cron>/.jobs.lock` 的排他 flock 才读写,不会和 scheduler 的写交叉;`list` 只读不抢锁。
- 写是 mkstemp + fsync + os.replace + 目录 fsync,中途崩溃留旧文件。
- 写前先 `os.fchmod`+`os.fchown` 把原文件的 mode/owner 拷到临时文件——SSH 是 root 而 Hermes 跑在别人家目录下时,jobs.json 不会被改成 root 600 把 gateway 锁死。
- `.jobs.lock` 不存在时按 cron 目录的属主创建,不抢成 root:root。
- jobs.json 是符号链接时改写真实路径,不会把链接换成实体文件。
- 只改 `attach_to_session`,其余字段与文件顶层键原样保留;jobs 原本是 dict 形态的按 id 键回写,schema 不变。
- gateway 的 shrink-merge 只恢复缺失 id,同 id 字段以持锁方为准,改动不会被吃。
- `--profile` 精确匹配 profile 名,不会因子串误伤别的 profile 或同用户默认 home。
- 跨文件歧义全局判:`--job` 在任一 home 合计命中 >1 个作业就整体拒绝,不是每个文件各放行一个。
- 目标值已是现状就跳过写(`changed` 照列,文件不重写)。
- home 发现扫 `$HERMES_HOME`、SSH 用户 `~/.hermes`、`/root/.hermes`、`/home/*/.hermes` 及各自 `profiles/*`,按 jobs.json 的 realpath 去重——SSH 用户和 Hermes 用户经常不同。
- 远端除 `jobs.json`(必要时`.jobs.lock`、临时文件自清理)外不落盘:payload 走 SSH stdin(`ssh host 'python3 -'`)。
- 远端任一 home 写失败只记进 `errors`,不影响其它 home;局部应用会如实报告。
- `--host` 用 `StrictHostKeyChecking=accept-new`(TOFU);首次连接新节点仍有 MITM 窗口,敏感环境先手动 ssh 一次写进 known_hosts。
- `command_argv` 跳链把参数拼进最后一项远端命令,对 `ssh` 可靠;换别的包装器(如 sudo 直接执行)需确认它会把尾部参数透传给 `python3 -`。

## 名册格式(可选,仅 --node 用)

```json
{"nodes": {
  "web-1": {"ip": "1.2.3.4", "ssh": {"user": "root", "port": 22}},
  "me":    {"ip": "127.0.0.1", "ssh": {"local": true}},
  "jumped": {"ip": "10.0.0.7", "ssh": {"command_argv": ["ssh", "bastion", "ssh -o BatchMode=yes 10.0.0.7 'python3 -'"]}}
}}
```

`command_argv` 是跳链逃生口:最后一项是远端命令,工具往它后面追加 mode + base64 参数。这要求最后一项本身是个「接受尾部参数并透传给 python3 -」的命令——对 `ssh` 成立(ssh 会把多余参数拼进远端命令行),对直接的 `sudo`/`rsh` 包装器要先验证。

## 不做什么

- 不发消息,投递仍是 gateway 的事。
- 不做会话 seeding——续聊是 gateway 侧状态,本工具不复刻。
- 不在远端安装任何东西;除 `jobs.json` 写入外,只在 cron 目录留 `.jobs.lock`(必要的锁文件)和自清理的临时文件。

## 出处

为管理多台 VPS 上的 Hermes 实例而写;`attach_to_session` 字段语义见 Hermes Agent 源码 `cron/scheduler_delivery.py`。

## 许可证

MIT——见 [LICENSE](LICENSE)。
