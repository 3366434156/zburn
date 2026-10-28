# -*- coding: utf-8 -*-
"""配置发现、模型守卫、交接时间戳、监控名单这几块的回归测试。

这几处都是「静默失效」型问题：不报错，但工具就是在空转、或者把额度烧到别处。
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import agents  # noqa: E402
import zburn    # noqa: E402


# ---------------------------------------------------------------- 配置发现
def test_resolve_config_finds_cwd_config(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text('{"prompt": {"max_chars": 77}}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZBURN_CONFIG", raising=False)
    p = zburn.resolve_config_path(None)
    assert p is not None and p.name == "config.json"


def test_load_config_reports_source(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text('{"prompt": {"max_chars": 77}}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZBURN_CONFIG", raising=False)
    cfg = zburn.load_config(None)
    assert cfg["prompt"]["max_chars"] == 77          # 真的读到了文件，而不是内置默认值
    assert zburn.config_source().endswith("config.json")


def test_explicit_missing_config_exits(tmp_path):
    with pytest.raises(SystemExit):
        zburn.load_config(str(tmp_path / "nope.json"))


def test_missing_config_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZBURN_CONFIG", raising=False)
    monkeypatch.setattr(zburn, "config_candidates", lambda: [tmp_path / "nope.json"])
    cfg = zburn.load_config(None)
    assert zburn.config_source() == zburn.CONFIG_FALLBACK
    assert cfg["feed"]["interval_seconds"] > 0        # 内置默认值至少是能跑的


# ---------------------------------------------------------------- 模型钉死
def test_normalize_agent_keeps_model_block():
    """agents[].model.want 必须能走到 agent_view：以前这里被丢掉，配了等于没配。"""
    cfg = {"agents": [{"id": "zcode", "process_name": "ZCode",
                       "model": {"want": "zcode2api"}}]}
    spec = agents.agent_specs(cfg)[0]
    assert spec["model"]["want"] == "zcode2api"
    assert spec["model"]["enforce"] is True           # 默认：切不回去就别投喂


def _fake_state(tmp_path):
    (tmp_path / "quota-history.csv").write_text("time,remaining\n", encoding="utf-8")

    class S:
        dir = tmp_path
        status_path = tmp_path / "status.json"
        log_path = tmp_path / "zburn.log"

        def log(self, msg):
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(str(msg) + "\n")

    return S()


def test_model_guard_ok_when_pinned(tmp_path, monkeypatch):
    st = _fake_state(tmp_path)
    monkeypatch.setattr(zburn, "probe_model", lambda cfg: "magpie/zcode2api/GLM-5.3-Flash 最高")
    ok, msg = zburn.model_guard({"model": {"want": "zcode2api"}}, st, "zcode")
    assert ok is True and "zcode2api" in msg
    assert agents.read_state(st, "zcode")["model_ok"] is True


def test_model_guard_locked_does_not_try_to_click(tmp_path, monkeypatch):
    st = _fake_state(tmp_path)
    monkeypatch.setattr(zburn, "probe_model", lambda cfg: "magpie/auto/GLM-5.3-Flash 最高")
    monkeypatch.setattr(zburn, "desktop_locked", lambda: True)
    called = {"n": 0}

    def boom(*a, **kw):
        called["n"] += 1
        return 0, "", ""

    monkeypatch.setattr(zburn, "run_ps", boom)
    ok, _ = zburn.model_guard({"model": {"want": "zcode2api"}, "app": {}}, st, "zcode")
    assert ok is False
    assert called["n"] == 0                  # 锁屏时点不动下拉，别白试
    assert agents.read_state(st, "zcode")["model_ok"] is False


def test_model_guard_repins_when_unlocked(tmp_path, monkeypatch):
    st = _fake_state(tmp_path)
    seen = {"n": 0}

    def probe(cfg):
        seen["n"] += 1
        return "magpie/auto/GLM" if seen["n"] == 1 else "magpie/zcode2api/GLM"

    monkeypatch.setattr(zburn, "probe_model", probe)
    monkeypatch.setattr(zburn, "desktop_locked", lambda: False)
    monkeypatch.setattr(zburn, "run_ps", lambda *a, **kw: (0, "ok", ""))
    ok, msg = zburn.model_guard({"model": {"want": "zcode2api"}, "app": {"process_name": "ZCode"}},
                                st, "zcode")
    assert ok is True and "钉回" in msg


def test_worker_skips_feed_when_model_wrong(tmp_path, monkeypatch):
    """模型没钉住时宁可这一轮空着，也不要把额度烧在别的模型上。"""
    st = _fake_state(tmp_path)
    cfg = {
        "paths": {"state_dir": str(tmp_path)},
        "app": {"process_name": "ZCode"}, "uia": {}, "feed": {}, "prompt": {},
        "agents": [{"id": "zcode", "role": "worker", "model": {"want": "zcode2api"}}],
    }
    monkeypatch.setattr(zburn, "app_running", lambda cfg: True)
    monkeypatch.setattr(zburn, "uia_ok", lambda cfg: (True, "ok"))
    monkeypatch.setattr(zburn, "model_guard", lambda *a, **kw: (False, "未钉住（magpie/auto/GLM）"))
    code, summary = zburn.run_agent_once(cfg, st, agents.agent_specs(cfg)[0], 1, None)
    assert code == 0 and summary.startswith("skip:model")


# ---------------------------------------------------------------- 交接时间戳
def test_handoff_created_is_not_fabricated():
    rec = agents._normalize_handoff({"action": "feed", "prompt": "continue"})
    assert rec["created"] == ""              # 没写就是没写，不能读一次盖一个「现在」
    rec2 = agents._normalize_handoff({"action": "feed", "prompt": "go",
                                      "created": "2026-10-28 02:00:00"})
    assert rec2["created"] == "2026-10-28 02:00:00"


def test_handoff_rejects_non_ascii():
    assert agents._normalize_handoff({"action": "feed", "prompt": "继续"}) is None


# ---------------------------------------------------------------- 监控名单
def test_set_dynamic_targets_replaces_and_clears(tmp_path):
    st = _fake_state(tmp_path)
    assert agents.set_dynamic_targets(st, "zcode", ["a", "b"]) == ["a", "b"]
    assert agents.load_dynamic_targets(st, "zcode") == ["a", "b"]
    assert agents.set_dynamic_targets(st, "zcode", []) == []
    assert agents.load_dynamic_targets(st, "zcode") == []


def test_worker_view_merges_dynamic_targets_only_for_workers(tmp_path):
    st = _fake_state(tmp_path)
    cfg = {"agents": [
        {"id": "zcode", "role": "worker", "app": {"process_name": "ZCode"}},
        {"id": "dsh", "role": "supervisor", "app": {"process_name": "DeepSeek Harness"}},
    ]}
    agents.set_dynamic_targets(st, "zcode", ["老板的对话"])
    workers = {s["id"]: s for s in agents.agent_specs(cfg)}
    assert agents.agent_view(cfg, workers["zcode"], st)["feed"]["targets"] == ["老板的对话"]
    # 监督者绝不能继承 worker 的名单（会把监督提示词灌进工作对话）
    assert agents.agent_view(cfg, workers["dsh"], st)["feed"]["targets"] == []
