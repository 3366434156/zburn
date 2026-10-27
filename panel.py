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
                               capture_output=True, text=True, errors="replace")
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
        p = subprocess.Popen(args, stdout=out, stderr=out, creationflags=flags)
        self.pid_file.write_text(str(p.pid))
        return True, f"已启动 pid={p.pid}"

    def stop(self) -> tuple[bool, str]:
        self.stop_file.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
        pid = self.pid()
        if pid and self.alive():
            time.sleep(1.5)
            if self.alive():
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
                else:
                    try:
                        os.kill(pid, 9)
                    except OSError:
                        pass
                return True, "已停止（STOP 文件 + 强制结束）"
        return True, "已请求停止"


# ------------------------------------------------------------------ 状态汇总
def collect(cfg: dict, st, sup: Supervisor, tail: int = 120) -> dict:
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
            "session_regex": cfg["uia"]["session_regex"],
            "max_sessions": cfg["feed"]["max_sessions"],
            "interval_seconds": cfg["feed"]["interval_seconds"],
            "stop_below": cfg["quota"]["stop_below"],
            "providers": cfg["quota"]["provider"],
            "prompt_file": cfg["prompt"].get("file"),
        },
    }


# ------------------------------------------------------------------ 页面
PAGE = r"""<!DOCTYPE html><html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>zburn 面板</title>
<style>
:root{--bg:#0b0f14;--card:#131a22;--line:#1f2b38;--fg:#d8e6f2;--dim:#7b91a6;--ok:#3ddc97;--warn:#ffc857;--bad:#ff6b6b;--acc:#4cc9f0}
*{box-sizing:border-box}
html,body{max-width:100%;overflow-x:hidden}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,"Segoe UI",Roboto,sans-serif;padding:12px 12px 40px}
h1{font-size:17px;margin:4px 0 12px;display:flex;align-items:center;gap:8px}
.badge{font-size:12px;padding:2px 8px;border-radius:10px;background:#1d2a36;color:var(--dim)}
.badge.on{background:rgba(61,220,151,.15);color:var(--ok)}
.badge.off{background:rgba(255,107,107,.15);color:var(--bad)}
.grid{display:grid;gap:10px;grid-template-columns:minmax(0,1fr)}
@media(min-width:640px){.grid{grid-template-columns:repeat(3,minmax(0,1fr))}
.kv{display:flex;justify-content:space-between;align-items:baseline}
.kv span{display:inline}
.kv b{display:inline;max-width:58%;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;min-width:0;overflow:hidden}
.card h2{font-size:12px;margin:0 0 8px;color:var(--dim);font-weight:600;letter-spacing:.04em}
.kv{display:block;padding:4px 0;font-size:13px;min-width:0}
.kv span{display:block;color:var(--dim);font-size:11px;line-height:1.3}
.kv b{display:block;font-weight:600;max-width:100%;overflow-wrap:anywhere;line-height:1.35}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0;min-width:0}
button{flex:1 1 44%;min-width:0;min-height:44px;border-radius:10px;border:1px solid var(--line);background:#1a2430;color:var(--fg);font-size:14px;font-weight:600}
button:active{transform:scale(.98)}
button.primary{background:linear-gradient(180deg,#1f6f5a,#155446);border-color:#2a8f74;color:#eafff7}
button.danger{background:linear-gradient(180deg,#6f2530,#541b24);border-color:#8f2f3d;color:#ffe9ec}
button.ghost{background:transparent}
input,textarea,select{width:100%;background:#0e151c;border:1px solid var(--line);border-radius:9px;color:var(--fg);padding:10px;font:13px/1.4 ui-monospace,Consolas,monospace}
textarea{min-height:64px;resize:vertical}
label{display:block;font-size:12px;color:var(--dim);margin:10px 0 4px}
pre#log{margin:0;width:100%;max-width:100%;max-height:44vh;overflow:auto;font:11.5px/1.5 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-all;color:#a9c2d6}
.spark{display:flex;align-items:flex-end;gap:2px;height:48px;margin-top:6px;width:100%;overflow:hidden}
.spark i{flex:1;background:var(--acc);opacity:.55;border-radius:2px 2px 0 0;min-height:2px}
.muted{color:var(--dim);font-size:12px}
.toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:#1d2a36;border:1px solid var(--line);padding:10px 16px;border-radius:10px;font-size:13px;opacity:0;transition:.2s;pointer-events:none}
.toast.show{opacity:1}
</style></head><body>
<h1>zburn 面板 <span id="runBadge" class="badge">…</span></h1>

<div class="grid">
  <div class="card"><h2>运行</h2>
    <div class="kv"><span>守护进程</span><b id="s_run">-</b></div>
    <div class="kv"><span>心跳</span><b id="s_hb">-</b></div>
    <div class="kv"><span>轮次</span><b id="s_cycle">-</b></div>
    <div class="kv"><span>上次投喂</span><b id="s_feed" style="font-size:11px">-</b></div>
  </div>
  <div class="card"><h2>额度</h2>
    <div class="kv"><span>剩余</span><b id="s_quota">-</b></div>
    <div class="kv"><span>阈值</span><b id="s_stop">-</b></div>
    <div class="spark" id="spark"></div>
  </div>
  <div class="card"><h2>环境</h2>
    <div class="kv"><span>应用</span><b id="s_app">-</b></div>
    <div class="kv"><span>桌面</span><b id="s_lock">-</b></div>
    <div class="kv"><span>网关</span><b id="s_gw">-</b></div>
  </div>
</div>

<div class="actions">
  <button class="primary" onclick="act('start')">启动守护</button>
  <button class="danger" onclick="act('stop')">停止</button>
  <button onclick="act('feed')">投喂一轮</button>
  <button onclick="act('gateway')">拉起网关</button>
  <button class="ghost" onclick="act('refresh')">刷新</button>
</div>

<div class="card"><h2>当前指令（会写进 next-prompt.txt，插件也读它）</h2>
  <textarea id="prompt"></textarea>
  <div class="actions"><button onclick="savePrompt()">保存指令</button></div>
</div>

<div class="card"><h2>参数</h2>
  <label>会话名正则（只喂匹配的会话）</label><input id="cfg_session">
  <label>巡检间隔（秒）</label><input id="cfg_interval" type="number" min="20">
  <label>并发会话上限</label><input id="cfg_max" type="number" min="1" max="12">
  <label>额度低于它自动收工</label><input id="cfg_stop" type="number" min="0">
  <div class="actions"><button onclick="saveCfg()">保存参数</button></div>
</div>

<div class="card"><h2>日志（最近 120 行，3 秒自动刷新）</h2><pre id="log">…</pre></div>

<div class="toast" id="toast"></div>
<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
const H = TOKEN ? {'X-Zburn-Token': TOKEN} : {};
let first = true;

function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),1600);}
const fmt = n => n===null||n===undefined ? '-' : Number(n).toLocaleString();
const badge = (el, ok, on, off) => {el.textContent = ok?on:off; el.className = 'badge '+(ok?'on':'off');};

async function api(path, opt={}){
  const r = await fetch(path, {headers:{...H, ...(opt.body?{'Content-Type':'application/json'}:{})}, ...opt});
  if(!r.ok) throw new Error(await r.text());
  return r.headers.get('content-type')?.includes('json') ? r.json() : r.text();
}

async function act(a){
  try{
    if(a==='refresh'){ await load(); toast('已刷新'); return; }
    const r = await api('/api/'+a, {method:'POST', body:'{}'});
    toast(r.msg || 'ok');
    setTimeout(load, 700);
  }catch(e){ toast('失败: '+e.message); }
}

async function load(){
  let s;
  try{ s = await api('/api/state'); }catch(e){ document.getElementById('log').textContent = '连接失败: '+e.message; return; }
  badge(document.getElementById('runBadge'), s.running, '运行中', s.stop_pending?'停止中':'已停止');
  document.getElementById('s_run').textContent = s.running ? ('pid '+s.pid) : '未运行';
  document.getElementById('s_hb').textContent = s.heartbeat_age===null ? '-' : (s.heartbeat_age+' 秒前');
  document.getElementById('s_cycle').textContent = s.status?.cycle ?? '-';
  document.getElementById('s_feed').textContent = (s.status?.last_feed_summary||'-').trim().slice(0,28);
  document.getElementById('s_quota').textContent = fmt(s.remaining);
  document.getElementById('s_stop').textContent = fmt(s.config.stop_below);
  badge(document.getElementById('s_app'), s.app_running, '在运行', '未运行');
  document.getElementById('s_lock').textContent = s.locked ? '锁屏' : '解锁';
  document.getElementById('s_lock').style.color = s.locked ? 'var(--warn)' : 'var(--ok)';
  badge(document.getElementById('s_gw'), s.gateway_ok, '可访问', '不通');
  const log = document.getElementById('log');
  log.textContent = (s.logs||[]).join('\n') || '（还没有日志）';
  log.scrollTop = log.scrollHeight;

  // 额度历史迷你图
  const hist = s.quota_history||[];
  const sp = document.getElementById('spark');
  if(hist.length>1){
    const mx = Math.max(...hist.map(h=>h[1])) || 1;
    sp.innerHTML = hist.map(h=>`<i style="height:${Math.max(3,Math.round(h[1]/mx*44))}px" title="${h[1]}"></i>`).join('');
    sp.title = hist[0][0]+' → '+hist[hist.length-1][0]+`：${hist[0][1]} → ${hist[hist.length-1][1]}`;
  } else sp.innerHTML = '<span class="muted">额度历史还不够，多跑几轮就有曲线</span>';

  if(first){
    document.getElementById('cfg_session').value = s.config.session_regex;
    document.getElementById('cfg_interval').value = s.config.interval_seconds;
    document.getElementById('cfg_max').value = s.config.max_sessions;
    document.getElementById('cfg_stop').value = s.config.stop_below;
    fetch('/api/prompt',{headers:H}).then(r=>r.text()).then(t=>document.getElementById('prompt').value=t);
    first = false;
  }
}

async function savePrompt(){
  const text = document.getElementById('prompt').value;
  await api('/api/prompt', {method:'POST', body: JSON.stringify({text})});
  toast('指令已保存');
}
async function saveCfg(){
  const body = {
    session_regex: document.getElementById('cfg_session').value,
    interval_seconds: +document.getElementById('cfg_interval').value,
    max_sessions: +document.getElementById('cfg_max').value,
    stop_below: +document.getElementById('cfg_stop').value
  };
  await api('/api/config', {method:'POST', body: JSON.stringify(body)});
  toast('参数已保存（下一轮生效）');
}

load(); setInterval(load, 3000);
</script></body></html>
"""


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
                return self._json(200, collect(cfg, st, sup))
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
                if config_path:
                    cp = pathlib.Path(config_path)
                    cur = json.loads(cp.read_text(encoding="utf-8")) if cp.exists() else {}
                else:
                    cur = {}
                if "session_regex" in body:
                    cur.setdefault("uia", {})["session_regex"] = body["session_regex"]
                if "interval_seconds" in body:
                    cur.setdefault("feed", {})["interval_seconds"] = int(body["interval_seconds"])
                if "max_sessions" in body:
                    cur.setdefault("feed", {})["max_sessions"] = int(body["max_sessions"])
                if "stop_below" in body:
                    cur.setdefault("quota", {})["stop_below"] = int(body["stop_below"])
                if config_path:
                    cp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
                    # 热更新内存配置（下一轮 run 会重新读到文件，面板自己也立刻生效）
                    import importlib
                    importlib.reload(zerun_stub(cfg, cur))
                st.log(f"[panel] 参数已更新：{body}")
                return self._json(200, {"ok": True, "msg": "参数已保存（守护进程下轮生效）"})
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


def serve(cfg: dict, *, host: str, port: int, token: str, config_path: str | None) -> int:
    from zburn import State

    st = State(cfg)
    st.log(f"[panel] 面板启动 http://{host}:{port}/  鉴权={'token' if token else '仅本机'}")
    sup = Supervisor(cfg, st.dir, config_path)
    handler = make_handler(cfg, st, sup, config_path, token)
    srv = ThreadingHTTPServer((host, port), handler)
    print(f"zburn 面板: http://{host}:{port}/" + (f"?token={token}" if token else ""))
    print("手机/别的电脑访问：把 host 设成 0.0.0.0，并用 --token 保护；" 
          "局域网地址 = 本机 IP + 端口。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n面板已停止")
    return 0
