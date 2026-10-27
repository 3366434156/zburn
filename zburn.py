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

VERSION = "1.0.0"
HERE = pathlib.Path(__file__).resolve().parent
SCRIPTS = HERE / "scripts"
IS_WIN = os.name == "nt"

DEFAULT_CONFIG = {
    "app": {
        "process_name": "ZCode",
        "exe": r"E:\exe\Zcode\ZCode.exe",
        "start_args": [],
        # Electron 应用被这些环境变量污染会直接退出（实测 ELECTRON_RUN_AS_NODE 会让 ZCode 秒退）
        "strip_env": ["ELECTRON_RUN_AS_NODE", "ELECTRON_NO_ATTACH_CONSOLE"],
        "start_if_down": True,
    },
    "uia": {
        "send_button_regex": "发送|加入队列|立即|Send",
        "busy_regex": "停止生成|Stop generating",
        "queued_regex": "移除待发送消息|Remove queued",
        "session_regex": ".",
        "max_depth": 40,
        "char_delay_ms": 20,
    },
    "prompt": {
        # 轮换模板，避免每次发一模一样的话被模型"记住"
        "templates": [
            "Continue exactly where you left off. Do not summarize, do not ask questions, keep producing output until stopped.",
            "Keep going on the current task. No recap, no stopping, next batch now.",
            "Continue. Stay on the same task, do not stop to explain anything.",
        ],
        "file": "next-prompt.txt",
    },
    "feed": {"max_sessions": 5, "interval_seconds": 120},
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
        "command": "",
    },
    "paths": {"state_dir": ".zburn", "stop_file": "STOP"},
    "watchdog": {"heartbeat_stale_seconds": 420, "check_seconds": 30},
    "panel": {"host": "127.0.0.1", "port": 8788, "token": "", "autostart": False},
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


def load_config(path: str | None) -> dict:
    if not path:
        return dict(DEFAULT_CONFIG)
    p = pathlib.Path(path)
    if not p.exists():
        raise SystemExit(f"配置文件不存在: {p}")
    return deep_merge(DEFAULT_CONFIG, json.loads(p.read_text(encoding="utf-8")))


class State:
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


def subprocess_hidden(args: list[str], timeout: int = 180) -> tuple[int, str, str]:
    flags = 0
    if IS_WIN:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        r = subprocess.run(args, capture_output=True, text=True, errors="replace",
                           timeout=timeout, creationflags=flags)
        return r.returncode, r.stdout or "", r.stderr or ""
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
    code, out, _ = subprocess_hidden(
        [pwsh(), "-NoProfile", "-Command",
         f"(Get-Process -Name {cfg['app']['process_name']} -ErrorAction SilentlyContinue | "
         f"Where-Object {{ $_.MainWindowHandle -ne 0 }} | Measure-Object).Count"])
    return out.strip().isdigit() and int(out.strip()) > 0


def desktop_locked() -> bool:
    if not IS_WIN:
        return False
    code, out, _ = subprocess_hidden(
        [pwsh(), "-NoProfile", "-Command",
         "(Get-Process LockApp,LogonUI -ErrorAction SilentlyContinue | Measure-Object).Count"])
    try:
        return int(out.strip()) > 0
    except ValueError:
        return False


def uia_ok(cfg: dict) -> tuple[bool, str]:
    """无障碍树是否可用：锁屏久了 Chromium 会把 a11y 关掉，只有重启应用才能恢复。"""
    if not IS_WIN:
        return False, "非 Windows"
    code, out, err = run_ps("uia-tree.ps1", {
        "ProcessName": cfg["app"]["process_name"], "Filter": "Edit|Button|ListItem", "Json": True})
    if code != 0:
        return False, (err or out).strip()[:120] or f"exit {code}"
    try:
        rows = json.loads(out)
    except Exception:
        return False, "解析无障碍树失败"
    if isinstance(rows, dict):
        rows = [rows]
    kinds = {r.get("type") for r in rows}
    if "Edit" not in kinds:
        return False, "找不到输入框（无障碍树可能被禁用，重启应用可恢复）"
    return True, f"控件 {len(rows)} 个，含输入框与按钮"


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
        headers = {}
        if q.get("admin_key"):
            headers["x-admin-key"] = q["admin_key"]
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
    headers = {}
    if q.get("admin_key"):
        headers["x-admin-key"] = q["admin_key"]
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


def feed_once(cfg: dict, st: State, prompt: str, dry_run: bool = False) -> tuple[int, str]:
    params = {
        "ProcessName": cfg["app"]["process_name"],
        "SessionRegex": cfg["uia"]["session_regex"],
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
        summary = tail[0] if tail else (err or "").strip()[:160]
    else:
        summary = tail[-1] if tail else (err or "").strip()[:160]
        for ln in tail[:-1][-6:]:
            st.log("  " + ln)
    return code, summary


def inject_once(cfg: dict, st: State, text: str, no_enter: bool = False, dry_run: bool = False) -> int:
    params = {"Text": text, "ProcessName": cfg["app"]["process_name"],
              "SendButtonRegex": cfg["uia"]["send_button_regex"],
              "NoEnter": no_enter, "DryRun": dry_run,
              "CharDelayMs": cfg["uia"]["char_delay_ms"], "MaxDepth": cfg["uia"]["max_depth"]}
    code, out, err = run_ps("inject-uia.ps1", params, timeout=300)
    st.log((out or "").strip() or (err or "").strip() or f"inject exit {code}")
    return code



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


# ---------------------------------------------------------------- 子命令
def cmd_doctor(a) -> int:
    cfg = load_config(a.config)
    st = State(cfg)
    print(f"zburn {VERSION}  配置: {a.config or '<内置默认>'}")
    print(f"平台: {sys.version.split()[0]} / {'Windows' if IS_WIN else os.name}")
    print(f"状态目录: {st.dir.resolve()}")

    ok_app = app_running(cfg)
    print(f"[{'OK ' if ok_app else 'FAIL'}] 应用 {cfg['app']['process_name']} 在运行且有主窗口")
    if ok_app:
        ok_uia, why = uia_ok(cfg)
        print(f"[{'OK ' if ok_uia else 'FAIL'}] 无障碍树可用：{why}")
        print(f"[{'锁屏' if desktop_locked() else '解锁'}] 当前桌面状态（锁屏走 UIA 路径，本来就没问题）")

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

    tpls = cfg["prompt"]["templates"]
    ascii_bad = [t for t in tpls if any(ord(c) > 126 for c in t)]
    print(f"[{'OK ' if not ascii_bad else 'WARN'}] 提示模板 {len(tpls)} 条"
          + ("" if not ascii_bad else "，含非 ASCII 会被丢弃（WM_CHAR 限制）"))
    print(f"[OK ] 脚本目录: {SCRIPTS}（{len(list(SCRIPTS.glob('*.ps1')))} 个 ps1）")
    return 0 if ok_app else 1


def cmd_quota(a) -> int:
    cfg = load_config(a.config)
    if a.refresh:
        print("刷新额度:", "成功" if refresh_quota(cfg) else "失败")
    remaining = read_quota(cfg)
    print("剩余额度:", "未配置" if remaining is None else f"{remaining:,}")
    return 0 if remaining is not None else 1


def cmd_status(a) -> int:
    cfg = load_config(a.config)
    st = State(cfg)
    if st.status_path.exists():
        print(json.dumps(json.loads(st.status_path.read_text(encoding="utf-8")), ensure_ascii=False, indent=1))
    else:
        print("还没有 status.json（没跑过 run）")
    hb = st.heartbeat_path
    if hb.exists():
        age = int(time.time()) - int(hb.read_text() or 0)
        print(f"心跳: {age} 秒前")
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
    code, out, err = run_ps("uia-tree.ps1", {
        "ProcessName": cfg["app"]["process_name"],
        "Filter": a.filter or "", "Json": a.json, "MaxDepth": cfg["uia"]["max_depth"]})
    print(out or err)
    return code


def cmd_inject(a) -> int:
    cfg = load_config(a.config)
    st = State(cfg)
    return inject_once(cfg, st, a.text, no_enter=a.no_enter, dry_run=a.dry_run)


def cmd_feed(a) -> int:
    cfg = load_config(a.config)
    st = State(cfg)
    prompt = a.prompt or build_prompt(cfg, st, 0, read_quota(cfg))
    st.log(f"投喂一轮（dry-run={a.dry_run}）指令: {prompt[:60]}…")
    code, summary = feed_once(cfg, st, prompt, dry_run=a.dry_run)
    st.log("结果: " + (summary or f"exit {code}"))
    st.status(last_feed=now(), last_feed_summary=summary)
    return 0 if code == 0 else code


def cmd_run(a) -> int:
    cfg = load_config(a.config)
    st = State(cfg)
    st.log(f"=== zburn {VERSION} 启动 (dry-run={a.dry_run}) ===")
    st.log(f"应用={cfg['app']['process_name']} 间隔={cfg['feed']['interval_seconds']}s "
           f"会话上限={cfg['feed']['max_sessions']} 额度源={cfg['quota']['provider']}")
    cycle = 0
    fails = 0
    last_refresh = 0.0
    try:
        while True:
            if st.stop_file.exists():
                st.log("发现 STOP 文件 → 收工")
                st.status(state="stopped", reason="stop-file")
                return 0
            cycle += 1
            st.heartbeat()

            # 1) 应用保活
            if cfg["app"].get("start_if_down") and not app_running(cfg):
                ok, why = start_app(cfg)
                st.log(f"应用不在 → {'已拉起' if ok else '拉起失败'}：{why}")
                time.sleep(12)

            # 2) 网关保活（只探活，不重启）
            gw = ensure_gateway(cfg, st)

            # 3) 额度
            remaining = read_quota(cfg)
            q = cfg["quota"]
            if remaining is not None:
                st.quota_row(remaining, gw)
                if q.get("refresh_every_seconds") and time.time() - last_refresh > q["refresh_every_seconds"]:
                    refresh_quota(cfg)
                    last_refresh = time.time()
                if q.get("stop_below") and remaining <= q["stop_below"]:
                    st.log(f"额度 {remaining:,} ≤ 阈值 {q['stop_below']:,} → 收工")
                    st.status(state="finished", remaining=remaining)
                    return 0
            st.status(state="running", cycle=cycle, remaining=remaining, gateway=gw,
                      locked=desktop_locked())

            # 4) 无障碍树健康（锁屏久了会被 Chromium 关掉）
            ok_uia, why = uia_ok(cfg)
            if not ok_uia:
                st.log(f"无障碍树不可用（{why}）→ 尝试重启应用恢复")
                if IS_WIN and cfg["app"].get("start_if_down"):
                    subprocess_hidden([pwsh(), "-NoProfile", "-Command",
                                       f"Stop-Process -Name {cfg['app']['process_name']} -Force "
                                       f"-ErrorAction SilentlyContinue"])
                    time.sleep(3)
                    ok, why2 = start_app(cfg)
                    st.log(f"重启应用：{'成功' if ok else '失败'} {why2}")
                    time.sleep(20)
                fails += 1
            else:
                # 5) 投喂
                prompt = build_prompt(cfg, st, cycle, remaining)
                code, summary = feed_once(cfg, st, prompt, dry_run=a.dry_run)
                st.log(f"第 {cycle} 轮投喂：{summary}")
                st.status(last_feed=now(), last_feed_summary=summary, uia=why)
                if code == 0 and ("投喂 0" in (summary or "")) and ("跳过" in (summary or "")):
                    pass
                fails = 0 if code == 0 else fails + 1

            if fails >= 5:
                st.log("连续失败过多 → 收工（看 docs/故障排查.md）")
                st.status(state="error", reason="too-many-failures")
                return 1

            time.sleep(max(20, cfg["feed"]["interval_seconds"]))
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
            args += ["--config", a.config]
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
    port = a.port or int(pc.get("port") or 8788)
    token = a.token if a.token is not None else panel_token(cfg, st, host)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/" + (f"?token={token}" if token else "")
    print("zburn 面板已启动：")
    print_endpoints(host, port, token)
    if getattr(a, "open", False):
        open_browser(url)
    return panel.serve(cfg, host=host, port=port, token=token, config_path=a.config)


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
            args += ["--config", a.config]
        subprocess.Popen(args, stdout=open(st.dir / "watchdog.out", "a", encoding="utf-8"),
                         stderr=subprocess.STDOUT, creationflags=flags)
        print("看门狗: 已启动")

    host = a.host or pc.get("host") or "0.0.0.0"
    port = a.port or int(pc.get("port") or 8788)
    token = a.token if a.token is not None else panel_token(cfg, st, host)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/" + (f"?token={token}" if token else "")
    print("面板: 已启动")
    print_endpoints(host, port, token)
    if not a.no_browser:
        open_browser(url)
    return panel.serve(cfg, host=host, port=port, token=token, config_path=a.config)


def cmd_panel_token(a) -> int:
    """生成一个随机 token（面板鉴权用）。"""
    import secrets
    print(secrets.token_urlsafe(18))


# ---------------------------------------------------------------- 入口
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="zburn", description="熄屏守护：锁屏下持续驱动桌面 AI")
    ap.add_argument("-V", "--version", action="version", version=f"zburn {VERSION}")
    ap.add_argument("--config", default=os.environ.get("ZBURN_CONFIG"),
                    help="配置文件路径（默认环境变量 ZBURN_CONFIG，或内置默认值）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="环境自检").set_defaults(func=cmd_doctor)

    p = sub.add_parser("quota", help="读一次额度")
    p.add_argument("--refresh", action="store_true", help="先请求后端刷新")
    p.set_defaults(func=cmd_quota)

    p = sub.add_parser("status", help="看状态与日志")
    p.add_argument("--follow", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("probe", help="导出无障碍树（换应用时用）")
    p.add_argument("--filter", default="", help="按控件类型过滤，如 'Edit|Button'")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("inject", help="直接往输入框投一条")
    p.add_argument("--text", required=True)
    p.add_argument("--no-enter", action="store_true", help="只输入不提交")
    p.add_argument("--dry-run", action="store_true", help="只定位控件，不输入")
    p.set_defaults(func=cmd_inject)

    p = sub.add_parser("feed", help="投喂一轮")
    p.add_argument("--prompt", default=None, help="覆盖提示词（默认按模板轮换）")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_feed)

    p = sub.add_parser("run", help="守护循环")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_run)

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

    sub.add_parser("gateway", help="只探活/必要时启动网关").set_defaults(func=cmd_gateway)

    sub.add_parser("token", help="生成面板 token").set_defaults(func=cmd_panel_token)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
