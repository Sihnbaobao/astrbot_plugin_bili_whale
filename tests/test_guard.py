"""预算与闸门：读限额、写冷却、分享四道坎、跨天归零。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.store import Guard, Store


def make(tmp_path, **kwargs) -> Guard:
    defaults = dict(
        read_daily=3,
        write_daily=2,
        write_cooldown=0,
        share_daily=2,
        share_min_score=7,
        video_cooldown_days=7,
    )
    defaults.update(kwargs)
    return Guard(Store(tmp_path), **defaults)


def test_read_budget_blocks_when_exhausted(tmp_path):
    guard = make(tmp_path)
    assert guard.can_read(1)[0] is True
    guard.note_read(3)
    ok, why = guard.can_read(1)
    assert ok is False and "预算" in why


def test_read_budget_counts_batch(tmp_path):
    guard = make(tmp_path)
    assert guard.can_read(3)[0] is True
    assert guard.can_read(4)[0] is False


def test_write_cooldown(tmp_path):
    guard = make(tmp_path, write_cooldown=60)
    assert guard.write_gate()[0] is True
    guard.note_write()
    ok, why = guard.write_gate()
    assert ok is False and "先等等" in why


def test_write_daily_cap(tmp_path):
    guard = make(tmp_path, write_cooldown=0, write_daily=2)
    guard.note_write()
    guard.note_write()
    ok, why = guard.write_gate()
    assert ok is False and "写预算" in why


def test_share_requires_score(tmp_path):
    guard = make(tmp_path)
    ok, why = guard.write_gate(bvid="BV1", reason="这条讲得很清楚", is_share=True)
    assert ok is False and "score" in why


def test_share_score_threshold(tmp_path):
    guard = make(tmp_path)
    ok, why = guard.write_gate(bvid="BV1", score=6, reason="这条讲得很清楚啊", is_share=True)
    assert ok is False and "低于门槛" in why


def test_share_requires_meaningful_reason(tmp_path):
    guard = make(tmp_path)
    ok, why = guard.write_gate(bvid="BV1", score=15, reason="好看", is_share=True)
    assert ok is False and "reason" in why


def test_share_blocks_duplicate_video(tmp_path):
    guard = make(tmp_path)
    assert guard.write_gate(bvid="BV1", score=15, reason="这条值得你花两分钟", is_share=True)[0]
    guard.note_write(bvid="BV1", is_share=True)
    ok, why = guard.write_gate(bvid="BV1", score=16, reason="这条值得你花两分钟", is_share=True)
    assert ok is False and "分享过" in why


def test_share_daily_cap(tmp_path):
    guard = make(tmp_path, share_daily=1, write_cooldown=0, write_daily=10)
    guard.note_write(bvid="BV1", is_share=True)
    ok, why = guard.write_gate(bvid="BV2", score=18, reason="这条真的值得看", is_share=True)
    assert ok is False and "分享额度" in why


def test_pause_blocks_everything(tmp_path):
    guard = make(tmp_path)
    guard.pause(600, "HTTP 412")
    assert guard.pause_left() > 0
    assert guard.can_read(1)[0] is False
    assert guard.write_gate()[0] is False


def test_counters_reset_on_new_day(tmp_path):
    guard = make(tmp_path, read_daily=3)
    guard.note_read(3)
    state = guard.store.load_state()
    state["day"] = "2020-01-01"
    guard.store.save_state(state)
    assert guard.read_budget() == (0, 3)


def test_summary_mentions_all_three(tmp_path):
    guard = make(tmp_path)
    text = guard.summary()
    assert "读" in text and "写" in text and "分享" in text
