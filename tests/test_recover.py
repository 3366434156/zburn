# -*- coding: utf-8 -*-
"""无障碍树「找不到输入框」时的自愈路径：先点开对话，别急着重启应用。"""
import pathlib
import sys
import time

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
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(str(msg) + "\n")


def _cfg(tmp_path):
    return zburn.deep_merge(zburn.DEFAULT_CONFIG, {
        "paths": {"state_dir": str(tmp_path)},
        "app": {"process_name": "ZCode", "start_if_down": True},
        "agents": [{"id": "zcode", "role": "worker", "app": {"process_name": "ZCode"}}],
    })


def test_open_session_params_include_openonly_and_targets(monkeypatch):
    seen = {}

    def fake_run_ps(script, params, timeout=180):
        seen.update({"script": script, "params": params})
        return 0, "已打开对话 [x]，输入框就绪", ""

    monkeypatch.setattr(zburn, "run_ps", fake_run_ps)
    cfg = zburn.deep_merge(zburn.DEFAULT_CONFIG, {
        "uia": {"session_regex": "着色器预览"},
        "feed": {"targets": ["我的对话"]},
    })
    code, msg = zburn.open_session(cfg)
    assert code == 0 and "输入框就绪" in msg
    assert seen["script"] == "feed-sessions.ps1"
    assert seen["params"]["OpenOnly"] is True
    assert seen["params"]["MaxSessions"] == 1
    assert "我的对话" in seen["params"]["Targets"]


def test_run_agent_once_opens_session_instead_of_counting_a_strike(tmp_path, monkeypatch):
    st = _St(tmp_path)
    cfg = _cfg(tmp_path)
    calls = {"uia": 0, "open": 0}

    def fake_uia(acfg):
        calls["uia"] += 1
        if calls["uia"] == 1:
            return False, "找不到输入框（无障碍树可能被禁用，重启应用可恢复）"
        return True, "控件 300 个，含输入框与按钮"

    def fake_open(acfg):
        calls["open"] += 1
        return 0, "已打开对话 [x]，输入框就绪"

    monkeypatch.setattr(zburn, "app_running", lambda acfg: True)
    monkeypatch.setattr(zburn, "uia_ok", fake_uia)
    monkeypatch.setattr(zburn, "open_session", fake_open)
    monkeypatch.setattr(zburn, "feed_clean", lambda *a, **kw: (0, "汇总：投喂 1 / 跳过 0 / 失败 0"))

    code, summary = zburn.run_agent_once(cfg, st, agents.agent_specs(cfg)[0], 1, None)
    assert code == 0
    assert calls["open"] == 1 and calls["uia"] == 2
    assert not agents.read_state(st, "zcode").get("uia_fail_streak")
    assert any("点开一个对话" in ln for ln in st.lines)


def test_uia_failure_restart_respects_cooldown(tmp_path, monkeypatch):
    """刚重启过还是读不到：不许无限重启（否则一晚上重启十几次，全在打断任务）。"""
    st = _St(tmp_path)
    cfg = _cfg(tmp_path)
    agents.update_state(st, "zcode", uia_fail_streak=2,
                        last_app_restart_ts=time.time())
    killed = {"n": 0}
    monkeypatch.setattr(zburn, "app_running", lambda acfg: True)
    monkeypatch.setattr(zburn, "uia_ok", lambda acfg: (False, "找不到输入框"))
    monkeypatch.setattr(zburn, "open_session", lambda acfg: (2, "点开了但没有输入框"))
    monkeypatch.setattr(zburn, "subprocess_hidden", lambda *a, **kw: killed.__setitem__("n", killed["n"] + 1))
    monkeypatch.setattr(zburn, "start_app", lambda acfg: killed.__setitem__("n", killed["n"] + 1))

    code, summary = zburn.run_agent_once(cfg, st, agents.agent_specs(cfg)[0], 1, None)
    assert code == 1 and summary.startswith("uia:")
    assert killed["n"] == 0                     # 没有杀进程、没有重启
    assert any("先不重启" in ln for ln in st.lines)
