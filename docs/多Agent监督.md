# 多 Agent：worker + supervisor

zburn 原来只驱动一个桌面应用（ZCode），每轮只会补一句 `Continue...`。
现在可以配置多个 agent，让一个 **worker** 干活、另一个 **supervisor**（例如 dsh）定时醒来
看 worker 的对话，决定「继续 / 纠正 / 换一个新对话」。

```
┌────────────┐   定时唤醒（UIA 注入）    ┌──────────────┐
│ supervisor │ ◀─────────────────────── │              │
│ (dsh)      │ ── 写 handoff.json ────▶ │   zburn run  │
└────────────┘                          │   （调度器）  │
      ▲  读 context.txt / status.json   │              │
      │                                  └──────┬───────┘
      │                                         │ feed / new_chat（UIA）
      │                                         ▼
┌────────────┐       ZCode 本地会话库      ┌──────────────┐
│ worker     │ ── ~/.zcode/cli/db/db.sqlite │  ZCode 桌面  │
│ (ZCode)    │ ◀─────────────────────────── │              │
└────────────┘                              └──────────────┘
```

## 1. 配置

顶层 `app/uia/feed/prompt` 仍然是默认值；`agents` 里每一项只写差异字段，会与顶层深合并：

```jsonc
"agents": [
  {
    "id": "zcode",
    "name": "ZCode",
    "role": "worker",
    "enabled": true,
    "feed": { "targets": [] }          // 从面板「会话监控」勾选后写这里
  },
  {
    "id": "dsh",
    "name": "dsh",
    "role": "supervisor",
    "enabled": false,                  // 先按第 2 步重启 dsh，再在面板里启用
    "supervises": "zcode",
    "app": {
      "process_name": "DeepSeek Harness",
      "exe": "E:\\exe\\DeepSeek Harness\\DeepSeek Harness.exe",
      "start_args": ["--force-renderer-accessibility"],
      "strip_env": ["ELECTRON_RUN_AS_NODE"],
      "start_if_down": true
    },
    "uia": {
      "send_button_regex": "发送|Send",
      "busy_regex": "停止|Stop",
      "queued_regex": "排队|Queue",
      "session_db": null               // 不要读 ZCode 的库
    },
    "feed": { "targets": ["zburn 监督"], "interval_seconds": 600, "max_sessions": 1 },
    "prompt": { "file": "supervisor-prompt.txt" }
  }
],
"orchestrator": {
  "enabled": true,
  "supervisor": "dsh",
  "worker": "zcode",
  "prompt": "",                        // 空 = 内置提示词
  "handoff_wait_seconds": 120
},
"new_chat": {
  "enabled": true,
  "button_regex": "新建任务|新建对话|新对话|New chat",
  "project": "zburn"                   // ZCode 里点哪个项目的「新建任务」
}
```

`feed.targets` 里的 supervisor 对话，是 dsh 里专门用来做监督的那个对话标题
（例如新建一个叫 `zburn supervisor` 的对话）。面板「多 Agent」卡会列出 agent，
可以扫描侧栏、勾选后保存到对应 agent 的 targets。

## 2. dsh 必须带无障碍开关启动一次

Electron 应用不带 `--force-renderer-accessibility` 时，UIA 只能读到窗口按钮，读不到聊天区。
第一次接入（或 dsh 升级后）执行一次：

```powershell
python zburn.py restart-app --config config.json --agent dsh --yes
```

它会结束 `DeepSeek Harness.exe` 并用配置里的 `start_args` 重新拉起。
**会打断 dsh 里正在跑的回合**，所以挑没任务的时候做。之后在面板「多 Agent」卡点「启用」。

重启后先确认无障碍树读得到聊天区（不是只有最小化/关闭三个按钮）：

```powershell
python zburn.py probe --config config.json --agent dsh --filter "Edit|Button|ListItem"
```

如果输入框/发送按钮的名字跟默认正则对不上，就在 dsh 的 `uia` 段里改
`send_button_regex` / `busy_regex` / `queued_regex`。`new-chat.ps1` 同理用
`new_chat.button_regex` 适配「新建对话」按钮。

## 3. 监督桥（ASCII 通道）

WM_CHAR 只能投 ASCII，所以监督提示词里不能出现中文路径。zburn 把交接目录放在
`%LOCALAPPDATA%\zburn-bridge`（默认通常是纯 ASCII）：

```
zburn-bridge/
  status.json          运行状态、额度、各 agent 最近结果、最近日志
  context.txt          worker 的真实对话尾部（优先读 ZCode SQLite，读不到才用 UIA 快照）
  supervisor-prompt.txt 这次唤醒 supervisor 用的原文
  handoff.json         supervisor 写：{"action":"feed"|"new_chat","prompt":"..."}
  handoff.txt          退路：整份文件内容当成 feed 的 prompt
  handoff-ack.txt      zburn 取走交接后的确认
  README.txt           给 supervisor 看的协议说明
```

supervisor 被唤醒后，提示词会让它读 `context.txt` / `status.json`，然后二选一：

* worker 还在跑 → 不写文件（zburn 下一轮再来）；
* 需要纠正 → 写 `handoff.json`（`action=feed`）；
* 验收通过、该换新话题 → 写 `handoff.json`（`action=new_chat`）。

zburn 每轮开始时取走交接，排进目标 worker 的待执行队列：
worker 空闲时执行；worker 正在出字时保留到下一轮，不会丢。

也可以手动写：

```powershell
python zburn.py handoff --config config.json --action feed --prompt "Add tests for the new parser."
python zburn.py handoff --config config.json --action new_chat --prompt "Start a fresh task: ..."
python zburn.py handoff --config config.json --list
python zburn.py handoff --config config.json --apply
python zburn.py handoff --config config.json --action clear --clear-pending
```

## 4. 新对话（不再只会在旧会话里续命）

`new_chat.ps1` 的做法：

1. 在侧栏找「新建任务/新建对话」按钮（`new_chat.button_regex`）；
2. 如果给了 `project`，先定位项目行，再点同一行的「新建任务」；
3. 等新会话的输入框出现（用宽度排除 ZCode 右侧内置浏览器里的 textarea）；
4. 输入 ASCII 指令并提交；
5. 扫一次侧栏，把「当前对话」加入该 agent 的动态监控名单
   （`.zburn/agents/<id>.targets.json`），后续「继续」会自动喂到新对话。

手动试：

```powershell
python zburn.py newchat --config config.json --agent zcode --project zburn --prompt "Reply with OK only." --dry-run
python zburn.py newchat --config config.json --agent zcode --project zburn --prompt "Start a new task: ..."
```

`--dry-run` 只定位按钮和输入框，不点击；`--click-only` 只点开新对话，不输入。

> 「新建任务」按钮必须在侧栏可见。项目被滚出可视区域时，脚本会拒绝点击并提示
> （而不是点空后把字打进当前对话）。把侧栏滚到目标项目再试，或改用
> `new_chat.allow_offscreen: true` 硬点（仅排障用）。

## 5. 常用命令

| 命令 | 作用 |
|---|---|
| `agents` | 列出所有 agent、角色、进程、监控数、待执行交接 |
| `newchat --agent … --prompt …` | 新建对话并投递首条指令 |
| `handoff --action feed\|new_chat` | 手动写一份交接 |
| `handoff --apply` | 把桥里的交接排进 worker 的待执行队列 |
| `supervise` | 立刻刷新监督桥并唤醒 supervisor；`--wait 120` 等它写回交接 |
| `snapshot --agent zcode` | 导出 worker 的对话尾部（调试监督上下文） |
| `restart-app --agent dsh --yes` | 带 a11y 开关重启 dsh |

## 6. 限制

* **只投 ASCII**：监督者和 worker 的下一步指令都必须是英文/ASCII；
  `context.txt` 可以是中文（它是文件，不走 WM_CHAR）。
* **dsh 的会话标题要稳定**：`feed.targets` 用的是侧栏标题；改名后要重新扫描勾选。
* **ZCode 会话库**：默认读 `~/.zcode/cli/db/db.sqlite`；换版本后如果表结构变了，
  `zsession.py` 会读不到并自动退回 UIA 可见文本。
* **supervisor 写文件需要权限**：dsh 的文件工具/终端要能写 `%LOCALAPPDATA%\zburn-bridge`；
  如果它的工作区沙箱不允许，给它加一个可写目录，或让它用 shell 的 `echo >` 写。
* **两个 zburn 进程不要同时开**：`run` 和 `autopilot`/`batch` 都会操作 UIA；
  同一时间只留一个调度器。现在有硬锁：`run` / `autopilot` / `batch` 启动时抢
  `%TEMP%\zburn-scheduler.lock`，被占就拒绝并打印是谁在跑；进程崩了留下的死锁会被自动接管。
  `batch`/`autopilot` 内部的每个分片不再各自抢锁（它们共用父进程那把）。
* **输入框残留文本**：ZCode 2.x 上退格、`Ctrl+A`、`ValuePattern.SetValue('')` 全是空操作，
  唯一可靠的腾空办法是**把残留当一条普通消息发出去**（`scripts/flush-composer.ps1`）。
  `batch` 投喂失败会自动调它重试；`new-chat.ps1 -ClearResidual` 也走这条路，
  所以残留不会再堵死新对话。

## 7. 别让额度白烧（2026-10-28 实测血泪）

* **指令上限**：`prompt.max_chars`（默认 180）。WM_CHAR 打长了会丢字，长指令写进文件让模型读。
  实测夜里 400 字符的指令只打进去 300 多，剩下的被吞，然后 flush 又把半截当消息发出去。
* **对话轮换**：`feed.rotate_after_turns`（默认 3）。一个对话续太多轮，上下文越滚越大，
  每轮都要重发整段历史 —— 这是最隐形的烧钱方式。跑满就 `new_chat`。
* **模型钉死**：`agents[].model.want`（如 `zcode2api`） + `zburn model --want zcode2api`。
  ZCode 里显示 `magpie/auto/…` 时，`auto` 会自己挑账号池，可能把某几个号打空、
  另一个号的额度一分没动（实测：一个号 500 万整桶未使用）。
* **看速率**：`zburn status` 第一行会打印「近一小时掉多少 / 约多少 tokens 每小时」，
  速率高但任务没推进 = 空转，先降频（`interval_seconds` 300）再看。

## 8. 监督通道的四个必须（2026-10-28 二次实测）

夜里 supervisor 被叫醒了 **158 轮**，全在同一个工作对话里刷 WAIT，会话堆到 1.5M 输入 token。
现在这四条规定写进代码了：

1. **专用对话**：`agents[dsh].uia.session_regex = "zburn supervisor"`，
   唤醒只打到标题以它开头的对话；找不到就**自动新建**一个（`supervisor_seed()`），
   绝不再往「随便一个工作对话」里灌。
2. **没变化不叫醒**：`orchestrator.skip_when_idle`。唤醒前先算指纹
   （`status.json` + `context.txt` + 有没有待处理交接），指纹没变直接跳过，日志写
   `监督：worker 无变化，跳过本轮唤醒`。
3. **限频**：`orchestrator.max_wakes_per_hour`（默认 6），超了跳过。
4. **轮换**：`agents[dsh].feed.rotate_after_turns`（默认 3）。监督对话跑满就换一个新的
   —— 它是长对话，压缩也追不上膨胀速度。

唤醒词本身只做一件事：**让 supervisor 去读桥里的任务书**。

```
Read C:\Users\Administrator\AppData\Local\zburn-bridge\supervisor-task.txt and follow it exactly. Reply DONE when finished.
```

只有 123 字符（能在 WM_CHAR 限制内完整打进去）；完整规则（含 status/context/handoff 三个长路径）
落在 `zburn-bridge\supervisor-task.txt`，由 `zburn` 每次唤醒前重写。
