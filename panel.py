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

    @staticmethod
    def _pid_image(pid: int) -> str:
        """进程的可执行文件路径；进程不存在/打不开返回空串。"""
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return ""
            return buf.value
        finally:
            k32.CloseHandle(h)

    def alive(self) -> bool:
        """守护是否真的活着。

        光看 PID 会误判：pid 文件放久了 PID 会被系统复用（实测复用成了 ZCode 的 node，
        于是面板显示"运行中"，点停止还会把应用杀掉）。所以还要核对可执行文件是不是 Python。
        """
        pid = self.pid()
        ok = False
        if pid and os.name == "nt":
            exe = self._pid_image(pid)
            if exe:
                name = pathlib.Path(exe).name.lower()
                ok = name.startswith("python") or exe.lower() == sys.executable.lower()
        elif pid:
            try:
                os.kill(pid, 0)
                ok = True
            except OSError:
                ok = False
        # 命令行直接起的 run（没写 pid 文件）也算在跑：看调度器锁
        return ok or bool(self._other_scheduler())

    def start(self) -> tuple[bool, str]:
        if self.alive():
            return False, "已在运行"
        stray = self._other_scheduler()
        if stray:
            return False, f"已有调度器在跑（{stray}），不会再起第二个；要重启请先点「停止」"
        self.stop_file.unlink(missing_ok=True)
        # --config 必须写在子命令前面，否则 argparse 会报 unrecognized arguments
        args = [sys.executable, str(pathlib.Path(__file__).resolve().parent / "zburn.py")]
        if self.config_path:
            args += ["--config", self.config_path]
        args.append("run")
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

    def _other_scheduler(self) -> str:
        """单调度器锁：没有 pid 文件但锁被别的活进程持有时，别硬起第二个。"""
        import os as _os
        lock = pathlib.Path(_os.environ.get("TEMP") or ".") / "zburn-scheduler.lock"
        try:
            parts = lock.read_text(errors="replace").strip().split()
        except OSError:
            return ""
        if len(parts) < 2 or not parts[1].isdigit():
            return ""
        pid = int(parts[1])
        if pid == (self.pid() or -1):
            return ""
        if _os.name == "nt":
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                               text=True, errors="replace",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if str(pid) not in (r.stdout or ""):
                return ""
        return f"{parts[0]} pid={pid}"

    def _kill_stray_daemons(self) -> int:
        """兜底：pid 文件对不上/缺失时，按命令行找「python … zburn.py … run」的守护进程，
        连同它的子进程（正在投喂的 pwsh）一起杀。匹配很收紧，不会碰到 panel/watchdog。"""
        if os.name != "nt":
            return 0
        import zburn
        ps = ("$pids = Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%'\" | "
              "Where-Object { $_.CommandLine -match 'zburn\\.py' -and $_.CommandLine -match '\\srun(\\s|$)' "
              "-and $_.CommandLine -notmatch 'panel|watchdog|doctor|inject|feed' } | "
              "Select-Object -ExpandProperty ProcessId; "
              "foreach ($p in $pids) { taskkill /PID $p /T /F 2>$null | Out-Null }; "
              "@($pids) -join ','")
        r = subprocess.run([zburn.pwsh(), "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout or "").strip()
        return len([p for p in out.split(",") if p.strip().isdigit()])

    def stop(self) -> tuple[bool, str]:
        self.stop_file.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
        pid = self.pid()
        killed = 0
        if pid and not self.alive():
            # pid 文件过期（PID 已被系统复用给别的程序）：先扔掉，别留着吓人
            self.pid_file.unlink(missing_ok=True)
        if pid and self.alive():
            # 先给守护一点点时间自己看 STOP 收工（正常路径，最干净），来不及就强杀
            for _ in range(6):
                time.sleep(0.15)
                if not self.alive():
                    break
            if self.alive():
                if os.name == "nt":
                    # /T 很关键：把守护正在跑的投喂脚本（pwsh）一起结束，
                    # 否则它成了孤儿进程，会继续把指令逐字打完（几分钟），感觉"关了还在打字"
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    killed += 1
                else:
                    try:
                        os.kill(pid, 9)
                        killed += 1
                    except OSError:
                        pass
        if not killed:
            # pid 对不上（被看门狗重启过 / pid 文件丢了）才去按命令行兜底找
            killed += self._kill_stray_daemons()
        if killed:
            return True, "已停止（守护与投喂子进程一起结束）"
        return True, "已请求停止（STOP 文件已放好）"


# ------------------------------------------------------------------ 配置字段
# 面板上能改的字段：扁平名 -> (配置里的路径, 类型)
CONFIG_FIELDS: dict[str, tuple[tuple[str, str], type]] = {
    "process_name": (("app", "process_name"), str),
    "app_exe": (("app", "exe"), str),
    "session_regex": (("uia", "session_regex"), str),
    "window_title_regex": (("uia", "window_title_regex"), str),
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
    "new_chat_button_regex": (("new_chat", "button_regex"), str),
    "new_chat_project": (("new_chat", "project"), str),
    "bridge_dir": (("paths", "bridge_dir"), str),
}


def apply_config_body(cur: dict, body: dict) -> tuple[dict, list[str]]:
    """把面板提交的扁平字段写进配置结构，返回 (新配置, 提示列表)。"""
    import re
    notes: list[str] = []
    for flat, value in body.items():
        if flat == "targets":       # 监控名单：面板「会话监控」勾选的对话标识
            if isinstance(value, list):
                cur.setdefault("feed", {})["targets"] = [
                    str(v).strip() for v in value if str(v).strip()]
            else:
                notes.append("targets 要是字符串数组，已忽略")
            continue
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
# 环境探测有代价（要起 PowerShell），而面板每 3 秒轮询一次——缓存几秒，
# 既不让页面变卡，也不会把 CPU 烧在一次次的进程枚举上。
_PROBE_TTL = 4.0
_probe_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, fn, ttl: float = _PROBE_TTL):
    hit = _probe_cache.get(key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception:
        val = None
    _probe_cache[key] = (now, val)
    return val


def collect(cfg: dict, st, sup: Supervisor, tail: int = 120,
            config_path: str | None = None, panel_url: str = "") -> dict:
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

    app_on = _cached("app_running", lambda: zburn.app_running(cfg)) if os.name == "nt" else False
    locked = _cached("locked", zburn.desktop_locked) if os.name == "nt" else False

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

    running = sup.alive()
    agent_list = []
    handoff = {}
    try:
        agent_list = zburn.agentlib.describe(cfg, st)
        worker = zburn.agentlib.orchestrator(cfg)[1]
        handoff = zburn.agentlib.handoff_status(cfg, st, worker["id"] if worker else "")
    except Exception:
        pass
    sup_prompt = ((cfg.get("orchestrator") or {}).get("prompt")
                  or zburn.agentlib.DEFAULT_SUPERVISOR_PROMPT)
    return {
        "version": VERSION,
        "running": running,
        "panel_url": panel_url,
        # pid 文件会过期（PID 被系统复用），守护没在跑就别把旧 pid 报给前端
        "pid": sup.pid() if running else None,
        "heartbeat_age": hb_age,
        # 只有守护还活着时才算「正在停」：STOP 文件会一直留在磁盘上，
        # 光看文件存在会让面板永远显示「停止中」
        "stop_pending": running and sup.stop_file.exists(),
        "status": status,
        "app_running": bool(app_on),
        "locked": bool(locked),
        "gateway_ok": gw,
        "remaining": remaining,
        "quota_history": hist,
        "logs": logs,
        "agents": agent_list,
        "handoff": handoff,
        "supervisor_prompt": sup_prompt,
        "bridge_dir": str(zburn.agentlib.bridge_dir(cfg)),
        "config": {
            "process_name": cfg["app"]["process_name"],
            "app_exe": cfg["app"].get("exe", ""),
            "session_regex": cfg["uia"]["session_regex"],
            "window_title_regex": cfg["uia"].get("window_title_regex", ""),
            "targets": cfg["feed"].get("targets", []),
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
            "new_chat_button_regex": (cfg.get("new_chat") or {}).get("button_regex", ""),
            "new_chat_project": (cfg.get("new_chat") or {}).get("project", ""),
            "bridge_dir": str(zburn.agentlib.bridge_dir(cfg)),
        },
    }


# ------------------------------------------------------------------ 页面
from panel_ui import PAGE  # 前端单页（见 panel_ui.py）


# ------------------------------------------------------------------ 服务
def make_handler(cfg: dict, st, sup: Supervisor, config_path: str | None, token: str,
                 host: str = "127.0.0.1", port: int = 0):
    import zburn

    class Handler(BaseHTTPRequestHandler):
        server_version = f"zburn-panel/{VERSION}"

        def log_message(self, fmt, *args):  # 面板不刷访问日志
            pass

        def _panel_url(self) -> str:
            """手机同网段访问的完整地址（含 token）；只绑了本机或没 token 时给空串。

            绑定的是具体地址就用它，绑 0.0.0.0/:: 才现查局域网 IP——面板
            实际怎么监听决定给什么地址，配置里写什么不算数。
            """
            if not token or host in ("127.0.0.1", "localhost", "::1"):
                return ""
            ip = host if host not in ("0.0.0.0", "::") else zburn.lan_ip()
            if not ip or ip in ("127.0.0.1", "::1"):
                return ""
            return f"http://{ip}:{port}/?token={token}"

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
                return self._json(200, collect(cfg, st, sup, config_path=config_path,
                                               panel_url=self._panel_url()))
            if u.path == "/api/sessions":
                # 扫描指定 agent 侧栏的对话（只读，不输入），给「会话监控」卡片勾选用
                q = parse_qs(u.query)
                agent = (q.get("agent") or [None])[0]
                spec = zburn.agentlib.find_agent(cfg, agent)
                acfg = zburn.agentlib.agent_view(cfg, spec, st) if spec else cfg
                sessions, msg = zburn.scan_sessions(acfg)
                return self._json(200, {
                    "ok": msg.startswith("扫描到"),
                    "agent": spec["id"] if spec else (cfg.get("agent_id") or "default"),
                    "sessions": sessions,
                    "targets": acfg["feed"].get("targets", []),
                    "msg": msg,
                })
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
                spec = zburn.agentlib.find_agent(cfg, body.get("agent"))
                if not spec:
                    spec = zburn.agentlib.orchestrator(cfg)[1] or zburn.agentlib.agent_specs(cfg)[0]
                acfg = zburn.agentlib.agent_view(cfg, spec, st)
                prompt = (body.get("prompt") or "").strip() or \
                    zburn.build_prompt(acfg, st, int(time.time() / 60) % 3, zburn.read_quota(cfg))
                code, summary = zburn.feed_once(acfg, st, prompt)
                return self._json(200, {"ok": code == 0, "msg": summary, "agent": spec["id"]})
            if u.path == "/api/gateway":
                res = zburn.ensure_gateway(cfg, st)
                return self._json(200, {"ok": "started" in res or "alive" in res, "msg": res})
            if u.path == "/api/agent":
                action = str(body.get("action") or "").strip()
                if action == "supervise":
                    code, msg = zburn.supervise_now(
                        cfg, st, dry_run=bool(body.get("dry_run")),
                        wait=int(body.get("wait") or 0))
                    st.log(f"[panel] 唤醒监督：{msg}")
                    return self._json(200, {"ok": code == 0, "msg": msg})
                spec = zburn.agentlib.find_agent(cfg, body.get("agent"))
                if not spec:
                    return self._json(400, {"ok": False, "msg": "没指定 agent"})
                if action == "toggle":
                    want = bool(body.get("enabled", not spec.get("enabled", True)))
                    cp = pathlib.Path(config_path) if config_path else None
                    cur = {}
                    if cp and cp.exists():
                        try:
                            cur = json.loads(cp.read_text(encoding="utf-8"))
                        except Exception as e:
                            return self._json(500, {"ok": False, "msg": f"配置读不出来：{e}"})
                    hit = False
                    for ent in (cur.get("agents") or []):
                        if isinstance(ent, dict) and ent.get("id") == spec["id"]:
                            ent["enabled"] = want
                            hit = True
                    if not hit:
                        return self._json(400, {"ok": False,
                                                "msg": "这个 agent 来自顶层 app，不在 agents 列表中，无法单独启停"})
                    if cp:
                        try:
                            (cp.parent / (cp.name + ".bak")).write_text(
                                cp.read_text(encoding="utf-8"), encoding="utf-8")
                        except OSError:
                            pass
                        cp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
                    cfg["agents"] = cur.get("agents") or []
                    st.log(f"[panel] agent {spec['id']} → {'启用' if want else '停用'}")
                    return self._json(200, {"ok": True,
                                            "msg": f"{spec['id']} 已{'启用' if want else '停用'}（守护下一轮生效）"})
                if action == "targets":
                    targets = body.get("targets")
                    if not isinstance(targets, list):
                        return self._json(400, {"ok": False, "msg": "targets 要是数组"})
                    targets = [str(t).strip() for t in targets if str(t).strip()]
                    cp = pathlib.Path(config_path) if config_path else None
                    cur = {}
                    if cp and cp.exists():
                        try:
                            cur = json.loads(cp.read_text(encoding="utf-8"))
                        except Exception as e:
                            return self._json(500, {"ok": False, "msg": f"配置读不出来：{e}"})
                    hit = False
                    for ent in (cur.get("agents") or []):
                        if isinstance(ent, dict) and ent.get("id") == spec["id"]:
                            ent.setdefault("feed", {})["targets"] = targets
                            hit = True
                    if not hit:
                        return self._json(400, {"ok": False,
                                                "msg": "这个 agent 来自顶层 app，不在 agents 列表中"})
                    if cp:
                        try:
                            (cp.parent / (cp.name + ".bak")).write_text(
                                cp.read_text(encoding="utf-8"), encoding="utf-8")
                        except OSError:
                            pass
                        cp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
                    cfg["agents"] = cur.get("agents") or []
                    st.log(f"[panel] agent {spec['id']} targets={targets}")
                    return self._json(200, {"ok": True,
                                            "msg": f"{spec['id']} 监控名单已保存（{len(targets)} 个）"})
                if action == "cleardyn":
                    zburn.agentlib.clear_dynamic_targets(st, spec["id"])
                    st.log(f"[panel] agent {spec['id']} 清空动态监控名单")
                    return self._json(200, {"ok": True, "msg": f"{spec['id']} 动态监控名单已清空"})
                acfg = zburn.agentlib.agent_view(cfg, spec, st)
                if action == "feed":
                    prompt = (body.get("prompt") or "").strip() or \
                        zburn.build_prompt(acfg, st, int(time.time() / 60) % 3, zburn.read_quota(cfg))
                    code, msg = zburn.feed_once(acfg, st, prompt)
                elif action == "newchat":
                    prompt = (body.get("prompt") or "").strip()
                    if not prompt:
                        p = pathlib.Path(acfg["prompt"].get("file") or "next-prompt.txt")
                        if not p.is_absolute():
                            p = st.dir / p
                        prompt = p.read_text(encoding="utf-8").strip() if p.exists() else ""
                    if not prompt:
                        return self._json(400, {"ok": False, "msg": "新对话指令为空"})
                    code, msg = zburn.new_chat_once(
                        acfg, st, prompt,
                        project=(body.get("project") or acfg["new_chat"].get("project", "")),
                        agent_id=spec["id"])
                elif action == "snapshot":
                    text = zburn.snapshot_agent(acfg, st, spec)
                    return self._json(200, {"ok": True, "agent": spec["id"],
                                            "msg": text[:8000]})
                else:
                    return self._json(400, {"ok": False, "msg": f"未知 action: {action}"})
                st.log(f"[panel] {spec['id']} {action}：{msg}")
                return self._json(200, {"ok": code == 0, "msg": msg, "agent": spec["id"]})
            if u.path == "/api/handoff":
                act = str(body.get("action") or "feed")
                sup, work = zburn.agentlib.orchestrator(cfg)
                default_agent = work["id"] if work else ""
                if act == "clear":
                    n = zburn.agentlib.clear_handoff(cfg, st)
                    if body.get("clear_pending"):
                        for s in zburn.agentlib.agent_specs(cfg):
                            zburn.agentlib.clear_pending(st, s["id"])
                    return self._json(200, {"ok": True, "msg": f"已清空 {n} 个交接文件"})
                if act == "apply":
                    rec = zburn.agentlib.consume_handoff(cfg, st, default_agent)
                    if not rec:
                        return self._json(200, {"ok": True, "msg": "没有待处理交接"})
                    pending = zburn.apply_handoff(cfg, st, rec, work)
                    return self._json(200, {"ok": bool(pending), "msg": "已排入待执行队列",
                                            "pending": pending})
                try:
                    rec = zburn.agentlib.write_handoff(
                        cfg, st, act, body.get("prompt") or "",
                        agent=body.get("agent") or default_agent,
                        project=body.get("project") or "")
                except ValueError as e:
                    return self._json(400, {"ok": False, "msg": str(e)})
                st.log(f"[panel] 写入交接 {rec['action']} → {rec['agent']}")
                return self._json(200, {"ok": True, "msg": "交接已写入", "handoff": rec})
            if u.path == "/api/supervisor_prompt":
                text = str(body.get("text") or "").strip()
                cp = pathlib.Path(config_path) if config_path else None
                cur = {}
                if cp and cp.exists():
                    try:
                        cur = json.loads(cp.read_text(encoding="utf-8"))
                    except Exception as e:
                        return self._json(500, {"ok": False, "msg": f"配置读不出来：{e}"})
                cur.setdefault("orchestrator", {})["prompt"] = text
                if cp:
                    try:
                        (cp.parent / (cp.name + ".bak")).write_text(
                            cp.read_text(encoding="utf-8"), encoding="utf-8")
                    except OSError:
                        pass
                    cp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
                zerun_stub(cfg, cur)
                st.log("[panel] 监督提示词已更新")
                return self._json(200, {"ok": True, "msg": "监督提示词已保存"})
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
    handler = make_handler(cfg, st, sup, config_path, token, host=host, port=port)
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
