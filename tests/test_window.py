# -*- coding: utf-8 -*-
"""允许时段（23:00-09:00）与额度不足时的行为。"""
import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import zburn  # noqa: E402

NIGHT = {"window": {"enabled": True, "start": "23:00", "end": "09:00"}}


def at(day: int, hour: int, minute: int = 30) -> dt.datetime:
    return dt.datetime(2026, 10, day, hour, minute)


def test_disabled_window_always_in():
    assert zburn.in_window({"window": {"enabled": False}}) is True
    assert zburn.in_window({}) is True
    assert zburn.seconds_until_window({}) == 0


def test_night_window_spans_midnight():
    assert zburn.in_window(NIGHT, at(28, 22)) is False      # 22:30 还没开始
    assert zburn.in_window(NIGHT, at(28, 23)) is True        # 23:30 开始
    assert zburn.in_window(NIGHT, at(29, 0)) is True         # 00:30 跨过午夜
    assert zburn.in_window(NIGHT, at(29, 8)) is True         # 08:30
    assert zburn.in_window(NIGHT, at(29, 9)) is False        # 09:00 结束（不含）
    assert zburn.in_window(NIGHT, at(29, 12)) is False


def test_seconds_until_window_points_at_next_start():
    assert zburn.seconds_until_window(NIGHT, at(28, 22, 0)) == 3600        # 22:00 → 23:00
    assert zburn.seconds_until_window(NIGHT, at(28, 23, 30)) == 0          # 已在时段
    assert zburn.seconds_until_window(NIGHT, at(29, 9, 30)) == 13 * 3600 + 1800   # 09:30 → 次日 23:00


def test_daytime_window_still_works():
    day = {"window": {"enabled": True, "start": "09:00", "end": "18:00"}}
    assert zburn.in_window(day, at(28, 12)) is True
    assert zburn.in_window(day, at(28, 8)) is False
    assert zburn.in_window(day, at(28, 19)) is False


def test_window_spec_tolerates_junk():
    assert zburn.window_spec({"window": {"enabled": True, "start": "oops"}}) == (23 * 60, 9 * 60)
    assert zburn.window_spec({"window": {"enabled": True, "start": "25:99"}}) == (23 * 60, 9 * 60)
    assert zburn.window_spec({"window": {"enabled": True, "start": "7:5", "end": "8:5"}}) == (7 * 60 + 5, 8 * 60 + 5)
    assert zburn.window_spec({"window": {"enabled": True, "start": "0:00", "end": "0:00"}}) is not None


def test_window_desc():
    assert zburn.window_desc(NIGHT) == "23:00-09:00"
    assert zburn.window_desc({}) == "不限时段"


def test_stop_action_defaults_to_exit():
    assert zburn.DEFAULT_CONFIG["quota"]["stop_action"] == "exit"
    assert zburn.DEFAULT_CONFIG["window"]["enabled"] is False
