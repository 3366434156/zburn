# -*- coding: utf-8 -*-
"""任务队列派活（治跑题）+ 只认一个模型（切不动就停）。"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import agents  # noqa: E402
import zburn    # noqa: E402


class _St:
    def __init__(self, tmp_path):
        self.dir = tmp_path
        self.status_path = tmp_path / "status.json"
        self.log_path = tmp_path / "zburn.log"
        self.lines = []

    def log(self, msg):
        self.lines.append(str(msg))


def _cfg(tmp_path, **over):
    cfg = zburn.deep_merge(zburn.DEFAULT_CONFIG, {
        "paths": {"state_dir": str(tmp_path)},
        "queue": {"enabled": True, "path": str(tmp_path / "q.json")},
    })
    cfg.update(over)
    return cfg


def _write_queue(tmp_path, tasks):
    (tmp_path / "q.json").write_text(json.dumps({"tasks": tasks}, ensure_ascii=False),
                                     encoding="utf-8")


# ---------------------------------------------------------------- 队列
def test_queue_next_skips_dispatched(tmp_path, monkeypatch):
    st = _St(tmp_path)
    _write_queue(tmp_path, [{"id": "Fx1_A", "prompt": "do A"},
                            {"id": "Fx2_B", "prompt": "do B"}])
    monkeypatch.setattr(agents, "bridge_dir", lambda cfg: tmp_path / "bridge")
    cfg = _cfg(tmp_path)
    assert zburn.queue_next(cfg, st)["id"] == "Fx1_A"
    zburn.queue_mark(cfg, st, "Fx1_A")
    assert zburn.queue_next(cfg, st)["id"] == "Fx2_B"
    zburn.queue_mark(cfg, st, "Fx2_B")
    assert zburn.queue_next(cfg, st) is None      # 派完了 → 回退模板


def test_queue_disabled_means_template_prompt(tmp_path):
    st = _St(tmp_path)
    _write_queue(tmp_path, [{"id": "Fx1_A", "prompt": "do A"}])
    cfg = _cfg(tmp_path)
    cfg["queue"]["enabled"] = False
    prompt, task = zburn.next_prompt(cfg, st, 0, None)
    assert task is None                            # 队列关掉就用模板
    assert prompt.isascii()


def test_queue_prompt_points_at_brief_file(tmp_path, monkeypatch):
    st = _St(tmp_path)
    _write_queue(tmp_path, [{"id": "Fx9_C", "group": "thunder",
                             "prompt": "TASK: rewrite Fx9_C so 雷云+折线雷 appear",
                             "verify": "python fxgate.py Fx9_C --render"}])
    bridge = tmp_path / "bridge"
    monkeypatch.setattr(agents, "bridge_dir", lambda cfg: bridge)
    cfg = _cfg(tmp_path, prompt={"max_chars": 180})
    prompt, task = zburn.next_prompt(cfg, st, 0, None)
    assert task["id"] == "Fx9_C"
    assert prompt.isascii() and len(prompt) <= 180       # WM_CHAR 只吃 ASCII，且不能太长
    brief = bridge / "tasks" / "Fx9_C.txt"
    assert brief.exists()
    body = brief.read_text(encoding="utf-8")
    assert "雷云" in body and "fxgate" in body           # 题材和验收都写进任务书了
    assert str(brief) in prompt                          # 指令只指路


def test_queue_respects_only_group(tmp_path, monkeypatch):
    st = _St(tmp_path)
    _write_queue(tmp_path, [{"id": "Fx1_A", "group": "landscape", "prompt": "a"},
                            {"id": "Fx2_B", "group": "thunder", "prompt": "b"}])
    monkeypatch.setattr(agents, "bridge_dir", lambda cfg: tmp_path / "bridge")
    cfg = _cfg(tmp_path)
    cfg["queue"]["only_group"] = "thunder"
    assert zburn.queue_next(cfg, st)["id"] == "Fx2_B"


def test_feed_next_marks_task_after_success(tmp_path, monkeypatch):
    st = _St(tmp_path)
    _write_queue(tmp_path, [{"id": "Fx1_A", "prompt": "do A"}])
    monkeypatch.setattr(agents, "bridge_dir", lambda cfg: tmp_path / "bridge")
    monkeypatch.setattr(zburn, "feed_clean", lambda *a, **kw: (0, "汇总：投喂 1 / 跳过 0 / 失败 0"))
    cfg = _cfg(tmp_path)
    code, summary, task = zburn.feed_next(cfg, st, 1, None)
    assert code == 0 and task["id"] == "Fx1_A"
    assert "Fx1_A" in zburn.queue_load_done(st)


def test_feed_next_does_not_mark_on_failure(tmp_path, monkeypatch):
    st = _St(tmp_path)
    _write_queue(tmp_path, [{"id": "Fx1_A", "prompt": "do A"}])
    monkeypatch.setattr(agents, "bridge_dir", lambda cfg: tmp_path / "bridge")
    monkeypatch.setattr(zburn, "feed_clean", lambda *a, **kw: (0, "汇总：投喂 0 / 跳过 0 / 失败 2"))
    cfg = _cfg(tmp_path)
    zburn.feed_next(cfg, st, 1, None)
    assert zburn.queue_load_done(st) == {}          # 没喂进去就不许记账，下一轮再试


# ---------------------------------------------------------------- 只认一个模型
def test_default_on_mismatch_is_stop():
    assert agents.DEFAULT_MODEL["on_mismatch"] == "stop"


def test_supervise_stops_on_model_violation(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    st = _St(tmp_path)

    def boom(*a, **kw):
        raise zburn.ModelViolation("不是 zcode2api")

    monkeypatch.setattr(zburn, "run_agent_once", boom)
    monkeypatch.setattr(zburn, "build_supervisor_prompt", lambda *a, **kw: "wake")
    monkeypatch.setattr(zburn, "write_supervisor_bridge",
                        lambda *a, **kw: {"dir": str(tmp_path), "handoff": str(tmp_path)})
    monkeypatch.setattr(agents, "orchestrator",
                        lambda cfg: ({"id": "dsh", "name": "dsh", "role": "supervisor",
                                      "enabled": True, "app": {}, "uia": {}, "feed": {},
                                      "prompt": {}, "model": {}},
                                     {"id": "zcode", "name": "ZCode", "role": "worker",
                                      "app": {}, "uia": {}, "feed": {}, "prompt": {},
                                      "model": {}}))
    code, msg = zburn.supervise_now(cfg, st)
    assert code == 3 and "已停止" in msg
