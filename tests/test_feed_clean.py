# -*- coding: utf-8 -*-
"""投喂判据：只要有一个会话真被喂进去，就算这一轮成功。"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import zburn  # noqa: E402


class _St:
    def __init__(self):
        self.line = []

    def log(self, msg):
        self.line.append(str(msg))


def test_feed_stats_parses_summary_counts():
    s = zburn.feed_stats("汇总：投喂 2 / 跳过 1 / 失败 3")
    assert (s["done"], s["skipped"], s["failed_n"]) == (2, 1, 3)


def test_feed_stats_parses_single_session_line():
    s = zburn.feed_stats("  [某某对话] 已投喂 8 字符")
    assert s["typed"] == 8 and s["fed"] is True


def test_feed_clean_accepts_partial_success(monkeypatch):
    calls = []

    def fake_feed(cfg, st, prompt, dry_run=False):
        calls.append(prompt)
        return 0, "汇总：投喂 1 / 跳过 1 / 失败 2"

    monkeypatch.setattr(zburn, "flush_composer", lambda cfg: None)
    monkeypatch.setattr(zburn, "feed_once", fake_feed)
    st = _St()
    code, summary = zburn.feed_clean({}, st, "continue")
    assert code == 0
    assert len(calls) == 1                      # 不因为另一个会话失败就重投
    assert st.line == []


def test_feed_clean_retries_when_nothing_went_in(monkeypatch):
    calls = []

    def fake_feed(cfg, st, prompt, dry_run=False):
        calls.append(prompt)
        return 0, "汇总：投喂 0 / 跳过 0 / 失败 3"

    monkeypatch.setattr(zburn, "flush_composer", lambda cfg: None)
    monkeypatch.setattr(zburn.time, "sleep", lambda s: None)
    monkeypatch.setattr(zburn, "feed_once", fake_feed)
    st = _St()
    code, _ = zburn.feed_clean({}, st, "continue")
    assert code == 0
    assert len(calls) == 2                      # 一条都没进去 → 腾空重投一次
    assert any("重投" in ln for ln in st.line)
