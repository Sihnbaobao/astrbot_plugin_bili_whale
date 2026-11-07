"""落盘：日记、观看历史、损坏状态不炸。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
from datetime import datetime, timedelta

from core.store import Store


def test_diary_roundtrip(tmp_path):
    store = Store(tmp_path)
    store.append_diary("yours", "今天看到一条猫猫")
    assert "猫猫" in store.read_diary("yours")


def test_diary_appends_not_overwrites(tmp_path):
    store = Store(tmp_path)
    store.append_diary("mine", "第一段")
    store.append_diary("mine", "第二段")
    body = store.read_diary("mine")
    assert "第一段" in body and "第二段" in body


def test_diary_missing_returns_empty(tmp_path):
    assert Store(tmp_path).read_diary("mine") == ""


def test_watched_history_and_seen(tmp_path):
    store = Store(tmp_path)
    store.mark_watched({"bvid": "BV1", "title": "甲"})
    store.mark_watched({"bvid": "BV2", "title": "乙", "score": 15})
    items = store.watched_recent(1)
    assert [item["bvid"] for item in items] == ["BV2", "BV1"]
    assert store.seen("BV1") is True
    assert store.seen("BV9") is False


def test_watched_skips_broken_lines(tmp_path):
    from datetime import datetime

    store = Store(tmp_path)
    now = datetime.now().isoformat(timespec="seconds")
    store.watched_path.write_text(
        f'{{"at": "{now}", "bvid": "BV1"}}\n这不是JSON\n', encoding="utf-8"
    )
    assert [item["bvid"] for item in store.watched_recent(1)] == ["BV1"]


def test_prune_shots_drops_old_files(tmp_path):
    import os

    store = Store(tmp_path)
    old = store.shots_dir / "old.jpg"
    old.write_bytes(b"x" * 10)
    past = datetime.now().timestamp() - 40 * 86400
    os.utime(old, (past, past))
    fresh = store.shots_dir / "fresh.jpg"
    fresh.write_bytes(b"y" * 10)

    assert store.prune_shots(30, 200) == 1
    assert not old.exists()
    assert fresh.exists()


def test_prune_shots_enforces_size_limit(tmp_path):
    store = Store(tmp_path)
    for index in range(5):
        (store.shots_dir / f"f{index}.jpg").write_bytes(b"x" * (1024 * 1024))

    removed = store.prune_shots(365, 2)
    assert removed >= 3
    remaining = sum(p.stat().st_size for p in store.shots_dir.iterdir())
    assert remaining <= 2 * 1024 * 1024


def test_delete_watched_removes_only_target(tmp_path):
    store = Store(tmp_path)
    store.mark_watched({"bvid": "BV1", "title": "a"})
    store.mark_watched({"bvid": "BV1", "title": "a", "note": "改过"})
    store.mark_watched({"bvid": "BV2", "title": "b"})

    assert store.delete_watched("BV1") == 2
    assert [item["bvid"] for item in store.watched_recent(7)] == ["BV2"]
    assert store.delete_watched("BV404") == 0


def test_delete_diary_round_keeps_others(tmp_path):
    from core.store import today_str

    store = Store(tmp_path)
    path = store.diary_dir / f"{today_str()}-yours.md"
    path.write_text("\n## 09:10\n\n第一轮\n\n## 18:52\n\n第二轮\n", encoding="utf-8")

    assert store.delete_diary_round("yours", "09:10") is True
    text = path.read_text(encoding="utf-8")
    assert "第一轮" not in text
    assert "第二轮" in text
    assert store.delete_diary_round("yours", "23:59") is False


def test_memory_block_is_empty_without_history(tmp_path):
    assert Store(tmp_path).memory_block() == ""


def test_memory_block_reads_like_memory(tmp_path):
    """注入的内容要像她自己的回忆，不能像系统日志。"""
    store = Store(tmp_path)
    store.mark_watched(
        {
            "bvid": "BV1",
            "title": "【某频道】百合短篇",
            "score": 8,
            "shared": True,
            "note": "错送的巧克力。璃月记住的是那一幕",
        }
    )
    block = store.memory_block(days=1)
    assert "百合短篇" in block
    assert "某频道" not in block  # 频道前缀去掉
    assert "璃月记住的是那一幕" in block  # 取她自己的结论那句
    assert "已发给对方" in block
    assert "/10" not in block  # 不报表内部分数


def test_memory_block_leads_with_just_shared(tmp_path):
    """刚分享过的那条要顶到最前面，否则用户说"这个"她没有指代对象。"""
    from datetime import datetime

    store = Store(tmp_path)
    store.mark_watched(
        {
            "bvid": "BV1",
            "title": "【花谱】日文翻唱《ダイダイダイダイダイキライ》",
            "shared": True,
            "score": 8,
            "message": "这首...一口气喊完，璃月循环三遍了",
            "shared_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    block = store.memory_block(days=1, self_name="璃月", other_name="暴暴")
    # 用人格名而不是"你/他"：这段贴在用户侧，代词会被答成"你发的"
    assert "璃月刚给暴暴发过" in block
    assert "你" not in block
    assert "暴暴说" in block
    assert "循环三遍" in block
    assert block.index("她刚给对方发过") < block.index("今天还刷过") if "今天还刷过" in block else True


def test_memory_block_respects_limit(tmp_path):
    store = Store(tmp_path)
    for index in range(8):
        store.mark_watched({"bvid": f"BV{index}", "title": f"第{index}条"})
    block = store.memory_block(days=1, limit=3)
    assert block.count("、") == 2  # 三条，用顿号串成一句
    assert "第3条" not in block
    assert block.startswith("（") and block.endswith("。）")  # 一句括号里的记忆，不是清单

def test_pending_report_roundtrip(tmp_path):
    store = Store(tmp_path)
    store.save_pending_report("今天看了三条", [{"bvid": "BV1"}])
    report = store.take_pending_report()
    assert report["text"] == "今天看了三条"
    assert report["shared"] == [{"bvid": "BV1"}]
    store.mark_pending_report_pushed()
    assert store.take_pending_report() is None


def test_pending_report_expires(tmp_path):
    import time as _time

    store = Store(tmp_path)
    store.save_pending_report("旧汇报", [])
    payload = json.loads(store.pending_path.read_text(encoding="utf-8"))
    payload["at"] = _time.time() - 99999
    store.pending_path.write_text(json.dumps(payload), encoding="utf-8")
    assert store.take_pending_report(max_age=7200) is None


def test_pending_report_survives_state_write(tmp_path):
    """时钟循环曾拿唤醒前的 state 快照写回，把刚写的汇报冲掉过。"""
    store = Store(tmp_path)
    store.save_pending_report("刚刚写的汇报", [])
    store.save_state(store.load_state())  # 模拟一次无关的状态写回
    assert store.take_pending_report()["text"] == "刚刚写的汇报"


def test_recall_matches_tags_and_title(tmp_path):
    store = Store(tmp_path)
    store.mark_watched(
        {
            "bvid": "BV1",
            "title": "猫猫合集",
            "desc": "三十只猫",
            "tags": ["猫", "萌宠"],
            "tname": "动物圈",
        }
    )
    store.mark_watched(
        {"bvid": "BV2", "title": "显卡评测", "desc": "", "tags": ["硬件"], "tname": "科技"}
    )
    hits = store.recall("猫")
    assert [item["bvid"] for item in hits] == ["BV1"]
    assert store.recall("硬件")[0]["bvid"] == "BV2"
    assert store.recall("不存在的词") == []
    assert store.recall("") == []


def test_recall_ranks_more_hits_first(tmp_path):
    store = Store(tmp_path)
    store.mark_watched({"bvid": "BV1", "title": "猫和狗", "tags": ["猫", "狗"]})
    store.mark_watched({"bvid": "BV2", "title": "只有猫", "tags": ["猫"]})
    hits = store.recall("猫 狗")
    assert hits[0]["bvid"] == "BV1"


def test_remember_adds_note_and_merges_tags(tmp_path):
    store = Store(tmp_path)
    store.mark_watched({"bvid": "BV1", "title": "猫", "tags": ["猫"]})
    assert store.remember("BV1", "讲的是猫科动物怎么睡觉", ["科普"]) is True

    items = store.watched_recent(1)
    assert len(items) == 1  # 合并后仍然只有一条
    assert items[0]["note"] == "讲的是猫科动物怎么睡觉"
    assert "猫" in items[0]["tags"] and "科普" in items[0]["tags"]
    assert store.recall("睡觉")[0]["bvid"] == "BV1"


def test_remember_unknown_bvid_returns_false(tmp_path):
    assert Store(tmp_path).remember("BV404", "没看过") is False


def test_stats_counts_tags_scores_and_shares(tmp_path):
    store = Store(tmp_path)
    store.mark_watched(
        {
            "bvid": "BV1",
            "title": "a",
            "tags": ["猫", "萌宠"],
            "tname": "动物圈",
            "score": 15,
            "shared": True,
        }
    )
    store.mark_watched(
        {"bvid": "BV2", "title": "b", "tags": ["猫"], "tname": "动物圈", "score": 11}
    )
    stats = store.stats(30)
    assert stats["count"] == 2
    assert stats["shared"] == 1
    assert stats["avg_score"] == 13.0
    assert stats["tags"][0] == ("猫", 2)
    assert stats["tnames"][0] == ("动物圈", 2)


def test_stats_is_empty_safe(tmp_path):
    stats = Store(tmp_path).stats(30)
    assert stats["count"] == 0
    assert stats["tags"] == []


def test_prune_drops_old_records(tmp_path):
    store = Store(tmp_path)
    old = (datetime.now() - timedelta(days=400)).isoformat(timespec="seconds")
    fresh = datetime.now().isoformat(timespec="seconds")
    store.watched_path.write_text(
        json.dumps({"at": old, "bvid": "OLD"})
        + "\n"
        + json.dumps({"at": fresh, "bvid": "NEW"})
        + "\n",
        encoding="utf-8",
    )
    assert store.prune(180) == 1
    assert [item["bvid"] for item in store.watched_recent(400)] == ["NEW"]


def test_corrupt_state_returns_empty(tmp_path):
    store = Store(tmp_path)
    store.state_path.write_text("{半截", encoding="utf-8")
    assert store.load_state() == {}


def test_directories_are_created(tmp_path):
    store = Store(tmp_path / "nested")
    assert store.diary_dir.is_dir()
    assert store.shots_dir.is_dir()
