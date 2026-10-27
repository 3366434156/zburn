# -*- coding: utf-8 -*-
"""从 ZCode 本地会话库提取「真实对话尾部」，给 supervisor 做验收依据。

为什么不用屏幕快照
------------------
UIA 只能看到当前渲染出来的控件；ZCode 右栏经常开着内置浏览器/面板，
快照会把浏览器页面当成对话内容。ZCode 自己把每个会话完整写进了
~/.zcode/cli/db/db.sqlite（session / message / part 三张表），
读它比读屏幕稳定得多，也不受滚动位置影响。

数据库始终以只读 + WAL 方式打开；读不到就返回 None，调用方退回 UIA 快照。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import sqlite3

REL_TIME_RE = re.compile(r"(刚刚|\s*\d+\s*(秒|秒钟|分|分钟|小时|天|周|个月|月|年))$")
SYSTEM_PREFIXES = (
    "The TodoWrite tool hasn't been used",
    "The following skills are available",
    "<system-reminder>",
    "<command-name>",
    "[Request interrupted",
)


def _key(text: str) -> str:
    return REL_TIME_RE.sub("", (text or "").strip()).strip()


def default_db() -> pathlib.Path:
    return pathlib.Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"


def _connect(db: str | pathlib.Path) -> sqlite3.Connection | None:
    p = pathlib.Path(db)
    if not p.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        return con
    except Exception:
        return None


def _fmt_time(ms) -> str:
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _norm_path(p: str) -> str:
    return os.path.normcase(os.path.normpath(p or "")).rstrip("\\/")


def find_session(con: sqlite3.Connection, *, targets: list[str] | None = None,
                 work_dir: str = "") -> dict | None:
    """按监控名单挑会话；名单没命中就用 work_dir 里最近更新的那个。"""
    rows = [dict(r) for r in con.execute(
        "select id,title,directory,time_updated,last_outcome from session "
        "where time_deleted is null order by time_updated desc")]
    if not rows:
        return None

    for raw in (targets or []):
        raw = (raw or "").strip()
        if not raw:
            continue
        want_ord = 0
        m = re.match(r"^(.*)#(\d+)$", raw)
        if m:
            raw, want_ord = m.group(1), int(m.group(2))
        want = _key(raw)
        if not want:
            continue
        exact = [r for r in rows if _key(r.get("title", "")) == want]
        if exact:
            return exact[min(max(want_ord, 1), len(exact)) - 1]
        # UI 里标题会被截断成「前半段…」；允许前缀匹配，但要求足够长，避免误配
        loose = [r for r in rows if want and _key(r.get("title", "")).startswith(want)]
        if loose:
            return loose[min(max(want_ord, 1), len(loose)) - 1]
        loose = [r for r in rows if want and want.startswith(
            _key(r.get("title", ""))[:max(8, len(want) - 3)])]
        if loose:
            return loose[min(max(want_ord, 1), len(loose)) - 1]

    # 给了监控名单却一条都没对上：宁可返回 None（调用方退回 UIA 快照），
    # 也不要悄悄拿「最近更新的会话」——那可能是另一个项目的对话。
    if any((t or "").strip() for t in (targets or [])):
        return None

    if work_dir:
        wd = _norm_path(work_dir)
        same = [r for r in rows if _norm_path(r.get("directory", "")) == wd]
        if same:
            return same[0]
        under = [r for r in rows if _norm_path(r.get("directory", "")).startswith(wd + os.sep)]
        if under:
            return under[0]
    return rows[0]


def _part_summary(part: dict) -> tuple[str, str]:
    """返回 (kind, text)；只保留对验收有用、又不至于淹没上下文的字段。"""
    typ = part.get("type") or ""
    if typ == "text":
        return "text", str(part.get("text") or "").strip()
    if typ == "reasoning":
        return "reasoning", ""
    if typ == "tool":
        name = str(part.get("tool") or "tool")
        state = part.get("state") or {}
        status = str(state.get("status") or "")
        inp = state.get("input") or {}
        hint = ""
        if isinstance(inp, dict):
            for k in ("command", "filePath", "file_path", "path", "pattern", "query", "description"):
                if inp.get(k):
                    hint = str(inp[k]).replace("\n", " ")[:220]
                    break
            if not hint:
                hint = json.dumps(inp, ensure_ascii=False)[:220]
        return "tool", f"{name} ({status}) {hint}".strip()
    return "", ""


def _message_parts(con: sqlite3.Connection, message_id: str) -> tuple[str, list[str], list[str]]:
    """返回 (role, text_parts, tool_summaries)。"""
    try:
        meta = json.loads(con.execute(
            "select data from message where id=?", (message_id,)).fetchone()["data"])
    except Exception:
        meta = {}
    role = str(meta.get("role") or "?")
    texts, tools = [], []
    for p in con.execute("select data,sequence from part where message_id=? order by sequence",
                         (message_id,)):
        try:
            pd = json.loads(p["data"])
        except Exception:
            continue
        kind, txt = _part_summary(pd)
        if kind == "text" and txt:
            texts.append(txt)
        elif kind == "tool" and txt:
            tools.append(txt)
    return role, texts, tools


def first_user_message(con: sqlite3.Connection, sid: str) -> str:
    """会话最初那条真正的用户指令（跳过 system-reminder）。"""
    for m in con.execute("select id from message where session_id=? order by sequence asc limit 80",
                         (sid,)):
        role, texts, _ = _message_parts(con, m["id"])
        if role != "user":
            continue
        body = "\n".join(texts).strip()
        if not body or any(body.startswith(pre) for pre in SYSTEM_PREFIXES):
            continue
        return body[:2500]
    return ""


def transcript(con: sqlite3.Connection, session: dict, *,
               max_messages: int = 100, max_chars: int = 30000) -> str:
    sid = session["id"]
    msgs = [dict(r) for r in con.execute(
        "select id,data,sequence from message where session_id=? order by sequence desc limit ?",
        (sid, max_messages))]
    msgs.reverse()
    lines: list[str] = []
    for m in msgs:
        role, texts, tools = _message_parts(con, m["id"])
        body = "\n".join(texts).strip()
        if role == "user":
            if any(body.startswith(pre) for pre in SYSTEM_PREFIXES):
                continue
            body = body[:1500]
            if body:
                lines.append(f"[user] {body}")
        elif role == "assistant":
            if body:
                lines.append(f"[assistant] {body[:3000]}")
            for t in tools[:8]:
                lines.append(f"  [tool] {t}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[-max_chars:]
    task = first_user_message(con, sid)
    head = [
        f"# session: {session.get('title', '')}",
        f"# id: {sid}",
        f"# dir: {session.get('directory', '')}",
        f"# updated: {_fmt_time(session.get('time_updated'))}",
        f"# last_outcome: {session.get('last_outcome') or '-'}",
    ]
    if task:
        head += ["", "## task (first user message)", task, ""]
    return "\n".join(head) + "\n## transcript (oldest -> newest)\n" + (text or "(empty)")


def context(cfg: dict, targets: list[str] | None = None,
            work_dir: str = "") -> str | None:
    """读配置里的 ZCode 会话库；不可用/没会话时返回 None。"""
    db = (cfg.get("uia") or {}).get("session_db")
    if db is None or str(db).strip().lower() in ("-", "none", "off", "disabled"):
        return None
    db = str(db).strip() or str(default_db())
    if not db:
        return None
    con = _connect(db)
    if con is None:
        return None
    try:
        session = find_session(con, targets=targets, work_dir=work_dir)
        if not session:
            return None
        return transcript(con, session)
    except Exception:
        return None
    finally:
        con.close()


def latest_session(cfg: dict, work_dir: str = "") -> dict | None:
    """最近更新的会话（新对话发出后，用它把新会话加进动态监控名单）。"""
    db = (cfg.get("uia") or {}).get("session_db")
    if db is None or str(db).strip().lower() in ("-", "none", "off", "disabled"):
        return None
    con = _connect(str(db).strip() or default_db())
    if con is None:
        return None
    try:
        return find_session(con, targets=[], work_dir=work_dir)
    except Exception:
        return None
    finally:
        con.close()
