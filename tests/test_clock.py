"""生物钟的行为：随机、跳过权、最小间隔、跨午夜。"""

from __future__ import annotations

import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.clock import Clock, parse_windows


def make_clock(**kwargs) -> Clock:
    defaults = dict(skip_percent=0, min_gap_min=10, rng=random.Random(7))
    defaults.update(kwargs)
    return Clock(parse_windows("20:00-24:00"), **defaults)


def test_parse_windows_plain():
    windows = parse_windows("20:00-24:00")
    assert len(windows) == 1
    assert (windows[0].start, windows[0].end) == (1200, 1440)


def test_parse_windows_wraps_midnight():
    window = parse_windows("22:00-02:00")[0]
    assert window.start == 1320
    assert window.end == 1560


def test_parse_windows_multiple_and_garbage():
    windows = parse_windows("09:00-11:00, 坏数据 ,20:00-24:00")
    assert [w.label for w in windows] == ["09:00-11:00", "20:00-24:00"]


def test_parse_windows_rejects_empty():
    try:
        parse_windows("没有窗口")
    except ValueError:
        return
    raise AssertionError("应当抛 ValueError")


def test_plan_lands_inside_window():
    clock = make_clock()
    state = {}
    now = datetime(2026, 11, 6, 19, 0)
    plan = clock.plan(state, now)
    assert plan["action"] == "wake"
    at = datetime.fromtimestamp(plan["at"])
    assert datetime(2026, 11, 6, 20, 0) <= at < datetime(2026, 11, 7, 0, 0)


def test_plan_is_stable_until_fired():
    clock = make_clock()
    state = {}
    now = datetime(2026, 11, 6, 19, 0)
    first = clock.plan(state, now)
    second = clock.plan(state, now)
    assert first["at"] == second["at"]


def test_skip_percent_is_respected():
    always = make_clock(skip_percent=100)
    never = make_clock(skip_percent=0)
    now = datetime(2026, 11, 6, 19, 0)
    assert always.plan({}, now)["action"] == "skip"
    assert never.plan({}, now)["action"] == "wake"


def test_no_more_wakes_after_capacity():
    clock = make_clock()
    state = {}
    now = datetime(2026, 11, 6, 19, 0)
    plan = clock.plan(state, now)
    Clock.consume(state, plan, datetime(2026, 11, 6, 20, 30))
    again = clock.plan(state, datetime(2026, 11, 6, 21, 0))
    assert again["action"] == "done"


def test_due_only_after_time():
    clock = make_clock()
    state = {}
    now = datetime(2026, 11, 6, 19, 0)
    plan = clock.plan(state, now)
    early = datetime.fromtimestamp(plan["at"]) - __import__("datetime").timedelta(seconds=1)
    assert clock.due(plan, early) is False
    assert clock.due(plan, datetime.fromtimestamp(plan["at"])) is True


def test_min_gap_pushes_second_wake_away():
    # 这个窗口允许醒两次，否则第一次用过容量后直接就是 done，测不到间隔
    clock = Clock(
        parse_windows("20:00-24:00", 2),
        skip_percent=0,
        min_gap_min=30,
        rng=random.Random(7),
    )
    state = {}
    start = datetime(2026, 11, 6, 19, 0)
    first = clock.plan(state, start)
    Clock.consume(state, first, datetime(2026, 11, 6, 20, 10))
    second = clock.plan(state, datetime(2026, 11, 6, 20, 11))
    assert second["action"] == "wake"
    gap = (datetime.fromtimestamp(second["at"]) - datetime(2026, 11, 6, 20, 10)).total_seconds()
    assert gap >= 30 * 60


def test_midnight_window_still_has_tonight():
    clock = Clock(parse_windows("22:00-02:00"), skip_percent=0, min_gap_min=10, rng=random.Random(1))
    state = {}
    now = datetime(2026, 11, 6, 23, 30)
    plan = clock.plan(state, now)
    assert plan["action"] == "wake"
    end = datetime(2026, 11, 7, 2, 0)
    assert datetime.fromtimestamp(plan["at"]) < end


def test_old_counters_are_cleaned():
    clock = make_clock()
    state = {"used": {"2020-01-01|20:00-24:00": 3}}
    clock.plan(state, datetime(2026, 11, 6, 19, 0))
    assert "2020-01-01|20:00-24:00" not in state["used"]
