# -*- coding: utf-8 -*-
"""zburn 面板：一个页面管住熄屏守护（手机/别的电脑都能开）。

设计取舍：
* 不依赖前端构建：单个 HTML 字符串内嵌 CSS/JS，手机打开即用；
* 不用第三方框架：标准库 ThreadingHTTPServer + fetch 轮询（3 秒）；
* 面板自己就是控制端：启停守护/投喂一轮/改配置/看日志/看额度历史/拉起网关；
* 默认只监听 127.0.0.1；要远程访问必须给 --token（或 config.panel.token），
  否则别人扫到端口就能操作你的机器。
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "1.0.0"


# ------------------------------------------------------------------ 进程控制
class Supervisor:
    """守护进程句柄：start/stop/存活判断。"""

    def __init__(self, cfg: dict, state_dir: pathlib.Path, config_path: str | None):
        self.cfg = cfg
        self.dir = state_dir
        self.config_path = config_path
        self.pid_file = state_dir / "supervisor.pid"
        self.stop_file = pathlib.Path(cfg["paths"]["stop_file"])

    def pid(self) -> int | None:
        try:
            return int(self.pid_file.read_text().strip())
        except Exception:
            return None

    def alive(self) -> bool:
        pid = self.pid()
        if not pid:
            return False
        if os.name == "nt":
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                               capture_output=True, text=True, errors="replace",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return str(pid) in (r.stdout or "")
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def start(self) -> tuple[bool, str]:
        if self.alive():
            return False, "已在运行"
        self.stop_file.unlink(missing_ok=True)
        args = [sys.executable, str(pathlib.Path(__file__).resolve().parent / "zburn.py"), "run"]
        if self.config_path:
            args += ["--config", self.config_path]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        out = open(self.dir / "supervisor.out", "a", encoding="utf-8")
        startup = 0
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0  # SW_HIDE：绝不让控制台窗口/终端标签页冒出来
        p = subprocess.Popen(args, stdout=out, stderr=out, creationflags=flags,
                             startupinfo=startup or None)
        self.pid_file.write_text(str(p.pid))
        return True, f"已启动 pid={p.pid}"

    def stop(self) -> tuple[bool, str]:
        self.stop_file.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
        pid = self.pid()
        if pid and self.alive():
            time.sleep(1.5)
            if self.alive():
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                else:
                    try:
                        os.kill(pid, 9)
                    except OSError:
                        pass
                return True, "已停止（STOP 文件 + 强制结束）"
        return True, "已请求停止"


# ------------------------------------------------------------------ 配置字段
# 面板上能改的字段：扁平名 -> (配置里的路径, 类型)
CONFIG_FIELDS: dict[str, tuple[tuple[str, str], type]] = {
    "process_name": (("app", "process_name"), str),
    "app_exe": (("app", "exe"), str),
    "session_regex": (("uia", "session_regex"), str),
    "send_button_regex": (("uia", "send_button_regex"), str),
    "interval_seconds": (("feed", "interval_seconds"), int),
    "max_sessions": (("feed", "max_sessions"), int),
    "health_url": (("gateway", "health_url"), str),
    "start_command": (("gateway", "start_command"), str),
    "work_dir": (("gateway", "work_dir"), str),
    "quota_url": (("quota", "url"), str),
    "admin_key": (("quota", "admin_key"), str),
    "stop_below": (("quota", "stop_below"), int),
    "panel_host": (("panel", "host"), str),
    "panel_port": (("panel", "port"), int),
}


def apply_config_body(cur: dict, body: dict) -> tuple[dict, list[str]]:
    """把面板提交的扁平字段写进配置结构，返回 (新配置, 提示列表)。"""
    import re
    notes: list[str] = []
    for flat, value in body.items():
        spec = CONFIG_FIELDS.get(flat)
        if not spec:
            continue
        (sec, key), typ = spec
        if typ is int:
            try:
                value = int(str(value).strip() or 0)
            except ValueError:
                notes.append(f"{flat} 不是数字，已忽略")
                continue
        else:
            value = str(value)
        if flat == "session_regex":
            try:
                re.compile(value)
            except re.error as e:
                notes.append(f"会话正则写错了（{e}），已忽略")
                continue
        if flat == "panel_port" and not (1 <= value <= 65535):
            notes.append("端口要在 1-65535 之间，已忽略")
            continue
        cur.setdefault(sec, {})[key] = value
    return cur, notes


# ------------------------------------------------------------------ 状态汇总
def collect(cfg: dict, st, sup: Supervisor, tail: int = 120,
            config_path: str | None = None) -> dict:
    import zburn  # 延迟导入，避免循环

    status = {}
    if st.status_path.exists():
        try:
            status = json.loads(st.status_path.read_text(encoding="utf-8"))
        except Exception:
            status = {}
    hb_age = None
    if st.heartbeat_path.exists():
        try:
            hb_age = int(time.time()) - int(st.heartbeat_path.read_text() or 0)
        except ValueError:
            pass

    gw = None
    try:
        zburn.http_json(cfg["gateway"]["health_url"], timeout=3)
        gw = True
    except Exception:
        gw = False

    remaining = None
    try:
        remaining = zburn.read_quota(cfg)
    except Exception:
        pass

    logs = []
    if st.log_path.exists():
        try:
            logs = st.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]
        except OSError:
            pass

    hist = []
    if st.quota_csv.exists():
        try:
            lines = st.quota_csv.read_text(encoding="utf-8-sig").splitlines()
            for ln in lines[1:][-60:]:
                parts = ln.split(",")
                if len(parts) >= 2 and parts[1].strip().isdigit():
                    hist.append([parts[0], int(parts[1])])
        except OSError:
            pass

    return {
        "version": VERSION,
        "running": sup.alive(),
        "pid": sup.pid(),
        "heartbeat_age": hb_age,
        "stop_pending": sup.stop_file.exists(),
        "status": status,
        "app_running": zburn.app_running(cfg) if os.name == "nt" else False,
        "locked": zburn.desktop_locked() if os.name == "nt" else False,
        "gateway_ok": gw,
        "remaining": remaining,
        "quota_history": hist,
        "logs": logs,
        "config": {
            "process_name": cfg["app"]["process_name"],
            "app_exe": cfg["app"].get("exe", ""),
            "session_regex": cfg["uia"]["session_regex"],
            "send_button_regex": cfg["uia"].get("send_button_regex", ""),
            "max_sessions": cfg["feed"]["max_sessions"],
            "interval_seconds": cfg["feed"]["interval_seconds"],
            "stop_below": cfg["quota"]["stop_below"],
            "providers": cfg["quota"]["provider"],
            "prompt_file": cfg["prompt"].get("file"),
            "health_url": cfg["gateway"].get("health_url", ""),
            "start_command": cfg["gateway"].get("start_command", ""),
            "work_dir": cfg["gateway"].get("work_dir", ""),
            "quota_url": cfg["quota"].get("url", ""),
            "admin_key": cfg["quota"].get("admin_key", ""),
            "panel_host": (cfg.get("panel") or {}).get("host", "127.0.0.1"),
            "panel_port": (cfg.get("panel") or {}).get("port", 8788),
            "config_path": config_path or "<内置默认>",
        },
    }


# ------------------------------------------------------------------ 页面
from panel_ui import PAGE  # 前端单页（见 panel_ui.py）


# ------------------------------------------------------------------ 服务
def make_handler(cfg: dict, st, sup: Supervisor, config_path: str | None, token: str):
    import zburn

    class Handler(BaseHTTPRequestHandler):
        server_version = f"zburn-panel/{VERSION}"

        def log_message(self, fmt, *args):  # 面板不刷访问日志
            pass

        # ---------- 鉴权
        def _authorized(self) -> bool:
            if not token:
                return self.client_address[0] in ("127.0.0.1", "::1")
            q = parse_qs(urlparse(self.path).query)
            return token in q.get("token", []) or self.headers.get("X-Zburn-Token") == token

        def _json(self, code: int, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _text(self, code: int, text: str, ctype="text/html; charset=utf-8"):
            body = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                if not self._authorized():
                    return self._text(401, "<h3>需要 token</h3><p>地址后加 ?token=你的token</p>")
                return self._text(200, PAGE)
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            if u.path == "/api/state":
                return self._json(200, collect(cfg, st, sup, config_path=config_path))
            if u.path == "/api/prompt":
                p = pathlib.Path(cfg["prompt"].get("file") or "next-prompt.txt")
                if not p.is_absolute():
                    p = st.dir / p
                txt = p.read_text(encoding="utf-8") if p.exists() else ""
                return self._text(200, txt, "text/plain; charset=utf-8")
            if u.path == "/api/logs":
                n = int(parse_qs(u.query).get("tail", ["200"])[0])
                lines = st.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:] \
                    if st.log_path.exists() else []
                return self._text(200, "\n".join(lines), "text/plain; charset=utf-8")
            if u.path == "/api/health":
                return self._json(200, {"ok": True, "version": VERSION})
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                body = json.loads(raw.decode() or "{}")
            except Exception:
                body = {}

            if u.path == "/api/start":
                ok, msg = sup.start()
                st.log(f"[panel] 启动守护：{msg}")
                return self._json(200, {"ok": ok, "msg": msg})
            if u.path == "/api/stop":
                ok, msg = sup.stop()
                st.log(f"[panel] 停止守护：{msg}")
                return self._json(200, {"ok": ok, "msg": msg})
            if u.path == "/api/feed":
                prompt = zburn.build_prompt(cfg, st, int(time.time() / 60) % 3, zburn.read_quota(cfg))
                code, summary = zburn.feed_once(cfg, st, prompt)
                return self._json(200, {"ok": code == 0, "msg": summary})
            if u.path == "/api/gateway":
                res = zburn.ensure_gateway(cfg, st)
                return self._json(200, {"ok": "started" in res or "alive" in res, "msg": res})
            if u.path == "/api/prompt":
                text = (body.get("text") or "").strip()
                p = pathlib.Path(cfg["prompt"].get("file") or "next-prompt.txt")
                if not p.is_absolute():
                    p = st.dir / p
                p.write_text(text, encoding="utf-8")
                st.log(f"[panel] 指令已更新：{text[:60]}…")
                return self._json(200, {"ok": True, "msg": "指令已保存"})
            if u.path == "/api/config":
                cp = pathlib.Path(config_path) if config_path else None
                cur = {}
                if cp and cp.exists():
                    try:
                        cur = json.loads(cp.read_text(encoding="utf-8"))
                    except Exception as e:
                        return self._json(500, {"ok": False, "msg": f"配置读不出来：{e}"})
                cur, notes = apply_config_body(cur, body)
                if cp:
                    if cp.exists():          # 留一份上一版，改坏了能手动还原
                        try:
                            (cp.parent / (cp.name + ".bak")).write_text(
                                cp.read_text(encoding="utf-8"), encoding="utf-8")
                        except OSError:
                            pass
                    cp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
                    zerun_stub(cfg, cur)     # 面板自身立刻生效；守护进程下轮读文件
                st.log(f"[panel] 参数已更新：{json.dumps(body, ensure_ascii=False)[:200]}")
                msg = "参数已保存" + ("（守护进程下一轮生效）" if sup.alive() else "")
                if notes:
                    msg += "；" + "；".join(notes)
                return self._json(200, {"ok": True, "msg": msg, "notes": notes})
            return self._json(404, {"error": "not found"})

    return Handler


def zerun_stub(cfg: dict, over: dict):
    """把面板改的字段合并进 cfg（供当前进程内的后续请求使用）。"""
    import zburn
    merged = zburn.deep_merge(cfg, {k: v for k, v in over.items()
                                    if isinstance(v, dict)})
    cfg.clear()
    cfg.update(merged)
    return sys.modules[__name__]


def pick_port(host: str, port: int, tries: int = 20) -> int:
    """端口被占用就往后找一个能绑的：绝不抢别人的端口，也绝不让面板起不来。"""
    import socket
    for p in range(port, port + tries):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return port


def serve(cfg: dict, *, host: str, port: int, token: str, config_path: str | None) -> int:
    from zburn import State

    st = State(cfg)
    port = pick_port(host, port)
    st.log(f"[panel] 面板启动 http://{host}:{port}/  鉴权={'token' if token else '仅本机'}")
    sup = Supervisor(cfg, st.dir, config_path)
    handler = make_handler(cfg, st, sup, config_path, token)
    try:
        srv = ThreadingHTTPServer((host, port), handler)
    except OSError as e:
        print(f"面板启动失败：{host}:{port} 绑不上（{e}）。换个端口：--port 8791")
        return 1
    print(f"zburn 面板已就绪: http://127.0.0.1:{port}/" + (f"?token={token}" if token else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n面板已停止")
    return 0
