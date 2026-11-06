"""素材层：刷屏剔除、热区对齐、文本拼装。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.debrief import Debrief, DebriefEngine, _fmt


def make_engine(tmp_path) -> DebriefEngine:
    return DebriefEngine(None, tmp_path, "")


def test_fmt_is_mm_ss():
    assert _fmt(0) == "00:00"
    assert _fmt(75) == "01:15"
    assert _fmt(3661) == "61:01"


def test_spam_wave_is_excluded_from_hot_zones(tmp_path):
    engine = make_engine(tmp_path)
    result = Debrief(bvid="BV1")
    danmaku = [{"t": float(i % 30), "text": "前方高能"} for i in range(40)]
    danmaku += [{"t": 60.0 + i, "text": f"真实评论{i}"} for i in range(10)]
    engine._analyze_danmaku(result, danmaku, top=3, bucket=30)

    assert result.danmaku_total == 50
    assert result.hot_zones
    hottest = max(result.hot_zones, key=lambda z: z["count"])
    assert hottest["count"] == 10
    assert hottest["raw"] >= 10
    joined = " ".join(result.top_danmaku)
    assert "前方高能" not in joined


def test_hot_zone_carries_subtitle_line(tmp_path):
    engine = make_engine(tmp_path)
    result = Debrief(
        bvid="BV1",
        subtitle_lines=[
            {"from": 0.0, "to": 10.0, "content": "开场白"},
            {"from": 10.0, "to": 20.0, "content": "这里是重点"},
        ],
    )
    danmaku = [{"t": 12.0 + i * 0.5, "text": f"弹幕{i}"} for i in range(8)]
    engine._analyze_danmaku(result, danmaku, top=1, bucket=30)
    assert result.hot_zones[0]["line"] == "这里是重点"


def test_punctuation_only_spam_counts_as_text(tmp_path):
    engine = make_engine(tmp_path)
    result = Debrief(bvid="BV1")
    danmaku = [{"t": 1.0, "text": "?"} for _ in range(40)]
    danmaku += [{"t": 2.0, "text": f"内容{i}"} for i in range(5)]
    engine._analyze_danmaku(result, danmaku, top=2, bucket=30)
    assert all("?" != text for text in result.top_danmaku)


def test_empty_danmaku_is_harmless(tmp_path):
    engine = make_engine(tmp_path)
    result = Debrief(bvid="BV1")
    engine._analyze_danmaku(result, [], top=3, bucket=30)
    assert result.hot_zones == []


def test_materials_render_every_section():
    result = Debrief(
        bvid="BV1",
        title="一个标题",
        up="某个UP",
        duration=125,
        views=1000,
        likes=10,
        desc="简介内容",
        subtitle_lines=[{"from": 0.0, "to": 3.0, "content": "第一句"}],
        danmaku_total=5,
        hot_zones=[{"start": 0, "end": 30, "count": 3, "hot": True, "line": "第一句", "texts": ["哈哈"]}],
        comments=[{"uname": "观众", "like": 7, "message": "好看"}],
    )
    text = result.materials()
    assert "【元信息】" in text
    assert "【官方字幕】" in text
    assert "【弹幕热区】" in text
    assert "【热评】" in text
    assert "第一句" in text and "好看" in text


def test_long_subtitle_is_sampled_not_truncated_at_tail():
    lines = [{"from": float(i), "to": float(i + 1), "content": f"第{i}句" * 20} for i in range(200)]
    result = Debrief(bvid="BV1", subtitle_lines=lines, subtitle_lang="zh")
    text = result.materials(subtitle_chars=500)
    assert "第0句" in text
    assert "中间省略" in text
    assert "第199句" in text


def test_images_order_and_filtering():
    result = Debrief(bvid="BV1", cover_path="/a.jpg", sheet_path="/b.jpg")
    assert result.images() == ["/a.jpg", "/b.jpg"]
