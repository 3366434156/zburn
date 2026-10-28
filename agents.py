# -*- coding: utf-8 -*-
"""多 agent 编排：配置归一化、交接文件、ASCII 监督桥。

为什么单独一层
--------------
zburn 原来只认一个 app（ZCode）。要让 dsh 之类的第二个桌面 AI 参与进来，
最省事的做法不是把 UIA 代码复制一遍，而是把「某一个 agent 的一整套配置」
当成一个视图：app / uia / feed / prompt 四段合并后交给原来的 feed_once、
uia_ok、start_app 使用。这样单应用路径完全不变，多 agent 只是多跑几遍。

两种角色
--------
* worker     ：真正干活的应用（默认 ZCode）。定时补「继续」，也接受交接。
* supervisor ：监督者（例如 dsh）。定时被唤醒，读 worker 的可见对话尾部，
               判断是否验收通过，把下一步写成 handoff.json / handoff.txt。

为什么有 ASCII 监督桥
---------------------
WM_CHAR 只能投 ASCII，所以监督提示词里不能出现中文路径。桥目录放在
%LOCALAPPDATA%\\zburn-bridge（通常是纯 ASCII），supervisor 只需要读写这里的
文件；zburn 负责把状态/worker 对话快照写进去、把 handoff 读出来。

交接文件
--------
handoff.json:
  {"action": "feed" | "new_chat", "prompt": "<单行 ASCII>",
   "agent": "<worker id>", "project": "<可选，ZCode 项目名>"}

handoff.txt 是更宽松的退路：整份文件内容当成 action=feed 的 prompt。
处理的交接会归档到 <state_dir>/handoffs/，桥目录留一份 handoff-ack.txt。
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import pathlib
import re
import time

WORKER = "worker"
SUPERVISOR = "supervisor"

# 「新建对话」在各家 Electron 客户端里的常见叫法。
DEFAULT_NEW_CHAT = {
    "enabled": True,
    "button_regex": "新建任务|新建对话|新对话|新聊天|新建会话|New chat|New task|New conversation",
    "project": "",
    "send_button_regex": "",
    "allow_offscreen": False,
    "clear_residual": False,
}

DEFAULT_MODEL = {
    "want": "",            # 名字里要包含的字样，例如 "zcode2api"；空=不校验
    "auto_pin": True,      # 发现没钉死就尝试切回去（需要解锁桌面能点下拉）
    "enforce": True,       # 切不回去时宁可这一轮不投喂，也别把额度烧在别的模型上
    "check_every": 10,     # 每 N 轮复检一次，防中途漂移
}

DEFAULT_SUPERVISOR_PROMPT = (
    "You are the zburn supervisor. Read {context} (the worker's session tail), "
    "{status} (runtime state and recent log), and the task description. "
    "Judge whether the worker's latest work is acceptable. "
    "If the worker is still running, reply WAIT and change nothing. "
    "If it needs correction, write {handoff} as JSON "
    '{"action":"feed","prompt":"<one-line ASCII instruction>"}. '
    "If the task is accepted and a fresh conversation should start, write {handoff} as JSON "
    '{"action":"new_chat","prompt":"<one-line ASCII task>"}. '
    "If JSON is hard, write the instruction as plain text to the sibling handoff.txt instead. "
    "Write the file with your file tool or shell. Do not summarize. Do not ask questions. "
    "Write the file now, then reply DONE."
)


# ---------------------------------------------------------------- 小工具
def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def ascii_only(text: str) -> str:
    """WM_CHAR 只认 ASCII；换行折成空格。"""
    text = (text or "").replace("\r", " ").replace("\n", " ")
    return "".join(ch for ch in text if 32 <= ord(ch) <= 126).strip()


def _slug(text: str) -> str:
    s = re.sub(r"[^0-9A-Za-z_.-]+", "-", (text or "").strip()).strip("-._")
    return s.lower() or "agent"


def _state_root(st) -> pathlib.Path:
    return pathlib.Path(st.dir)


def _agent_file(st, agent_id: str, suffix: str) -> pathlib.Path:
    d = _state_root(st) / "agents"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_slug(agent_id)}.{suffix}"


# ---------------------------------------------------------------- 配置归一化
def normalize_agent(cfg: dict, raw, index: int = 0, *, is_default: bool = False) -> dict:
    raw = raw if isinstance(raw, dict) else {"id": str(raw)}
    role = str(raw.get("role") or WORKER).strip().lower()
    if role not in (WORKER, SUPERVISOR):
        role = WORKER

    app = _deep_merge(cfg.get("app") or {}, raw.get("app") or {})
    uia = _deep_merge(cfg.get("uia") or {}, raw.get("uia") or {})
    feed = _deep_merge(cfg.get("feed") or {}, raw.get("feed") or {})
    prompt = _deep_merge(cfg.get("prompt") or {}, raw.get("prompt") or {})

    proc = str(app.get("process_name") or "").strip()
    name = str(raw.get("name") or raw.get("label") or proc or f"agent{index + 1}").strip()
    aid = str(raw.get("id") or "").strip()
    if not aid:
        aid = "default" if is_default else (_slug(name) or f"agent{index + 1}")

    # 非默认 agent 不能继承 worker 的监控名单/窗口正则：dsh 继承 ZCode 的 targets
    # 会把监督提示词灌进 ZCode 的对话里（这是最危险的一种串台）。
    raw_feed = raw.get("feed") or {}
    raw_uia = raw.get("uia") or {}
    if not is_default:
        if "targets" not in raw_feed:
            feed["targets"] = []
        if "session_regex" not in raw_uia:
            uia["session_regex"] = ""
        if "window_title_regex" not in raw_uia:
            uia["window_title_regex"] = ""

    # 监督 agent 默认别跟 worker 一个节奏；没显式写就 10 分钟醒一次。
    if role == SUPERVISOR and "interval_seconds" not in raw_feed:
        feed["interval_seconds"] = 600
    if role == SUPERVISOR:
        if "file" not in (raw.get("prompt") or {}):
            prompt["file"] = "supervisor-prompt.txt"
    elif not is_default and "file" not in (raw.get("prompt") or {}):
        prompt["file"] = f"next-prompt-{_slug(aid)}.txt"

    new_chat = _deep_merge(DEFAULT_NEW_CHAT,
                           _deep_merge(cfg.get("new_chat") or {}, raw.get("new_chat") or {}))
    # 也允许写成 uia.new_chat_button_regex / uia.new_chat_project，少一层嵌套。
    if "new_chat_button_regex" in (raw.get("uia") or {}):
        new_chat["button_regex"] = raw["uia"]["new_chat_button_regex"]
    if "new_chat_project" in (raw.get("uia") or {}):
        new_chat["project"] = raw["uia"]["new_chat_project"]

    return {
        "id": aid,
        "name": name,
        "role": role,
        "enabled": bool(raw.get("enabled", True)),
        "app": app,
        "uia": uia,
        "feed": feed,
        "prompt": prompt,
        "new_chat": new_chat,
        # model 以前没被带出来：agents[].model.want 就算写了也到不了
        # agent_view / cmd_model / 新对话后的校验，等于「配了但没生效」。
        # 顶层 model 当默认值，agent 自己写的覆盖它。
        "model": _deep_merge(_deep_merge(DEFAULT_MODEL, cfg.get("model") or {}),
                             raw.get("model") or {}),
        "supervises": str(raw.get("supervises") or "").strip(),
    }


def agent_specs(cfg: dict) -> list[dict]:
    """把 config.agents 归一化成列表；没有 agents 配置时退化成原来的单应用。"""
    raws = cfg.get("agents")
    if not isinstance(raws, list) or not raws:
        return [normalize_agent(cfg, {
            "id": "default",
            "name": (cfg.get("app") or {}).get("process_name") or "worker",
            "role": WORKER,
        }, 0, is_default=True)]

    def role_of(raw) -> str:
        return str(raw.get("role") or WORKER).strip().lower() if isinstance(raw, dict) else WORKER

    # 「主 worker」= 第一个 worker：它沿用顶层 next-prompt.txt（插件读这个文件）
    # 和顶层 feed.targets；supervisor 即使写在第一位也不能继承 worker 的名单。
    first_worker = next((i for i, r in enumerate(raws)
                         if r is not None and role_of(r) == WORKER), -1)

    specs: list[dict] = []
    used: set[str] = set()
    for i, raw in enumerate(raws):
        if raw is None:
            continue
        spec = normalize_agent(cfg, raw, i,
                               is_default=(i == first_worker and first_worker >= 0))
        base = spec["id"]
        n = 2
        while spec["id"] in used:
            spec["id"] = f"{base}-{n}"
            n += 1
        used.add(spec["id"])
        specs.append(spec)
    return specs


def enabled_agents(cfg: dict) -> list[dict]:
    return [s for s in agent_specs(cfg) if s.get("enabled", True)]


def find_agent(cfg: dict, agent_id: str | None) -> dict | None:
    if not agent_id:
        return None
    for s in agent_specs(cfg):
        if s["id"] == agent_id or s["name"] == agent_id:
            return s
    return None


def orchestrator(cfg: dict) -> tuple[dict | None, dict | None]:
    """返回 (supervisor, worker)。orchestrator 里的 id 优先，否则按 role 找。"""
    specs = agent_specs(cfg)
    o = cfg.get("orchestrator") or {}
    sup = find_agent(cfg, o.get("supervisor")) or next(
        (s for s in specs if s["role"] == SUPERVISOR), None)
    work = find_agent(cfg, o.get("worker")) or next(
        (s for s in specs if s["role"] == WORKER), None)
    return sup, work


def agent_view(cfg: dict, spec: dict, st=None, *, include_dynamic: bool = True) -> dict:
    """一个 agent 的「完整配置视图」：顶层四段被它自己的四段替换。"""
    view = copy.deepcopy(cfg)
    for sec in ("app", "uia", "feed", "prompt"):
        view[sec] = copy.deepcopy(spec[sec])
    view["new_chat"] = copy.deepcopy(spec.get("new_chat") or DEFAULT_NEW_CHAT)
    view["model"] = copy.deepcopy(spec.get("model") or DEFAULT_MODEL)
    view["agent_id"] = spec["id"]
    view["agent_name"] = spec["name"]
    view["agent_role"] = spec["role"]
    view["_agent_spec"] = spec

    if include_dynamic and st is not None and spec["role"] == WORKER:
        targets = list(view["feed"].get("targets") or [])
        for t in load_dynamic_targets(st, spec["id"]):
            if t not in targets:
                targets.append(t)
        view["feed"]["targets"] = targets
    return view


# ---------------------------------------------------------------- 动态监控名单
def load_dynamic_targets(st, agent_id: str) -> list[str]:
    p = _agent_file(st, agent_id, "targets.json")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [str(x) for x in (data.get("targets") or []) if str(x).strip()]
    except Exception:
        return []


def add_dynamic_target(st, agent_id: str, target: str) -> bool:
    target = (target or "").strip()
    if not target:
        return False
    cur = load_dynamic_targets(st, agent_id)
    if target in cur:
        return False
    cur.append(target)
    p = _agent_file(st, agent_id, "targets.json")
    p.write_text(json.dumps({"targets": cur, "updated": _now()},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    return True


def clear_dynamic_targets(st, agent_id: str) -> None:
    _agent_file(st, agent_id, "targets.json").unlink(missing_ok=True)


def set_dynamic_targets(st, agent_id: str, targets: list[str]) -> list[str]:
    """直接设定动态监控名单（命令行 zburn target 用）。"""
    cur = [str(t).strip() for t in (targets or []) if str(t).strip()]
    p = _agent_file(st, agent_id, "targets.json")
    if not cur:
        p.unlink(missing_ok=True)
        return []
    p.write_text(json.dumps({"targets": cur, "updated": _now()},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    return cur


# ---------------------------------------------------------------- agent 状态
def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_state(st, agent_id: str) -> dict:
    p = _agent_file(st, agent_id, "json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def update_state(st, agent_id: str, **kw) -> dict:
    cur = read_state(st, agent_id)
    cur.update(kw)
    cur["updated"] = _now()
    p = _agent_file(st, agent_id, "json")
    p.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    return cur


def describe(cfg: dict, st) -> list[dict]:
    """给面板/status 用的 agent 摘要。"""
    out = []
    for s in agent_specs(cfg):
        state = read_state(st, s["id"])
        pending = get_pending(st, s["id"])
        out.append({
            "id": s["id"],
            "name": s["name"],
            "role": s["role"],
            "enabled": s.get("enabled", True),
            "process_name": s["app"].get("process_name", ""),
            "interval_seconds": s["feed"].get("interval_seconds", 0),
            "targets": s["feed"].get("targets") or [],
            "dynamic_targets": load_dynamic_targets(st, s["id"]),
            "supervises": s.get("supervises", ""),
            "last_feed": state.get("last_feed", ""),
            "last_result": state.get("last_result", ""),
            "last_action": state.get("last_action", ""),
            "last_run": state.get("last_run", ""),
            "turns_on_target": state.get("turns_on_target", 0),
            "last_model": state.get("last_model", ""),
            "model_ok": state.get("model_ok"),
            "model_want": (s.get("model") or {}).get("want", ""),
            "pending": pending,
        })
    return out


# ---------------------------------------------------------------- 待执行交接
def set_pending(st, agent_id: str, action: str, prompt: str, *,
                project: str = "", source: str = "") -> dict:
    action = "new_chat" if action in ("new", "newchat", "new_chat") else "feed"
    rec = {
        "action": action,
        "prompt": ascii_only(prompt),
        "project": (project or "").strip(),
        "source": source,
        "created": _now(),
    }
    p = _agent_file(st, agent_id, "pending.json")
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return rec


def get_pending(st, agent_id: str) -> dict | None:
    p = _agent_file(st, agent_id, "pending.json")
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        return rec if (rec.get("prompt") or "").strip() else None
    except Exception:
        return None


def clear_pending(st, agent_id: str) -> None:
    _agent_file(st, agent_id, "pending.json").unlink(missing_ok=True)


# ---------------------------------------------------------------- 监督桥
def bridge_dir(cfg: dict) -> pathlib.Path:
    p = (cfg.get("paths") or {}).get("bridge_dir")
    if p:
        return pathlib.Path(p)
    # LOCALAPPDATA 一般是纯 ASCII；这里的路径会被写进 WM_CHAR 提示词。
    base = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home())
    d = pathlib.Path(base) / "zburn-bridge"
    if not str(d).isascii():
        # 用户名是中文时 LOCALAPPDATA 也可能带中文；换到 PUBLIC（通常是 ASCII）
        d = pathlib.Path(os.environ.get("PUBLIC") or "C:/Users/Public") / "zburn-bridge"
    return d


def ensure_bridge(cfg: dict) -> pathlib.Path:
    d = bridge_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    return d


def supervisor_prompt(cfg: dict, supervisor: dict | None, worker: dict | None) -> str:
    o = cfg.get("orchestrator") or {}
    tpl = str(o.get("prompt") or "").strip() or DEFAULT_SUPERVISOR_PROMPT
    bridge = bridge_dir(cfg)
    values = {
        "bridge": str(bridge),
        "context": str(bridge / "context.txt"),
        "status": str(bridge / "status.json"),
        "handoff": str(bridge / "handoff.json"),
        "ack": str(bridge / "handoff-ack.txt"),
        "worker": (worker or {}).get("name", "worker"),
        "supervisor": (supervisor or {}).get("name", "supervisor"),
    }
    for k, v in values.items():
        tpl = tpl.replace("{" + k + "}", v)
    # 文件里的规则可能带中文；真正投喂前仍会只保留 ASCII。
    return ascii_only(tpl)


def write_bridge(cfg: dict, st, *, status: dict, context: str,
                 prompt: str, worker_name: str = "", supervisor_name: str = "") -> dict:
    """刷新监督桥里的只读文件；不动 handoff.json / handoff.txt。"""
    d = ensure_bridge(cfg)
    (d / "status.json").write_text(
        json.dumps({"updated": _now(), "status": status, "agents": describe(cfg, st)},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "context.txt").write_text(context or "(worker context unavailable)\n",
                                   encoding="utf-8")
    (d / "supervisor-prompt.txt").write_text(prompt or "", encoding="utf-8")
    (d / "README.txt").write_text(
        "zburn supervisor bridge\n"
        "=======================\n"
        "This directory is the only hand-off channel between zburn and a supervisor agent.\n"
        "Paths here are plain ASCII on purpose: the supervisor prompt is typed through WM_CHAR.\n\n"
        "Read:\n"
        "  status.json        runtime state, agents, quota, recent log\n"
        "  context.txt        visible tail of the worker's chat (what it is doing now)\n"
        "  supervisor-prompt.txt  the exact prompt that woke you\n\n"
        "Write ONE of:\n"
        '  handoff.json       {"action":"feed","prompt":"<one-line ASCII instruction>"}\n'
        '                     {"action":"new_chat","prompt":"<one-line ASCII task>"}\n'
        "  handoff.txt        plain text = action feed with this prompt\n\n"
        "Rules:\n"
        "  * Only write a handoff when the worker needs a NEW instruction or a NEW chat.\n"
        "  * If the worker is still running, write nothing.\n"
        "  * prompt must be ASCII only (WM_CHAR limitation).\n"
        "  * zburn consumes the file and writes handoff-ack.txt.\n",
        encoding="utf-8")
    return {
        "dir": str(d),
        "status": str(d / "status.json"),
        "context": str(d / "context.txt"),
        "handoff": str(d / "handoff.json"),
        "ack": str(d / "handoff-ack.txt"),
        "worker": worker_name,
        "supervisor": supervisor_name,
    }


def _normalize_handoff(data: dict, default_agent: str = "") -> dict | None:
    action = str(data.get("action") or data.get("kind") or "feed").strip().lower()
    if action in ("new", "newchat", "new_chat", "new-conversation", "new_conversation"):
        action = "new_chat"
    elif action in ("continue", "feed", "prompt", "message"):
        action = "feed"
    else:
        return None
    raw = str(data.get("prompt") or data.get("text") or data.get("message")
              or data.get("reason") or "")
    # 交接指令会走 WM_CHAR：中文/全角标点会被丢字。宁可整条拒绝，也别发半句。
    if any(ord(ch) > 126 and ch not in "\r\n\t" for ch in raw):
        return None
    prompt = ascii_only(raw)
    if not prompt:
        return None
    return {
        "action": action,
        "prompt": prompt,
        "agent": str(data.get("agent") or default_agent or "").strip(),
        "project": str(data.get("project") or "").strip(),
        # 文件里没写 created 就别替它编一个：以前读一次就盖一个新时间，
        # 交接等待多久完全看不出来（每次读都显示「刚刚写的」）。
        "created": str(data.get("created") or ""),
    }


def read_handoff(cfg: dict, st, default_agent: str = "") -> tuple[dict | None, str]:
    """看有没有待处理交接（不删除）。"""
    candidates = [bridge_dir(cfg) / "handoff.json", bridge_dir(cfg) / "handoff.txt"]
    for src in candidates:
        if not src.exists():
            continue
        try:
            raw = src.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        if src.suffix == ".json":
            try:
                rec = _normalize_handoff(json.loads(raw), default_agent)
            except Exception:
                rec = None
        else:
            rec = _normalize_handoff({"action": "feed", "prompt": raw}, default_agent)
        if rec:
            return rec, str(src)
    return None, ""


def consume_handoff(cfg: dict, st, default_agent: str = "") -> dict | None:
    """取走并归档一份交接；桥目录留 ack。"""
    rec, src = read_handoff(cfg, st, default_agent)
    if not rec:
        return None
    srcp = pathlib.Path(src)
    archive = _state_root(st) / "handoffs"
    archive.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        (archive / f"{stamp}-{rec['action']}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    try:
        srcp.unlink(missing_ok=True)
    except OSError:
        pass
    # json 和 txt 可能同时存在：取走一份就把另一份也清掉，避免下一轮重复执行
    for sibling in (bridge_dir(cfg) / "handoff.json", bridge_dir(cfg) / "handoff.txt"):
        try:
            sibling.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        (bridge_dir(cfg) / "handoff-ack.txt").write_text(
            f"{_now()} accepted action={rec['action']} agent={rec['agent'] or '-'}\n"
            f"prompt={rec['prompt']}\n", encoding="utf-8")
    except OSError:
        pass
    return rec


def write_handoff(cfg: dict, st, action: str, prompt: str, *,
                  agent: str = "", project: str = "") -> dict:
    rec = _normalize_handoff({"action": action, "prompt": prompt,
                              "agent": agent, "project": project})
    if not rec:
        raise ValueError("action 只能是 feed/new_chat；prompt 必须非空且只含 ASCII"
                         "（WM_CHAR 不能投中文，中文请只放在 context.txt）")
    d = ensure_bridge(cfg)
    (d / "handoff.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    return rec


def clear_handoff(cfg: dict, st) -> int:
    n = 0
    for p in (bridge_dir(cfg) / "handoff.json", bridge_dir(cfg) / "handoff.txt",
              bridge_dir(cfg) / "handoff-ack.txt"):
        if p.exists():
            p.unlink(missing_ok=True)
            n += 1
    return n


def handoff_status(cfg: dict, st, default_agent: str = "") -> dict:
    rec, src = read_handoff(cfg, st, default_agent)
    age = None
    if rec and rec.get("created"):
        try:
            age = int(time.time() - dt.datetime.strptime(
                rec["created"], "%Y-%m-%d %H:%M:%S").timestamp())
        except ValueError:
            age = None
    return {
        "pending": rec,
        "age_seconds": age,
        "source": src,
        "bridge": str(bridge_dir(cfg)),
        "prompt": supervisor_prompt(cfg, *orchestrator(cfg)),
    }
