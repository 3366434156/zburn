# -*- coding: utf-8 -*-
"""面板前端（单个 HTML 字符串）。设计目标：手机上像原生 App，桌面上像后台。

配色/间距刻意贴近常见管理后台：深底、卡片分层、状态用色点 + 药丸、操作放底部吸附条。
"""

PAGE = r"""<!DOCTYPE html><html lang="zh"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0d1117">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>zburn 控制台</title>
<style>
:root{
  --bg:#0d1117; --bg2:#0a0e13; --card:#161b22; --card2:#1b222b; --line:#243040;
  --fg:#e6edf3; --dim:#8b949e; --dim2:#6b7480;
  --acc:#2f81f7; --acc2:#58a6ff; --ok:#3fb950; --warn:#d29922; --bad:#f85149; --purple:#a371f7;
  --r:14px; --sh:0 1px 0 rgba(255,255,255,.03),0 8px 24px rgba(0,0,0,.35);
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{max-width:100%;overflow-x:hidden}
body{margin:0;background:radial-gradient(1200px 600px at 20% -10%,#16202c 0%,var(--bg) 45%,var(--bg2) 100%);
  color:var(--fg);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
  padding:0 0 calc(84px + env(safe-area-inset-bottom))}

/* ---------- 顶栏 ---------- */
header{position:sticky;top:0;z-index:20;backdrop-filter:blur(14px);
  background:rgba(13,17,23,.82);border-bottom:1px solid var(--line);
  padding:14px 16px calc(12px + env(safe-area-inset-top))}
.hrow{display:flex;align-items:center;gap:10px;min-width:0}
.logo{width:30px;height:30px;border-radius:9px;flex:0 0 auto;
  background:linear-gradient(145deg,#2f81f7,#a371f7);box-shadow:0 4px 14px rgba(47,129,247,.45);
  display:grid;place-items:center;font-size:15px}
h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.2px}
.sub{color:var(--dim);font-size:11.5px;margin-top:1px}
.pill{margin-left:auto;display:inline-flex;align-items:center;gap:6px;flex:0 0 auto;
  font-size:12px;font-weight:600;padding:5px 10px;border-radius:999px;
  border:1px solid var(--line);background:#11161d;color:var(--dim)}
.pill i{width:7px;height:7px;border-radius:50%;background:currentColor;display:block}
.pill.on{color:#7ee787;border-color:rgba(63,185,80,.4);background:rgba(63,185,80,.10)}
.pill.on i{box-shadow:0 0 0 0 rgba(63,185,80,.55);animation:pulse 1.8s infinite}
.pill.off{color:#ff9a94;border-color:rgba(248,81,73,.35);background:rgba(248,81,73,.08)}
.pill.wait{color:#e3b341;border-color:rgba(210,153,34,.35);background:rgba(210,153,34,.08)}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(63,185,80,.5)}70%{box-shadow:0 0 0 7px rgba(63,185,80,0)}100%{box-shadow:0 0 0 0 rgba(63,185,80,0)}}

/* ---------- 额度大数字 ---------- */
.hero{margin:14px 16px 0;padding:16px;border-radius:var(--r);border:1px solid var(--line);
  background:linear-gradient(160deg,#182231,#131820 60%);box-shadow:var(--sh);position:relative;overflow:hidden}
.hero:after{content:"";position:absolute;right:-40px;top:-60px;width:180px;height:180px;border-radius:50%;
  background:radial-gradient(circle,rgba(47,129,247,.28),transparent 65%)}
.hero .lab{color:var(--dim);font-size:12px;letter-spacing:.08em;text-transform:uppercase}
.hero .num{min-height:40px;font-size:34px;font-weight:750;letter-spacing:-.5px;margin:2px 0 2px;
  font-variant-numeric:tabular-nums;line-height:1.1}
.hero .meta{color:var(--dim);font-size:12.5px;display:flex;gap:14px;flex-wrap:wrap}
.hero .meta b{color:var(--fg);font-weight:600}
.spark{height:44px;margin-top:10px}
.spark svg{display:block;width:100%;height:44px}

/* ---------- 卡片 ---------- */
.wrap{padding:14px 16px 0;display:grid;gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--sh);
  padding:14px;min-width:0;overflow:hidden}
.card>h2{margin:0 0 10px;font-size:12px;color:var(--dim);font-weight:650;letter-spacing:.09em;text-transform:uppercase;
  display:flex;align-items:center;gap:8px}
.card>h2 span{color:var(--dim2);font-weight:500;letter-spacing:0;text-transform:none}
.rows{display:grid;gap:2px}
.row{display:block;padding:8px 0;border-bottom:1px dashed rgba(36,48,64,.7);min-width:0}
.row:last-child{border-bottom:0}
.row .k{color:var(--dim);font-size:11.5px;display:block}
.row .v{font-weight:600;font-size:14px;display:block;text-align:left;max-width:100%;word-break:break-all;
  white-space:normal;overflow-wrap:anywhere}
.dot{display:none}
.dot.ok{background:var(--ok);box-shadow:0 0 8px rgba(63,185,80,.6)}
.dot.bad{background:var(--bad);box-shadow:0 0 8px rgba(248,81,73,.5)}
.dot.warn{background:var(--warn);box-shadow:0 0 8px rgba(210,153,34,.5)}

/* ---------- 表单 ---------- */
label{display:block;font-size:12px;color:var(--dim);margin:12px 0 6px}
input,textarea,select{width:100%;background:#0d1219;border:1px solid var(--line);border-radius:10px;
  color:var(--fg);padding:11px 12px;font:13.5px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;
  transition:border-color .15s,box-shadow .15s}
input:focus,textarea:focus{outline:0;border-color:var(--acc);box-shadow:0 0 0 3px rgba(47,129,247,.18)}
textarea{min-height:72px;resize:vertical}
.hint{color:var(--dim2);font-size:11.5px;margin-top:6px}

/* ---------- 按钮 ---------- */
.btn{appearance:none;border:1px solid var(--line);background:var(--card2);color:var(--fg);
  border-radius:10px;padding:10px 14px;font-size:14px;font-weight:600;cursor:pointer;
  display:inline-flex;align-items:center;justify-content:center;gap:7px;min-height:42px;width:100%;
  transition:transform .06s,filter .15s,background .15s}
.btn:active{transform:scale(.985)}
.btn.primary{background:linear-gradient(180deg,#2f81f7,#1f6feb);border-color:#388bfd;color:#fff;
  box-shadow:0 6px 18px rgba(47,129,247,.28)}
.btn.danger{background:linear-gradient(180deg,#da3633,#b62324);border-color:#f85149;color:#fff}
.btn.ghost{background:transparent}
.btn.sm{min-height:36px;padding:8px 12px;font-size:13px;width:auto}
.btnrow{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}

/* ---------- 日志 ---------- */
.logbox{background:#0a0e13;border:1px solid var(--line);border-radius:12px;overflow:hidden}
.logbox .bar{display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--line);
  background:#0e141b;color:var(--dim);font-size:12px}
.logbox .bar .grow{margin-left:auto;display:flex;gap:6px}
pre#log{margin:0;padding:12px;max-height:42vh;overflow:auto;font:11.5px/1.6 ui-monospace,Consolas,monospace;
  white-space:pre-wrap;word-break:break-all;color:#9fb3c8}

/* ---------- 底部吸附操作条 ---------- */
.dock{position:fixed;left:0;right:0;bottom:0;z-index:30;padding:10px 16px calc(10px + env(safe-area-inset-bottom));
  background:linear-gradient(180deg,rgba(13,17,23,.6),rgba(13,17,23,.96));backdrop-filter:blur(14px);
  border-top:1px solid var(--line);display:grid;grid-template-columns:1.25fr 1fr 1fr 1fr;gap:8px}
.dock .btn{min-height:44px;padding:0 8px;font-size:13px}
.dock .btn span.ico{font-size:15px}

/* ---------- 提示 ---------- */
.toast{position:fixed;left:50%;bottom:calc(92px + env(safe-area-inset-bottom));transform:translate(-50%,8px);
  background:#1f6feb;color:#fff;padding:10px 16px;border-radius:999px;font-size:13px;font-weight:600;
  opacity:0;transition:.18s;pointer-events:none;box-shadow:0 8px 24px rgba(31,111,235,.45);max-width:88vw;text-align:center}
.toast.show{opacity:1;transform:translate(-50%,0)}
.toast.err{background:#b62324;box-shadow:0 8px 24px rgba(182,35,36,.45)}

@media(min-width:720px){
  body{padding-bottom:24px}
  .wrap{grid-template-columns:1fr 1fr}
  .wrap .span2{grid-column:1/-1}
  .dock{position:sticky;bottom:16px;margin:16px;border-radius:var(--r);border:1px solid var(--line);
    grid-template-columns:repeat(4,minmax(0,1fr));background:rgba(22,27,34,.95)}
  .hero{margin:16px 16px 0}
}
</style></head><body>

<header>
  <div class="hrow">
    <div class="logo">⚡</div>
    <div style="min-width:0">
      <h1>zburn 控制台</h1>
      <div class="sub" id="subline">连接中…</div>
    </div>
    <div class="pill" id="runPill"><i></i><span>…</span></div>
  </div>
</header>

<div class="hero">
  <div class="lab">剩余额度</div>
  <div class="num" id="quota">—</div>
  <div class="meta">
    <span>阈值 <b id="stopAt">—</b></span>
    <span>第 <b id="cycle">—</b> 轮</span>
    <span>心跳 <b id="hb">—</b></span>
  </div>
  <div class="spark" id="spark"></div>
</div>

<div class="wrap">

  <div class="card">
    <h2>环境</h2>
    <div class="rows">
      <div class="row"><span class="dot" id="dApp"></span><span class="k">桌面应用</span><span class="v" id="vApp">—</span></div>
      <div class="row"><span class="dot" id="dLock"></span><span class="k">桌面状态</span><span class="v" id="vLock">—</span></div>
      <div class="row"><span class="dot" id="dGw"></span><span class="k">网关</span><span class="v" id="vGw">—</span></div>
      <div class="row"><span class="dot" id="dFeed"></span><span class="k">上次投喂</span><span class="v" id="vFeed">—</span></div>
    </div>
  </div>

  <div class="card">
    <h2>运行 <span id="pidLine"></span></h2>
    <div class="rows">
      <div class="row"><span class="k">守护进程</span><span class="v" id="vRun">—</span></div>
      <div class="row"><span class="k">会话正则</span><span class="v" id="vRegex">—</span></div>
      <div class="row"><span class="k">巡检间隔</span><span class="v" id="vInterval">—</span></div>
      <div class="row"><span class="k">并发上限</span><span class="v" id="vMax">—</span></div>
    </div>
    <div class="btnrow">
      <button class="btn sm ghost" onclick="act('feed')">投喂一轮</button>
      <button class="btn sm ghost" onclick="act('gateway')">拉起网关</button>
    </div>
  </div>

  <div class="card span2">
    <h2>当前指令 <span>写进 next-prompt.txt，插件与守护共用</span></h2>
    <textarea id="prompt" placeholder="Continue exactly where you left off…"></textarea>
    <div class="btnrow" style="grid-template-columns:1fr">
      <button class="btn" onclick="savePrompt()">保存指令</button>
    </div>
  </div>

  <div class="card span2">
    <h2>参数 <span>改完下一轮生效</span></h2>
    <div style="display:grid;gap:0 14px;grid-template-columns:1fr">
      <div><label>会话名正则（只喂匹配的会话）</label><input id="cfgSession" placeholder="Supervisor:|项目名"></div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px">
        <div><label>间隔(秒)</label><input id="cfgInterval" type="number" min="20" step="10"></div>
        <div><label>并发上限</label><input id="cfgMax" type="number" min="1" max="12"></div>
        <div><label>额度阈值</label><input id="cfgStop" type="number" min="0" step="100000"></div>
      </div>
    </div>
    <div class="btnrow" style="grid-template-columns:1fr">
      <button class="btn" onclick="saveCfg()">保存参数</button>
    </div>
  </div>

  <div class="card span2">
    <h2>运行日志</h2>
    <div class="logbox">
      <div class="bar"><span>最近 120 行</span><span class="grow"><button class="btn sm ghost" onclick="load()">刷新</button></span></div>
      <pre id="log">加载中…</pre>
    </div>
  </div>

</div>

<div class="dock">
  <button class="btn primary" onclick="act('start')"><span class="ico">▶</span>启动</button>
  <button class="btn danger" onclick="act('stop')"><span class="ico">■</span>停止</button>
  <button class="btn" onclick="act('feed')"><span class="ico">↻</span>投喂</button>
  <button class="btn ghost" onclick="act('gateway')"><span class="ico">⚑</span>网关</button>
</div>

<div class="toast" id="toast"></div>
<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
const H = TOKEN ? {'X-Zburn-Token': TOKEN} : {};
let first = true;

function toast(msg, isErr){
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(t._h); t._h = setTimeout(()=>{ t.className='toast'; }, 2000);
}
const fmt = n => (n===null||n===undefined||n==='') ? '—' : Number(n).toLocaleString('zh-CN');
function setDot(id, state){ const d=document.getElementById(id); d.className='dot'+(state?(' '+state):''); }
function setPill(el, cls, text){ el.className='pill '+cls; el.querySelector('span').textContent=text; }

async function api(path, opt={}){
  const r = await fetch(path, {headers:{...H, ...(opt.body?{'Content-Type':'application/json'}:{})}, ...opt});
  if(!r.ok) throw new Error((await r.text()).slice(0,120));
  const ct = r.headers.get('content-type')||'';
  return ct.includes('json') ? r.json() : r.text();
}

async function act(a){
  try{
    const r = await api('/api/'+a, {method:'POST', body:'{}'});
    toast(r.msg || '已完成'); setTimeout(load, 700);
  }catch(e){ toast('失败：'+e.message, true); }
}
async function savePrompt(){
  try{ await api('/api/prompt', {method:'POST', body: JSON.stringify({text: document.getElementById('prompt').value})});
       toast('指令已保存'); }catch(e){ toast('保存失败：'+e.message, true); }
}
async function saveCfg(){
  const body = {
    session_regex: document.getElementById('cfgSession').value.trim(),
    interval_seconds: +document.getElementById('cfgInterval').value,
    max_sessions: +document.getElementById('cfgMax').value,
    stop_below: +document.getElementById('cfgStop').value
  };
  try{ await api('/api/config', {method:'POST', body: JSON.stringify(body)}); toast('参数已保存'); }
  catch(e){ toast('保存失败：'+e.message, true); }
}

function drawSpark(hist){
  const box = document.getElementById('spark');
  if(!hist || hist.length < 2){ box.innerHTML = '<div style="color:#6b7480;font-size:12px">额度历史还不够，多跑几轮会出现曲线</div>'; return; }
  const vals = hist.map(h=>h[1]), mx = Math.max(...vals) || 1, mn = Math.min(...vals);
  const W = 300, Hh = 44, pad = 2;
  const pts = vals.map((v,i)=>{
    const x = pad + i*(W-2*pad)/(vals.length-1);
    const y = Hh - pad - (v-mn)/((mx-mn)||1)*(Hh-2*pad);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  box.innerHTML = `<svg viewBox="0 0 ${W} ${Hh}" preserveAspectRatio="none">
    <polyline points="${pts}" fill="none" stroke="#58a6ff" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    <polyline points="${pts}" fill="none" stroke="#58a6ff" stroke-opacity=".25" stroke-width="6"/>
  </svg>`;
  box.title = `${hist[0][0]} → ${hist[hist.length-1][0]}：${fmt(hist[0][1])} → ${fmt(hist[hist.length-1][1])}`;
}

async function load(){
  let s;
  try{ s = await api('/api/state'); }
  catch(e){ document.getElementById('log').textContent = '连接失败：'+e.message; setPill(document.getElementById('runPill'),'off','离线'); return; }

  const running = !!s.running, stopping = !!s.stop_pending;
  setPill(document.getElementById('runPill'), running?'on':(stopping?'wait':'off'),
          running ? '运行中' : (stopping ? '停止中' : '已停止'));
  document.getElementById('subline').textContent =
    `${s.config.process_name} · ${running ? 'pid '+s.pid : '未运行'} · ${new Date().toLocaleTimeString('zh-CN')}`;
  document.getElementById('pidLine').textContent = running ? `pid ${s.pid}` : '';

  document.getElementById('quota').textContent = fmt(s.remaining);
  document.getElementById('stopAt').textContent = fmt(s.config.stop_below);
  document.getElementById('cycle').textContent = s.status?.cycle ?? '—';
  document.getElementById('hb').textContent = s.heartbeat_age===null ? '—' : (s.heartbeat_age+'s');

  setDot('dApp', s.app_running?'ok':'bad');   document.getElementById('vApp').textContent = s.app_running?'在运行':'未运行';
  setDot('dLock', s.locked?'warn':'');        document.getElementById('vLock').textContent = s.locked?'锁屏':'解锁';
  setDot('dGw', s.gateway_ok?'ok':'bad');     document.getElementById('vGw').textContent = s.gateway_ok?'可访问':'不通';
  const feed = (s.status?.last_feed_summary||'').trim();
  setDot('dFeed', feed?'' : '');
  document.getElementById('vFeed').textContent = feed ? feed.slice(0,26) : '—';

  document.getElementById('vRun').textContent = running ? '运行中' : '未运行';
  document.getElementById('vRegex').textContent = s.config.session_regex || '—';
  document.getElementById('vInterval').textContent = s.config.interval_seconds + ' 秒';
  document.getElementById('vMax').textContent = s.config.max_sessions + ' 个';

  const log = document.getElementById('log');
  log.textContent = (s.logs||[]).join('\n') || '（还没有日志）';
  log.scrollTop = log.scrollHeight;
  drawSpark(s.quota_history||[]);

  if(first){
    document.getElementById('cfgSession').value = s.config.session_regex || '';
    document.getElementById('cfgInterval').value = s.config.interval_seconds;
    document.getElementById('cfgMax').value = s.config.max_sessions;
    document.getElementById('cfgStop').value = s.config.stop_below;
    api('/api/prompt').then(t=>{ document.getElementById('prompt').value = t; }).catch(()=>{});
    first = false;
  }
}

load(); setInterval(load, 3000);
</script></body></html>
"""
