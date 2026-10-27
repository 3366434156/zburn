# zburn · 熄屏守护

**锁屏/熄屏状态下，持续驱动桌面 AI 应用干活**——不需要解锁、不需要前台焦点、不会把字打进密码框。
默认场景：让 ZCode 一直跑下去直到后端额度烧完；换个配置也能驱动别的桌面应用。

```
┌─────────────┐   探活/必要时启动(绝不重启)   ┌──────────────┐
│ zburn run   │ ───────────────────────────▶ │ 网关/后端进程 │
│ （守护循环） │ ◀─── 剩余额度（HTTP/命令）── │              │
└──────┬──────┘                              └──────────────┘
       │ UI Automation + WM_CHAR（不抢焦点）
       ▼
┌─────────────────────────────────────────────────────────┐
│ 桌面应用（ZCode）：会话列表 → 找到空闲会话 → 补一条续派  │
│ 正在出字(busy)/已排队(queued) 的会话自动跳过             │
└─────────────────────────────────────────────────────────┘
```

## 为什么不是 SendKeys

锁屏时 `SetForegroundWindow` 必然失败，`SendKeys` 会把字符送进**锁屏密码框**（真实事故风险）；
而 zburn 全程走 UI Automation 的 `ValuePattern / InvokePattern` + 目标窗口 `WM_CHAR`，
**不抢焦点、不点鼠标**，锁屏照常工作。

实测踩到的坑（都在代码里避开了，详见 [docs/熄屏原理.md](docs/熄屏原理.md)）：

| 现象 | 结论 |
|---|---|
| 快速连发 WM_CHAR 丢字 | 每字 20~25ms，每 6 字停一下 |
| 回车提交不稳定 | 改成 Invoke「发送」按钮，回车只做兜底 |
| 「移除待发送消息」「停止生成」名字里也含"发送/生成" | 发送按钮必须按正则**排除**它们，否则点错按钮 |
| **退格清不掉输入框内容**（WM_CHAR 0x08 / VK_BACK / Ctrl+A 全无效） | 不依赖清空：改成**内容比对 + 只补缺失后缀**；框里有别人的文本就跳过（exit 7） |
| WM_CHAR 只认 ASCII | 指令必须是英文，CJK 会被映射成乱码 |
| 锁屏久了 Chromium 关掉无障碍树（只剩 D3D 子窗） | 检测到就重启应用（这是唯一有效的恢复手段） |
| Electron 应用被 `ELECTRON_RUN_AS_NODE` 污染秒退 | 拉起前清掉这个环境变量 |

## 安装

无第三方依赖，Python 3.10+ 即可；脚本是 PowerShell（Windows 自带 5.1 或 pwsh 7）。

```powershell
git clone https://github.com/3366434156/zburn.git
cd zburn
copy config.example.json config.json     # 按需改路径/正则/额度接口
python zburn.py doctor --config config.json
```

## 快速上手

```powershell
# 0) 自检：应用在不在、无障碍树能不能用、网关通不通、额度读不读得到
python zburn.py doctor --config config.json

# 1) 看一眼输入框和发送按钮定位得对不对（只定位，不输入）
python zburn.py inject --config config.json --text "loop test" --dry-run

# 2) 投喂一轮（--dry-run 只列会话不投喂）
python zburn.py feed --config config.json --dry-run
python zburn.py feed --config config.json

# 3) 正式守护：每 120 秒巡检一次，直到额度烧完 / 出现 STOP 文件 / Ctrl+C
python zburn.py run --config config.json

# 4) 看门狗（心跳超过 420 秒没更新就重新拉起 run）
python zburn.py watchdog --config config.json
```

停止：在状态目录放一个 `STOP` 文件，或按 Ctrl+C。`run` 每轮都会检查它。

## 三种跑法，按场景选

| 场景 | 用什么 | 说明 |
|---|---|---|
| 人在，想要「回合结束自动继续」 | **插件** `zburn.py plugin install` | 应用自己的 Stop 钩子，不用轮询，最省事；只在回合结束时触发 |
| 无人值守 / 锁屏 | **`zburn.py run`** | UIA 轮询投喂；能处理应用崩溃、无障碍树被关、网关挂掉 |
| 两个都要 | 插件 + run | 双保险：插件负责"下一回合"，run 负责"应用挂了/卡住" |

插件安装：

```powershell
python zburn.py plugin install
# 然后在你应用的 CLI 里启用并**重启会话**（钩子在会话启动时绑定）
# 开关文件：~/.zburn/continue.enabled（没有它钩子只写日志不干预）
```

## 换一个应用也能用

zburn 不认死 ZCode，只认「无障碍树里的控件形状」：

```powershell
# 1) 摸清控件：聊天输入框是面积最大的 Edit，发送/停止按钮叫什么名字
python zburn.py probe --config config.json --filter "Edit|Button"
pwsh -File scripts/probe-edits.ps1 -ProcessName Notepad

# 2) 改 config 里的三个东西：process_name / exe / 按钮与状态正则
```

配置里所有"应用相关"的都在 `app` 和 `uia` 两段：

```json
"app": {"process_name": "ZCode", "exe": "E:\\exe\\Zcode\\ZCode.exe", "strip_env": ["ELECTRON_RUN_AS_NODE"]},
"uia": {
  "send_button_regex": "发送|加入队列|Send",
  "busy_regex": "停止生成|Stop generating",
  "queued_regex": "移除待发送消息|Remove queued",
  "session_regex": "Supervisor:|我的项目"
}
```

`session_regex` 决定"哪些会话值得喂"，**一定要按你的项目名收紧**，否则会往别的会话里灌指令。

## 额度：三种来源

| provider | 说明 |
|---|---|
| `none` | 不看额度，永远不停（靠 STOP 文件/Ctrl+C 停） |
| `gateway_zcode2api` | 读 `GET /admin/api/accounts`（`Authorization: Bearer <admin_key>`），自动在 JSON 里找 `remaining/balance/left…` 求和 |
| `command` | 跑任意命令，输出里第一个 ≥4 位的整数就当剩余额度 |

```json
"quota": {
  "provider": "gateway_zcode2api",
  "url": "http://127.0.0.1:3000/admin/api/accounts",
  "admin_key": "zcode",
  "refresh_url": "http://127.0.0.1:3000/admin/api/accounts/refresh",
  "refresh_body": {"all": true},
  "refresh_every_seconds": 600,
  "stop_below": 1000000
}
```

低于 `stop_below` 自动收工；`refresh_every_seconds` 到点会请求后端刷新额度。

网关保活是**只探活、必要时启动，绝不重启**——重启正在跑的网关会让所有对话断流（"connection lost / turn failed"）。
这一条写死在 [scripts/ensure-gateway.ps1](scripts/ensure-gateway.ps1) 里。

## 命令一览

| 命令 | 作用 |
|---|---|
| `doctor` | 环境自检（应用/无障碍树/锁屏状态/网关/额度/模板） |
| `quota [--refresh]` | 读一次额度 |
| `status [--follow]` | 看 status.json、心跳、最近日志 |
| `probe [--filter] [--json]` | 导出目标窗口无障碍树（换应用第一件事） |
| `inject --text "…" [--dry-run] [--no-enter]` | 直接往输入框投一条 |
| `feed [--dry-run] [--prompt "…"]` | 投喂一轮 |
| `run [--dry-run]` | 守护循环（保活/网关/额度/投喂/状态） |
| `watchdog` | 心跳超时拉起 run |
| `plugin install\|remove` | 安装/卸载自动继续插件 |

## 状态与日志

都写在 `paths.state_dir`（默认 `.zburn/`）：

```
.zburn/status.json          当前状态（cycle / remaining / last_feed_summary / pid …）
.zburn/zburn.log            全量日志
.zburn/heartbeat            心跳时间戳（watchdog 看这个）
.zburn/quota-history.csv    额度历史（每轮一行）
.zburn/hook.log             插件钩子日志
.zburn/continue.enabled     插件开关（存在才续派）
.zburn/next-prompt.txt      当前要发的指令（人肉可读，可手改）
```

## 目录

```
zburn.py                    CLI 主程序（doctor/quota/status/probe/inject/feed/run/watchdog/plugin）
scripts/
  inject-uia.ps1            锁屏注入器（内容比对 + 只补后缀 + Invoke 提交）
  feed-sessions.ps1         多会话批量投喂（busy/queued 自动跳过）
  uia-tree.ps1              无障碍树导出
  probe-edits.ps1           列出所有 Edit（定位真正的输入框）
  probe-clear.ps1           验证"清空是否可用"（本项目结论：不可用）
  clear-composer.ps1        尝试清理输入框残留（清不掉会明说）
  ensure-gateway.ps1        网关探活/必要时启动（不重启）
plugin/zcode-continue/      Stop 钩子插件（额度没烧完就自动续派）
config.example.json         配置样例
docs/                       原理 / 配置 / 排查
```

## 已知限制

- **Windows only**：UIA + PostMessage 是 Win32 那套；mac/Linux 需要换 AXAPI / AT-SPI 实现。
- **只能投 ASCII**：中文指令会被丢弃（WM_CHAR 的硬限制），要发中文得换剪贴板 + 粘贴方案（锁屏下不可靠）。
- **退格无效**：输入框里有残留文本时 zburn 不会强行追加，而是跳过并报 `exit 7`；需要人工清一次。
- 无头/锁屏环境下拿不到 GPU 加速的窗口内容，所以不做截图校验，只看控件状态。
- 无障碍树在长时间锁屏后可能被 Chromium 关闭，`run` 会自动重启应用（会打断正在生成的会话，权衡后选了"能继续跑"）。

## License

MIT
