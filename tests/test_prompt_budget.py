# -*- coding: utf-8 -*-
"""指令预算 / 投喂结果解析 / 额度速率 的单元测试。

这几个都是"昨天白烧 14 亿"直接相关的纯函数：指令太长会丢字、投喂结果要能判定
成功还是空转、额度速率要用来看是不是在没产出的情况下狂烧。
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zburn  # noqa: E402


def test_clamp_prompt_short_passthrough():
    cfg = {"prompt": {"max_chars": 180}}
    assert zburn.clamp_prompt(cfg, "continue") == "continue"


def test_clamp_prompt_truncates_on_word_boundary():
    cfg = {"prompt": {"max_chars": 40}}
    text = "rewrite the effect then run the gate until it prints pass"
    out = zburn.clamp_prompt(cfg, text)
    assert len(out) <= 40
    assert not out.endswith(" ")
    # 不能把单词截成半个
    assert text.startswith(out)
    assert text[len(out)] == " "


def test_clamp_prompt_disabled():
    cfg = {"prompt": {"max_chars": 0}}
    text = "x" * 500
    assert zburn.clamp_prompt(cfg, text) == text


def test_feed_stats_success():
    s = zburn.feed_stats("  [会话A] 已投喂 9 字符")
    assert s["fed"] and not s["failed"] and not s["busy"]
    assert s["typed"] == 9


def test_feed_stats_busy_and_failed():
    busy = zburn.feed_stats("  [会话A] 跳过（busy=True queued=False）")
    assert busy["busy"] and not busy["fed"]
    bad = zburn.feed_stats("  [会话A] 投喂失败 typed=56 send=True")
    assert bad["failed"] and bad["typed"] == 56


def _state_with_history(tmp_path, rows):
    (tmp_path / "quota-history.csv").write_text(
        "time,remaining\n" + "\n".join(f"{t},{v}" for t, v in rows) + "\n", encoding="utf-8")

    class S:
        quota_csv = tmp_path / "quota-history.csv"

    return S()


def test_quota_rate_uses_window(tmp_path):
    now = dt.datetime.now()
    # 越接近现在剩余越少：50 分钟前 4000 万 → 10 分钟前 0，掉 4000 万
    rows = [((now - dt.timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S"),
             40_000_000 - (50 - m) * 1_000_000) for m in (50, 30, 10)]
    rate, drop = zburn.quota_rate(_state_with_history(tmp_path, rows))
    assert drop == 40_000_000
    assert rate == 60_000_000        # 4000 万 / (40/60 小时)


def test_quota_rate_needs_two_points(tmp_path):
    rows = [(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 5)]
    assert zburn.quota_rate(_state_with_history(tmp_path, rows)) == (None, None)
