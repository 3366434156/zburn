# -*- coding: utf-8 -*-
"""面板前端（单个 HTML 字符串）。设计目标：手机上像原生 App，桌面上像后台。

配色/间距刻意贴近常见管理后台：深底、卡片分层、状态用色点 + 药丸、操作吸顶在头部。
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
/* 用 clip 而不是 hidden：hidden 会让 body 变成滚动容器，header 的 position:sticky 就失效了 */
html,body{max-width:100%;overflow-x:clip}
body{margin:0;background:radial-gradient(1200px 600px at 20% -10%,#16202c 0%,var(--bg) 45%,var(--bg2) 100%);
  color:var(--fg);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
  padding:0 0 calc(24px + env(safe-area-inset-bottom))}

/* ---------- 顶栏 ---------- */
header{position:sticky;top:0;z-index:20;backdrop-filter:blur(14px);
  background:rgba(13,17,23,.94);border-bottom:1px solid var(--line);
  box-shadow:0 8px 20px rgba(0,0,0,.28);
  padding:14px 16px calc(12px + env(safe-area-inset-top))}
.hrow{display:flex;align-items:center;gap:10px;min-width:0}
.hrow h1,.hrow .sub{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
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
.wrap{padding:14px 16px 0;display:grid;gap:12px;grid-template-columns:minmax(0,1fr)}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--sh);
  padding:14px;min-width:0;overflow:hidden}
.card>h2{margin:0 0 10px;font-size:12px;color:var(--dim);font-weight:650;letter-spacing:.09em;text-transform:uppercase;
  display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0}
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
input,textarea,select{width:100%;min-width:0;background:#0d1219;border:1px solid var(--line);border-radius:10px;
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
.btnrow{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:12px}
.g3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.g2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
@media(max-width:560px){.g3,.g2{grid-template-columns:minmax(0,1fr)}}

/* ---------- 高级配置折叠 ---------- */
details{border:1px solid var(--line);border-radius:12px;background:#0f141b;padding:0 12px;margin-top:10px}
details[open]{padding-bottom:12px}
summary{cursor:pointer;list-style:none;padding:11px 0;color:var(--acc2);font-size:13px;font-weight:600;
  display:flex;align-items:center;gap:8px}
summary::-webkit-details-marker{display:none}
summary:before{content:"▸";color:var(--dim);transition:transform .18s}
details[open] summary:before{transform:rotate(90deg)}

/* ---------- 会话监控 ---------- */
.sesslist{max-height:300px;overflow:auto;border:1px solid var(--line);border-radius:10px;
  background:#0d1219;padding:6px 10px;margin-top:10px}
.sesslist label{display:flex;align-items:center;gap:9px;margin:2px 0;padding:5px 7px;border-radius:8px;
  font-size:13.5px;color:var(--fg);cursor:pointer;overflow-wrap:anywhere;word-break:normal}
.sesslist label:hover{background:rgba(255,255,255,.03)}
.sesslist label.mon{background:rgba(63,185,80,.06)}
.sesslist label.cur{background:rgba(47,129,247,.10);box-shadow:inset 2px 0 0 var(--acc)}
.sesslist input[type=checkbox]{width:16px;height:16px;flex:0 0 auto;accent-color:#2f81f7}
.sesslist .none{color:var(--dim2);font-size:12.5px}
.sessproj{display:flex;align-items:center;gap:7px;margin:9px 2px 3px;font-size:11.5px;font-weight:700;
  color:var(--acc2);letter-spacing:.02em}
.sessproj:before{content:"";width:6px;height:6px;border-radius:2px;background:var(--acc);flex:0 0 auto}
.sessproj span{color:var(--dim2);font-weight:500}
.badge{margin-left:auto;flex:0 0 auto;font-size:10.5px;font-weight:700;letter-spacing:.02em;
  padding:2px 7px;border-radius:999px;color:#7ee787;background:rgba(63,185,80,.12);
  border:1px solid rgba(63,185,80,.35)}
.badge.cur{margin-left:auto;color:#79c0ff;background:rgba(47,129,247,.14);border-color:rgba(47,129,247,.45)}
.badge.cur + .badge{margin-left:6px}
.monlist{display:grid;gap:6px;margin-top:10px}
.monrow{display:flex;align-items:center;gap:8px;background:#0f141b;border:1px solid var(--line);
  border-radius:9px;padding:8px 10px;font-size:13px}
.monrow .monname{min-width:0;overflow-wrap:anywhere}
.monrow .xbtn{margin-left:auto;flex:0 0 auto;appearance:none;cursor:pointer;
  width:26px;height:26px;border-radius:8px;border:1px solid var(--line);background:transparent;
  color:var(--dim);font-size:13px;line-height:1;display:grid;place-items:center}
.monrow .xbtn:hover{color:#ff9a94;border-color:rgba(248,81,73,.45);background:rgba(248,81,73,.08)}
.monempty{color:var(--dim2);font-size:12.5px;padding:6px 2px}

/* ---------- 多 Agent ---------- */
.agentlist{display:grid;gap:8px}
.agentrow{display:flex;align-items:center;gap:9px;flex-wrap:wrap;background:#0f141b;
  border:1px solid var(--line);border-radius:11px;padding:10px 11px}
.agentrow .aname{font-weight:700;font-size:13.5px;min-width:0;overflow-wrap:anywhere}
.agentrow .ameta{color:var(--dim2);font-size:11.5px;min-width:0;overflow-wrap:anywhere;flex:1 1 100%}
.agentrow .role{font-size:10.5px;font-weight:700;padding:2px 7px;border-radius:999px;
  color:#79c0ff;background:rgba(47,129,247,.14);border:1px solid rgba(47,129,247,.4)}
.agentrow .role.sup{color:#d2a8ff;background:rgba(163,113,247,.12);border-color:rgba(163,113,247,.4)}
.agentrow .role.off{color:#8b949e;background:rgba(139,148,158,.08);border-color:rgba(139,148,158,.3)}
.agentrow .abtns{display:flex;gap:6px;flex-wrap:wrap;width:100%}
.agentrow .abtns .btn{width:auto;min-height:32px;padding:6px 10px;font-size:12.5px}
.agentrow .pending{color:#e3b341;font-size:11.5px;flex:1 1 100%}
.agentrow .atarget{display:flex;gap:6px;width:100%}
.agentrow .atarget input{flex:1;min-width:0;padding:7px 9px;font-size:12px}
#snapBox{margin:10px 0 0;padding:10px;max-height:220px;overflow:auto;border:1px solid var(--line);
  border-radius:10px;background:#0a0e13;color:#9fb3c8;font:11.5px/1.55 ui-monospace,Consolas,monospace;
  white-space:pre-wrap;word-break:break-all;display:none}

/* ---------- 日志 ---------- */
.logbox{background:#0a0e13;border:1px solid var(--line);border-radius:12px;overflow:hidden}
.logbox .bar{display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--line);
  background:#0e141b;color:var(--dim);font-size:12px}
.logbox .bar .grow{margin-left:auto;display:flex;gap:6px}
pre#log{margin:0;padding:12px;max-height:42vh;overflow:auto;font:11.5px/1.6 ui-monospace,Consolas,monospace;
  white-space:pre-wrap;word-break:break-all;color:#9fb3c8}

/* ---------- 顶部操作条（跟 header 一起吸顶，不用滚到底部找按钮） ---------- */
.acts{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-top:10px}
.acts .btn{min-height:40px;padding:0 6px;font-size:13px;min-width:0;white-space:nowrap}
.acts .btn span.ico{font-size:15px}
@media(max-width:380px){.acts .btn span.ico{display:none}}

/* ---------- 提示 ---------- */
.toast{position:fixed;left:50%;bottom:calc(24px + env(safe-area-inset-bottom));transform:translate(-50%,8px);
  background:#1f6feb;color:#fff;padding:10px 16px;border-radius:999px;font-size:13px;font-weight:600;
  opacity:0;transition:.18s;pointer-events:none;box-shadow:0 8px 24px rgba(31,111,235,.45);max-width:88vw;text-align:center}
.toast.show{opacity:1;transform:translate(-50%,0)}
.toast.err{background:#b62324;box-shadow:0 8px 24px rgba(182,35,36,.45)}


/* ---------- 卡片动效 ---------- */
.card{position:relative;transition:transform .18s cubic-bezier(.2,.7,.3,1),border-color .18s,box-shadow .18s}
.card:before{content:"";position:absolute;left:14%;right:14%;top:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(88,166,255,.85),transparent);
  opacity:.35;transition:opacity .18s,left .25s,right .25s}
.card:hover{transform:translateY(-2px);border-color:rgba(88,166,255,.42);
  box-shadow:0 14px 34px rgba(0,0,0,.5),0 0 0 1px rgba(88,166,255,.12)}
.card:hover:before{opacity:1;left:6%;right:6%}
.card>h2{transition:color .18s}
.card:hover>h2{color:var(--acc2)}
.hero{transition:box-shadow .25s,border-color .25s}
.hero:hover{box-shadow:0 16px 40px rgba(31,111,235,.28),var(--sh);border-color:rgba(88,166,255,.35)}
.hero:before{content:"";position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(115deg,transparent 30%,rgba(255,255,255,.06) 45%,transparent 60%);
  transform:translateX(-60%);animation:sheen 6.5s ease-in-out infinite}
@keyframes sheen{0%,70%{transform:translateX(-70%)}100%{transform:translateX(70%)}}
.btn:hover{filter:brightness(1.08)}
.btn.primary:hover{box-shadow:0 10px 26px rgba(47,129,247,.42)}
.dot.ok{animation:dotpulse 2.4s ease-in-out infinite}
@keyframes dotpulse{0%,100%{opacity:1}50%{opacity:.45}}
input:hover,textarea:hover{border-color:#2c3a4d}
@media(prefers-reduced-motion:reduce){.card,.hero,.hero:before,.dot.ok{transition:none;animation:none}}


.gh{flex:0 0 auto;width:34px;height:34px;border-radius:10px;display:grid;place-items:center;
  border:1px solid var(--line);background:#11161d;color:var(--dim);text-decoration:none;
  transition:transform .15s,color .15s,border-color .15s,background .15s}
.gh:hover{color:var(--fg);border-color:rgba(88,166,255,.5);background:#161d26;transform:translateY(-1px)}
.gh svg{width:18px;height:18px;fill:currentColor}

@media(min-width:720px){
  body{padding-bottom:24px}
  .wrap{grid-template-columns:1fr 1fr}
  .wrap .span2{grid-column:1/-1}
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
    <a class="gh" href="https://github.com/3366434156/zburn" target="_blank" rel="noreferrer"
       title="在 GitHub 上查看 zburn" aria-label="GitHub">
      <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38
      0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01
      1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95
      0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27
      .68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15
      0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8
      c0-4.42-3.58-8-8-8z"/></svg>
    </a>

  </div>
  <div class="acts">
    <button class="btn primary" onclick="act('start')"><span class="ico">▶</span>启动</button>
    <button class="btn danger" onclick="act('stop')"><span class="ico">■</span>停止</button>
    <button class="btn" onclick="act('feed')"><span class="ico">↻</span>投喂</button>
    <button class="btn ghost" onclick="act('gateway')"><span class="ico">⚑</span>网关</button>
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
      <div class="row"><span class="k">监控对话</span><span class="v" id="vMon">—</span></div>
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
    <div style="display:grid;gap:0 14px;grid-template-columns:minmax(0,1fr)">
      <div><label>会话名正则（可选：监控名单之外还想按正则多喂几个时用）</label><input id="cfgSession" placeholder="留空即只喂监控名单"></div>
      <div class="g3">
        <div><label>间隔(秒)</label><input id="cfgInterval" type="number" min="20" step="10"></div>
        <div><label>并发上限</label><input id="cfgMax" type="number" min="1" max="12"></div>
        <div><label>额度阈值</label><input id="cfgStop" type="number" min="0" step="100000"></div>
      </div>
      <div class="g3">
        <div><label>对话轮换(轮) <b>0=不换</b></label><input id="cfgRotate" type="number" min="0" step="1" placeholder="3"></div>
        <div><label>指令上限(字符)</label><input id="cfgMaxChars" type="number" min="0" step="10" placeholder="180"></div>
        <div><label>钉死的模型</label><input id="cfgModelWant" placeholder="zcode2api"></div>
      </div>
      <div class="hint">轮换：一个对话跑满这么多轮就换新的（上下文平方级膨胀，不换就是白烧额度）。
        指令上限：WM_CHAR 打太长会丢字，超出的部分会被截掉。钉死的模型：新对话后自动复查，
        不对就切回去；切不回去宁可这一轮不投喂。</div>
    </div>
    <div class="btnrow" style="grid-template-columns:1fr">
      <button class="btn" onclick="saveCfg()">保存参数</button>
    </div>
  </div>

  <div class="card span2">
    <h2>会话监控 <span>只对名单里的对话续派；名单为空且正则为空就不投喂</span></h2>
    <div class="btnrow" style="grid-template-columns:repeat(2,minmax(0,1fr))">
      <button class="btn sm ghost" onclick="scanSessions()">扫描侧栏对话</button>
      <button class="btn sm" onclick="saveTargets()">保存勾选</button>
    </div>
    <label style="margin-top:14px">监控中的对话 <span id="monCount"></span></label>
    <div id="monList" class="monlist"></div>
    <div id="sessList" class="sesslist"><div class="none">点「扫描侧栏对话」列出 ZCode 侧栏里的会话，勾选要监控的，再点「保存勾选」。</div></div>
    <div class="hint" id="sessWarn"></div>
    <div class="hint" id="sessHint"></div>
    <div class="hint">这里只改 worker（顶层）的监控名单；supervisor 的监督对话在多 Agent 卡里单独填。</div>
  </div>

  <div class="card span2">
    <h2>多 Agent <span>worker 干活；supervisor（如 dsh）定时读对话、验收后写下一步</span></h2>
    <div id="agentList" class="agentlist"><div class="none">还没有 agent 配置。在 config.json 里写 agents。</div></div>
    <div class="g2" style="margin-top:12px">
      <div><label>新对话指令（只收 ASCII）</label><input id="newChatPrompt" placeholder="Start a new task: ..."></div>
      <div><label>新对话所在项目（ZCode 可填 zburn）</label><input id="newChatProject" placeholder="留空 = 配置里的 new_chat.project"></div>
    </div>
    <div class="btnrow" style="grid-template-columns:repeat(2,minmax(0,1fr))">
      <button class="btn sm ghost" onclick="actAgent('supervise')">唤醒监督（现在验收）</button>
      <button class="btn sm ghost" onclick="applyHandoff()">执行待处理交接</button>
    </div>
    <div class="hint" id="handoffHint">没有待处理交接</div>
    <pre id="snapBox"></pre>
    <label style="margin-top:14px">监督提示词（{context} / {status} / {handoff} 会替换成桥目录路径）</label>
    <textarea id="supPrompt" style="min-height:120px" placeholder="读 context.txt，判断是否验收通过；需要下一步就写 handoff.json…"></textarea>
    <div class="btnrow" style="grid-template-columns:1fr">
      <button class="btn" onclick="saveSupPrompt()">保存监督提示词</button>
    </div>
    <div class="hint" id="bridgeHint"></div>
  </div>

  <div class="card span2">
    <h2>高级配置 <span>写进 config.json，改前自动留 .bak</span></h2>
    <div class="rows">
      <div class="row"><span class="k">配置文件</span><span class="v" id="vCfgPath">—</span></div>
      <div class="row"><span class="k">面板地址</span><span class="v" id="vPanelUrl">—</span>
        <button class="btn sm ghost" id="btnCopyUrl" style="margin-top:9px;display:none"
                onclick="copyPhoneUrl()" title="复制带 token 的完整局域网地址，发到手机（微信/QQ 传给自己）直接打开">📋 复制给手机</button>
      </div>
    </div>
    <details>
      <summary>展开：应用 / 网关 / 额度接口</summary>
      <label>应用进程名</label><input id="cfgProc" placeholder="ZCode">
      <label>应用 exe 路径</label><input id="cfgExe" placeholder="E:\exe\Zcode\ZCode.exe">
      <label>窗口标题正则（同一客户端开了多个窗口时选哪个；留空 = 第一个主窗口）</label><input id="cfgWinTitle" placeholder="比如 zburn|我的项目">
      <label>网关健康检查地址</label><input id="cfgHealth" placeholder="http://127.0.0.1:3000/meta">
      <label>网关启动命令（网关挂了自动拉起）</label><input id="cfgStart" placeholder="python cli.py serve --port 3000">
      <label>网关工作目录</label><input id="cfgWorkdir" placeholder="E:\数据\AI\zcode2api">
      <label>额度接口</label><input id="cfgQuotaUrl" placeholder="http://127.0.0.1:3000/admin/api/accounts">
      <label>额度管理密钥</label><input id="cfgAdminKey" placeholder="zcode">
      <label>发送按钮正则</label><input id="cfgSendRegex" placeholder="发送|加入队列|Send">
      <label>新建对话按钮正则</label><input id="cfgNewChat" placeholder="新建任务|新建对话|New chat">
      <label>新建对话默认项目（ZCode 填项目名）</label><input id="cfgNewChatProject" placeholder="zburn">
      <label>监督桥目录（留空 = %LOCALAPPDATA%\zburn-bridge）</label><input id="cfgBridge" placeholder="C:\Users\...\AppData\Local\zburn-bridge">
      <div class="g2">
        <div><label>面板监听地址</label><input id="cfgHost" placeholder="127.0.0.1 或 0.0.0.0"></div>
        <div><label>面板端口</label><input id="cfgPort" type="number" min="1" max="65535"></div>
      </div>
      <div class="hint">面板监听/端口改完要重启面板才生效（关掉窗口后重新双击 一键启动.bat）。</div>
    </details>
    <div class="btnrow" style="grid-template-columns:1fr">
      <button class="btn" onclick="saveCfg()">保存高级配置</button>
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
  // 停止要立刻给反馈：接口要等守护真的结束才返回，别让按钮看起来没反应
  if(a === 'stop'){
    setPill(document.getElementById('runPill'),'wait','停止中');
    toast('正在停止…');
  }
  try{
    const r = await api('/api/'+a, {method:'POST', body:'{}'});
    toast(r.msg || '已完成'); setTimeout(load, a==='stop'?300:700);
  }catch(e){ toast('失败：'+e.message, true); }
  if(a === 'stop') setTimeout(load, 1600);
}
async function savePrompt(){
  try{ await api('/api/prompt', {method:'POST', body: JSON.stringify({text: document.getElementById('prompt').value})});
       toast('指令已保存'); }catch(e){ toast('保存失败：'+e.message, true); }
}

let lastPanelUrl = '';

async function copyText(t){
  // 手机/局域网走的是 http，不算安全上下文，navigator.clipboard 可能没有——退化到 execCommand
  try{ await navigator.clipboard.writeText(t); return true; }catch(e){}
  const ta = document.createElement('textarea');
  ta.value = t; ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
  document.body.appendChild(ta); ta.focus(); ta.select();
  try{ return document.execCommand('copy'); } finally { ta.remove(); }
}

async function copyPhoneUrl(){
  if(!lastPanelUrl){
    toast('面板只监听了本机：把高级配置里的「面板监听地址」改成 0.0.0.0 保存后重启面板', true);
    return;
  }
  const ok = await copyText(lastPanelUrl);
  toast(ok ? '已复制手机地址（完整地址在「高级配置 → 面板地址」）'
           : '自动复制失败，请手动长按复制', !ok);
}
const esc = s => (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let scanned = [];      // 最近一次扫描到的侧栏对话
let monitored = [];    // 监控名单（config.feed.targets 的本地副本）
let lastTargetSig = null;

// 名单里的标识：唯一对话就是标题，同名对话是「标题#序号」
function dispId(id){
  const m = /^(.*)#(\d+)$/.exec(id || '');
  return m ? `${m[1]}（同名第 ${m[2]} 个）` : (id || '');
}

function renderMonitored(){
  document.getElementById('monCount').textContent = monitored.length ? `（${monitored.length} 个）` : '';
  const box = document.getElementById('monList');
  if(!monitored.length){
    box.innerHTML = '<div class="monempty">还没监控任何对话。扫描侧栏 → 勾选 → 保存勾选。</div>';
  }else{
    box.innerHTML = monitored.map((id,i)=>
      `<div class="monrow"><span class="monname">${esc(projPrefix(id) + dispId(id))}</span>` +
      `<button class="xbtn" data-i="${i}" title="取消监控">✕</button></div>`).join('');
    box.querySelectorAll('.xbtn').forEach(b => { b.onclick = () => removeTarget(+b.dataset.i); });
  }
  refreshBadges();
}

// 最近一次扫描里这个会话属于哪个项目（没扫描过就只显示标题）
function projPrefix(id){
  const s = scanned.find(x => x.id === id);
  return s && s.project ? s.project + ' · ' : '';
}

function refreshBadges(){
  const set = new Set(monitored);
  document.querySelectorAll('#sessList label[data-id]').forEach(l=>{
    const on = set.has(l.dataset.id);
    l.classList.toggle('mon', on);
    const cb = l.querySelector('input[type=checkbox]');
    if(cb) cb.checked = on;
    let b = l.querySelector('.badge.mon');
    if(on && !b){ b = document.createElement('span'); b.className = 'badge mon'; b.textContent = '监控中'; l.appendChild(b); }
    if(!on && b) b.remove();
  });
}

async function postTargets(list){
  const next = [...new Set(list)];
  try{
    await api('/api/config', {method:'POST', body: JSON.stringify({targets: next})});
    // 签名格式必须跟 load() 里一致，否则名单会每 3 秒被重绘一次（点击正好撞上就丢）
    monitored = next; lastTargetSig = JSON.stringify(next); renderMonitored();
    toast(next.length ? `已监控 ${next.length} 个对话` : '已清空监控名单，现在不会投喂');
    setTimeout(load, 700);
  }catch(e){ toast('保存失败：'+e.message, true); }
}

function removeTarget(i){ postTargets(monitored.filter((_,k)=>k!==i)); }

async function scanSessions(){
  const box = document.getElementById('sessList');
  box.innerHTML = '<div class="none">扫描中…（读 ZCode 无障碍树，几秒钟）</div>';
  try{
    const r = await api('/api/sessions');
    scanned = r.sessions || [];
    if(!scanned.length){
      box.innerHTML = '<div class="none">侧栏没列出对话：ZCode 没开，或无障碍树被关了（试 python zburn.py restart-app）。</div>';
      return;
    }
    const cur = new Set(monitored);
    // 按项目分组（保持侧栏顺序），当前打开的对话标「当前」——扫描前很难分清同名/跨项目会话
    const groups = [];
    const gmap = new Map();
    scanned.forEach((s, i)=>{
      const p = s.project || '未分组';
      if(!gmap.has(p)){ gmap.set(p, []); groups.push([p, gmap.get(p)]); }
      gmap.get(p).push(i);
    });
    box.innerHTML = groups.map(([p, idxs]) =>
      `<div class="sessproj">${esc(p)}<span>${idxs.length}</span></div>` +
      idxs.map(i => {
        const s = scanned[i], on = cur.has(s.id);
        const dup = s.count > 1 ? ` <span style="color:#6b7480">#${s.ord}</span>` : '';
        const badges = (s.active?'<span class="badge cur" title="扫描时 ZCode 正打开的就是这个对话">当前</span>':'') +
                       (on?'<span class="badge mon">监控中</span>':'');
        return `<label data-id="${esc(s.id)}" class="${on?'mon':''}${s.active?' cur':''}">` +
          `<input type="checkbox" data-i="${i}" ${on?'checked':''}>` +
          `<span>${esc(s.key)}${dup}</span>` + badges + `</label>`;
      }).join('')
    ).join('');
    box.querySelectorAll('input[type=checkbox]').forEach(cb=>{
      cb.onchange = () => refreshBadgesFromCheck();
    });
    // 当前对话可能在滚动区外面，扫描完把它连同所属项目头一起带到眼前
    const curRow = box.querySelector('label.cur');
    if(curRow){
      let head = curRow.previousElementSibling;
      while(head && !head.classList.contains('sessproj')) head = head.previousElementSibling;
      const anchor = head || curRow;
      box.scrollTop = Math.max(0, anchor.offsetTop - box.offsetTop - 8);
    }
    renderMonitored();   // 监控名单补上「项目 · 标题」
    // 名单里有、但这次侧栏没扫到的：可能改了名/被删了，直接说出来
    const missing = monitored.filter(id => !scanned.some(s => s.id === id));
    const warn = document.getElementById('sessWarn');
    warn.style.color = '#e3b341';
    warn.textContent = missing.length
      ? `名单里这 ${missing.length} 个在侧栏没找到（改名/删除？）：` + missing.map(dispId).join('、')
      : '';
    toast(`扫描到 ${scanned.length} 个对话 / ${groups.length} 个项目（「当前」= 扫描时正打开的对话），勾选后点「保存勾选」`);
  }catch(e){ box.innerHTML = '<div class="none">扫描失败：'+esc(e.message)+'</div>'; }
}

// 勾选状态变了就同步角标（不重新渲染，避免打断勾选）
function refreshBadgesFromCheck(){
  document.querySelectorAll('#sessList label[data-id]').forEach(l=>{
    const cb = l.querySelector('input[type=checkbox]');
    const on = !!(cb && cb.checked);
    l.classList.toggle('mon', on);
    let b = l.querySelector('.badge.mon');
    if(on && !b){ b = document.createElement('span'); b.className='badge mon'; b.textContent='监控中'; l.appendChild(b); }
    if(!on && b){ b.remove(); }
  });
}

async function saveTargets(){
  const checked = [...document.querySelectorAll('#sessList input[type=checkbox]:checked')]
    .map(c => scanned[+c.dataset.i].id).filter(Boolean);
  const seen = new Set(scanned.map(s=>s.id));
  // 这次侧栏没列出来的（被归档/滚动没渲染/应用没开）不能因为保存就丢掉
  const keep = monitored.filter(id => !seen.has(id));
  await postTargets([...keep, ...checked]);
}

async function saveCfg(){
  const val = id => document.getElementById(id).value.trim();
  const body = {
    session_regex: val('cfgSession'),
    window_title_regex: val('cfgWinTitle'),
    interval_seconds: +val('cfgInterval'),
    max_sessions: +val('cfgMax'),
    rotate_after_turns: +val('cfgRotate'),
    prompt_max_chars: +val('cfgMaxChars'),
    model_want: val('cfgModelWant'),
    stop_below: +val('cfgStop'),
    process_name: val('cfgProc'),
    app_exe: val('cfgExe'),
    health_url: val('cfgHealth'),
    start_command: val('cfgStart'),
    work_dir: val('cfgWorkdir'),
    quota_url: val('cfgQuotaUrl'),
    admin_key: val('cfgAdminKey'),
    send_button_regex: val('cfgSendRegex'),
    new_chat_button_regex: val('cfgNewChat'),
    new_chat_project: val('cfgNewChatProject'),
    bridge_dir: val('cfgBridge'),
    panel_host: val('cfgHost'),
    panel_port: +val('cfgPort')
  };
  // 会话正则/窗口正则允许清空（清空=只喂勾选的会话），其余空值照旧跳过
  const clearable = ['session_regex', 'window_title_regex', 'new_chat_project', 'bridge_dir', 'model_want'];
  Object.keys(body).forEach(k => {
    if((body[k]==='' || Number.isNaN(body[k])) && !clearable.includes(k)) delete body[k];
  });
  try{
    const r = await api('/api/config', {method:'POST', body: JSON.stringify(body)});
    toast(r.msg || '参数已保存'); setTimeout(load, 700);
  }catch(e){ toast('保存失败：'+e.message, true); }
}

// ---------- 多 Agent ----------
let agentSig = '';
function renderAgents(list){
  const box = document.getElementById('agentList');
  if(!list || !list.length){
    box.innerHTML = '<div class="none">还没有 agent 配置。在 config.json 里写 agents。</div>';
    return;
  }
  box.innerHTML = list.map(a=>{
    const role = a.role === 'supervisor'
      ? '<span class="role sup">supervisor</span>'
      : '<span class="role">worker</span>';
    const off = a.enabled ? '' : ' <span class="role off">off</span>';
    const pending = a.pending
      ? `<div class="pending">待执行：${esc(a.pending.action)} · ${esc((a.pending.prompt||'').slice(0,180))}</div>` : '';
    const last = a.last_result ? ` · 上次：${esc(a.last_result).slice(0,100)}` : '';
    const dyn = (a.dynamic_targets||[]).length;
    const nMon = (a.targets||[]).length + dyn;
    const monTxt = `监控 ${nMon} 个` + (dyn ? `（动态 +${dyn}）` : '');
    const tgt = esc((a.targets||[]).join(' | '));
    const clearDyn = dyn ? `<button class="btn sm ghost" data-act="cleardyn" data-id="${esc(a.id)}">清空动态</button>` : '';
    return `<div class="agentrow">` +
      `<span class="aname">${esc(a.name)}</span>${role}${off}` +
      `<span class="ameta">${esc(a.process_name)} · 每 ${a.interval_seconds}s · ${monTxt}${last}</span>` +
      pending +
      `<div class="atarget"><input data-target-id="${esc(a.id)}" value="${tgt}" placeholder="监控会话标题，多个用 | 分隔">` +
        `<button class="btn sm ghost" data-act="targets" data-id="${esc(a.id)}">保存目标</button></div>` +
      `<div class="abtns">` +
        (a.role === 'supervisor'
          ? `<button class="btn sm ghost" data-act="supervise" data-id="${esc(a.id)}">唤醒</button>`
          : `<button class="btn sm ghost" data-act="feed" data-id="${esc(a.id)}">投喂</button>`) +
        `<button class="btn sm ghost" data-act="newchat" data-id="${esc(a.id)}">新对话</button>` +
        `<button class="btn sm ghost" data-act="handoff" data-id="${esc(a.id)}">写交接</button>` +
        `<button class="btn sm ghost" data-act="snapshot" data-id="${esc(a.id)}">看快照</button>` +
        `<button class="btn sm ghost" data-act="toggle" data-id="${esc(a.id)}" data-enabled="${a.enabled?1:0}">${a.enabled?'停用':'启用'}</button>` +
        clearDyn +
      `</div></div>`;
  }).join('');
  box.querySelectorAll('button[data-act]').forEach(b=>{
    b.onclick = () => actAgent(b.dataset.act, b.dataset.id, b.dataset.enabled);
  });
}

async function actAgent(action, id, enabledFlag){
  const prompt = document.getElementById('newChatPrompt').value.trim();
  const project = document.getElementById('newChatProject').value.trim();
  if(action === 'newchat' && !prompt){
    toast('先填「新对话指令」（只收 ASCII）', true);
    return;
  }
  if(action === 'handoff' && !prompt){
    toast('先填要交给 worker 的指令', true);
    return;
  }
  try{
    if(action === 'supervise'){
      toast('正在刷新监督桥并唤醒 supervisor…');
      const r = await api('/api/agent', {method:'POST', body: JSON.stringify({action:'supervise'})});
      toast(r.msg || '已唤醒');
    }else if(action === 'snapshot'){
      const r = await api('/api/agent', {method:'POST', body: JSON.stringify({action:'snapshot', agent:id})});
      const box = document.getElementById('snapBox');
      box.style.display = 'block'; box.textContent = r.msg || '(空)';
      toast('快照已更新');
    }else if(action === 'handoff'){
      const r = await api('/api/handoff', {method:'POST', body: JSON.stringify({action:'feed', agent:id, prompt})});
      toast(r.msg || '交接已写入');
    }else if(action === 'toggle'){
      const want = enabledFlag !== '1';
      const r = await api('/api/agent', {method:'POST', body: JSON.stringify({action:'toggle', agent:id, enabled:want})});
      toast(r.msg || '已切换');
    }else if(action === 'targets'){
      const inp = document.querySelector(`[data-target-id="${id}"]`);
      const list = (inp ? inp.value : '').split(/[|,，\n]/).map(s=>s.trim()).filter(Boolean);
      const r = await api('/api/agent', {method:'POST', body: JSON.stringify({action:'targets', agent:id, targets:list})});
      toast(r.msg || '目标已保存');
    }else{
      const r = await api('/api/agent', {method:'POST', body: JSON.stringify({action, agent:id, prompt, project})});
      toast(r.msg || '已完成');
    }
    setTimeout(load, 800);
  }catch(e){ toast('失败：'+e.message, true); }
}

async function applyHandoff(){
  try{
    const r = await api('/api/handoff', {method:'POST', body: JSON.stringify({action:'apply'})});
    toast(r.msg || '已执行');
    setTimeout(load, 700);
  }catch(e){ toast('失败：'+e.message, true); }
}

async function saveSupPrompt(){
  try{
    const r = await api('/api/supervisor_prompt', {method:'POST',
      body: JSON.stringify({text: document.getElementById('supPrompt').value})});
    toast(r.msg || '已保存');
    setTimeout(load, 700);
  }catch(e){ toast('保存失败：'+e.message, true); }
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
  document.getElementById('hb').textContent = (!running || s.heartbeat_age===null) ? '—' : (s.heartbeat_age+'s');

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

  // 监控名单以服务端为准；只有真的变了才重绘，避免 3 秒轮询把勾选打断
  const tg = s.config.targets || [];
  const sig = JSON.stringify(tg);
  if(sig !== lastTargetSig){
    lastTargetSig = sig;
    monitored = tg.slice();
    renderMonitored();
  }
  document.getElementById('vMon').textContent = tg.length ? tg.length + ' 个' : '未指定';
  document.getElementById('sessHint').textContent = tg.length
    ? '只对上面这些对话续派；侧栏里改过名的对话需要重新扫描勾选。'
    : ('当前没有监控任何对话' + (s.config.session_regex ? '（按正则喂）' : '（不会投喂）'));

  // 多 agent：列表/待执行交接变了才重绘，避免 3 秒轮询把按钮点掉
  const agList = s.agents || [];
  // 不要把 last_result 放进签名：它每轮都会变，会把正在输入的「监控目标」输入框重绘掉
  const agSig = JSON.stringify(agList.map(a=>[a.id, a.enabled, a.pending,
                                               a.targets, a.dynamic_targets]));
  if(agSig !== agentSig){ agentSig = agSig; renderAgents(agList); }
  const h = (s.handoff || {}).pending;
  document.getElementById('handoffHint').textContent = h
    ? `待处理：${h.action} · ${(h.prompt||'').slice(0,180)}`
    : '没有待处理交接';
  document.getElementById('bridgeHint').textContent = '监督桥：' + (s.bridge_dir || '—');

  // 手机地址：服务端按实际绑定地址算（绑了 0.0.0.0 且有 token 才给）
  lastPanelUrl = s.panel_url || '';
  document.getElementById('vPanelUrl').textContent = lastPanelUrl || (location.origin + '/?token=…');
  document.getElementById('btnCopyUrl').style.display = lastPanelUrl ? '' : 'none';

  const log = document.getElementById('log');
  log.textContent = (s.logs||[]).join('\n') || '（还没有日志）';
  log.scrollTop = log.scrollHeight;
  drawSpark(s.quota_history||[]);

  if(first){
    const set = (id, v) => { const el = document.getElementById(id); if(el) el.value = (v===null||v===undefined)?'':v; };
    set('cfgSession', s.config.session_regex);
    set('cfgWinTitle', s.config.window_title_regex);
    set('cfgInterval', s.config.interval_seconds);
    set('cfgMax', s.config.max_sessions);
    set('cfgRotate', s.config.rotate_after_turns);
    set('cfgMaxChars', s.config.prompt_max_chars);
    set('cfgModelWant', s.config.model_want);
    set('cfgStop', s.config.stop_below);
    set('cfgProc', s.config.process_name);
    set('cfgExe', s.config.app_exe);
    set('cfgHealth', s.config.health_url);
    set('cfgStart', s.config.start_command);
    set('cfgWorkdir', s.config.work_dir);
    set('cfgQuotaUrl', s.config.quota_url);
    set('cfgAdminKey', s.config.admin_key);
    set('cfgSendRegex', s.config.send_button_regex);
    set('cfgNewChat', s.config.new_chat_button_regex);
    set('cfgNewChatProject', s.config.new_chat_project);
    set('cfgBridge', s.config.bridge_dir);
    set('cfgHost', s.config.panel_host);
    set('cfgPort', s.config.panel_port);
    set('supPrompt', s.supervisor_prompt);
    set('newChatProject', s.config.new_chat_project);
    document.getElementById('vCfgPath').textContent = s.config.config_path || '—';
    api('/api/prompt').then(t=>{ document.getElementById('prompt').value = t; }).catch(()=>{});
    first = false;
  }
}

load(); setInterval(load, 3000);
</script></body></html>
"""
