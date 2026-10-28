# -*- coding: utf-8 -*-
"""上下文压缩闸门：被监控会话滚过阈值就投 /compact，健康会话照常续派。"""
import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import zburn  # noqa: E402
import zsession  # noqa: E402


def make_db(tmp_path: pathlib.Path) -> str:
    p = tmp_path / "db.sqlite"
    con = sqlite3.connect(p)
    con.execute("create table session (id text, title text, time_updated int, "
                "time_compacting int, time_deleted int)")
    con.execute("create table model_usage (session_id text, input_tokens int, started_at int)")
    rows = [
        ("s_fat", "着色器预览任务", 200, None),
        ("s_ok", "New chat task: build shader", 150, None),
        ("s_compacting", "又一个小胖子", 180, 12345),
    ]
    con.executemany("insert into session values (?,?,?,?,?)",
                    [(r[0], r[1], r[2], r[3], None) for r in rows])
    usage = [
        ("s_fat", 885343, 300),
        ("s_fat", 0, 310),           # 失败请求记 0，不能被当成当前上下文
        ("s_ok", 42000, 290),
        ("s_compacting", 640000, 280),
    ]
    con.executemany("insert into model_usage values (?,?,?)", usage)
    con.commit()
    con.close()
    return str(p)


def cfg_with(db: str) -> dict:
    return {
        "app": {"process_name": "ZCode"},
        "uia": {"session_db": db, "session_regex": ""},
        "feed": {"targets": ["着色器预览任务", "New chat task", "又一个小胖子"],
                 "compact_at_tokens": 300000, "compact_command": "/compact"},
        "prompt": {"max_chars": 180},
    }


class _St:
    def __init__(self):
        self.line = []

    def log(self, msg):
        self.line.append(str(msg))


def test_session_contexts_reads_model_usage(tmp_path):
    db = make_db(tmp_path)
    out = zsession.session_contexts(cfg_with(db), targets=["着色器预览任务"])
    assert out and out[0]["context_tokens"] == 885343      # 不是 0：跳过失败请求
    assert out[0]["matched"] == "着色器预览任务"
    assert out[0]["compacting"] is False


def test_session_contexts_prefix_and_regex(tmp_path):
    db = make_db(tmp_path)
    cfg = cfg_with(db)
    # UI 截断过的名单（前缀）也配得上
    out = zsession.session_contexts(cfg, targets=["New chat task"])
    assert [s["id"] for s in out] == ["s_ok"]
    out = zsession.session_contexts(cfg, targets=["又一个小胖子#1"])
    assert [s["id"] for s in out] == ["s_compacting"]
    assert out[0]["matched"] == "又一个小胖子#1"           # 保留 #序号，定向投喂不错组
    # 正则兜底
    out = zsession.session_contexts(cfg, targets=[], session_regex="着色器|New chat")
    assert {s["id"] for s in out} == {"s_fat", "s_ok"}
    # 名单一条没对上 → None（闸门失明），不悄悄回退成全量
    assert zsession.session_contexts(cfg, targets=["不存在的对话"]) is None


def test_gate_splits_over_and_under(tmp_path):
    db = make_db(tmp_path)
    gate = zburn.compact_gate(cfg_with(db), _St())
    assert gate is not None
    over, under, note = gate
    assert {s["id"] for s in over} == {"s_fat"}            # 压缩中的不算 over（再投会排队）
    assert {s["id"] for s in under} == {"s_ok"}            # 压缩中的也不算 under
    assert "885" in note


def test_gate_all_over_means_no_continue(tmp_path):
    """全部监控会话都超限时 under 为空：这一轮不再喂 continue（否则白烧几十万 token）。"""
    db = make_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("insert into session values (?,?,?,?,?)",
                ("s_fat2", "另一个大胖子", 210, None, None))
    con.execute("insert into model_usage values (?,?,?)", ("s_fat2", 700000, 320))
    con.commit()
    con.close()
    cfg = cfg_with(db)
    cfg["feed"]["targets"] = ["着色器预览任务", "另一个大胖子"]
    over, under, note = zburn.compact_gate(cfg, _St())
    assert {s["id"] for s in over} == {"s_fat", "s_fat2"}
    assert under == []


def test_session_contexts_duplicate_title_uses_ord(tmp_path):
    """同名会话按「标题#序号」定向：压缩闸门投 /compact 时不能打错那一条。"""
    db = make_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("insert into session values (?,?,?,?,?)", ("s_dup1", "重名会话", 120, None, None))
    con.execute("insert into session values (?,?,?,?,?)", ("s_dup2", "重名会话", 110, None, None))
    con.execute("insert into model_usage values (?,?,?)", ("s_dup1", 400000, 400))
    con.execute("insert into model_usage values (?,?,?)", ("s_dup2", 350000, 390))
    con.commit()
    con.close()
    out = zsession.session_contexts(cfg_with(db), targets=["重名会话#2"])
    assert [s["id"] for s in out] == ["s_dup2"]
    assert out[0]["matched"] == "重名会话#2"               # 定向投喂还按 #2 找


def test_gate_disabled_or_blind(tmp_path):
    db = make_db(tmp_path)
    st = _St()
    cfg = cfg_with(db)
    cfg["feed"]["compact_at_tokens"] = 0
    assert zburn.compact_gate(cfg, st) is None             # 没开阈值
    cfg["feed"]["compact_at_tokens"] = 300000
    cfg["feed"]["targets"] = []
    cfg["uia"]["session_regex"] = ""
    assert zburn.compact_gate(cfg, st) is None             # 没配筛选：闸门不掺和
    cfg["feed"]["targets"] = ["着色器预览任务"]
    cfg["uia"]["session_db"] = str(tmp_path / "nope.sqlite")
    assert zburn.compact_gate(cfg, st) is None             # 库读不到：按原流程投喂
    assert any("闸门不可用" in ln for ln in st.line)


def _feed_cfg() -> dict:
    return {
        "app": {"process_name": "ZCode"},
        "uia": {"session_regex": "着色器预览|New chat", "window_title_regex": "",
                "send_button_regex": "发送", "busy_regex": "停止生成",
                "queued_regex": "移除", "char_delay_ms": 1, "max_depth": 10},
        "feed": {"targets": ["别的会话"], "max_sessions": 5},
        "prompt": {"max_chars": 0},
    }


def test_feed_once_only_targets_overrides_selection(monkeypatch):
    seen = {}

    def fake_run_ps(script, params, timeout=180):
        seen.update(params)
        return 0, "  [x] 已投喂 8 字符\n汇总：投喂 1 / 跳过 0 / 失败 0", ""

    monkeypatch.setattr(zburn, "run_ps", fake_run_ps)
    monkeypatch.setattr(zburn, "flush_composer", lambda cfg: None)
    code, summary = zburn.feed_clean(_feed_cfg(), _St(), "/compact",
                                     only_targets=["着色器预览任务#2"])
    assert code == 0
    assert json.loads(seen["Targets"]) == ["着色器预览任务#2"]
    assert seen["SessionRegex"] == ""                      # 定向投喂不许正则捞别的会话
    assert seen["Prompt"] == "/compact"


def test_feed_clean_retry_keeps_only_targets_and_command(monkeypatch):
    """失败重投也要只投超限会话、仍然投 /compact —— 不能退回正则/全量。"""
    calls: list[dict] = []

    def fake_run_ps(script, params, timeout=180):
        calls.append(dict(params))
        if len(calls) == 1:
            return 0, "  [x] 投喂失败 typed=0 send=False\n汇总：投喂 0 / 跳过 0 / 失败 1", ""
        return 0, "  [x] 已投喂 8 字符\n汇总：投喂 1 / 跳过 0 / 失败 0", ""

    monkeypatch.setattr(zburn, "run_ps", fake_run_ps)
    monkeypatch.setattr(zburn, "flush_composer", lambda cfg: None)
    monkeypatch.setattr(zburn.time, "sleep", lambda seconds: None)
    code, summary = zburn.feed_clean(_feed_cfg(), _St(), "/compact",
                                     only_targets=["着色器预览任务#2"])
    assert code == 0 and "投喂 1" in summary
    assert len(calls) == 2
    for c in calls:
        assert json.loads(c["Targets"]) == ["着色器预览任务#2"]
        assert c["SessionRegex"] == ""
        assert c["Prompt"] == "/compact"
