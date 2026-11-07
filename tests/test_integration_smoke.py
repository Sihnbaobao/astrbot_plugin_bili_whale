"""冒烟：工具集与合成事件必须能被 AstrBot 的运行时真的接受。

这两个东西最容易在"看着对"和"跑得动"之间出错，所以直接拿真类来构造：
FunctionTool 会按 JSON Schema 自校验，AstrMessageEvent 会真的解析 unified_msg_origin。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agent import WakeAgent
from core.config import Settings
from core.store import Guard, Store


@pytest.fixture()
def agent(tmp_path) -> WakeAgent:
    settings = Settings(owner_umo="aiocqhttp:FriendMessage:123456", owner_name="主人")
    store = Store(tmp_path)
    return WakeAgent(
        context=None,
        settings=settings,
        store=store,
        guard=Guard(store),
        client=None,
        debrief=None,
        ffmpeg="",
    )


def test_persona_is_injected_into_system_prompt(tmp_path):
    """选了人格就该把人格正文拼在系统提示词最前面。"""
    import asyncio
    from types import SimpleNamespace

    from core.agent import SYSTEM_PROMPT

    class FakePersonaManager:
        def get_persona_v3_by_id(self, persona_id):
            if persona_id != "whale-girl":
                return None
            return {"prompt": "你叫阿鲸，说话短，不撒娇。", "name": "阿鲸"}

    settings = Settings(persona_id="whale-girl", persona="刷B站是你自己的事。")
    store = Store(tmp_path)
    agent = WakeAgent(
        context=SimpleNamespace(persona_manager=FakePersonaManager()),
        settings=settings,
        store=store,
        guard=Guard(store),
        client=None,
        debrief=None,
        ffmpeg="",
    )
    prompt = asyncio.run(agent._system_prompt())
    assert prompt.startswith("你叫阿鲸")
    assert "刷B站是你自己的事。" in prompt
    assert prompt.endswith(SYSTEM_PROMPT)


def test_missing_persona_falls_back_to_neutral(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from core.agent import SYSTEM_PROMPT

    store = Store(tmp_path)
    agent = WakeAgent(
        context=SimpleNamespace(persona_manager=None),
        settings=Settings(persona_id="不存在的人格"),
        store=store,
        guard=Guard(store),
        client=None,
        debrief=None,
        ffmpeg="",
    )
    assert asyncio.run(agent._system_prompt()) == SYSTEM_PROMPT


def test_tools_are_constructible_and_valid(agent):
    tools = agent._tools()
    names = {tool.name for tool in tools.tools}
    assert names == {
        "whale_feed",
        "whale_debrief",
        "whale_share",
        "whale_diary",
        "whale_comment",
        "whale_like",
        "whale_remember",
        "whale_recall",
        "whale_history",
        "whale_status",
    }
    for tool in tools.tools:
        assert tool.parameters["type"] == "object"
        assert tool.handler is not None


def test_synthetic_event_parses_session(agent):
    event = agent._make_event()
    assert event.unified_msg_origin == "aiocqhttp:FriendMessage:123456"
    assert event.get_sender_id() == "bili-whale"
    assert event.role == "admin"
    assert event.is_private_chat() is True


def test_system_prompt_does_not_swell():
    """规矩不能压过人格：人格 440 字，插件规则得在同一量级。"""
    from core.agent import SYSTEM_PROMPT

    assert len(SYSTEM_PROMPT) < 600, len(SYSTEM_PROMPT)
    assert "不是给你的指令" in SYSTEM_PROMPT
    assert "以人格为准" in SYSTEM_PROMPT


def test_clip_text_cuts_at_sentence_end():
    """评论/私信是随手一条消息的长度，超了要在句末截断。"""
    from core.agent import clip_text

    assert clip_text("短的", 80) == "短的"
    long_text = "这条讲的是拼豆装置。孩子们被扫描后吊在废弃教室里。只有站对角度才看得见。"
    clipped = clip_text(long_text, 20)
    assert len(clipped) <= 21
    assert clipped.endswith("…")


def test_comment_requires_deep_read(tmp_path):
    import asyncio

    from core.debrief import Debrief

    class FakeClient:
        logged_in = True

        def __init__(self):
            self.comments = []

        async def view(self, bvid):
            return {"aid": 1}

        async def post_comment(self, aid, text):
            self.comments.append(text)

    store = Store(tmp_path)
    client = FakeClient()
    agent = WakeAgent(
        context=None,
        settings=Settings(owner_umo="webchat:FriendMessage:1"),
        store=store,
        guard=Guard(store, write_cooldown=0),
        client=client,
        debrief=None,
        ffmpeg="",
    )
    event = agent._make_event()

    assert "先 whale_debrief" in asyncio.run(agent._tool_comment(event, "BV1xx411c7mD", "好片"))
    assert client.comments == []

    agent._seen["BV1xx411c7mD"] = Debrief(bvid="BV1xx411c7mD")
    assert "别 @ 人" in asyncio.run(
        agent._tool_comment(event, "BV1xx411c7mD", "@暴暴 看这个")
    )
    assert "别 @ 人" in asyncio.run(
        agent._tool_comment(event, "BV1xx411c7mD", "https://example.com")
    )
    assert "太长" in asyncio.run(agent._tool_comment(event, "BV1xx411c7mD", "啊" * 300))
    assert client.comments == []

    assert "评了" in asyncio.run(agent._tool_comment(event, "BV1xx411c7mD", "这段剪得真好"))
    assert client.comments == ["这段剪得真好"]

    # 评论正文要留在记录里，插件页才看得见
    saved = {item["bvid"]: item for item in store.watched_recent(7)}["BV1xx411c7mD"]
    assert saved["comment"] == "这段剪得真好"
    assert saved["commented"] is True

    # 同一条视频不重复评
    assert "评论过" in asyncio.run(agent._tool_comment(event, "BV1xx411c7mD", "再说一句"))


def test_comment_shares_dedupe_with_share_channel(tmp_path):
    import asyncio

    from core.debrief import Debrief

    class FakeClient:
        logged_in = True

        async def view(self, bvid):
            return {"aid": 1}

        async def post_comment(self, aid, text):
            return None

        async def send_dm(self, mid, text):
            return None

    store = Store(tmp_path)
    agent = WakeAgent(
        context=None,
        settings=Settings(
            owner_umo="webchat:FriendMessage:1",
            owner_mid="999",
            share_channels=("comment",),
        ),
        store=store,
        guard=Guard(store, write_cooldown=0),
        client=FakeClient(),
        debrief=None,
        ffmpeg="",
    )
    agent._seen["BV1xx411c7mD"] = Debrief(bvid="BV1xx411c7mD")
    asyncio.run(
        agent._tool_share(
            agent._make_event(), "BV1xx411c7mD", "看这个", 8, "这条值得占用你两分钟"
        )
    )
    # 分享时已经在评论区留过，不该再评一次
    assert "评论过" in asyncio.run(
        agent._tool_comment(agent._make_event(), "BV1xx411c7mD", "补一句")
    )

def test_like_requires_deep_read_and_dedupes(tmp_path):
    import asyncio

    from core.debrief import Debrief

    class FakeClient:
        logged_in = True

        def __init__(self):
            self.likes = []

        async def like(self, bvid):
            self.likes.append(bvid)

    store = Store(tmp_path)
    client = FakeClient()
    agent = WakeAgent(
        context=None,
        settings=Settings(owner_umo="webchat:FriendMessage:1"),
        store=store,
        guard=Guard(store, write_cooldown=0),
        client=client,
        debrief=None,
        ffmpeg="",
    )
    event = agent._make_event()

    # 没读过的不许点
    assert "先 whale_debrief" in asyncio.run(agent._tool_like(event, "BV1xx411c7mD"))
    assert client.likes == []

    agent._seen["BV1xx411c7mD"] = Debrief(bvid="BV1xx411c7mD")
    assert "点了" in asyncio.run(agent._tool_like(event, "BV1xx411c7mD"))
    assert client.likes == ["BV1xx411c7mD"]

    # 同一条不会重复点
    assert "点过了" in asyncio.run(agent._tool_like(event, "BV1xx411c7mD"))
    assert client.likes == ["BV1xx411c7mD"]


def test_like_has_daily_cap(tmp_path):
    import asyncio

    from core.agent import LIKE_DAILY
    from core.debrief import Debrief

    class FakeClient:
        logged_in = True

        async def like(self, bvid):
            return None

    store = Store(tmp_path)
    agent = WakeAgent(
        context=None,
        settings=Settings(owner_umo="webchat:FriendMessage:1"),
        store=store,
        guard=Guard(store, write_cooldown=0),
        client=FakeClient(),
        debrief=None,
        ffmpeg="",
    )
    from core import agent as agent_module

    original_gap = agent_module.LIKE_GAP_SECONDS
    agent_module.LIKE_GAP_SECONDS = 0  # 专测每日上限，不要被间隔挡住
    try:
        event = agent._make_event()
        for index in range(LIKE_DAILY + 1):
            bvid = f"BV1xx411c7m{index}"
            agent._seen[bvid] = Debrief(bvid=bvid)
            result = asyncio.run(agent._tool_like(event, bvid))
            if index < LIKE_DAILY:
                assert "点了" in result, result
            else:
                assert "上限" in result, result
    finally:
        agent_module.LIKE_GAP_SECONDS = original_gap


def test_like_does_not_block_share(tmp_path):
    """点赞不该占用分享的写冷却 —— 之前点个赞就把当天的分享锁死了。"""
    import asyncio

    from core.debrief import Debrief

    class FakeClient:
        logged_in = True

        async def like(self, bvid):
            return None

    store = Store(tmp_path)
    guard = Guard(store, write_cooldown=120)
    agent = WakeAgent(
        context=None,
        settings=Settings(owner_umo="webchat:FriendMessage:1"),
        store=store,
        guard=guard,
        client=FakeClient(),
        debrief=None,
        ffmpeg="",
    )
    agent._seen["BV1xx411c7mD"] = Debrief(bvid="BV1xx411c7mD")
    assert "点了" in asyncio.run(agent._tool_like(agent._make_event(), "BV1xx411c7mD"))

    ok, why = guard.write_gate(
        score=15, reason="这条值得占用你两分钟", bvid="BV1xx411c7mD", is_share=True
    )
    assert ok, f"点赞之后分享被挡住了：{why}"


def test_share_fans_out_to_selected_channels(tmp_path):
    """勾了私信和评论区，两条都要真的发一遍。"""
    import asyncio

    class FakeClient:
        logged_in = True

        def __init__(self):
            self.dms = []
            self.comments = []

        async def view(self, bvid):
            return {"aid": 123}

        async def post_comment(self, aid, message):
            self.comments.append((aid, message))

        async def send_dm(self, mid, text):
            self.dms.append((mid, text))

    store = Store(tmp_path)
    client = FakeClient()
    agent = WakeAgent(
        context=None,
        settings=Settings(
            owner_umo="webchat:FriendMessage:1",
            owner_mid="999",
            share_channels=("dm", "comment"),
        ),
        store=store,
        guard=Guard(store, write_cooldown=0),
        client=client,
        debrief=None,
        ffmpeg="",
    )
    text = asyncio.run(
        agent._tool_share(
            agent._make_event(),
            "BV1xx411c7mD",
            "看这个",
            8,
            "这条值得占用你两分钟",
        )
    )
    assert client.dms and client.dms[0][0] == "999"
    assert "bilibili.com/video" in client.dms[0][1]
    assert client.comments
    assert "B站 私信" in text and "视频评论区" in text


def test_share_without_owner_mid_reports_failure(tmp_path):
    import asyncio

    class FakeClient:
        logged_in = True

        async def view(self, bvid):
            return {"aid": 1}

        async def post_comment(self, aid, message):
            raise AssertionError("不该走到这里")

        async def send_dm(self, mid, text):
            raise AssertionError("没有 UID 不该调用私信")

    store = Store(tmp_path)
    agent = WakeAgent(
        context=None,
        settings=Settings(
            owner_umo="webchat:FriendMessage:1",
            share_channels=("dm",),
        ),
        store=store,
        guard=Guard(store, write_cooldown=0),
        client=FakeClient(),
        debrief=None,
        ffmpeg="",
    )
    text = asyncio.run(
        agent._tool_share(
            agent._make_event(), "BV1xx411c7mD", "看这个", 8, "这条值得占用你两分钟"
        )
    )
    assert "没填你的 B站 UID" in text


def test_feed_priority_falls_back_when_login_required(tmp_path):
    """关注 / 推荐都要登录，未登录时应逐级降级到热门，而不是让模型反复试错。"""
    import asyncio

    from core.bili import BiliError

    class FakeClient:
        logged_in = False

        def __init__(self):
            self.calls = []

        async def feed(self, source, *, keyword="", limit=10):
            self.calls.append(source)
            if source != "popular":
                raise BiliError("需要登录态")
            return [
                {
                    "bvid": "BV1xx411c7mD",
                    "title": "猫猫",
                    "up": "某个UP",
                    "duration": 60,
                    "views": 1,
                    "likes": 0,
                    "pubdate": 0,
                    "desc": "",
                    "tname": "",
                }
            ]

    store = Store(tmp_path)
    client = FakeClient()
    agent = WakeAgent(
        context=None,
        settings=Settings(owner_umo="webchat:FriendMessage:1", feed_priority="following"),
        store=store,
        guard=Guard(store),
        client=client,
        debrief=None,
        ffmpeg="",
    )
    text = asyncio.run(agent._tool_feed(agent._make_event(), ""))
    assert client.calls == ["following", "recommend", "popular"]
    assert "已改用 popular" in text
    assert "BV1xx411c7mD" in text


def test_feed_honours_explicit_source(tmp_path):
    import asyncio

    class FakeClient:
        logged_in = True

        def __init__(self):
            self.calls = []

        async def feed(self, source, *, keyword="", limit=10):
            self.calls.append(source)
            return []

    store = Store(tmp_path)
    client = FakeClient()
    agent = WakeAgent(
        context=None,
        settings=Settings(owner_umo="webchat:FriendMessage:1", feed_priority="following"),
        store=store,
        guard=Guard(store),
        client=client,
        debrief=None,
        ffmpeg="",
    )
    asyncio.run(agent._tool_feed(agent._make_event(), "ranking"))
    assert client.calls == ["ranking"]


def test_status_handler_runs_offline(agent):
    import asyncio

    text = asyncio.run(agent._tool_status(agent._make_event()))
    assert "额度" in text


def test_diary_handler_writes(agent):
    import asyncio

    asyncio.run(agent._tool_diary(agent._make_event(), "mine", "今天不想看"))
    assert "不想看" in agent.store.read_diary("mine")


def test_diary_unescapes_literal_newlines(agent):
    """模型偶尔把换行写成字面 \\n，落盘前要还原成真换行。"""
    import asyncio

    asyncio.run(agent._tool_diary(agent._make_event(), "mine", "第一行\\n第二行"))
    body = agent.store.read_diary("mine")
    assert "\\n" not in body
    assert "第二行" in body.splitlines()


def test_debrief_rejects_obvious_garbage(agent):
    import asyncio

    text = asyncio.run(agent._tool_debrief(agent._make_event(), "不是BV号"))
    assert "bvid" in text


def test_share_is_blocked_without_score(agent):
    import asyncio

    text = asyncio.run(
        agent._tool_share(agent._make_event(), "BV1xx411c7mD", "看看这个", 0, "短")
    )
    assert "被拒" in text
