#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zburn —— 熄屏守护：锁屏状态下持续驱动桌面 AI（默认 ZCode）干活，直到额度烧完或手动喊停。

为什么需要它
------------
桌面 AI 应用只会「你发一句它跑一句」。想让它在无人值守时一直跑，会遇到四件事：
  1. 熄屏/锁屏后 SendKeys 会打进密码框（危险），SetForegroundWindow 也一定失败；
  2. 会话跑完就停在那里，不会自己继续；
  3. 应用自己会崩/退出（尤其 Electron 应用被环境变量污染时）；
  4. 网关/后端进程挂了，对话就全断。

zburn 的答案
------------
* 全程走 UI Automation + WM_CHAR（见 scripts/inject-uia.ps1），**不抢焦点、锁屏可用**；
* 定时巡检各会话：正在出字或已排队就跳过，空闲的补一条续派指令；
* 应用没了就按配置拉起来（并清掉会污染 Electron 的环境变量）；
* 网关只「探活→必要时启动」，**绝不重启正在跑的网关**（那会把对话打断）；
* 额度来源可配置（网关接口 / 任意命令），低于阈值自动收工；
* 状态写到 status.json + 心跳文件，另有 watchdog 模式负责把卡死的守护进程拉起来。

命令
----
  zburn.py doctor                 环境自检（应用/无障碍树/网关/额度/锁屏状态）
  zburn.py quota                  读一次额度
  zburn.py status [--follow]      看状态与最近日志
  zburn.py feed [--dry-run]       投喂一轮（不进入循环）
  zburn.py inject --text "..."    直接往输入框投一条（--dry-run 只看控件）
  zburn.py run [--dry-run]        进入守护循环
  zburn.py watchdog               看门狗：heartbeat 超时就把 run 拉起来
  zburn.py probe                  导出目标窗口无障碍树（换应用时用）
  zburn.py plugin install|remove  安装/卸载「自动继续」的 ZCode 插件
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

import agents as agentlib
import zsession

VERSION = "1.0.0"
HERE = pathlib.Path(__file__).resolve().parent
SCRIPTS = HERE / "scripts"
IS_WIN = os.name == "nt"

DEFAULT_CONFIG = {
    "app": {
        "process_name": "ZCode",
        "exe": r"E:\exe\Zcode\ZCode.exe",
        # 必须带这个开关：不带的话 Chromium 只在有辅助技术时才开无障碍树，
        # 锁屏久了会把 a11y 关掉，UIA 就什么都读不到（这是熄屏注入的前提）。
        "start_args": ["--force-renderer-accessibility"],
        # Electron 应用被这些环境变量污染会直接退出（实测 ELECTRON_RUN_AS_NODE 会让 ZCode 秒退）
        "strip_env": ["ELECTRON_RUN_AS_NODE", "ELECTRON_NO_ATTACH_CONSOLE"],
        "start_if_down": True,
    },
    "uia": {
        "send_button_regex": "发送|加入队列|立即|Send",
        "busy_regex": "停止生成|Stop generating",
        "queued_regex": "移除待发送消息|Remove queued",
        # 会话筛选：feed.targets（面板里勾选的精确会话名）优先；正则兜底。
        # 两个都为空 = 不投喂。之前默认 "." 会给每个对话灌指令，实测就是事故。
        "session_regex": "",
        # 同一个客户端开了多个窗口时按标题挑；留空 = 第一个有主窗口的进程
        "window_title_regex": "",
        "max_depth": 40,
        "char_delay_ms": 20,
        # ZCode 本地会话库（给 supervisor 读真实对话；留空 = ~/.zcode/cli/db/db.sqlite）
        "session_db": "",
        "session_work_dir": "",
    },
    "prompt": {
        # 轮换模板，避免每次发一模一样的话被模型"记住"
        # 注意：WM_CHAR 打长了会丢字（实测 400 字符开始翻车），所以模板要短，
        # 长指令应该写进文件让模型自己读；max_chars 是硬上限（0=不限制）。
        "max_chars": 180,
        "templates": [
            "Continue exactly where you left off. Do not summarize, do not ask questions, keep producing output until stopped.",
            "Keep going on the current task. No recap, no stopping, next batch now.",
            "Continue. Stay on the same task, do not stop to explain anything.",
        ],
        "file": "next-prompt.txt",
    },
    "feed": {"max_sessions": 5, "interval_seconds": 120, "targets": [],
             # 一个对话跑满几轮就换新的（README 写的默认 3）；0 = 不换
             "rotate_after_turns": 3,
             # 上下文压缩闸门：被监控会话滚过这个 token 数就先投 compact_command
             # 压回去，而不是继续往里灌 continue（88 万 token 的会话每续一句
             # 都整段历史重发，白烧额度）。0 = 关闭。
             "compact_at_tokens": 300000,
             "compact_command": "/compact"},
    "gateway": {
        "health_url": "http://127.0.0.1:3000/meta",
        "start_command": "",
        "work_dir": "",
        "grace_seconds": 60,
        "keep_alive": False,
    },
    "quota": {
        "provider": "none",           # none | gateway_zcode2api | command
        "url": "http://127.0.0.1:3000/admin/api/accounts",
        "admin_key": "",
        "refresh_url": "http://127.0.0.1:3000/admin/api/accounts/refresh",
        "refresh_body": {"all": True},
        "refresh_every_seconds": 600,
        "stop_below": 0,
        # 额度低于阈值时怎么办：exit=收工（默认）｜wait=原地等额度回来
        "stop_action": "exit",
        "command": "",
    },
    "paths": {"state_dir": ".zburn", "stop_file": "STOP", "bridge_dir": ""},
    # 只在这个时段干活（跨午夜也认：23:00-09:00）。没开=全天。
    "window": {"enabled": False, "start": "23:00", "end": "09:00"},
    # 任务队列：开了就按队列里的题材逐条派活，不再喂「continue」
    # （空指令 = 模型自由发挥 = 跑题，这是连续几晚变文玩展的根因）
    "queue": {"enabled": False, "path": "", "only_group": ""},
    "watchdog": {"heartbeat_stale_seconds": 420, "check_seconds": 30},
    "panel": {"host": "127.0.0.1", "port": 8790, "token": "", "autostart": False},
    # 多 agent：空列表 = 沿用上面 app/uia/feed/prompt 的单应用行为。
    # 有 agents 时，每一项都可以只写差异字段（会与上面四段深合并）。
    "agents": [],
    # 监督编排：supervisor 定时醒来读 worker 的对话尾部，把下一步写进交接文件。
    "orchestrator": {
        "enabled": True,
        "supervisor": "",
        "worker": "",
        "prompt": "",
        "handoff_wait_seconds": 120,
    },
    # 「新建对话」按钮：不同客户端叫法不一样，可覆盖。
    "new_chat": {
        "enabled": True,
        "button_regex": "新建任务|新建对话|新对话|新聊天|新建会话|New chat|New task|New conversation",
        "project": "",
        "send_button_regex": "",
    },
}


# ---------------------------------------------------------------- 小工具
def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def config_candidates() -> list[pathlib.Path]:
    """自动找配置文件的候选位置（按优先级）。

    以前 `--config` 缺省直接落到内置默认值：在仓库目录里 `python zburn.py status`
    会显示「额度: 未配置 / agents=1 default」，看着像配置丢了，其实是根本没读文件。
    现在按「当前目录 → 脚本目录 → 用户配置目录」找，找到就用。
    """
    out = [pathlib.Path("config.json")]
    me = HERE / "config.json"
    if me not in out:
        out.append(me)
    base = os.environ.get("LOCALAPPDATA") or ""
    if base:
        out.append(pathlib.Path(base) / "zburn" / "config.json")
    return out


CONFIG_SOURCE = ""          # 最近一次实际读到配置文件（doctor/status 里显示）
CONFIG_FALLBACK = "<内置默认>"


def resolve_config_path(path: str | None) -> pathlib.Path | None:
    """定下这次要用哪个配置文件：显式参数 > 环境变量 > 候选位置。"""
    explicit = path or os.environ.get("ZBURN_CONFIG") or ""
    if explicit:
        return pathlib.Path(explicit)
    for c in config_candidates():
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def load_config(path: str | None) -> dict:
    global CONFIG_SOURCE
    p = resolve_config_path(path)
    if p is None:
        CONFIG_SOURCE = CONFIG_FALLBACK
        return dict(DEFAULT_CONFIG)
    if not p.exists():
        raise SystemExit(f"配置文件不存在: {p}")
    cfg = deep_merge(DEFAULT_CONFIG, json.loads(p.read_text(encoding="utf-8")))
    try:
        CONFIG_SOURCE = str(p.resolve())
    except OSError:
        CONFIG_SOURCE = str(p)
    return cfg


def config_source() -> str:
    return CONFIG_SOURCE or CONFIG_FALLBACK


def config_path_arg(path: str | None) -> str | None:
    """给面板/看门狗/batch 传的配置路径：显式给的就用它，否则用自动找到的那份。

    没这条，`python zburn.py panel`（不带 --config）启动的面板是只读的：
    能看状态，但「保存参数」会因为没有配置文件而失败。
    """
    p = resolve_config_path(path)
    if not p:
        return None
    try:
        return str(p.resolve())
    except OSError:
        return str(p)


def reload_config(path: str | None, cur: dict) -> dict:
    """守护每轮重读配置（面板改完下一轮就生效）。

    读不出来（面板正在写、文件被手改坏、临时删掉）就沿用上一版：
    参数没生效可以下一轮再试，守护不能因为一次读文件失败就退出。
    """
    p = resolve_config_path(path)
    if p is None or not p.exists():
        return cur
    try:
        return load_config(path)
    except Exception:
        return cur


class State:
    LOG_MAX_BYTES = 2_000_000

    def __init__(self, cfg: dict):
        self.dir = pathlib.Path(cfg["paths"]["state_dir"])
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / "zburn.log"
        self.status_path = self.dir / "status.json"
        self.heartbeat_path = self.dir / "heartbeat"
        self.quota_csv = self.dir / "quota-history.csv"
        self.stop_file = pathlib.Path(cfg["paths"]["stop_file"])

    def log(self, msg: str) -> None:
        line = f"{now()} | {msg}"
        print(line, flush=True)
        try:
            # 一夜跑下来日志能到几十万行，磁盘写满会把整台机器拖死：超过阈值留一份 .1
            if self.log_path.exists() and self.log_path.stat().st_size > self.LOG_MAX_BYTES:
                old = self.log_path.with_name(self.log_path.name + ".1")
                try:
                    old.unlink(missing_ok=True)
                    self.log_path.rename(old)
                except OSError:
                    pass
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def status(self, **kw) -> None:
        cur = {}
        if self.status_path.exists():
            try:
                cur = json.loads(self.status_path.read_text(encoding="utf-8"))
            except Exception:
                cur = {}
        cur.update(kw)
        cur["updated"] = now()
        cur["pid"] = os.getpid()
        self.status_path.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")

    def heartbeat(self) -> None:
        self.heartbeat_path.write_text(str(int(time.time())), encoding="ascii")

    def quota_row(self, remaining: int | None, note: str = "") -> None:
        new = not self.quota_csv.exists()
        try:
            with open(self.quota_csv, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["time", "remaining", "note"])
                w.writerow([now(), "" if remaining is None else remaining, note])
        except OSError:
            pass


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "")


def subprocess_hidden(args: list[str], timeout: int = 180) -> tuple[int, str, str]:
    flags = 0
    if IS_WIN:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        r = subprocess.run(args, capture_output=True, text=True, errors="replace",
                           timeout=timeout, creationflags=flags)
        # pwsh 7 的报错带 ANSI 颜色码，原样写进面板/日志会是乱码
        return r.returncode, strip_ansi(r.stdout or ""), strip_ansi(r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError as e:
        return 127, "", str(e)


def pwsh() -> str:
    for n in ("pwsh", "powershell"):
        p = shutil.which(n)
        if p:
            return p
    return "powershell"


def run_ps(script: str, params: dict, timeout: int = 180) -> tuple[int, str, str]:
    args = [pwsh(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / script)]
    for k, v in params.items():
        if v is True:
            args.append(f"-{k}")
        elif v not in (None, False, ""):
            args += [f"-{k}", str(v)]
    return subprocess_hidden(args, timeout=timeout)


# ---------------------------------------------------------------- 环境探测
def app_running(cfg: dict) -> bool:
    if not IS_WIN:
        return False
    # 进程名可能带空格（如 "DeepSeek Harness"），必须加引号，否则 Get-Process 直接报错
    name = str(cfg["app"]["process_name"]).replace("'", "''")
    code, out, _ = subprocess_hidden(
        [pwsh(), "-NoProfile", "-Command",
         f"(Get-Process -Name '{name}' -ErrorAction SilentlyContinue | "
         f"Where-Object {{ $_.MainWindowHandle -ne 0 }} | Measure-Object).Count"])
    return out.strip().isdigit() and int(out.strip()) > 0


def desktop_locked() -> bool:
    """当前桌面是不是锁屏（含 UAC 安全桌面）。

    不要看 LockApp.exe / LogonUI.exe：Win10/11 上它们开机后常驻，解锁了也不退出
    （实测未锁屏时两个进程都在），按进程判断会一直显示「锁屏」。
    WTSINFOEX 的 SessionFlags 也不可靠：这台机器未锁屏时仍返回 LOCKED。
    直接问「当前输入桌面叫什么」最准：解锁 = Default，锁屏/UAC = Winlogon。
    """
    if not IS_WIN:
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    open_input = user32.OpenInputDesktop
    open_input.restype = wintypes.HANDLE
    open_input.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    get_info = user32.GetUserObjectInformationW
    get_info.restype = wintypes.BOOL
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]

    DESKTOP_READOBJECTS = 0x0001
    UOI_NAME = 2
    h = open_input(0, False, DESKTOP_READOBJECTS)
    if not h:
        # 打不开输入桌面：拿不到名字通常意味着正停在安全桌面（非管理员进程读不到 Winlogon）
        return True
    try:
        buf = ctypes.create_unicode_buffer(256)
        got = wintypes.DWORD(0)
        if not get_info(h, UOI_NAME, ctypes.byref(buf), ctypes.sizeof(buf), ctypes.byref(got)):
            return True
        return buf.value.strip().lower() != "default"
    finally:
        user32.CloseDesktop(h)


def uia_ok(cfg: dict) -> tuple[bool, str]:
    """无障碍树是否可用：锁屏久了 Chromium 会把 a11y 关掉，只有重启应用才能恢复。

    探测失败（空树/输出解析不了）先隔 2 秒重试一次：应用刚启动或正忙着渲染时树可能是空的，
    直接判死会白重启一次应用（重启会打断正在生成的会话），比多等 2 秒代价大得多。
    """
    if not IS_WIN:
        return False, "非 Windows"
    why = ""
    for attempt in range(2):
        if attempt:
            time.sleep(2)
        code, out, err = run_ps("uia-tree.ps1", {
            "ProcessName": cfg["app"]["process_name"], "Filter": "Edit|Button|ListItem", "Json": True,
            "WindowTitleRegex": cfg["uia"].get("window_title_regex", "")})
        if code != 0:
            why = (err or out).strip()[:120] or f"exit {code}"
            continue
        # ConvertTo-Json 前后偶尔会夹别的输出；只取 JSON 数组那一段
        body = out[out.index("["):out.rindex("]") + 1] if "[" in out and "]" in out else out
        try:
            rows = json.loads(body) if body.strip() else []
        except Exception:
            why = "解析无障碍树失败：" + (out or err).strip()[:100]
            continue
        if isinstance(rows, dict):
            rows = [rows]
        if not rows:
            why = "无障碍树为空（0 个控件）"
            continue
        kinds = {r.get("type") for r in rows}
        if "Edit" not in kinds:
            return False, "找不到输入框（无障碍树可能被禁用，重启应用可恢复）"
        return True, f"控件 {len(rows)} 个，含输入框与按钮"
    return False, why or "探测失败"


def start_app(cfg: dict) -> tuple[bool, str]:
    exe = cfg["app"]["exe"]
    if not pathlib.Path(exe).exists():
        return False, f"可执行文件不存在: {exe}"
    if not IS_WIN:
        return False, "只支持 Windows"
    env_note = "|".join(cfg["app"].get("strip_env") or [])
    args = " ".join(cfg["app"].get("start_args") or [])
    cmd = (f"$env:{env_note} = $null; " if env_note else "")
    # 逐个清掉污染变量
    if env_note:
        cmd = "; ".join([f"Remove-Item Env:\\{v} -ErrorAction SilentlyContinue" for v in env_note.split("|")])
        cmd += "; "
    cmd += f"Start-Process -FilePath '{exe}' -ArgumentList '{args}' -WindowStyle Minimized"
    code, out, err = subprocess_hidden([pwsh(), "-NoProfile", "-Command", cmd])
    return code == 0, (err or out).strip()[:160] or "已拉起"


def ensure_gateway(cfg: dict, st: State) -> str:
    g = cfg["gateway"]
    if not g.get("keep_alive") or not g.get("start_command"):
        return "disabled"
    params = {"HealthUrl": g["health_url"], "Command": g["start_command"],
              "GraceSeconds": g.get("grace_seconds", 0)}
    if g.get("work_dir"):
        params["WorkDir"] = g["work_dir"]
    code, out, err = run_ps("ensure-gateway.ps1", params, timeout=300)
    res = (out or err).strip().splitlines()[-1] if (out or err).strip() else f"exit {code}"
    st.log(f"网关：{res}")
    return res


# ---------------------------------------------------------------- 额度
def http_json(url: str, method: str = "GET", body: dict | None = None,
              headers: dict | None = None, timeout: int = 12):
    import urllib.request
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _sum_remaining(obj) -> int | None:
    """在任意 JSON 里找「剩余额度」并求和：认 remaining / balance / quota 这类键。"""
    total = None
    keys = {"remaining", "remain", "balance", "left", "quota_remaining", "surplus"}

    def walk(node):
        nonlocal total
        if isinstance(node, dict):
            for k, v in node.items():
                if k.lower() in keys and isinstance(v, (int, float)):
                    total = (total or 0) + int(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(obj)
    return total


# ---------------------------------------------------------------- 任务队列
def queue_conf(cfg: dict) -> dict:
    return cfg.get("queue") or {}


def queue_path(cfg: dict) -> pathlib.Path | None:
    p = str(queue_conf(cfg).get("path") or "").strip()
    return pathlib.Path(p) if p else None


def queue_done_file(st: State) -> pathlib.Path:
    return st.dir / "queue_done.json"


def queue_load_done(st: State) -> dict:
    try:
        return json.loads(queue_done_file(st).read_text(encoding="utf-8"))
    except Exception:
        return {}


def queue_save_done(st: State, done: dict) -> None:
    try:
        queue_done_file(st).write_text(json.dumps(done, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    except OSError:
        pass


def queue_tasks(cfg: dict) -> list[dict]:
    p = queue_path(cfg)
    if not p or not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    tasks = data.get("tasks") if isinstance(data, dict) else data
    return [t for t in (tasks or []) if isinstance(t, dict) and t.get("id")]


def queue_next(cfg: dict, st: State) -> dict | None:
    """下一个还没派过的任务；队列关掉/读完返回 None（那就回退到普通续派）。"""
    q = queue_conf(cfg)
    if not q.get("enabled"):
        return None
    done = queue_load_done(st)
    only = str(q.get("only_group") or "")
    for t in queue_tasks(cfg):
        if str(t["id"]) in done:
            continue
        if only and only not in str(t.get("group") or ""):
            continue
        return t
    return None


def queue_mark(cfg: dict, st: State, task_id: str, note: str = "") -> None:
    done = queue_load_done(st)
    done[str(task_id)] = note or now()
    queue_save_done(st, done)


def queue_prompt(cfg: dict, st: State, task: dict) -> str:
    """把任务书写进桥目录（纯 ASCII 路径），指令只指路。

    「continue」这种空指令是跑题的根源：模型只能自己挑题材，越挑越像文玩静物。
    队列里每条任务都带题材、要素和验收命令，让它照着改，就不会自由发挥。
    """
    tid = str(task["id"])
    d = agentlib.bridge_dir(cfg) / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{tid}.txt"
    lines = []
    if task.get("group"):
        lines.append(f"# group: {task['group']}")
    lines.append(str(task.get("prompt") or "").strip())
    if task.get("verify"):
        lines.append("")
        lines.append("Verify with: " + str(task["verify"]))
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    text = (f"Read {f} and follow it exactly. "
            f"Reply DONE when effects/{tid}.frag passes the gate.")
    return clamp_prompt(cfg, text, st)


def next_prompt(cfg: dict, st: State, cycle: int, remaining: int | None) -> tuple[str, dict | None]:
    """有队列就派队列任务，没有就退回模板续派。"""
    task = queue_next(cfg, st)
    if task:
        return queue_prompt(cfg, st, task), task
    return build_prompt(cfg, st, cycle, remaining), None


def feed_next(cfg: dict, st: State, cycle: int, remaining: int | None, *,
              only_targets: list[str] | None = None, dry_run: bool = False) -> tuple[int, str, dict | None]:
    """投喂「下一步」：先从队列取任务，队列空了才用模板。成功的队列任务记账。"""
    prompt, task = next_prompt(cfg, st, cycle, remaining)
    if only_targets:
        code, summary = feed_clean(cfg, st, prompt, only_targets=only_targets)
    else:
        code, summary = feed_clean(cfg, st, prompt, dry_run=dry_run)
    if task and code == 0 and not dry_run:
        stats = feed_stats(summary)
        if (stats["done"] or 0) >= 1 or (stats["typed"] or 0) > 0:
            queue_mark(cfg, st, task["id"], f"fed {now()}")
            st.log(f"[queue] 已派 {task['id']}（{task.get('group', '')}）")
    return code, summary, task


# ---------------------------------------------------------------- 允许时段
def _hhmm_minutes(text, default: int) -> int:
    """'23:00' → 1380。写错就退回默认值（宁可跑错时段，也别因为一行配置不干活）。"""
    m = re.match(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*$", str(text or ""))
    if not m:
        return default
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 24 and 0 <= mi < 60):
        return default
    return (h % 24) * 60 + mi


def window_spec(cfg: dict) -> tuple[int, int] | None:
    """(开始分钟, 结束分钟)；没开或写错返回 None（= 不限时段）。"""
    w = cfg.get("window") or {}
    if not w.get("enabled"):
        return None
    return (_hhmm_minutes(w.get("start"), 23 * 60),
            _hhmm_minutes(w.get("end"), 9 * 60))


def in_window(cfg: dict, when: dt.datetime | None = None) -> bool:
    """现在是不是允许干活的时间。支持跨午夜（23:00-09:00）。"""
    spec = window_spec(cfg)
    if spec is None:
        return True
    start, end = spec
    if start == end:
        return True
    t = when or dt.datetime.now()
    cur = t.hour * 60 + t.minute
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def seconds_until_window(cfg: dict, when: dt.datetime | None = None) -> int:
    """离下一次进入时段还有多少秒；已经在时段里就是 0。"""
    spec = window_spec(cfg)
    t = when or dt.datetime.now()
    if spec is None or in_window(cfg, t):
        return 0
    start = spec[0]
    nxt = t.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
    if nxt <= t:
        nxt += dt.timedelta(days=1)
    return int((nxt - t).total_seconds())


def window_desc(cfg: dict) -> str:
    spec = window_spec(cfg)
    if spec is None:
        return "不限时段"
    w = cfg.get("window") or {}
    return f"{w.get('start') or '23:00'}-{w.get('end') or '09:00'}"


def quota_headers(q: dict) -> dict:
    """管理接口的鉴权头：不同版本 zcode2api 认不同的写法，两个都带上最省事。

    v2.6 及更早：x-admin-key: <key>
    v2.7 起：    Authorization: Bearer <key>
    """
    key = (q or {}).get("admin_key") or ""
    if not key:
        return {}
    return {"x-admin-key": key, "Authorization": f"Bearer {key}"}


def read_quota(cfg: dict) -> int | None:
    q = cfg["quota"]
    prov = q.get("provider", "none")
    if prov == "none":
        return None
    if prov == "command":
        cmd = q.get("command") or ""
        if not cmd:
            return None
        code, out, err = subprocess_hidden([pwsh(), "-NoProfile", "-Command", cmd], timeout=120)
        m = re.search(r"(\d[\d,]{3,})", out or "")
        return int(m.group(1).replace(",", "")) if m else None
    if prov == "gateway_zcode2api":
        headers = quota_headers(q)
        try:
            data = http_json(q["url"], headers=headers)
        except Exception:
            return None
        return _sum_remaining(data)
    return None


def refresh_quota(cfg: dict) -> bool:
    q = cfg["quota"]
    if q.get("provider") != "gateway_zcode2api" or not q.get("refresh_url"):
        return False
    headers = quota_headers(q)
    try:
        http_json(q["refresh_url"], method="POST", body=q.get("refresh_body") or {}, headers=headers)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 指令
def build_prompt(cfg: dict, st: State, cycle: int, remaining: int | None) -> str:
    p = cfg["prompt"]
    tpls = p.get("templates") or ["Continue the current task."]
    text = tpls[cycle % len(tpls)]
    text = text.replace("{cycle}", str(cycle))
    text = text.replace("{remaining}", "" if remaining is None else f"{remaining:,}")
    text = text.replace("{time}", now())
    # 只保留 ASCII（WM_CHAR 限制）
    text = "".join(ch for ch in text.replace("\r", " ").replace("\n", " ")
                   if 32 <= ord(ch) <= 126)
    if p.get("file"):
        target = pathlib.Path(p["file"])
        if not target.is_absolute():          # 相对路径落到状态目录，插件才读得到
            target = st.dir / target
        try:
            target.write_text(text, encoding="utf-8")
        except OSError:
            pass
    return text


def feed_targets(cfg: dict) -> list[str]:
    return [str(t).strip() for t in (cfg["feed"].get("targets") or []) if str(t).strip()]


def describe_selection(cfg: dict) -> str:
    """给日志/面板看的「这轮会喂哪些会话」。"""
    targets = feed_targets(cfg)
    if targets:
        shown = "、".join(targets[:3]) + ("…" if len(targets) > 3 else "")
        return f"指定 {len(targets)} 个会话（{shown}）"
    regex = (cfg["uia"].get("session_regex") or "").strip()
    if regex:
        return f"正则 /{regex}/"
    return "未指定（不投喂）"


NO_SELECTION_MSG = "未指定监控对话（监控名单为空且会话正则为空）→ 不投喂，去面板「会话监控」勾一下"


def flush_composer(cfg: dict) -> None:
    """把输入框里的残文「发出去」腾空。

    ZCode / DSH 在 Electron 2.x 上对退格、Ctrl+A、ValuePattern.SetValue('') 全是空操作，
    唯一可靠的办法是 Invoke 发送按钮。注入被截断时会留下半截指令，不清掉后面每次都失败。
    """
    try:
        run_ps("flush-composer.ps1", {"ProcessName": cfg["app"]["process_name"]}, timeout=120)
    except Exception:
        pass


def feed_clean(cfg: dict, st: State, prompt: str, dry_run: bool = False,
               only_targets: list[str] | None = None) -> tuple[int, str]:
    """投喂的稳妥版：先腾空、失败再腾空重投一次。

    「成功」的判据是**这一轮至少有一个会话真的被喂进去了**，而不是「零失败」：
    监控 3 个会话时，其中一个常年 busy/卡住是常态，为它整轮重投等于把同一条指令
    往刚开跑的那个会话里再灌一遍（还会把它的上下文推快一倍）。

    only_targets 给定时只投这几个会话（按标题精确定向，忽略 session_regex）——
    压缩闸门用它把 /compact 只送进超限的会话，健康会话照常续派。
    """
    kw = {} if only_targets is None else {"only_targets": only_targets}
    if dry_run:
        return feed_once(cfg, st, prompt, dry_run=True)
    flush_composer(cfg)
    code, summary = feed_once(cfg, st, prompt, **kw)
    stats = feed_stats(summary)
    if code in (3, 4, 6):                       # 4=指令非法 3=没指定会话 6=结构性问题：重投也一样
        return code, summary
    all_busy = (stats["done"] == 0 and stats["failed_n"] == 0 and (stats["skipped"] or 0) > 0)
    if code == 0 and (all_busy or (stats["done"] or 0) >= 1 or (stats["typed"] or 0) > 0):
        # 全部「跳过（在忙）」也算正常：没有一条失败，只是都在生成，下一轮再来。
        # 这种情况重投只会白等 40 秒，还可能把残留发进正在跑的对话。
        return code, summary
    st.log(f"[feed] 首次未成功（{summary}）→ 腾空输入框后重投")
    flush_composer(cfg)
    time.sleep(3)
    return feed_once(cfg, st, prompt, **kw)


PROMPT_MAX_DEFAULT = 180      # WM_CHAR 打长了会被吞字，实测 400 字符就开始丢


def clamp_prompt(cfg: dict, prompt: str, st=None) -> str:
    """把指令压到能完整打进去的长度（在词边界截断，不留半截单词）。

    prompt.max_chars = 0 表示不限制（自己清楚在做什么时才关）。
    """
    raw = (cfg.get("prompt") or {}).get("max_chars")
    limit = PROMPT_MAX_DEFAULT if raw is None else int(raw)
    if limit <= 0 or len(prompt) <= limit:
        return prompt
    cut = prompt[:limit]
    tail = cut[int(limit * 0.6):]
    if " " in tail:
        cut = cut[: cut.rindex(" ")]
    if st:
        st.log(f"[feed] 指令过长（{len(prompt)}>{limit} 字符），已截到 {len(cut)}："
               f"WM_CHAR 打太长会丢字，长内容应该写进文件让模型自己读")
    return cut


def feed_stats(summary: str) -> dict:
    """从 feed 的摘要行里解析出结构化结果（打进去多少字 / 是否跳过 / 是否失败）。"""
    s = summary or ""
    m = re.search(r"typed=(\d+)", s) or re.search(r"已投喂\s*(\d+)\s*字符", s)
    total = re.search(r"汇总：投喂\s*(\d+)\s*/\s*跳过\s*(\d+)\s*/\s*失败\s*(\d+)", s)
    return {
        "typed": int(m.group(1)) if m else None,
        "busy": "busy=True" in s,
        "fed": "已投喂" in s,
        "failed": "失败" in s or "投喂失败" in s,
        # 三个计数：只要「投喂」≥1 就说明这一轮真的推进了，
        # 哪怕另有一个会话因为忙/卡住失败——不该为它把整轮重投一遍。
        "done": int(total.group(1)) if total else None,
        "skipped": int(total.group(2)) if total else None,
        "failed_n": int(total.group(3)) if total else None,
    }


def feed_once(cfg: dict, st: State, prompt: str, dry_run: bool = False,
              only_targets: list[str] | None = None) -> tuple[int, str]:
    prompt = clamp_prompt(cfg, prompt, st)
    if only_targets is None:
        targets = feed_targets(cfg)
        regex = (cfg["uia"].get("session_regex") or "").strip()
    else:
        # 定向投喂（压缩闸门用）：只认给出来的标题，正则会把别的会话也捞进来
        targets = [str(t).strip() for t in (only_targets or []) if str(t).strip()]
        regex = ""
    if not targets and not regex:
        # 空筛选 = 给每个对话灌指令。宁可不动，也不能乱喂。返回 3 跟「投喂失败」区分开。
        return 3, NO_SELECTION_MSG
    params = {
        "ProcessName": cfg["app"]["process_name"],
        "WindowTitleRegex": cfg["uia"].get("window_title_regex", ""),
        "SessionRegex": regex if only_targets is not None else (regex or "."),
        "Targets": json.dumps(targets, ensure_ascii=False),
        "SendButtonRegex": cfg["uia"]["send_button_regex"],
        "BusyRegex": cfg["uia"]["busy_regex"],
        "QueuedRegex": cfg["uia"]["queued_regex"],
        "MaxSessions": cfg["feed"]["max_sessions"],
        "CharDelayMs": cfg["uia"]["char_delay_ms"],
        "Prompt": prompt,
        "DryRun": dry_run,
        "MaxDepth": cfg["uia"]["max_depth"],
    }
    code, out, err = run_ps("feed-sessions.ps1", params, timeout=600)
    tail = [ln for ln in (out or "").strip().splitlines() if ln.strip()]
    if dry_run:
        for ln in tail:
            st.log("  " + ln)
        # 摘要取「候选会话 N 个」那行，比第一行（目标窗口）有用
        pick = [ln for ln in tail if "候选会话" in ln]
        summary = pick[0] if pick else (tail[0] if tail else (err or "").strip()[:160])
    else:
        summary = tail[-1] if tail else (err or "").strip()[:160]
        for ln in tail[:-1][-6:]:
            st.log("  " + ln)
    return code, summary


def inject_once(cfg: dict, st: State, text: str, no_enter: bool = False, dry_run: bool = False) -> int:
    params = {"Text": text, "ProcessName": cfg["app"]["process_name"],
              "WindowTitleRegex": cfg["uia"].get("window_title_regex", ""),
              "SendButtonRegex": cfg["uia"]["send_button_regex"],
              "NoEnter": no_enter, "DryRun": dry_run,
              "CharDelayMs": cfg["uia"]["char_delay_ms"], "MaxDepth": cfg["uia"]["max_depth"]}
    code, out, err = run_ps("inject-uia.ps1", params, timeout=300)
    st.log((out or "").strip() or (err or "").strip() or f"inject exit {code}")
    return code


# ---------------------------------------------------------------- 会话状态闸门
def compact_gate(cfg: dict, st: State, want: str = "") -> dict | None:
    """被监控会话的真实状态：上下文大小、是否在压缩、实际跑在哪个模型上。

    返回 {"over", "under", "compacting", "offpin", "note"}：
      over     = 上下文 ≥ feed.compact_at_tokens 且不在压缩中（该投 compact_command）
      under    = 上下文还在阈值内的（可以照常续派）
      offpin   = 实际模型不含 model.want 的会话。auto/... 就是这种：网关自己挑桶，
                 额度烧在别的模型上。只读界面上「当前打开那个对话」的模型按钮
                 完全看不见它们 —— 实测 6 个被监控会话里 3 个在 auto/ 上，
                 其中一个滚到 93 万 token 还在被每 2 分钟喂一次 continue。
    返回 None = 读不到（没配筛选 / 会话库打不开 / 名单没匹配上），按原流程投喂。
    """
    feed = cfg.get("feed") or {}
    thr = int(feed.get("compact_at_tokens") or 0)
    targets = feed_targets(cfg)
    regex = (cfg["uia"].get("session_regex") or "").strip()
    if not targets and not regex:
        return None
    try:
        sessions = zsession.session_contexts(cfg, targets=targets, session_regex=regex)
    except Exception:
        sessions = None
    if not sessions:
        st.log("[gate] 会话库读不到或监控名单没匹配上，本轮按原流程投喂")
        return None

    def _offpin(s: dict) -> bool:
        model = str(s.get("model") or "")
        return bool(want and model and want not in model)

    offpin = [s for s in sessions if _offpin(s)]
    pinned = [s for s in sessions if not _offpin(s)]
    # 正在压缩的会话两头都不进：再投 /compact 只会排队，投 continue 会把刚压完的又灌胖
    over = [s for s in pinned if thr > 0 and s["context_tokens"] >= thr and not s["compacting"]]
    compacting = [s for s in pinned if thr > 0 and s["context_tokens"] >= thr and s["compacting"]]
    under = [s for s in pinned if thr <= 0 or s["context_tokens"] < thr]

    def _tag(s: dict) -> str:
        t = s["title"] or s["id"]
        return (f"{t[:30]}{'…' if len(t) > 30 else ''}={s['context_tokens'] // 1000}k"
                + ("/auto" if _offpin(s) else ""))

    bits = []
    if thr > 0:
        bits.append(f"阈值 {thr // 1000}k")
    bits.append(f"超限 [{'、'.join(_tag(s) for s in over) or '无'}]")
    if compacting:
        bits.append(f"压缩中 {len(compacting)}")
    bits.append(f"健康 {len(under)}")
    if offpin:
        bits.append(f"⚠ 不在 {want} 上（额度会烧到 auto 桶）：[{'、'.join(_tag(s) for s in offpin)}]")
    return {"over": over, "under": under, "compacting": compacting,
            "offpin": offpin, "note": " ".join(bits)}


# ---------------------------------------------------------------- 多 agent / 交接
def _read_json_tail(out: str):
    """ps1 的 stdout 可能夹着别的行；只取最外层的 JSON 数组/对象。"""
    body = out or ""
    if "[" in body and "]" in body:
        body = body[body.index("["): body.rindex("]") + 1]
    elif "{" in body and "}" in body:
        body = body[body.index("{"): body.rindex("}") + 1]
    return json.loads(body) if body.strip() else []


def uia_rows(cfg: dict, filter_: str = "Text|ListItem|Paragraph|Document|Window|Button|Edit") -> list[dict]:
    code, out, err = run_ps("uia-tree.ps1", {
        "ProcessName": cfg["app"]["process_name"],
        "WindowTitleRegex": cfg["uia"].get("window_title_regex", ""),
        "Filter": filter_, "Json": True, "MaxDepth": cfg["uia"]["max_depth"]}, timeout=120)
    if code != 0:
        raise RuntimeError((err or out).strip()[:200] or f"uia-tree exit {code}")
    data = _read_json_tail(out)
    if isinstance(data, dict):
        data = [data]
    return [r for r in data if isinstance(r, dict)]


def scan_sessions(cfg: dict) -> tuple[list[dict], str]:
    """扫描侧栏对话（只读），面板和 run 共用。"""
    code, out, err = run_ps("feed-sessions.ps1", {
        "ProcessName": cfg["app"]["process_name"],
        "WindowTitleRegex": cfg["uia"].get("window_title_regex", ""),
        "DryRun": True, "Json": True, "MaxDepth": cfg["uia"]["max_depth"]}, timeout=120)
    try:
        data = _read_json_tail(out)
    except Exception:
        return [], (err or out).strip()[:160] or f"exit {code}"
    if isinstance(data, dict):
        data = [data]
    sessions = [{
        "name": d.get("name", ""), "key": d.get("key") or d.get("name", ""),
        "id": d.get("id") or d.get("key") or d.get("name", ""),
        "ord": d.get("ord", 1), "count": d.get("count", 1),
        "project": d.get("project", ""), "active": bool(d.get("active")),
        "x": d.get("x", 0), "y": d.get("y", 0),
    } for d in data if isinstance(d, dict) and d.get("name")]
    return sessions, f"扫描到 {len(sessions)} 个会话"


def new_chat_once(cfg: dict, st: State, prompt: str, *, project: str = "",
                  dry_run: bool = False, click_only: bool = False,
                  agent_id: str = "", clear_residual: bool = False, force: bool = False,
                  replace_targets: bool = True) -> tuple[int, str]:
    """点「新建任务/新对话」→ 输入首条指令 → 提交。"""
    nc = cfg.get("new_chat") or {}
    if not nc.get("enabled", True):
        return 4, "new_chat 已在配置里关闭"
    if any(ord(ch) > 126 and ord(ch) not in (10, 13, 9) for ch in prompt):
        return 4, "新对话指令只能含 ASCII（WM_CHAR 不能投中文/全角标点）"
    if not agentlib.ascii_only(prompt):
        return 4, "新对话指令没有 ASCII 字符（WM_CHAR 限制）"
    params = {
        "ProcessName": cfg["app"]["process_name"],
        "WindowTitleRegex": cfg["uia"].get("window_title_regex", ""),
        "NewChatButtonRegex": nc.get("button_regex") or agentlib.DEFAULT_NEW_CHAT["button_regex"],
        "ProjectName": project or nc.get("project") or "",
        "Text": prompt,
        "SendButtonRegex": nc.get("send_button_regex") or cfg["uia"]["send_button_regex"],
        "BusyRegex": cfg["uia"]["busy_regex"],
        "QueuedRegex": cfg["uia"]["queued_regex"],
        "ComposerMinWidthRatio": float(nc.get("composer_min_width_ratio", 0.40) or 0.40),
        "CharDelayMs": cfg["uia"]["char_delay_ms"],
        "MaxDepth": cfg["uia"]["max_depth"],
        "DryRun": dry_run,
        "ClickOnly": click_only,
        "ForceOffscreen": bool(nc.get("allow_offscreen", False)),
        "ClearResidual": clear_residual or bool(nc.get("clear_residual", False)),
        "Force": bool(force),
    }
    code, out, err = run_ps("new-chat.ps1", params, timeout=600)
    lines = [ln for ln in (out or "").strip().splitlines() if ln.strip()]
    summary = lines[-1] if lines else ((err or "").strip()[:200] or f"exit {code}")
    for ln in lines[:-1][-4:]:
        st.log("  " + ln)
    if code == 0 and not dry_run and not click_only:
        # 新对话就是「旧的翻篇了」：把上一批的动态监控名单清掉。
        # 不清的话名单只增不减，几轮之后「继续」被喂给一堆早已过时的对话，
        # 每个都在滚上下文，额度照样白烧。
        if replace_targets and agent_id:
            old = agentlib.load_dynamic_targets(st, agent_id)
            if old:
                agentlib.clear_dynamic_targets(st, agent_id)
                st.log(f"[{agent_id}] 已切换对话 → 清空旧监控名单 {len(old)} 个（{old[0][:40]}…）")
        # 监督者的新对话靠 session_regex 匹配（标题就是 seed 那句），
        # 不需要往动态名单里塞 —— 那份名单只有 worker 会读。
        if cfg.get("agent_role") != agentlib.SUPERVISOR:
            capture_active_session(cfg, st, agent_id)
            ok, msg = model_guard(cfg, st, agent_id)
            if ok is False:
                st.log(f"[{agent_id}] 新对话的模型没钉住：{msg}"
                       "（下一轮会再试；解锁桌面能自动切回）")
    return code, summary


def capture_active_session(cfg: dict, st: State, agent_id: str) -> str:
    """新会话发出去后，把它加进动态监控名单，后续「继续」才会喂到它。"""
    if not agent_id:
        return ""
    for attempt in range(3):
        time.sleep(1.5)
        sessions, msg = scan_sessions(cfg)
        active = next((s for s in sessions if s.get("active")), None)
        if active and active.get("id"):
            if agentlib.add_dynamic_target(st, agent_id, active["id"]):
                st.log(f"[{agent_id}] 新对话已加入监控：{active['id']}")
            return active["id"]
    # 侧栏可能还没渲染出新会话；用 ZCode 会话库兜底（它写库比 UI 快）
    try:
        work_dir = cfg["uia"].get("session_work_dir") or ""
        latest = zsession.latest_session(cfg, work_dir)
        if latest and latest.get("title"):
            if agentlib.add_dynamic_target(st, agent_id, latest["title"]):
                st.log(f"[{agent_id}] 新对话已加入监控（会话库）：{latest['title'][:60]}")
            return latest["title"]
    except Exception:
        pass
    st.log(f"[{agent_id}] 新对话已投递，但没扫描到当前会话（侧栏/会话库都没读到）")
    return ""


def snapshot_agent(cfg: dict, st: State, agent: dict | None = None) -> str:
    """导出 worker 的对话尾部，供 supervisor 读。

    优先读 ZCode 自己的会话库（完整、不受滚动/内置浏览器影响）；
    没有配置/没有库时退回 UIA 可见文本（取窗口右侧 30% 以外、当前屏幕内的控件）。
    """
    title = (agent or {}).get("name") or cfg["app"]["process_name"]
    status = {}
    try:
        if st.status_path.exists():
            status = json.loads(st.status_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    logs = []
    try:
        if st.log_path.exists():
            logs = st.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]
    except OSError:
        pass
    header = [
        "# zburn supervisor context",
        f"# agent: {title}",
        f"# captured: {now()}",
        f"# last_feed: {status.get('last_feed', '')}",
        f"# last_feed_summary: {status.get('last_feed_summary', '')}",
        "",
        "## recent zburn log",
        "\n".join(logs),
        "",
    ]

    # 1) ZCode 会话库：真实消息（推荐）
    try:
        db_text = zsession.context(
            cfg, feed_targets(cfg),
            cfg["uia"].get("session_work_dir") or cfg["app"].get("work_dir") or "")
        if db_text:
            return "\n".join(header + ["## worker session (from local db)", db_text, ""])
    except Exception:
        pass

    # 2) UIA 可见文本：通用兜底
    try:
        rows = uia_rows(cfg)
    except Exception as e:
        return "\n".join(header + [f"## worker visible transcript", f"(UIA unavailable: {e})", ""])
    win = None
    for r in rows:
        if r.get("type") == "Window" and (win is None or r.get("w", 0) * r.get("h", 0) >
                                         win.get("w", 0) * win.get("h", 0)):
            win = r
    if not win:
        return "\n".join(header + ["## worker visible transcript", "(no window found)", ""])
    x0 = float(win.get("x", 0)); y0 = float(win.get("y", 0))
    ww = float(win.get("w", 1920)); wh = float(win.get("h", 1080))
    x_min = x0 + ww * 0.30          # 左侧 30% 基本是会话列表/项目树
    y_min = y0 - 120
    y_max = y0 + wh + 120
    pick_types = {"Text", "ListItem", "Paragraph", "Document"}
    items = []
    for r in rows:
        if r.get("type") not in pick_types:
            continue
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        x = float(r.get("x", 0)); y = float(r.get("y", 0))
        if x < x_min or y < y_min or y > y_max:
            continue
        items.append((y, x, name))
    items.sort(key=lambda t: (t[0], t[1]))
    lines: list[str] = []
    last = None
    for _, _, name in items:
        if name == last:
            continue
        last = name
        lines.append(name.replace("\r", " ").replace("\n", " "))
    text = "\n".join(lines[-600:])
    if len(text) > 60000:
        text = text[-60000:]
    header.insert(3, f"# window: {win.get('name', '')} ({int(x0)},{int(y0)} {int(ww)}x{int(wh)})")
    return "\n".join(header + ["## worker visible transcript (oldest -> newest)",
                               text or "(no visible text)", ""])


def supervisor_seed(acfg: dict) -> str:
    """给「专用监督对话」的开场白（也是这个对话的标题）。

    唤醒词会被打进这个对话，所以标题要一眼能认出来；内容只指路，细节在桥里的 README。
    """
    bridge = str(agentlib.bridge_dir(acfg)) if hasattr(agentlib, "bridge_dir") else "zburn-bridge"
    seed = (f"zburn supervisor channel - read {bridge}\\README.txt and reply READY. "
            "You will be woken to judge the worker's batch and write handoff.json.")
    return clamp_prompt(acfg, agentlib.ascii_only(seed))


def wake_fingerprint(cfg: dict, st: State, info: dict | None = None) -> str:
    """worker 有没有新进展：状态 / 上下文尾部 / 待办交接，任何一样变了才值得叫醒。

    没有这个，supervisor 每 10 分钟就被叫起来读一遍巨大上下文、回一句 WAIT，
    一个会话能堆到 158 轮（实测），纯烧钱。
    """
    import hashlib
    h = hashlib.sha1()

    def feed_bytes(p):
        try:
            h.update(pathlib.Path(p).read_bytes())
        except OSError:
            h.update(b"-")

    feed_bytes(st.status_path)
    try:
        bridge = agentlib.bridge_dir(cfg)
    except Exception:
        bridge = None
    if bridge:
        feed_bytes(bridge / "context.txt")
        for name in ("handoff.json", "handoff.txt"):
            h.update(b"1" if (bridge / name).exists() else b"0")
    if info:
        for k in sorted(info):
            if isinstance(info[k], (str, int, float)):
                h.update(str(info[k]).encode("utf-8", "ignore"))
    return h.hexdigest()[:16]


def build_supervisor_prompt(cfg: dict, st: State, sup: dict, work: dict | None) -> str:
    # 完整说明（带三个长路径）写进桥里，真正打进去的唤醒词只指一条路：
    # WM_CHAR 打不了 480 字符，长指令一定会在半路被截断，
    # 而截掉的往往正是「这批过了就 new_chat」这种关键句。
    full = supervisor_full_prompt(cfg, sup, work)
    try:
        bridge = agentlib.bridge_dir(cfg)
        bridge.mkdir(parents=True, exist_ok=True)
        (bridge / "supervisor-task.txt").write_text(full, encoding="utf-8")
    except OSError:
        pass
    prompt = (f"Read {agentlib.bridge_dir(cfg)}\\supervisor-task.txt and follow it exactly. "
              "Reply DONE when finished.")
    p = (sup.get("prompt") or {}).get("file")
    if p:
        target = pathlib.Path(p)
        if not target.is_absolute():
            target = st.dir / target
        try:
            target.write_text(full, encoding="utf-8")
        except OSError:
            pass
    return prompt


def supervisor_full_prompt(cfg: dict, sup: dict, work: dict | None) -> str:
    """完整的监督说明（用内置长模板，不受 orchestrator.prompt 那行短唤醒词影响）。"""
    c2 = json.loads(json.dumps(cfg))
    c2.setdefault("orchestrator", {})["prompt"] = ""
    base = agentlib.supervisor_prompt(c2, sup, work)
    # 队列在跑时，把「当前该做哪条、怎么验收」写进任务书：
    # 监督者要能一眼看出 worker 是在照队列干活，还是在自由发挥跑题。
    q = queue_conf(cfg)
    if q.get("enabled"):
        tasks = queue_tasks(cfg)
        done = {}
        try:
            done = json.loads((pathlib.Path(cfg["paths"]["state_dir"]) /
                               "queue_done.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        nxt = next((t for t in tasks if str(t["id"]) not in done), None)
        qp = queue_path(cfg)
        lines = ["", "## queue",
                 f"queue={qp}  tasks={len(tasks)} dispatched={len(done)}"]
        if nxt:
            lines.append(f"next={nxt['id']} group={nxt.get('group', '')}")
            lines.append(f"verify: python fxgate.py {nxt['id']} --render")
        lines += [
            "RULE: this project makes 修仙/玄幻 effects (功法施放、法器、天象、灵兽、法阵).",
            "Anything that is a static object study (玉佩/香囊/玛瑙/紫砂壶/核舟/棋盘/扇骨"
            "/印章…) is OFF-THEME even if it passes lint.",
            "If the worker produced an off-theme still-life, write a handoff telling it to redo"
            " that stem with a real spell/artifact/sky effect (light beams, shockwave, particles).",
        ]
        base = base.rstrip() + "\n" + "\n".join(lines) + "\n"
    return base


def write_supervisor_bridge(cfg: dict, st: State, sup: dict, work: dict | None,
                            prompt: str, remaining: int | None = None) -> dict:
    worker_view = agentlib.agent_view(cfg, work, st) if work else None
    context = snapshot_agent(worker_view, st, work) if worker_view else "(no worker configured)"
    status = {}
    try:
        if st.status_path.exists():
            status = json.loads(st.status_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    status.update({"remaining": remaining, "worker": (work or {}).get("name", ""),
                   "supervisor": sup.get("name", "")})
    return agentlib.write_bridge(cfg, st, status=status, context=context, prompt=prompt,
                                 worker_name=(work or {}).get("name", ""),
                                 supervisor_name=sup.get("name", ""))


def open_session(cfg: dict, target: str = "") -> tuple[int, str]:
    """把聊天窗口里的一个对话点开（不投喂），让输入框出现。

    应用被强杀/崩溃重启后，ZCode 常常停在「一个对话都没打开」的空状态：侧栏和按钮
    都在，就是没有输入框。uia_ok 是按「有没有输入框」判断健康的，于是会误判成
    「无障碍树被禁用」→ 重启应用 → 还是空状态 → 再重启，一晚上白重启十几次。
    先试着点开一个对话，能点开就不算故障。
    """
    params = {
        "ProcessName": cfg["app"]["process_name"],
        "WindowTitleRegex": cfg["uia"].get("window_title_regex", ""),
        "SessionRegex": cfg["uia"].get("session_regex", "") or ".",
        "Targets": json.dumps([target] if target else feed_targets(cfg), ensure_ascii=False),
        "BusyRegex": cfg["uia"]["busy_regex"],
        "QueuedRegex": cfg["uia"]["queued_regex"],
        "MaxSessions": 1,
        "MaxDepth": cfg["uia"]["max_depth"],
        "OpenOnly": True,
    }
    try:
        code, out, err = run_ps("feed-sessions.ps1", params, timeout=180)
    except Exception as e:                      # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"
    lines = [ln.strip() for ln in (out or "").strip().splitlines() if ln.strip()]
    return code, (lines[-1] if lines else (err or "").strip()[:160] or f"exit {code}")


def repin_sessions(cfg: dict, st: State, agent_id: str, sessions: list[dict],
                   want: str) -> tuple[bool, str]:
    """逐个点开跑错模型的会话、把模型切回 want。全成功才算成功（需要解锁桌面）。"""
    bad: list[str] = []
    for s in sessions:
        title = str(s.get("matched") or s.get("title") or "")
        if not title:
            bad.append("(没有标题)")
            continue
        code, msg = open_session(cfg, target=title)
        if code != 0:
            bad.append(f"{title[:16]}(点不开：{msg[:40]})")
            continue
        ok, m = model_guard(cfg, st, agent_id, auto=True)
        if ok is not True:
            bad.append(f"{title[:16]}({m[:40]})")
    return (not bad), "；".join(bad)


def apply_handoff(cfg: dict, st: State, rec: dict, worker: dict | None = None) -> dict | None:
    target = worker
    if rec.get("agent"):
        target = agentlib.find_agent(cfg, rec["agent"]) or target
    if not target:
        st.log(f"[handoff] 找不到目标 agent（{rec.get('agent') or '未指定'}）→ 忽略")
        return None
    pending = agentlib.set_pending(st, target["id"], rec["action"], rec["prompt"],
                                   project=rec.get("project", ""), source="handoff")
    st.log(f"[handoff] {pending['action']} → {target['id']}：{pending['prompt'][:100]}")
    return pending


def supervise_now(cfg: dict, st: State, *, dry_run: bool = False, wait: int = 0) -> tuple[int, str]:
    """立刻让 supervisor 看一眼并验收；wait>0 时等它写出交接并落到 pending。"""
    sup, work = agentlib.orchestrator(cfg)
    if not sup:
        return 2, "没有配置 supervisor（agents 里 role=supervisor）"
    if not sup.get("enabled", True) and not dry_run:
        return 2, f"supervisor {sup['id']} 已停用：先在面板「多 Agent」卡点「启用」"
    work = work or sup
    prompt = build_supervisor_prompt(cfg, st, sup, work)
    info = write_supervisor_bridge(cfg, st, sup, work, prompt)
    if dry_run:
        return 0, f"监督桥已刷新：{info['dir']}（dry-run，未唤醒）"
    # 走和 run 循环同一条路径：这样「没变化就不叫醒」「没对话就建专用对话」
    # 「跑满轮数就换对话」这些策略对手动唤醒同样生效。
    os.environ["ZBURN_FORCE_WAKE"] = "1"      # 手动触发不被「无变化」挡住
    try:
        code, summary = run_agent_once(cfg, st, sup, cycle=0, remaining=None)
    except ModelViolation as e:
        return 3, f"模型不对且切不回来，已停止：{e}"
    if code != 0:
        return code, f"唤醒 {sup['name']} 失败：{summary}"
    deadline = time.time() + max(0, wait)
    while time.time() < deadline:
        time.sleep(5)
        rec = agentlib.consume_handoff(cfg, st, default_agent=work["id"])
        if rec:
            apply_handoff(cfg, st, rec, work)
            return 0, f"已唤醒并收到交接：{rec['action']} → {rec['prompt'][:80]}"
    return 0, f"已唤醒 {sup['name']}（{summary}）；交接文件：{info['handoff']}"


def run_agent_once(cfg: dict, st: State, spec: dict, cycle: int,
                   remaining: int | None, dry_run: bool = False) -> tuple[int, str]:
    """一个 agent 的一次完整巡检：保活 → a11y → 交接/投喂。"""
    acfg = agentlib.agent_view(cfg, spec, st)
    aid = spec["id"]
    name = spec["name"]

    if acfg["app"].get("start_if_down") and not app_running(acfg):
        ok, why = start_app(acfg)
        st.log(f"[{aid}] 应用不在 → {'已拉起' if ok else '拉起失败'}：{why}")
        time.sleep(10)

    ok_uia, why = uia_ok(acfg)
    if not ok_uia:
        # 「找不到输入框」有一半不是 a11y 挂了，而是应用停在「没打开任何对话」的空状态。
        # 先点开一个对话再看一次，能救回来就不算故障（重启应用会打断正在生成的任务）。
        if "输入框" in str(why) and not dry_run:
            oc, om = open_session(acfg)
            st.log(f"[{aid}] 试着点开一个对话（{om}）")
            if oc == 0:
                ok_uia, why = uia_ok(acfg)
                if ok_uia:
                    agentlib.update_state(st, aid, uia_fail_streak=0, render_fail_streak=0)
            elif oc == 2:
                # 对话点开了、但输入框还没渲染出来：这是冷启动/重绘慢，不是 a11y 挂了。
                # 重启应用没用（重启后同样是空状态，实测 06:41 重启到 06:43 才恢复），
                # 还会打断正在生成的会话。按「渲染中」跳过，连续 6 轮（≈12 分钟）都这样
                # 才把它当真正的故障走重启。
                state = agentlib.read_state(st, aid) or {}
                rstreak = int(state.get("render_fail_streak") or 0) + 1
                agentlib.update_state(st, aid, render_fail_streak=rstreak)
                if rstreak < 6:
                    st.log(f"[{aid}] 对话点开了但输入框还没渲染出来（第 {rstreak}/6 次）"
                           "→ 本轮不重启，下一轮再看")
                    agentlib.update_state(st, aid, last_run=now(), last_action="skip",
                                          last_result=f"render: {om}")
                    return 0, f"skip:render {om}"
                st.log(f"[{aid}] 连续 {rstreak} 轮等不到输入框 → 按无障碍树故障处理")
    if not ok_uia:
        state = agentlib.read_state(st, aid) or {}
        streak = int(state.get("uia_fail_streak") or 0) + 1
        agentlib.update_state(st, aid, uia_fail_streak=streak)
        st.log(f"[{aid}] 无障碍树不可用（{why}），连续第 {streak} 次")
        # 只有连续 3 次（≈3 分钟）都读不到才重启应用：重启会打断正在生成的会话，
        # 单次探测失败往往只是应用在忙 / 我们的探针撞在一起，不值得动手。
        cooldown = float(acfg["app"].get("restart_cooldown_seconds") or 300)
        last_kill = float(state.get("last_app_restart_ts") or 0)
        if (IS_WIN and acfg["app"].get("start_if_down")
                and acfg["app"].get("restart_on_uia_fail", True) and streak >= 3):
            if time.time() - last_kill < cooldown:
                # 刚重启过还是读不到：再重启只会变成「重启 ↔ 读不到」的死循环，
                # 换成等它自己起来（应用刚启动时树常常是空的）
                st.log(f"[{aid}] {int(cooldown)} 秒内刚重启过，先不重启（等应用自己渲染完）")
            else:
                pname = acfg["app"]["process_name"]
                subprocess_hidden([pwsh(), "-NoProfile", "-Command",
                                   f"Stop-Process -Name '{str(pname).replace(chr(39), chr(39) * 2)}' "
                                   f"-Force -ErrorAction SilentlyContinue"])
                time.sleep(3)
                ok, why2 = start_app(acfg)
                st.log(f"[{aid}] 重启应用：{'成功' if ok else '失败'} {why2}")
                agentlib.update_state(st, aid, uia_fail_streak=0,
                                      render_fail_streak=0,
                                      last_app_restart_ts=time.time())
                time.sleep(15)
        agentlib.update_state(st, aid, last_run=now(), last_result=f"uia: {why}")
        return 1, f"uia: {why}"
    agentlib.update_state(st, aid, uia_fail_streak=0, render_fail_streak=0)

    # 1) 有交接：优先执行；新对话是监督者明确要求的「旧的翻篇」，即使当前会话
    #    还在生成也要建（-Force）。不强制的话 busy/queued 会让这条交接无限重试：
    #    实测连试 9 轮都停在「当前会话 busy=True queued=True，不新建对话」。
    pending = agentlib.get_pending(st, aid)
    if pending:
        action = pending["action"]
        if action == "new_chat":
            code, summary = new_chat_once(acfg, st, pending["prompt"],
                                          project=pending.get("project", ""),
                                          dry_run=dry_run, agent_id=aid, force=True)
        else:
            code, summary = feed_clean(acfg, st, pending["prompt"], dry_run=dry_run)
        st.log(f"[{aid}] 执行交接 {action}：{summary}")
        agentlib.update_state(st, aid, last_run=now(), last_action=f"handoff:{action}",
                              last_result=summary,
                              last_feed=now() if code == 0 else "")
        if code == 0 and not dry_run:
            agentlib.clear_pending(st, aid)
        return (0 if code in (0, 3, 6) else 1), summary

    # 2) supervisor：刷新监督桥 → 唤醒它
    if spec["role"] == agentlib.SUPERVISOR and (cfg.get("orchestrator") or {}).get("enabled", True):
        o = cfg.get("orchestrator") or {}
        _, work = agentlib.orchestrator(cfg)
        work = work or spec
        prompt = build_supervisor_prompt(cfg, st, spec, work)
        info = write_supervisor_bridge(cfg, st, spec, work, prompt, remaining)
        stt = agentlib.read_state(st, aid) or {}

        # ① worker 没变化就不要叫醒：否则每个周期都白白喂一次越来越大的上下文
        fp = wake_fingerprint(cfg, st, info)
        if (o.get("skip_when_idle", True) and fp and fp == stt.get("last_wake_fp")
                and not os.environ.get("ZBURN_FORCE_WAKE")):
            st.log(f"[{aid}] 监督：worker 无变化，跳过本轮唤醒")
            agentlib.update_state(st, aid, last_run=now(), last_action="skip",
                                  last_result="worker 无变化，跳过唤醒")
            return 0, "skip:no-change"

        # ② 每小时唤醒次数上限
        cap = int(o.get("max_wakes_per_hour") or 6)
        wakes = [t for t in (stt.get("wake_times") or []) if time.time() - float(t) < 3600]
        if cap > 0 and len(wakes) >= cap:
            st.log(f"[{aid}] 监督：最近一小时已唤醒 {len(wakes)} 次（上限 {cap}），跳过")
            agentlib.update_state(st, aid, wake_times=wakes, last_action="skip",
                                  last_result=f"超过唤醒上限 {cap}/小时")
            return 0, "skip:rate-limit"

        # ③ 监督对话跑太多轮就换一个（长对话只会越滚越大，压缩追不上）
        rotate_after = int(acfg["feed"].get("rotate_after_turns") or 0)
        wakes_on = int(stt.get("wakes_on_target") or 0)
        if rotate_after > 0 and wakes_on >= rotate_after:
            c, s = new_chat_once(acfg, st, supervisor_seed(acfg),
                                  project=acfg["new_chat"].get("project", ""),
                                  agent_id=aid, force=True)
            st.log(f"[{aid}] 监督对话已唤醒 {wakes_on} 轮 → 换新对话（{s}）")
            agentlib.update_state(st, aid, wakes_on_target=0, last_run=now(),
                                  last_action="new_chat", last_result=s)
            return (0 if c == 0 else 1), s

        code, summary = feed_clean(acfg, st, prompt, dry_run=dry_run)
        st.log(f"[{aid}] 监督唤醒：{summary}")
        agentlib.update_state(st, aid, last_run=now(), last_action="supervise",
                              last_result=summary, last_feed=now() if code == 0 else "")
        if code == 0:
            agentlib.update_state(st, aid, last_wake_fp=fp,
                                  wake_times=(wakes + [time.time()])[-20:],
                                  wakes_on_target=wakes_on + 1)
        # 没配目标 / 目标对话不见了（标题改了、被关掉）→ 建一个专用监督对话，
        # 别再往「随便一个会话」里灌（实测会把工作对话刷成 158 轮 WAIT）
        if code == 3 or "侧栏没找到" in str(summary) or "候选会话 0 个" in str(summary):
            st.log(f"[{aid}] 没有可用的监督对话 → 新建专用监督对话")
            c2, s2 = new_chat_once(acfg, st, supervisor_seed(acfg),
                                   project=acfg["new_chat"].get("project", ""),
                                   agent_id=aid, force=True)
            st.log(f"[{aid}] 新建监督对话：{s2}")
            agentlib.update_state(st, aid, wakes_on_target=0, last_result=s2,
                                  last_action="new_chat")
            return (0 if c2 == 0 else 1), s2
        return (0 if code in (0, 3) else 1), summary

    # 3) worker：普通续派（跑够轮数就换新对话，别把上下文喂成天文数字）
    rotate_after = int(acfg["feed"].get("rotate_after_turns") or 0)
    stt = agentlib.read_state(st, aid) or {}
    turns = int(stt.get("turns_on_target") or 0)

    # 3a) 模型守卫：模型不对时宁可这一轮空着，也别继续把额度烧在别的模型上
    m = acfg.get("model") or {}
    want = str(m.get("want") or "").strip()
    if want and not dry_run:
        every = max(1, int(m.get("check_every") or 10))
        if stt.get("model_ok") is False or turns % every == 0:
            ok, mmsg = model_guard(acfg, st, aid)
            if ok is False and m.get("enforce", True):
                if str(m.get("on_mismatch") or "stop").lower() == "skip":
                    st.log(f"[{aid}] 模型不对（{mmsg}）→ 本轮不投喂"
                           "：先把模型钉回去，否则额度会烧到别的模型上")
                    agentlib.update_state(st, aid, last_run=now(), last_action="skip",
                                          last_result=f"model: {mmsg}")
                    return 0, f"skip:model {mmsg}"
                # 默认：直接停。切过了（model_guard 里 auto_pin 已经试过），切不回来就别继续烧
                raise ModelViolation(f"[{aid}] {mmsg}")

    if rotate_after > 0 and turns >= rotate_after:
        seed = pathlib.Path(acfg["prompt"].get("file") or "next-prompt.txt")
        if not seed.is_absolute():
            seed = st.dir / seed
        text = ""
        try:
            text = seed.read_text(encoding="utf-8").strip().splitlines()[0]
        except OSError:
            pass
        text = clamp_prompt(acfg, text or "continue", st)
        code, summary = new_chat_once(acfg, st, text,
                                      project=acfg["new_chat"].get("project", ""),
                                      agent_id=aid, force=True)
        if code == 0:
            st.log(f"[{aid}] 已跑满 {turns} 轮 → 换新对话（{summary}）")
            agentlib.update_state(st, aid, turns_on_target=0, last_run=now(),
                                  last_action="new_chat", last_result=summary,
                                  last_feed=now())
            return 0, summary
        # 换不动（「新建任务」滚出可视区等）不能原地打转：旧会话每多一轮更大，
        # 落到下面的压缩闸门/续派继续推进，下一轮还会再试换新。
        st.log(f"[{aid}] 已跑满 {turns} 轮但换新对话失败（{summary}）→ 本轮改走压缩/续派")

    # 3b) 会话闸门：按会话看真实状态（上下文 / 是否在压缩 / 实际模型）。
    #     上下文：超限会话每续一句都要把几十万 token 的历史整段重发（实测 88 万
    #     token 的会话一句 continue ≈ 88 万 input token），轮换兜不住的老会话就是
    #     在这把额度烧穿的。
    #     模型：ZCode 的模型跟会话走，新对话默认落 `auto/...`，而 model_guard 只读
    #     界面上「当前打开那个对话」的按钮 —— 后台会话跑在 auto 上烧额度它看不见。
    #     所以这里按会话库里的实际 model_id 再挡一道（model.enforce 控制是否停喂）。
    #     读不到会话库时闸门返回 None，一切照旧。
    gate = compact_gate(acfg, st, want)
    if gate is not None:
        offpin, under, note = gate["offpin"], gate["under"], gate["note"]
        over = gate["over"]
        enforce = bool((acfg.get("model") or {}).get("enforce", True))
        if offpin and enforce:
            on_mismatch = str((acfg.get("model") or {}).get("on_mismatch") or "stop").lower()
            st.log(f"[{aid}] ⚠ {len(offpin)} 个会话不在 '{want}' 上：{note}")
            fixed, detail = False, ""
            if not dry_run:
                fixed, detail = repin_sessions(acfg, st, aid, offpin, want)
                st.log(f"[{aid}] 试着把它们切回 '{want}'："
                       + ("全部成功" if fixed else f"仍有失败（{detail}）"))
                if fixed:
                    gate = compact_gate(acfg, st, want) or gate
                    offpin, under, over = gate["offpin"], gate["under"], gate["over"]
            if not fixed:
                if on_mismatch == "skip":
                    agentlib.update_state(st, aid, last_run=now(), last_action="skip",
                                          last_result=f"skip:model {note}")
                    if not under and not over:
                        return 0, f"skip:model {note}"
                else:
                    # 默认：直接停。切过了，切不动就不烧了
                    raise ModelViolation(f"[{aid}] {len(offpin)} 个会话不在 '{want}' 上：{note}")
        elif offpin:
            st.log(f"[{aid}] ⚠ 会话不在 '{want}' 上（enforce=false，照喂）：{note}")

        if not over and not (offpin and enforce and under):
            pass                                   # 没有要压的：闸门只看不动
        elif dry_run:
            st.log(f"[{aid}] 会话闸门（dry-run）：{note}")
        else:
            cmd = str(acfg["feed"].get("compact_command") or "/compact").strip() or "/compact"
            for sess in over[:2]:                  # 一轮最多点两个：投喂是逐字打的
                c, s = feed_clean(acfg, st, cmd,
                                  only_targets=[sess.get("matched") or sess["title"]])
                st.log(f"[{aid}] 上下文 {sess['context_tokens']:,} ≥ 阈值 → 投 {cmd}：{s}")
            if over:
                agentlib.update_state(st, aid, last_run=now(), last_action="compact",
                                      last_result=note)
            if not under:
                # 全都超限/在压缩/跑错模型：这轮不再喂 continue，等压缩落地，
                # 下轮上下文掉回阈值内就自动恢复续派
                return 0, f"skip:compact {note}"
            prompt, _task = next_prompt(acfg, st, cycle, remaining)
            code, summary = feed_clean(acfg, st, prompt,
                                       only_targets=[s.get("matched") or s["title"]
                                                     for s in under])
            st.log(f"[{aid}] 第 {cycle} 轮投喂（仅健康会话，超限/跑错模型的先压着）：{summary}")
            if _task and code == 0:
                queue_mark(acfg, st, _task["id"], f"fed {now()}")
                st.log(f"[queue] 已派 {_task['id']}（{_task.get('group', '')}）")
            stats = feed_stats(summary)
            fed = code == 0 and not stats["failed"] and not stats["busy"]
            agentlib.update_state(st, aid, last_run=now(), last_action="feed",
                                  last_result=summary, last_typed=stats["typed"],
                                  turns_on_target=(turns + 1) if fed else turns,
                                  last_feed=now() if fed else "")
            return (0 if code in (0, 3) else 1), summary

    prompt, _task = next_prompt(acfg, st, cycle, remaining)
    code, summary = feed_clean(acfg, st, prompt, dry_run=dry_run)
    st.log(f"[{aid}] 第 {cycle} 轮投喂：{summary}")
    if _task and code == 0 and not dry_run:
        queue_mark(acfg, st, _task["id"], f"fed {now()}")
        st.log(f"[queue] 已派 {_task['id']}（{_task.get('group', '')}）")
    stats = feed_stats(summary)
    fed = code == 0 and not stats["failed"] and not stats["busy"]
    agentlib.update_state(st, aid, last_run=now(), last_action="feed",
                          last_result=summary,
                          last_typed=stats["typed"],
                          turns_on_target=(turns + 1) if fed else turns,
                          last_feed=now() if fed else "")
    if code == 3:
        st.log(f"[{aid}] 未指定监控对话：面板「会话监控」勾选或配置 feed.targets 后下一轮开始")
    return (0 if code in (0, 3) else 1), summary



def lan_ip() -> str:
    """取本机局域网 IP（手机同网段访问用）。"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def panel_token(cfg: dict, st: State, host: str) -> str:
    """远程监听时必须给 token；没配就自动生成一个并存到状态目录，下次复用。"""
    given = (cfg.get("panel") or {}).get("token") or ""
    if given:
        return given
    if host in ("127.0.0.1", "localhost", "::1"):
        return ""
    f = st.dir / "panel.token"
    if f.exists():
        t = f.read_text().strip()
        if t:
            return t
    import secrets
    t = secrets.token_urlsafe(12)
    f.write_text(t)
    return t


def open_browser(url: str) -> None:
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        pass


def print_endpoints(host: str, port: int, token: str) -> None:
    local = f"http://127.0.0.1:{port}/" + (f"?token={token}" if token else "")
    print(f"  本机:   {local}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        ip = lan_ip()
        print(f"  局域网: http://{ip}:{port}/" + (f"?token={token}" if token else ""))
        print(f"  手机:   连同一个 Wi-Fi，直接开上面的局域网地址")


# ---------------------------------------------------------------- 单调度器锁
SCHED_LOCK = pathlib.Path(os.environ.get("TEMP") or ".") / "zburn-scheduler.lock"


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                           text=True, errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return str(pid) in (r.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_scheduler(tag: str) -> bool:
    """同一时刻只允许一个调度器操作 UIA：run / autopilot / batch 互斥。

    它们都要「点侧栏切对话 → 打字」，两个同时跑会把字打进别人的对话里。
    autopilot 派生的分片用 ZBURN_SKIP_SCHED_LOCK=1 跳过这把锁 —— 父进程已经拿着了，
    分片之间靠 batch.py 里的「投喂锁」串行打字。
    """
    if os.environ.get("ZBURN_SKIP_SCHED_LOCK") == "1":
        return True
    try:
        if SCHED_LOCK.exists():
            parts = SCHED_LOCK.read_text(errors="replace").strip().split()
            pid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            if _pid_alive(pid):
                print(f"已有调度器在跑（{' '.join(parts)}）—— 同一时间只能有一个，")
                print("要换它就先停掉（面板「停止」，或删掉 "
                      f"{SCHED_LOCK}）。")
                return False
            SCHED_LOCK.unlink(missing_ok=True)
        SCHED_LOCK.write_text(f"{tag} {os.getpid()}")
        import atexit
        atexit.register(lambda: SCHED_LOCK.unlink(missing_ok=True))
        return True
    except OSError:
        return True                      # 锁文件本身出问题不阻塞干活


# ---------------------------------------------------------------- 子命令
def cmd_doctor(a) -> int:
    cfg = load_config(a.config)
    st = State(cfg)
    src = config_source()
    print(f"zburn {VERSION}  配置: {src}")
    if src == CONFIG_FALLBACK:
        print("     ↑ 没找到 config.json（会在当前目录/脚本目录找）。"
              "用内置默认值跑会没有 agents、没有额度源 —— 用 --config 指一份，"
              "或把 config.example.json 复制成当前目录的 config.json。")
    print(f"平台: {sys.version.split()[0]} / {'Windows' if IS_WIN else os.name}")
    print(f"状态目录: {st.dir.resolve()}")

    ok_app = app_running(cfg)
    print(f"[{'OK ' if ok_app else 'FAIL'}] 应用 {cfg['app']['process_name']} 在运行且有主窗口")
    if ok_app:
        ok_uia, why = uia_ok(cfg)
        print(f"[{'OK ' if ok_uia else 'FAIL'}] 无障碍树可用：{why}")
        if not ok_uia:
            print("     树是空的 → 应用启动时没带 --force-renderer-accessibility。")
            print(f"     关掉 {cfg['app']['process_name']} 后用 zburn 拉起（会带上这个开关）：")
            print("       python zburn.py restart-app --config config.json")
        print(f"[{'锁屏' if desktop_locked() else '解锁'}] 当前桌面状态（锁屏走 UIA 路径，本来就没问题）")

    if cfg.get("agents"):
        print("\nAgents:")
        for s in agentlib.agent_specs(cfg):
            acfg = agentlib.agent_view(cfg, s, st)
            on = app_running(acfg)
            print(f"  [{'OK ' if on else '-- '}] {s['id']:14} {s['role']:10} "
                  f"{acfg['app']['process_name']:20} "
                  f"{'在运行' if on else '未运行'}  interval={acfg['feed']['interval_seconds']}s")
        sup, work = agentlib.orchestrator(cfg)
        print(f"  监督编排: supervisor={sup['id'] if sup else '-'}  worker={work['id'] if work else '-'}"
              f"  bridge={agentlib.bridge_dir(cfg)}")

    g = cfg["gateway"]
    try:
        http_json(g["health_url"], timeout=5)
        print(f"[OK ] 网关可访问：{g['health_url']}")
    except Exception as e:
        print(f"[-- ] 网关未就绪：{g['health_url']}（{type(e).__name__}）"
              + ("，已配置自动拉起" if g.get("keep_alive") else "，未开启自动拉起"))

    remaining = read_quota(cfg)
    prov = cfg["quota"]["provider"]
    if prov == "none":
        print("[--] 额度来源：未配置（不会因为额度停）")
    else:
        print(f"[{'OK ' if remaining is not None else 'FAIL'}] 额度来源 {prov}："
              + (f"剩余 {remaining:,}" if remaining is not None else "读取失败"))
        if remaining is not None and cfg["quota"].get("stop_below"):
            print(f"     低于 {cfg['quota']['stop_below']:,} 时自动收工")
            if remaining <= cfg["quota"]["stop_below"]:
                print("     ⚠ 当前额度已经低于阈值：现在启动 run 会立刻收工。"
                      "换号/加额度后先跑一次 zburn accounts 确认")

    tpls = cfg["prompt"]["templates"]
    ascii_bad = [t for t in tpls if any(ord(c) > 126 for c in t)]
    print(f"[{'OK ' if not ascii_bad else 'WARN'}] 提示模板 {len(tpls)} 条"
          + ("" if not ascii_bad else "，含非 ASCII 会被丢弃（WM_CHAR 限制）"))
    long_tpl = [t for t in tpls if len(t) > 240]
    lim = (cfg.get("prompt") or {}).get("max_chars")
    print(f"[{'OK ' if not long_tpl and (lim is None or int(lim) == 0 or int(lim) <= 240) else 'WARN'}] "
          f"指令上限 {lim if lim is not None else PROMPT_MAX_DEFAULT} 字符"
          + ("（太长会丢字，长内容请写进文件让模型读）" if long_tpl else ""))
    rot = int(cfg["feed"].get("rotate_after_turns") or 0)
    print(f"[{'OK ' if rot else 'WARN'}] 对话轮换："
          + (f"每个对话跑满 {rot} 轮就换新对话" if rot else "未开启（长时间续同一个对话会让上下文越滚越大、白烧额度）"))
    ct = int(cfg["feed"].get("compact_at_tokens") or 0)
    cc = str(cfg["feed"].get("compact_command") or "/compact")
    print(f"[{'OK ' if ct else 'WARN'}] 上下文压缩："
          + (f"被监控会话 ≥ {ct:,} token 时投 {cc} 压回去"
             "（上下文从 ZCode 会话库 model_usage 读；库打不开闸门自动失效）" if ct
             else "未开启（feed.compact_at_tokens=0，会话上下文会一直滚大）"))
    # 模型是跟会话走的：界面上那个按钮只代表「当前打开的那个对话」，
    # 被后台投喂的会话有没有跑在 auto/ 上，只有会话库知道。
    for spec in agentlib.agent_specs(cfg):
        want = str((spec.get("model") or {}).get("want") or "").strip()
        if not want:
            continue
        acfg = agentlib.agent_view(cfg, spec, st)
        gate = compact_gate(acfg, st, want)
        if gate is None:
            continue
        off, on = gate["offpin"], gate["under"] + gate["over"] + gate["compacting"]
        print(f"[{'WARN' if off else 'OK '}] {spec['id']} 会话模型："
              f"{len(on)} 个在 {want} 上"
              + (f"，{len(off)} 个不在："
                 + "、".join(f"{(s.get('matched') or s['title'])[:20]}={s['model']}"
                             for s in off[:3])
                 + "（auto 会烧到别的桶；enforce 开着就不喂它们）" if off else ""))
    if window_spec(cfg) is None:
        print("[-- ] 允许时段：不限（全天都会跑）")
    else:
        inside = in_window(cfg)
        wait = seconds_until_window(cfg)
        print(f"[{'OK ' if inside else '-- '}] 允许时段：{window_desc(cfg)}"
              + ("（现在就在时段内）" if inside else
                 f"（现在时段外，要到 {(dt.datetime.now() + dt.timedelta(seconds=wait)).strftime('%m-%d %H:%M')} 才开始）"))
    if str(cfg["quota"].get("stop_action") or "exit").lower() == "wait":
        print(f"[OK ] 额度不足时：原地等（阈值 {cfg['quota'].get('stop_below', 0):,}）")
    if ok_app:
        # 每个配了 model.want 的 agent 各自探一次：不同应用、不同窗口，不能共用一个结果
        for spec in agentlib.agent_specs(cfg):
            want = str((spec.get("model") or {}).get("want") or "").strip()
            if not want:
                continue
            acfg = agentlib.agent_view(cfg, spec, st)
            if not app_running(acfg):
                continue
            cur = probe_model(acfg)
            bad = bool(cur) and want not in cur
            tag = "WARN" if bad else ("OK " if cur else "-- ")
            print(f"[{tag}] {spec['id']} 当前模型：{cur or '读不到模型按钮'}"
                  + (f"（期望含 '{want}'：解锁后 zburn model --agent {spec['id']} --set）"
                     if bad else ""))
    print(f"[OK ] 脚本目录: {SCRIPTS}（{len(list(SCRIPTS.glob('*.ps1')))} 个 ps1）")
    return 0 if ok_app else 1


def cmd_quota(a) -> int:
    cfg = load_config(a.config)
    if a.refresh:
        print("刷新额度:", "成功" if refresh_quota(cfg) else "失败")
    remaining = read_quota(cfg)
    print("剩余额度:", "未配置" if remaining is None else f"{remaining:,}")
    return 0 if remaining is not None else 1


def quota_rate(st: State, minutes: int = 60) -> dict:
    """最近 N 分钟的额度消耗速率（tokens/小时）。

    以前不管数据多旧都拿最后两个点算，然后把标签写成「近一小时」——
    采样停了一整夜之后再跑 status，会拿 10 小时前的两个点冒充「近一小时」。
    现在把真实跨度和数据年龄一起返回，谁看都不会被误导。
    """
    empty = {"rate": None, "drop": None, "span_minutes": None,
             "last_age_minutes": None, "stale": True, "samples": 0}
    try:
        rows = [ln.split(",") for ln in
                st.quota_csv.read_text(encoding="utf-8-sig").splitlines()[1:] if ln.strip()]
    except OSError:
        return empty
    pts: list[tuple[float, int]] = []
    for r in rows:
        if len(r) < 2 or not r[1].strip().isdigit():
            continue
        try:
            ts = dt.datetime.strptime(r[0].strip(), "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        pts.append((ts, int(r[1])))
    if len(pts) < 2:
        return empty
    t_now = time.time()
    last_age = (t_now - pts[-1][0]) / 60
    win = [p for p in pts if t_now - p[0] <= minutes * 60]
    if len(win) < 2:                     # 窗口里点太少：用最后两个点，但如实标跨度
        win = pts[-2:]
    (t0, v0), (t1, v1) = win[0], win[-1]
    drop = v0 - v1
    span = max((t1 - t0) / 60, 1e-6)
    return {"rate": drop / (span / 60), "drop": drop, "span_minutes": span,
            "last_age_minutes": last_age, "stale": last_age > 2 * minutes,
            "samples": len(pts)}


def read_accounts(cfg: dict) -> list[dict]:
    """网关里每个账号的额度明细（按模型分）。读不到就返回空表。"""
    q = cfg["quota"]
    if q.get("provider") != "gateway_zcode2api":
        return []
    try:
        data = http_json(q["url"], headers=quota_headers(q))
    except Exception:
        return []
    return [a for a in (data.get("accounts") or []) if isinstance(a, dict)]


def _fmt_int(v) -> str:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "?"


def account_rows(accounts: list[dict]) -> list[dict]:
    """把网关的账号结构压成表里一行（不同版本的字段名都兜一下）。"""
    rows = []
    for a in accounts:
        quota = a.get("quota") or {}
        models = {}
        for model, ent in quota.items():
            if not isinstance(ent, dict):
                continue
            models[model] = {
                "total": ent.get("total"), "used": ent.get("used"),
                "remaining": ent.get("remaining"),
            }
        rows.append({
            "name": str(a.get("name") or a.get("id") or "?"),
            "enabled": bool(a.get("enabled", True)),
            "status": str(a.get("status") or ""),
            "use_count": a.get("use_count"),
            "fail_count": a.get("fail_count"),
            "models": models,
            "unknown": not models,          # 没刷过额度，不知道还剩多少
            "cooling_until": a.get("cooling_until") or 0,
            "last_error": str(a.get("last_error") or ""),
        })
    return rows


def cmd_accounts(a) -> int:
    """按账号看额度（总剩余看不出「哪个号还有钱」，这个能看）。"""
    cfg = load_config(a.config)
    if getattr(a, "refresh", False):
        print("刷新额度:", "成功" if refresh_quota(cfg) else "失败")
    accs = read_accounts(cfg)
    if not accs:
        print("读不到账号明细（quota.provider 必须是 gateway_zcode2api，且网关要活着）")
        return 1
    rows = account_rows(accs)
    if getattr(a, "json", False):
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    width = max([len(r["name"]) for r in rows] + [8])
    print(f"{'账号'.ljust(width)}  {'状态':10} {'调用':>6}  各模型 剩余/总量")
    total_left = 0
    unknown = 0
    for r in rows:
        if not r["enabled"]:
            state = "disabled"
        elif r["cooling_until"] and float(r["cooling_until"]) > time.time():
            state = "冷却中"
        else:
            state = r["status"] or "?"
        if r["unknown"]:
            unknown += 1
            detail = "（还没刷过额度：zburn accounts --refresh）"
        else:
            parts = []
            for model, m in sorted(r["models"].items()):
                if m["remaining"] is not None:
                    total_left += int(m["remaining"])
                parts.append(f"{model} {_fmt_int(m['remaining'])}/{_fmt_int(m['total'])}")
            detail = "   ".join(parts) or "（空）"
        print(f"{r['name'].ljust(width)}  {state:10} {_fmt_int(r['use_count']):>6}  {detail}")
        if r["last_error"]:
            print(f"{' ' * width}  └ {r['last_error'][:90]}")
    print(f"\n合计剩余: {_fmt_int(total_left)}")
    if unknown:
        print(f"有 {unknown} 个账号没有额度信息（quota 字段是空的）："
              "用 `zburn accounts --refresh` 让网关去查一次")
    dead = [r["name"] for r in rows if r["enabled"] and not r["unknown"]
            and all((m["remaining"] or 0) <= 0 for m in r["models"].values())]
    if dead:
        print("已烧完：" + "、".join(dead))
    return 0


def cmd_status(a) -> int:
    cfg = load_config(a.config)
    st = State(cfg)
    print(f"配置: {config_source()}")
    if window_spec(cfg) is not None:
        inside = in_window(cfg)
        print(f"允许时段: {window_desc(cfg)} —— "
              + ("在时段内" if inside else
                 f"时段外（{seconds_until_window(cfg) // 60} 分钟后开始）"))
    r = quota_rate(st)
    if r["rate"] is None:
        print("额度速率: 样本不足（至少要有两个采样点）")
    elif r["stale"]:
        print(f"额度速率: 不估算 —— 最后一次采样在 {r['last_age_minutes']:.0f} 分钟前"
              "（采样只在 run 里做，守护没跑时不更新）")
    else:
        print(f"额度速率: 近 {r['span_minutes']:.0f} 分钟掉 {r['drop']:,}，"
              f"约 {int(r['rate']):,} tokens/小时"
              + ("  ← 没有产出还这么烧就说明在空转" if r["rate"] > 5_000_000 else ""))
    if st.status_path.exists():
        raw = st.status_path.read_text(encoding="utf-8")
        print(json.dumps(json.loads(raw), ensure_ascii=False, indent=1))
        try:
            upd = dt.datetime.strptime(json.loads(raw).get("updated", ""),
                                       "%Y-%m-%d %H:%M:%S").timestamp()
            age_min = (time.time() - upd) / 60
            if age_min > 5:
                print(f"⚠ 这份状态是 {age_min:.0f} 分钟前写的：守护进程现在没在跑，"
                      "上面是上次收工时的快照")
        except (ValueError, TypeError):
            pass
    else:
        print("还没有 status.json（没跑过 run）")
    hb = st.heartbeat_path
    if hb.exists():
        age = int(time.time()) - int(hb.read_text() or 0)
        print(f"心跳: {age} 秒前" + ("（陈旧）" if age > 300 else ""))
    print("\nAgents:")
    for ag in agentlib.describe(cfg, st):
        pending = ag.get("pending") or {}
        extra = f"  pending={pending.get('action')}" if pending else ""
        if ag.get("model_ok") is False and ag.get("last_model"):
            extra += f"  模型未钉死：{ag['last_model']}"
        print(f"  [{'on' if ag['enabled'] else 'off'}] {ag['id']:14} {ag['role']:10} "
              f"{ag['process_name']:20} {ag['last_result'][:60] if ag['last_result'] else '-'}"
              + extra)
    if st.log_path.exists():
        lines = st.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        print(f"\n最近 15 行日志（共 {len(lines)} 行）：")
        for ln in lines[-15:]:
            print("  " + ln)
        if a.follow:
            print("\n-- 跟随中，Ctrl+C 退出 --")
            with open(st.log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(0, 2)
                try:
                    while True:
                        chunk = f.readline()
                        if chunk:
                            print("  " + chunk.rstrip())
                        else:
                            time.sleep(1)
                except KeyboardInterrupt:
                    pass
    return 0


def cmd_probe(a) -> int:
    cfg = load_config(a.config)
    st = State(cfg)
    spec = agentlib.find_agent(cfg, getattr(a, "agent", None))
    acfg = agentlib.agent_view(cfg, spec, st) if spec else cfg
    code, out, err = run_ps("uia-tree.ps1", {
        "ProcessName": acfg["app"]["process_name"],
        "WindowTitleRegex": acfg["uia"].get("window_title_regex", ""),
        "Filter": a.filter or "", "Json": a.json, "MaxDepth": acfg["uia"]["max_depth"]})
    print(out or err)
    return code


def cmd_inject(a) -> int:
    cfg = load_config(a.config)
    st = State(cfg)
    spec = agentlib.find_agent(cfg, getattr(a, "agent", None))
    acfg = agentlib.agent_view(cfg, spec, st) if spec else cfg
    return inject_once(acfg, st, a.text, no_enter=a.no_enter, dry_run=a.dry_run)


def cmd_feed(a) -> int:
    cfg = load_config(a.config)
    st = State(cfg)
    spec = agentlib.find_agent(cfg, getattr(a, "agent", None))
    if not spec:
        spec = (agentlib.orchestrator(cfg)[1] or agentlib.agent_specs(cfg)[0])
    acfg = agentlib.agent_view(cfg, spec, st)
    prompt = a.prompt or build_prompt(acfg, st, 0, read_quota(cfg))
    st.log(f"[{spec['id']}] 投喂一轮（dry-run={a.dry_run}）指令: {prompt[:60]}…")
    code, summary = feed_clean(acfg, st, prompt, dry_run=a.dry_run)
    st.log("结果: " + (summary or f"exit {code}"))
    st.status(last_feed=now(), last_feed_summary=summary)
    agentlib.update_state(st, spec["id"], last_action="feed", last_result=summary,
                          last_feed=now() if code == 0 else "")
    return 0 if code == 0 else code


def _sleep_or_stop(st: State, seconds: int) -> bool:
    """分片睡眠，期间每 5 秒看一次 STOP 文件；返回 True 表示该收工了。

    之前一轮睡满 interval（默认 120 秒），手动放 STOP / 面板停止都要干等，
    现在最多 5 秒内响应。
    """
    end = time.time() + max(20, seconds)
    while True:
        if st.stop_file.exists():
            return True
        left = end - time.time()
        if left <= 0:
            return st.stop_file.exists()
        time.sleep(min(5, left))


def cmd_run(a) -> int:
    cfg = load_config(a.config)
    st = State(cfg)
    if not acquire_scheduler("run"):
        return 4
    st.log(f"=== zburn {VERSION} 启动 (dry-run={a.dry_run}) ===")
    for s in agentlib.enabled_agents(cfg):
        acfg = agentlib.agent_view(cfg, s, st)
        win_re = acfg["uia"].get("window_title_regex") or ""
        st.log(f"agent {s['id']}（{s['name']} / {s['role']}）"
               f" 应用={acfg['app']['process_name']}"
               f" 窗口={('/' + win_re + '/') if win_re else '任意'}"
               f" 会话={describe_selection(acfg)}"
               f" 间隔={acfg['feed']['interval_seconds']}s")
    st.log(f"额度源={cfg['quota']['provider']}；STOP 文件={st.stop_file}")
    st.log(f"允许时段={window_desc(cfg)}"
           + ("" if in_window(cfg) else "（现在不在时段内，先等着）"))

    # 只认一个模型：起跑前先确认，不对就先切、切不动直接不启动
    # （用户要求：检测到其他模型 → 停 → 切 → 还不行就停）
    if not a.dry_run:
        for s in agentlib.enabled_agents(cfg):
            if s["role"] != agentlib.WORKER:
                continue
            acfg = agentlib.agent_view(cfg, s, st)
            want = str((acfg.get("model") or {}).get("want") or "").strip()
            if not want or not app_running(acfg):
                continue
            ok, msg = model_guard(acfg, st, s["id"])
            if ok is False:
                st.log(f"[{s['id']}] 起跑前模型不对（{msg}）→ 不启动。"
                       "解锁桌面后跑 python zburn.py model --set，或先在应用里手选一次")
                st.status(state="error", reason="model", model=msg)
                return 3
            if ok is True:
                st.log(f"[{s['id']}] 起跑前模型检查：{msg}")

    cycle = 0
    fails: dict[str, int] = {}
    last_refresh = 0.0
    last_gateway = 0.0
    last_quota = 0.0
    last_run: dict[str, float] = {}
    remaining: int | None = None
    gw = "disabled"
    paused = False                 # 时段外暂停中（只用来把日志压成一条）
    quota_waiting = False          # 等额度中
    nxt_quota_log = 0.0
    try:
        while True:
            if st.stop_file.exists():
                st.log("发现 STOP 文件 → 收工")
                st.status(state="stopped", reason="stop-file")
                return 0
            cfg = reload_config(a.config, cfg)   # 面板改的参数下一轮生效
            specs = agentlib.enabled_agents(cfg)
            st.heartbeat()
            now_ts = time.time()

            # 0) 允许时段：只在 23:00-09:00 这类时间窗里干活
            if not in_window(cfg):
                if not paused:
                    secs = seconds_until_window(cfg)
                    nxt = dt.datetime.now() + dt.timedelta(seconds=secs)
                    st.log(f"不在允许时段（{window_desc(cfg)}）→ 暂停，"
                           f"{nxt.strftime('%m-%d %H:%M')} 自动开始")
                    paused = True
                st.status(state="paused", reason="outside-window",
                          window=window_desc(cfg), remaining=remaining, agents=agentlib.describe(cfg, st))
                if _sleep_or_stop(st, max(5, min(60, seconds_until_window(cfg)))):
                    st.log("发现 STOP 文件 → 收工")
                    st.status(state="stopped", reason="stop-file")
                    return 0
                continue
            if paused:
                st.log(f"进入允许时段（{window_desc(cfg)}）→ 继续")
                paused = False

            # 1) 网关保活（只探活，不重启）—— 每 60 秒一次，别让 pwsh 每 5 秒起一个
            if now_ts - last_gateway >= 60:
                gw = ensure_gateway(cfg, st)
                last_gateway = now_ts

            # 2) 额度：每 60 秒读一次；到点主动刷新
            if now_ts - last_quota >= 60:
                remaining = read_quota(cfg)
                last_quota = now_ts
                q = cfg["quota"]
                if remaining is not None:
                    st.quota_row(remaining, gw)
                    if q.get("refresh_every_seconds") and now_ts - last_refresh > q["refresh_every_seconds"]:
                        refresh_quota(cfg)
                        last_refresh = now_ts
                    if q.get("stop_below") and remaining <= q["stop_below"]:
                        action = str(q.get("stop_action") or "exit").lower()
                        if action == "wait":
                            # 夜间跑的时候额度常常是 0（每日额度按天重置），
                            # 直接收工就白等了，所以可以选择原地等
                            quota_waiting = True
                            if now_ts >= nxt_quota_log:
                                st.log(f"额度 {remaining:,} ≤ 阈值 {q['stop_below']:,} → 原地等额度"
                                       "（每分钟复查一次，够了自动继续；不想等就把 stop_action 改成 exit）")
                                nxt_quota_log = now_ts + 600
                            st.status(state="waiting", reason="quota", remaining=remaining,
                                      window=window_desc(cfg))
                            if _sleep_or_stop(st, 60):
                                st.log("发现 STOP 文件 → 收工")
                                st.status(state="stopped", reason="stop-file")
                                return 0
                            continue
                        st.log(f"额度 {remaining:,} ≤ 阈值 {q['stop_below']:,} → 收工")
                        st.status(state="finished", reason="quota", remaining=remaining)
                        return 0
                if quota_waiting and remaining is not None and (
                        not q.get("stop_below") or remaining > q["stop_below"]):
                    st.log(f"额度回来了（{remaining:,}）→ 继续干活")
                    quota_waiting = False

            # 3) 交接文件：按「监督者写的下一步」排队，不等它同步返回
            worker = agentlib.orchestrator(cfg)[1]
            default_agent = worker["id"] if worker else (specs[0]["id"] if specs else "")
            rec = agentlib.consume_handoff(cfg, st, default_agent)
            if rec:
                apply_handoff(cfg, st, rec, worker)

            # 4) 到点的 agent 各跑一轮（每个 agent 有自己的 interval）
            due = [s for s in specs
                   if now_ts - last_run.get(s["id"], 0.0) >=
                   max(20, int((s["feed"] or {}).get("interval_seconds") or 120))]
            if due:
                cycle += 1
                for s in due:
                    try:
                        code, summary = run_agent_once(cfg, st, s, cycle, remaining, a.dry_run)
                    except ModelViolation as e:
                        # 只认一个模型：切不回来就整体停下，别让额度继续烧在别的桶里
                        st.log(f"✗ 模型不对且切不回来 → 停止全部：{e}")
                        st.status(state="error", reason="model", model=str(e)[:300],
                                  cycle=cycle, remaining=remaining)
                        return 3
                    last_run[s["id"]] = time.time()
                    fails[s["id"]] = 0 if code == 0 else fails.get(s["id"], 0) + 1
                    st.status(state="running", cycle=cycle, remaining=remaining, gateway=gw,
                              locked=desktop_locked(), last_feed_summary=summary,
                              agents=agentlib.describe(cfg, st),
                              handoff=agentlib.handoff_status(cfg, st, default_agent))
                    # supervisor 挂了不该拖死 worker；只有 worker（或唯一 agent）连续失败才收工
                    if fails[s["id"]] >= 5 and (
                            s["role"] == agentlib.WORKER or len(specs) == 1):
                        st.log(f"[{s['id']}] 连续失败过多 → 收工（看 docs/故障排查.md）")
                        st.status(state="error", reason="too-many-failures")
                        return 1
            else:
                st.status(state="running", cycle=cycle, remaining=remaining, gateway=gw,
                          locked=desktop_locked(), agents=agentlib.describe(cfg, st),
                          handoff=agentlib.handoff_status(cfg, st, default_agent))

            if _sleep_or_stop(st, 5):
                st.log("发现 STOP 文件 → 收工")
                st.status(state="stopped", reason="stop-file")
                return 0
    except KeyboardInterrupt:
        st.log("收到 Ctrl+C → 收工")
        st.status(state="stopped", reason="ctrl-c")
        return 0


def cmd_watchdog(a) -> int:
    cfg = load_config(a.config)
    st = State(cfg)
    stale = cfg["watchdog"]["heartbeat_stale_seconds"]
    every = cfg["watchdog"]["check_seconds"]
    st.log(f"看门狗启动：每 {every}s 检查心跳（超过 {stale}s 未更新就重启守护）")
    while True:
        time.sleep(every)
        if st.stop_file.exists():
            # 已被叫停（面板停止/STOP 文件）：别把守护又拉起来，
            # 但看门狗自己不退出，等下次启动后继续值守
            continue
        age = None
        if st.heartbeat_path.exists():
            try:
                age = int(time.time()) - int(st.heartbeat_path.read_text() or 0)
            except ValueError:
                age = None
        if age is not None and age <= stale:
            continue
        st.log(f"心跳 {age} 秒未更新 → 拉起守护进程")
        log_f = open(st.dir / "supervisor.out", "a", encoding="utf-8")
        args = [sys.executable, str(pathlib.Path(__file__).resolve()), "run"]
        if a.config:
            args += ["--config", config_path_arg(a.config) or a.config]
        subprocess.Popen(args, stdout=log_f, stderr=log_f,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        time.sleep(max(30, stale // 2))


def cmd_plugin(a) -> int:
    src = HERE / "plugin" / "zcode-continue"
    if not src.exists():
        print(f"插件目录不存在: {src}")
        return 2
    if a.action == "install":
        root = pathlib.Path(os.environ.get("USERPROFILE", pathlib.Path.home())) / ".zcode"
        dst = root / "plugins" / "installed"
        dst.mkdir(parents=True, exist_ok=True)
        target = dst / "zburn-continue"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(src, target)
        print(f"已安装到 {target}")
        print("启用：在你的 ZCode CLI 里执行 `plugins enable zburn-continue`，并重启会话（钩子在会话启动时绑定）")
        return 0
    if a.action == "remove":
        root = pathlib.Path(os.environ.get("USERPROFILE", pathlib.Path.home())) / ".zcode"
        target = root / "plugins" / "installed" / "zburn-continue"
        if target.exists():
            shutil.rmtree(target)
            print(f"已删除 {target}")
        else:
            print("没装")
        return 0
    print("用法: zburn.py plugin install|remove")
    return 2


def cmd_gateway(a) -> int:
    """只处理网关：活着就什么都不做，挂了就按配置拉起来。"""
    cfg = load_config(a.config)
    st = State(cfg)
    g = cfg["gateway"]
    if not g.get("start_command"):
        print("配置里没有 gateway.start_command，无法自动拉起（只做探活）")
    res = ensure_gateway(cfg, st)
    print(f"网关: {res}")
    try:
        info = http_json(g["health_url"], timeout=5)
        print(f"健康检查通过: {json.dumps(info, ensure_ascii=False)[:120]}")
        return 0
    except Exception as e:
        print(f"健康检查失败: {type(e).__name__} {e}")
        return 1


def cmd_panel(a) -> int:
    import panel
    cfg = load_config(a.config)
    st = State(cfg)
    pc = cfg.get("panel") or {}
    host = a.host or pc.get("host") or "127.0.0.1"
    port = a.port or int(pc.get("port") or 8790)
    port = panel.pick_port(host, port)          # 端口被别的程序占了就自动往后挪
    token = a.token if a.token is not None else panel_token(cfg, st, host)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/" + (f"?token={token}" if token else "")
    print("zburn 面板已启动：")
    print_endpoints(host, port, token)
    if getattr(a, "open", False):
        open_browser(url)
    return panel.serve(cfg, host=host, port=port, token=token, config_path=config_path_arg(a.config))


def cmd_up(a) -> int:
    """一键起来：拉起网关（若挂了）→ 守护进程 → 看门狗 → 面板。"""
    import panel
    cfg = load_config(a.config)
    st = State(cfg)
    pc = cfg.get("panel") or {}
    st.log("=== zburn up 一键启动 ===")

    gw = ensure_gateway(cfg, st)
    print(f"网关: {gw}")

    sup = panel.Supervisor(cfg, st.dir, a.config)
    if sup.alive():
        print(f"守护: 已在运行 pid={sup.pid()}")
    else:
        ok, msg = sup.start()
        print(f"守护: {msg}")

    if not a.no_watchdog:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        args = [sys.executable, str(pathlib.Path(__file__).resolve()), "watchdog"]
        if a.config:
            args += ["--config", config_path_arg(a.config) or a.config]
        subprocess.Popen(args, stdout=open(st.dir / "watchdog.out", "a", encoding="utf-8"),
                         stderr=subprocess.STDOUT, creationflags=flags)
        print("看门狗: 已启动")

    host = a.host or pc.get("host") or "0.0.0.0"
    port = a.port or int(pc.get("port") or 8790)
    port = panel.pick_port(host, port)          # 端口被别的程序占了就自动往后挪
    token = a.token if a.token is not None else panel_token(cfg, st, host)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/" + (f"?token={token}" if token else "")
    print("面板: 已启动")
    print_endpoints(host, port, token)
    if not a.no_browser:
        open_browser(url)
    return panel.serve(cfg, host=host, port=port, token=token, config_path=config_path_arg(a.config))


def cmd_restart_app(a) -> int:
    """带 --force-renderer-accessibility 重启应用：修「无障碍树为空」。

    锁屏久了 Chromium 会把 a11y 关掉，之后 UIA 读不到任何控件；只有带开关重启应用
    才能恢复。会打断应用里正在跑的任务，所以默认要确认一次。
    """
    cfg = load_config(a.config)
    st = State(cfg)
    spec = agentlib.find_agent(cfg, getattr(a, "agent", None))
    if not spec:
        spec = agentlib.agent_specs(cfg)[0]
    acfg = agentlib.agent_view(cfg, spec, st)
    name = acfg["app"]["process_name"]
    if not app_running(acfg):
        ok, msg = start_app(acfg)
        print(f"[{spec['id']}] 应用没在跑，直接拉起：{msg}")
        return 0 if ok else 1
    if not a.yes:
        print(f"要重启 {name}（{spec['id']}，会打断里面正在跑的任务）。确认请输入 yes：", end=" ", flush=True)
        try:
            ans = input().strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes", "是"):
            print("已取消。")
            return 1
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    subprocess.run(["taskkill", "/IM", f"{name}.exe", "/F"], capture_output=True, creationflags=flags)
    st.log(f"[restart-app] 已结束 {name}，准备带 a11y 开关重启")
    time.sleep(3)
    ok, msg = start_app(acfg)
    print(f"重启：{msg}")
    time.sleep(8)
    ok_uia, why = uia_ok(acfg)
    print(f"[{'OK ' if ok_uia else 'FAIL'}] 无障碍树：{why}")
    return 0 if ok_uia else 1


def cmd_agents(a) -> int:
    """列出多 agent 配置与最近状态。"""
    cfg = load_config(a.config)
    st = State(cfg)
    specs = agentlib.agent_specs(cfg)
    sup, work = agentlib.orchestrator(cfg)
    print(f"zburn {VERSION}  agents={len(specs)}  配置={config_source()}")
    print(f"bridge={agentlib.bridge_dir(cfg)}")
    print(f"{'':3} {'id':14} {'role':10} {'process':20} {'间隔':>7}  监控")
    for s in specs:
        state = agentlib.read_state(st, s["id"])
        pending = agentlib.get_pending(st, s["id"])
        flag = "on" if s.get("enabled", True) else "off"
        targets = s["feed"].get("targets") or []
        dyn = agentlib.load_dynamic_targets(st, s["id"])
        extra = f"targets={len(targets)}"
        if dyn:
            extra += f"+动态{len(dyn)}"
        turns = int(state.get("turns_on_target") or 0)
        if turns:
            extra += f" 轮次={turns}"
        if state.get("last_model"):
            ok = state.get("model_ok")
            extra += f" 模型={'OK' if ok else '✗' if ok is False else '?'}"
        if pending:
            extra += f" pending={pending['action']}"
        if state.get("last_result"):
            extra += f" last={str(state['last_result'])[:42]}"
        print(f"{flag:3} {s['id']:14} {s['role']:10} {s['app'].get('process_name',''):20} "
              f"{s['feed'].get('interval_seconds', 0):>6}s  {extra}")
    print(f"\nsupervisor={sup['id'] if sup else '-'}  worker={work['id'] if work else '-'}")
    rec, src = agentlib.read_handoff(cfg, st, work["id"] if work else "")
    print("待处理交接: " + (json.dumps(rec, ensure_ascii=False) if rec else "无")
          + (f"（{src}）" if src else ""))
    return 0


def _worker_spec(cfg: dict, agent: str | None) -> dict:
    spec = agentlib.find_agent(cfg, agent)
    if spec:
        return spec
    return agentlib.orchestrator(cfg)[1] or agentlib.agent_specs(cfg)[0]


def cmd_sessions(a) -> int:
    """列出侧栏里的对话，标出「当前/在名单里/匹配正则」，方便确定要盯哪个。"""
    cfg = load_config(a.config)
    st = State(cfg)
    spec = _worker_spec(cfg, getattr(a, "agent", None))
    acfg = agentlib.agent_view(cfg, spec, st)
    sessions, msg = scan_sessions(acfg)
    rx = (acfg["uia"].get("session_regex") or "").strip()
    targets = feed_targets(acfg)
    print(f"[{spec['id']}] {msg}")
    print(f"会话正则: {('/' + rx + '/') if rx else '（空）'}")
    print(f"监控名单: {'、'.join(targets) if targets else '（空）'}")
    if not sessions:
        print("\n没扫到会话：应用没开、侧栏没渲染，或无障碍树不可用（先 zburn doctor）")
        return 1
    print("")
    for i, s in enumerate(sessions, 1):
        name = s.get("name") or ""
        marks = []
        if s.get("active"):
            marks.append("当前")
        if name in targets:
            marks.append("名单内")
        if rx:
            try:
                if re.search(rx, name):
                    marks.append("匹配正则")
            except re.error:
                pass
        print(f"{i:3}. {name[:58]:58} {'/'.join(marks)}")
    print("\n指定监控：zburn target --match <标题片段>   或   zburn target --add \"<完整标题>\"")
    return 0


def cmd_target(a) -> int:
    """看/改某个 agent 的监控名单（不开面板也能改）。"""
    cfg = load_config(a.config)
    st = State(cfg)
    spec = _worker_spec(cfg, getattr(a, "agent", None))
    acfg = agentlib.agent_view(cfg, spec, st)
    aid = spec["id"]
    cur = agentlib.load_dynamic_targets(st, aid)
    static = [str(t) for t in (spec["feed"].get("targets") or []) if str(t)]
    rx = (spec["uia"].get("session_regex") or "").strip()

    if a.clear:
        cur = agentlib.set_dynamic_targets(st, aid, [])
        print(f"[{aid}] 已清空动态监控名单")
    if a.match:
        sessions, msg = scan_sessions(acfg)
        picked = [s["name"] for s in sessions if a.match in (s.get("name") or "")]
        if not picked:
            print(f"[{aid}] 没扫到标题含 '{a.match}' 的对话（{msg}）")
            return 1
        cur = agentlib.set_dynamic_targets(st, aid, picked)
        print(f"[{aid}] 按 '{a.match}' 选中 {len(picked)} 个对话")
    if a.add:
        cur = agentlib.set_dynamic_targets(st, aid, cur + [a.add])
        print(f"[{aid}] 已加入：{a.add}")
    if a.remove:
        cur = agentlib.set_dynamic_targets(st, aid, [t for t in cur if t != a.remove])
        print(f"[{aid}] 已移除：{a.remove}")

    eff = static + [t for t in cur if t not in static]
    print(f"[{aid}] 静态名单 {len(static)} 个 / 动态名单 {len(cur)} 个 / 会话正则 "
          f"{('/' + rx + '/') if rx else '（空）'}")
    for t in cur:
        print(f"   - {t}")
    if not eff and not rx:
        print("⚠ 名单和正则都是空的 → 这个 agent 不会投喂任何东西（这是「空转」的常见原因）")
    elif not cur and rx:
        print(f"（名单为空，靠正则 /{rx}/ 匹配）")
    return 0


def cmd_newchat(a) -> int:
    """在指定 agent 里新建一个对话并投递首条指令。"""
    cfg = load_config(a.config)
    st = State(cfg)
    spec = agentlib.find_agent(cfg, getattr(a, "agent", None))
    if not spec:
        spec = agentlib.orchestrator(cfg)[1] or agentlib.agent_specs(cfg)[0]
    acfg = agentlib.agent_view(cfg, spec, st)
    prompt = a.prompt or ""
    if not prompt:
        p = acfg["prompt"].get("file") or "next-prompt.txt"
        target = pathlib.Path(p)
        if not target.is_absolute():
            target = st.dir / target
        if target.exists():
            prompt = target.read_text(encoding="utf-8").strip()
    if not prompt:
        print("没有指令：用 --prompt 或先写 next-prompt.txt")
        return 2
    code, summary = new_chat_once(acfg, st, prompt,
                                  project=a.project or acfg["new_chat"].get("project", ""),
                                  dry_run=a.dry_run, click_only=a.click_only,
                                  agent_id=spec["id"], clear_residual=a.clear_residual,
                                  force=bool(getattr(a, "force", False)),
                                  replace_targets=not getattr(a, "keep_targets", False))
    st.log(f"[{spec['id']}] 新建对话（dry-run={a.dry_run}）：{summary}")
    st.status(last_new_chat=now(), last_new_chat_summary=summary)
    print(summary)
    return code


def cmd_handoff(a) -> int:
    """写/看/执行一份监督交接。supervisor 也可以在 shell 里直接调它。"""
    cfg = load_config(a.config)
    st = State(cfg)
    sup, work = agentlib.orchestrator(cfg)
    default_agent = work["id"] if work else ""
    if a.action == "clear":
        n = agentlib.clear_handoff(cfg, st)
        print(f"已清空 {n} 个交接文件")
        if a.clear_pending:
            for s in agentlib.agent_specs(cfg):
                if agentlib.get_pending(st, s["id"]):
                    agentlib.clear_pending(st, s["id"])
                    print(f"  已清空 {s['id']} 的待执行队列")
        return 0
    if a.list:
        rec, src = agentlib.read_handoff(cfg, st, default_agent)
        queued = {s["id"]: agentlib.get_pending(st, s["id"])
                  for s in agentlib.agent_specs(cfg) if agentlib.get_pending(st, s["id"])}
        print(json.dumps({"pending": rec, "source": src, "queued": queued},
                         ensure_ascii=False, indent=1))
        return 0
    if a.apply:
        rec = agentlib.consume_handoff(cfg, st, default_agent)
        if not rec:
            print("没有待处理交接")
            return 1
        pending = apply_handoff(cfg, st, rec, work)
        print(json.dumps({"consumed": rec, "pending": pending}, ensure_ascii=False, indent=1))
        return 0 if pending else 1
    prompt = a.prompt or ""
    if a.file:
        prompt = pathlib.Path(a.file).read_text(encoding="utf-8", errors="replace")
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read()
    if not prompt.strip():
        print("没有 prompt：用 --prompt、--file，或从 stdin 传入")
        return 2
    try:
        rec = agentlib.write_handoff(cfg, st, a.action, prompt,
                                     agent=a.agent or default_agent, project=a.project)
    except ValueError as e:
        print(f"交接未写入：{e}")
        return 2
    st.log(f"[handoff] 写入 {rec['action']} → {rec['agent']}：{rec['prompt'][:100]}")
    print(json.dumps(rec, ensure_ascii=False, indent=1))
    return 0


def cmd_supervise(a) -> int:
    """立刻唤醒 supervisor（可选等它写出交接并落到 pending）。"""
    cfg = load_config(a.config)
    st = State(cfg)
    if a.apply:
        sup, work = agentlib.orchestrator(cfg)
        rec = agentlib.consume_handoff(cfg, st, work["id"] if work else "")
        if not rec:
            print("没有待处理交接")
            return 1
        pending = apply_handoff(cfg, st, rec, work)
        print(json.dumps({"consumed": rec, "pending": pending}, ensure_ascii=False, indent=1))
        return 0 if pending else 1
    wait = a.wait
    if wait is None:
        wait = int((cfg.get("orchestrator") or {}).get("handoff_wait_seconds") or 0)
    code, msg = supervise_now(cfg, st, dry_run=a.dry_run, wait=wait)
    st.log(f"[supervise] {msg}")
    print(msg)
    return code


def cmd_snapshot(a) -> int:
    """导出 worker 的可见对话尾部（supervisor 的输入）。"""
    cfg = load_config(a.config)
    st = State(cfg)
    spec = agentlib.find_agent(cfg, getattr(a, "agent", None))
    if not spec:
        spec = agentlib.orchestrator(cfg)[1] or agentlib.agent_specs(cfg)[0]
    acfg = agentlib.agent_view(cfg, spec, st)
    text = snapshot_agent(acfg, st, spec)
    p = st.dir / f"context-{spec['id']}.txt"
    p.write_text(text, encoding="utf-8")
    bridge = agentlib.ensure_bridge(cfg)
    (bridge / "context.txt").write_text(text, encoding="utf-8")
    print(f"[{spec['id']}] 对话快照：{p}（{len(text)} 字符）")
    print(text[:800])
    return 0


def cmd_batch(a) -> int:
    """按队列一条条做：投喂 → 等它改文件 → 跑验收 → 记账。"""
    import batch
    cfg = load_config(a.config)
    if getattr(a, "state_dir", ""):
        cfg.setdefault("paths", {})["state_dir"] = a.state_dir
    st = State(cfg)
    if not acquire_scheduler("batch"):
        return 4
    st.log(f"=== batch {a.queue} 开始（dry-run={a.dry_run}，targets={getattr(a, 'target', None)}）===")
    return batch.run_queue(cfg, st, a.queue, max_tasks=a.max, only=a.only or "",
                           dry_run=a.dry_run, retries=a.retries,
                           targets=getattr(a, "target", None))


def cmd_autopilot(a) -> int:
    """夜间总控：分片 + 看住进程 + 修应用/网关，直到队列做完或出现 STOP。"""
    import autopilot
    cfg = load_config(a.config)
    if getattr(a, "state_dir", ""):
        cfg.setdefault("paths", {})["state_dir"] = a.state_dir
    st = State(cfg)
    if not acquire_scheduler("autopilot"):
        return 4
    return autopilot.run(cfg, st, a.queue, a.target or feed_targets(cfg), shards=a.shards,
                         max_per_shard=a.max, config_path=config_path_arg(a.config),
                         poll=a.poll, max_hours=a.hours)


def pin_session_model(cfg: dict, st: State, target: str, want: str,
                      dry_run: bool = False) -> tuple[bool, str]:
    """把某个会话切到前台，再把**它自己的**模型钉到 want。

    模型是跟会话走的：界面上那个选择器只改当前打开的对话。所以钉一个后台会话
    必须先切过去（SwitchOnly，切不过去就失败，不回退到别的对话），切完再选模型
    并复查按钮文案。
    """
    label = (target or "")[:30]
    if dry_run:
        return True, f"(dry-run) 会切到 [{label}] 并钉 '{want}'"
    params = {
        "ProcessName": cfg["app"]["process_name"],
        "WindowTitleRegex": cfg["uia"].get("window_title_regex", ""),
        "SessionRegex": "",
        "Targets": json.dumps([target], ensure_ascii=False),
        "BusyRegex": cfg["uia"]["busy_regex"],
        "QueuedRegex": cfg["uia"]["queued_regex"],
        "MaxSessions": 1,
        "MaxDepth": cfg["uia"]["max_depth"],
        "SwitchOnly": True,
    }
    code, out, err = run_ps("feed-sessions.ps1", params, timeout=180)
    if code != 0:
        lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
        note = (lines[-1] if lines else "") or (err or "").strip()[:160] or f"exit {code}"
        return False, f"切不过去（{note}）"
    if desktop_locked():
        return False, "已切到该会话，但锁屏中点不动模型下拉（解锁后再跑一次）"
    run_ps("pick-model.ps1", {"ProcessName": cfg["app"]["process_name"], "Wanted": want},
           timeout=180)
    cur = probe_model(cfg)
    return (want in cur), (cur or "读不到模型按钮")


def pin_offpin_sessions(cfg: dict, st: State, want: str, *,
                        dry_run: bool = False) -> tuple[int, str]:
    """把「以后可能被喂到的会话」里跑在 auto/ 等别的模型上的，逐个切过去钉回 want。

    这里取「监控名单 ∪ 会话正则」的并集：投喂只认名单，但名单一空就会退回正则，
    那时以前被正则捞到的老会话又会开始吃 continue —— 它们要是还在 auto 上，
    额度照样烧错桶。
    """
    targets = feed_targets(cfg)
    regex = (cfg["uia"].get("session_regex") or "").strip()
    if not targets and not regex:
        return 3, "没配监控名单也没配会话正则，不知道要钉哪些会话"
    try:
        sessions = zsession.session_contexts(cfg, targets=targets,
                                             session_regex=regex, union=True)
    except Exception:
        sessions = None
    if not sessions:
        return 3, "读不到会话库或名单一条都没匹配上，无法判断哪些会话跑偏了"
    off = [s for s in sessions if s.get("model") and want not in s["model"]]
    if not off:
        return 0, f"所有可能被喂的会话都在 '{want}' 上（共 {len(sessions)} 个）"
    ok_n = 0
    for s in off:
        target = s.get("matched") or s["title"]
        ok, msg = pin_session_model(cfg, st, target, want, dry_run=dry_run)
        st.log(f"[model] [{target[:30]}] 原模型 {s.get('model') or '?'} → "
               f"{'会钉回' if dry_run else ('已钉回' if ok else '没成功')}（{msg}）")
        if not ok and not dry_run:
            # 第一次失败就收手：多半是锁屏/下拉点不动，继续只会把界面切来切去
            st.log(f"[model] 钉不动（{msg}）→ 停止后续切换，剩下的等解锁后再跑")
            break
        ok_n += 1 if ok else 0
    return (0 if ok_n == len(off) else 1), f"{ok_n}/{len(off)} 个会话已钉到 '{want}'"


class ModelViolation(RuntimeError):
    """发现当前不是在钉死的模型上，而且切不回来 → 立刻停下整条链。

    按用户要求：只认一个模型；检测到别的模型先尝试切回来，切不动就直接停，
    不再「这一轮跳过、下一轮再看看」——那等于让额度继续烧到别的桶里。
    """


def model_guard(cfg: dict, st: State, agent_id: str, *, auto: bool | None = None) -> tuple[bool | None, str]:
    """确认当前对话还是「钉死」的那个模型；不是就尝试切回去。

    为什么每次新建对话都要查一遍：ZCode 的模型选择是跟对话走的，新开一个对话不一定
    继承上次选的那个，默认会走 `magpie/auto/...`。auto 由网关自己挑「有额度的桶」，
    看起来一直在干活，额度却烧在另一个模型上（实测一夜 GLM-5.2 走了 95 次，
    而专门留的 5M 新桶一次没动）。轮换（每 3 轮一次）会让这件事反复发生。

    返回 (True/False/None, 说明)；None = 读不到模型按钮，无法判断（不阻塞干活）。
    """
    m = (cfg.get("model") or {})
    want = str(m.get("want") or "").strip()
    if not want:
        return None, "未配置 model.want（不校验）"
    if auto is None:
        auto = bool(m.get("auto_pin", True))
    cur = probe_model(cfg)
    if not cur:
        agentlib.update_state(st, agent_id, model_ok=None)
        return None, "读不到模型按钮（跳过校验）"
    if want in cur:
        agentlib.update_state(st, agent_id, model_ok=True, last_model=cur)
        return True, f"OK {cur}"
    if auto and not desktop_locked():
        run_ps("pick-model.ps1", {"ProcessName": cfg["app"]["process_name"], "Wanted": want},
               timeout=180)
        cur2 = probe_model(cfg)
        if cur2 and want in cur2:
            st.log(f"[{agent_id}] 模型已钉回：{cur2}")
            agentlib.update_state(st, agent_id, model_ok=True, last_model=cur2)
            return True, f"已钉回 {cur2}"
        cur = cur2 or cur
    # 锁屏时点不动 Radix 下拉，只能记下来让人解锁后处理
    st.log(f"[{agent_id}] ⚠ 模型没钉住：当前 '{cur}'，期望含 '{want}'"
           + ("（锁屏切不了，解锁后跑 zburn model --set）" if desktop_locked() else ""))
    agentlib.update_state(st, agent_id, model_ok=False, last_model=cur)
    return False, f"未钉住（{cur}）"


def probe_model(cfg: dict) -> str:
    """读当前对话的模型名（输入框旁那个按钮的文案）。只读。"""
    try:
        code, out, err = run_ps("probe-model.ps1",
                                {"ProcessName": cfg["app"]["process_name"]}, timeout=90)
    except Exception:
        return ""
    lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
    return lines[0] if lines else ""


def cmd_model(a) -> int:
    """看/校验/切换当前对话的模型（钉死模型，别让 auto 白烧额度）。"""
    cfg = load_config(a.config)
    st = State(cfg)
    spec = agentlib.find_agent(cfg, getattr(a, "agent", None)) or agentlib.agent_specs(cfg)[0]
    acfg = agentlib.agent_view(cfg, spec, st)
    want = (a.want or (acfg.get("model") or {}).get("want") or "").strip()
    if getattr(a, "pin_sessions", False):
        # 模型是跟会话走的：界面上那个按钮只代表当前打开的那个对话，
        # 后台被投喂的会话可能一直跑在 auto/ 上 —— 这里按会话库逐个钉回去。
        if not want:
            print("要用 --pin-sessions 得先配 model.want 或给 --want（例如 --want zcode2api）")
            return 2
        code, summary = pin_offpin_sessions(acfg, st, want, dry_run=a.dry_run)
        print(summary)                      # 每个会话的明细已经由 st.log 打出来了
        return code
    cur = probe_model(acfg)
    print(f"{spec['id']}: {cur or '(读不到模型按钮)'}")
    if a.set:
        if not want:
            print("要用 --set 得先给 --want（例如 --want zcode2api）")
            return 2
        code, out, err = run_ps("pick-model.ps1",
                                {"ProcessName": acfg["app"]["process_name"], "Wanted": want},
                                timeout=180)
        for ln in (out or "").splitlines()[-4:]:
            print("  " + ln.strip())
        if code != 0:
            print("切换失败（锁屏下列表项点不动）：解锁桌面后重跑，或在应用里手选一次")
        cur = probe_model(acfg)
        print(f"切换后：{cur or '(读不到)'}")
    if want and want not in cur:
        print(f"✗ 模型不符合预期：要含 '{want}'，实际 '{cur}'")
        return 2
    if want:
        print(f"✓ 模型符合预期（含 '{want}'）")
    agentlib.update_state(st, spec["id"], last_model=cur)
    return 0


def cmd_queue(a) -> int:
    """看/清任务队列：run 开着 queue 就会按队列逐条派活（不再喂 continue）。"""
    cfg = load_config(a.config)
    st = State(cfg)
    q = queue_conf(cfg)
    tasks = queue_tasks(cfg)
    done = queue_load_done(st)
    if a.reset:
        queue_save_done(st, {})
        done = {}
        print(f"已清空队列进度（{len(tasks)} 条重新可派）")
    if a.reset_id:
        done.pop(a.reset_id, None)
        queue_save_done(st, done)
        print(f"已重置任务 {a.reset_id}")
    if not q.get("enabled"):
        print("队列未开启：config 里写 queue.enabled=true、queue.path=<queue_fx.json>")
    print(f"队列: {queue_path(cfg) or '（未配置）'}")
    print(f"任务 {len(tasks)} 条，已派 {len(done)} 条，剩余 {max(0, len(tasks) - len(done))} 条")
    nxt = queue_next(cfg, st)
    if a.next and nxt:
        print(f"\n下一条: {nxt['id']}（{nxt.get('group', '')}）")
        print("任务书会写到 bridge/tasks/，实际投喂的指令是：")
        print("  " + queue_prompt(cfg, st, nxt))
        print("\n任务书内容：")
        print("\n".join(("  " + ln) for ln in str(nxt.get("prompt") or "").splitlines()))
    elif a.next:
        print("没有可派的任务（队列空/读不到/已全部派过）")
    if a.list:
        for t in tasks[:a.list]:
            flag = "✓" if str(t["id"]) in done else " "
            print(f"  [{flag}] {t['id']:<32} {t.get('group', '')}")
    return 0


def cmd_panel_token(a) -> int:
    """生成一个随机 token（面板鉴权用）。"""
    import secrets
    print(secrets.token_urlsafe(18))


# ---------------------------------------------------------------- 入口
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="zburn", description="熄屏守护：锁屏下持续驱动桌面 AI")
    ap.add_argument("-V", "--version", action="version", version=f"zburn {VERSION}")
    ap.add_argument("--config", default=os.environ.get("ZBURN_CONFIG"),
                    help="配置文件路径（不给就依次找 当前目录/config.json、脚本目录/config.json、"
                         "%%LOCALAPPDATA%%\\zburn\\config.json）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="环境自检").set_defaults(func=cmd_doctor)

    p = sub.add_parser("quota", help="读一次额度")
    p.add_argument("--refresh", action="store_true", help="先请求后端刷新")
    p.set_defaults(func=cmd_quota)

    p = sub.add_parser("accounts", help="按账号看额度明细（哪个号还有钱）")
    p.add_argument("--refresh", action="store_true", help="先请求后端刷新")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_accounts)

    p = sub.add_parser("status", help="看状态与日志")
    p.add_argument("--follow", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("probe", help="导出无障碍树（换应用时用）")
    p.add_argument("--filter", default="", help="按控件类型过滤，如 'Edit|Button'")
    p.add_argument("--agent", default=None, help="看哪个 agent 的窗口（默认第一个）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("inject", help="直接往输入框投一条")
    p.add_argument("--text", required=True)
    p.add_argument("--agent", default=None, help="往哪个 agent 投（默认第一个）")
    p.add_argument("--no-enter", action="store_true", help="只输入不提交")
    p.add_argument("--dry-run", action="store_true", help="只定位控件，不输入")
    p.set_defaults(func=cmd_inject)

    p = sub.add_parser("feed", help="投喂一轮")
    p.add_argument("--prompt", default=None, help="覆盖提示词（默认按模板轮换）")
    p.add_argument("--agent", default=None, help="只喂某个 agent（默认 worker/第一个）")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_feed)

    p = sub.add_parser("run", help="守护循环")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_run)

    sub.add_parser("agents", help="列出多 agent 配置与状态").set_defaults(func=cmd_agents)

    p = sub.add_parser("sessions", help="列出侧栏对话（确定要盯哪个）")
    p.add_argument("--agent", default=None, help="看哪个 agent 的窗口（默认 worker）")
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("queue", help="看/清任务队列（run 按队列派活，避免跑题）")
    p.add_argument("--next", action="store_true", help="显示下一条任务和它会投喂什么")
    p.add_argument("--list", type=int, default=0, help="列出前 N 条（带是否已派）")
    p.add_argument("--reset", action="store_true", help="清空派活进度")
    p.add_argument("--reset-id", default="", help="只重置某一条任务")
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("target", help="看/改监控名单")
    p.add_argument("--agent", default=None, help="改哪个 agent（默认 worker）")
    p.add_argument("--match", default="", help="选中所有标题含该片段的对话（替换名单）")
    p.add_argument("--add", default="", help="追加一个完整标题")
    p.add_argument("--remove", default="", help="移除一个标题")
    p.add_argument("--clear", action="store_true", help="清空动态名单")
    p.set_defaults(func=cmd_target)

    p = sub.add_parser("newchat", help="新建一个对话并投递首条指令")
    p.add_argument("--agent", default=None, help="目标 agent（默认 worker）")
    p.add_argument("--prompt", default=None, help="首条指令（ASCII）")
    p.add_argument("--project", default=None, help="目标项目名（ZCode 的项目行）")
    p.add_argument("--dry-run", action="store_true", help="只定位新建按钮，不点击")
    p.add_argument("--click-only", action="store_true", help="只点开新对话，不输入")
    p.add_argument("--clear-residual", action="store_true",
                   help="输入框有残留时用 SetValue 清空（只在你确认那是自己的草稿时用）")
    p.add_argument("--force", action="store_true", help="当前会话正在生成也照样新建对话")
    p.add_argument("--keep-targets", action="store_true",
                   help="保留旧的监控名单（默认会清掉：新对话=旧的翻篇了）")
    p.set_defaults(func=cmd_newchat)

    p = sub.add_parser("handoff", help="写/看/执行监督交接")
    p.add_argument("--action", default="feed", choices=["feed", "new_chat", "clear"])
    p.add_argument("--prompt", default=None, help="下一步指令（ASCII）")
    p.add_argument("--file", default=None, help="从文件读 prompt")
    p.add_argument("--agent", default=None, help="目标 agent（默认 worker）")
    p.add_argument("--project", default=None, help="新对话所在项目")
    p.add_argument("--list", action="store_true", help="只看待处理交接")
    p.add_argument("--apply", action="store_true", help="取走交接并排进目标 agent 的待执行队列")
    p.add_argument("--clear-pending", action="store_true", help="连同各 agent 的待执行队列一起清空")
    p.set_defaults(func=cmd_handoff)

    p = sub.add_parser("supervise", help="立刻唤醒 supervisor 验收（可等交接）")
    p.add_argument("--wait", type=int, default=None,
                   help="等多少秒收交接（默认 orchestrator.handoff_wait_seconds；0=不等）")
    p.add_argument("--dry-run", action="store_true", help="只刷新监督桥，不唤醒")
    p.add_argument("--apply", action="store_true", help="只执行已经存在的交接")
    p.set_defaults(func=cmd_supervise)

    p = sub.add_parser("snapshot", help="导出 worker 的可见对话尾部（给 supervisor 看）")
    p.add_argument("--agent", default=None, help="目标 agent（默认 worker）")
    p.set_defaults(func=cmd_snapshot)

    sub.add_parser("watchdog", help="看门狗（心跳超时拉起 run）").set_defaults(func=cmd_watchdog)

    p = sub.add_parser("plugin", help="安装/卸载自动继续插件")
    p.add_argument("action", choices=["install", "remove"])
    p.set_defaults(func=cmd_plugin)

    for name, fn, helptext in (
            ("panel", cmd_panel, "起可视化面板（手机/别的电脑可访问）"),
            ("up", cmd_up, "一条命令：守护 + 看门狗 + 面板")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--host", default=None, help="监听地址（远程访问用 0.0.0.0）")
        p.add_argument("--port", type=int, default=None)
        p.add_argument("--token", default=None, help="面板访问 token（远程访问必填）")
        if name == "up":
            p.add_argument("--no-watchdog", action="store_true")
            p.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
        if name == "panel":
            p.add_argument("--autostart", action="store_true", help="面板里默认自动启动守护")
            p.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
        p.set_defaults(func=fn)

    gp = sub.add_parser("gateway", help="只探活/必要时启动网关")
    gp.add_argument("--config", default=__import__("argparse").SUPPRESS)
    gp.set_defaults(func=cmd_gateway)

    bp = sub.add_parser("batch", help="按队列批量做任务（自带验收）")
    bp.add_argument("--config", default=__import__("argparse").SUPPRESS)
    bp.add_argument("--queue", required=True, help="队列 JSON 路径")
    bp.add_argument("--max", type=int, default=0, help="本次最多做几条（0=不限）")
    bp.add_argument("--only", default="", help="只做 id 含该字样 / 该组的任务")
    bp.add_argument("--retries", type=int, default=1, help="失败重试次数（默认 1）")
    bp.add_argument("--target", action="append", help="只喂这个对话（可多次；面板「会话监控」里的 id）")
    bp.add_argument("--state-dir", default="", help="状态目录（分片各用一份，互不干扰）")
    bp.add_argument("--dry-run", action="store_true", help="只打印要做什么，不投喂")
    bp.set_defaults(func=cmd_batch)

    ap2 = sub.add_parser("autopilot", help="夜间总控：分片并发 + 看住进程 + 修应用/网关")
    ap2.add_argument("--config", default=__import__("argparse").SUPPRESS)
    ap2.add_argument("--queue", required=True, help="队列 JSON 路径")
    ap2.add_argument("--target", action="append", help="专用对话 id（几个对话就几个分片）")
    ap2.add_argument("--shards", type=int, default=0, help="分片数（默认=对话数）")
    ap2.add_argument("--max", type=int, default=0, help="每片最多做几条（0=不限）")
    ap2.add_argument("--poll", type=int, default=60, help="巡检秒数")
    ap2.add_argument("--hours", type=float, default=12.0, help="最长跑几小时")
    ap2.add_argument("--state-dir", default="", help="状态目录")
    ap2.set_defaults(func=cmd_autopilot)

    sub.add_parser("token", help="生成面板 token").set_defaults(func=cmd_panel_token)

    mp = sub.add_parser("model", help="看/校验/切换当前对话的模型")
    mp.add_argument("--config", default=__import__("argparse").SUPPRESS)
    mp.add_argument("--agent", default=None, help="目标 agent（默认 worker）")
    mp.add_argument("--want", default=None, help="期望模型名里包含的字样（如 zcode2api）")
    mp.add_argument("--set", action="store_true", help="尝试切换（需要解锁桌面）")
    mp.add_argument("--pin-sessions", action="store_true",
                    help="把被监控会话里跑在 auto/ 等别的模型上的逐个切过去钉回 want")
    mp.add_argument("--dry-run", action="store_true", help="配合 --pin-sessions 只看会动哪几个会话")
    mp.set_defaults(func=cmd_model)

    rp = sub.add_parser("restart-app", help="带无障碍开关重启目标应用（修 a11y 树为空）")
    rp.add_argument("--config", default=__import__("argparse").SUPPRESS)
    rp.add_argument("--agent", default=None, help="要重启哪个 agent（默认第一个）")
    rp.add_argument("--yes", action="store_true", help="不问直接重启（会打断应用里正在跑的任务）")
    rp.set_defaults(func=cmd_restart_app)

    # 让每个子命令也能吃 --config（把参数写在子命令后面是常见手滑）
    for _sp in sub.choices.values():
        if not any(getattr(_a, "dest", None) == "config" for _a in _sp._actions):
            _sp.add_argument("--config", default=__import__("argparse").SUPPRESS)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())




