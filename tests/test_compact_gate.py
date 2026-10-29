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
    con.execute("create table model_usage (session_id text, input_tokens int, "
                "started_at int, model_id text)")
    rows = [
        ("s_fat", "着色器预览任务", 200, None),
        ("s_ok", "New chat task: build shader", 150, None),
        ("s_compacting", "又一个小胖子", 180, 12345),
    ]
    con.executemany("insert into session values (?,?,?,?,?)",
                    [(r[0], r[1], r[2], r[3], None) for r in rows])
    pinned = "zcode2api/GLM-5.3-Flash"
    usage = [
        ("s_fat", 885343, 300, pinned),
        ("s_fat", 0, 310, "auto/GLM-5.3-Flash"),  # 失败请求记 0，也不能改变「当前模型」
        ("s_ok", 42000, 290, pinned),
        ("s_compacting", 640000, 280, pinned),
    ]
    con.executemany("insert into model_usage values (?,?,?,?)", usage)
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
    assert out[0]["model"] == "zcode2api/GLM-5.3-Flash"    # 失败请求的 auto 不算当前模型
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
    assert {s["id"] for s in gate["over"]} == {"s_fat"}    # 压缩中的不算 over（再投会排队）
    assert {s["id"] for s in gate["under"]} == {"s_ok"}    # 压缩中的也不算 under
    assert gate["offpin"] == []                            # 没给 want：不判模型
    assert "885" in gate["note"]


def test_gate_all_over_means_no_continue(tmp_path):
    """全部监控会话都超限时 under 为空：这一轮不再喂 continue（否则白烧几十万 token）。"""
    db = make_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("insert into session values (?,?,?,?,?)",
                ("s_fat2", "另一个大胖子", 210, None, None))
    con.execute("insert into model_usage values (?,?,?,?)",
                ("s_fat2", 700000, 320, "zcode2api/GLM-5.3-Flash"))
    con.commit()
    con.close()
    cfg = cfg_with(db)
    cfg["feed"]["targets"] = ["着色器预览任务", "另一个大胖子"]
    gate = zburn.compact_gate(cfg, _St())
    assert {s["id"] for s in gate["over"]} == {"s_fat", "s_fat2"}
    assert gate["under"] == []


def test_gate_flags_sessions_on_auto(tmp_path):
    """模型跟会话走：后台会话跑在 auto/ 上时，光看界面按钮是发现不了的。"""
    db = make_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("insert into session values (?,?,?,?,?)", ("s_auto", "跑偏的会话", 220, None, None))
    con.execute("insert into model_usage values (?,?,?,?)",
                ("s_auto", 931061, 500, "auto/GLM-5.3-Flash"))
    con.commit()
    con.close()
    cfg = cfg_with(db)
    cfg["feed"]["targets"] = ["着色器预览任务", "New chat task", "跑偏的会话"]
    gate = zburn.compact_gate(cfg, _St(), want="zcode2api")
    assert [s["id"] for s in gate["offpin"]] == ["s_auto"]
    # 跑错模型的会话既不进 over（不给它投 /compact）、也不进 under（不给它续派）
    assert "s_auto" not in {s["id"] for s in gate["over"] + gate["under"]}
    assert "auto" in gate["note"]


def test_gate_wants_model_only_when_known(tmp_path):
    """model 读不到（老会话没 usage 记录）时不能判成「跑错模型」，否则会白白停喂。"""
    db = make_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("insert into session values (?,?,?,?,?)", ("s_new", "刚建的会话", 230, None, None))
    con.commit()
    con.close()
    cfg = cfg_with(db)
    cfg["feed"]["targets"] = ["刚建的会话"]
    gate = zburn.compact_gate(cfg, _St(), want="zcode2api")
    assert gate["offpin"] == []
    assert [s["id"] for s in gate["under"]] == ["s_new"]


def test_session_contexts_duplicate_title_uses_ord(tmp_path):
    """同名会话按「标题#序号」定向：压缩闸门投 /compact 时不能打错那一条。"""
    db = make_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("insert into session values (?,?,?,?,?)", ("s_dup1", "重名会话", 120, None, None))
    con.execute("insert into session values (?,?,?,?,?)", ("s_dup2", "重名会话", 110, None, None))
    con.execute("insert into model_usage values (?,?,?,?)",
                ("s_dup1", 400000, 400, "zcode2api/GLM-5.3-Flash"))
    con.execute("insert into model_usage values (?,?,?,?)",
                ("s_dup2", 350000, 390, "zcode2api/GLM-5.3-Flash"))
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
    gate = zburn.compact_gate(cfg, st)
    # 关了压缩也要能按会话看模型：闸门不返回 None，只是没人超限
    assert gate is not None and gate["over"] == [] and gate["under"]
    cfg["feed"]["compact_at_tokens"] = 300000
    cfg["feed"]["targets"] = []
    cfg["uia"]["session_regex"] = ""
    assert zburn.compact_gate(cfg, st) is None             # 没配筛选：闸门不掺和
    cfg["feed"]["targets"] = ["着色器预览任务"]
    cfg["uia"]["session_db"] = str(tmp_path / "nope.sqlite")
    assert zburn.compact_gate(cfg, st) is None             # 库读不到：按原流程投喂
    assert any("会话库读不到" in ln for ln in st.line)


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


def test_session_contexts_union_covers_regex_matches(tmp_path):
    """钉模型要覆盖「以后可能被喂到的会话」：名单 + 正则取并集。"""
    db = make_db(tmp_path)
    cfg = cfg_with(db)
    cfg["feed"]["targets"] = ["着色器预览任务"]          # 名单只有一条
    cfg["uia"]["session_regex"] = "New chat task"        # 正则还能捞到别的
    only_targets = zsession.session_contexts(cfg, targets=["着色器预览任务"],
                                             session_regex=cfg["uia"]["session_regex"])
    assert [s["id"] for s in only_targets] == ["s_fat"]   # 投喂判据：名单优先
    union = zsession.session_contexts(cfg, targets=["着色器预览任务"],
                                      session_regex=cfg["uia"]["session_regex"], union=True)
    assert {s["id"] for s in union} == {"s_fat", "s_ok"}


def test_pin_sessions_targets_names_only_offpin(tmp_path, monkeypatch):
    """--pin-sessions 只钉跑偏的会话，钉好的不碰（每次切换都会打断用户当前对话）。"""
    db = make_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("insert into session values (?,?,?,?,?)", ("s_auto", "跑偏的会话", 220, None, None))
    con.execute("insert into model_usage values (?,?,?,?)",
                ("s_auto", 931061, 500, "auto/GLM-5.3-Flash"))
    con.commit()
    con.close()
    cfg = cfg_with(db)
    cfg["uia"]["session_regex"] = "着色器预览|New chat|跑偏"
    cfg["feed"]["targets"] = ["着色器预览任务"]
    cfg["model"] = {"want": "zcode2api"}

    called = []

    def fake_pin(cfg, st, target, want, dry_run=False):
        called.append(target)
        return True, "ok"

    monkeypatch.setattr(zburn, "pin_session_model", fake_pin)
    st = _St()
    code, summary = zburn.pin_offpin_sessions(cfg, st, "zcode2api")
    assert code == 0
    assert called == ["跑偏的会话"]                        # 只有它跑在 auto 上
    assert "1/1" in summary
