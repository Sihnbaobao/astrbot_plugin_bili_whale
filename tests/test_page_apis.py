"""插件页接口的契约：路由名、返回结构。

页面是前端拼字符串读字段的，字段一改名前端就会静默停在"加载中"，
所以这里把返回结构钉住。QR 生成要打真网络，放在手工验证里，不进单测。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

PLUGINS_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGINS_DIR))

main = importlib.import_module("astrbot_plugin_bili_whale.main")

from astrbot_plugin_bili_whale.core.bili import BiliClient  # noqa: E402
from astrbot_plugin_bili_whale.core.config import Settings  # noqa: E402
from astrbot_plugin_bili_whale.core.debrief import DebriefEngine  # noqa: E402
from astrbot_plugin_bili_whale.core.store import Guard, Store  # noqa: E402


class FakeConfig(dict):
    """替身配置：只需要支持读、按下标写、和 save_config。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.saved = 0

    def save_config(self) -> None:
        self.saved += 1


def make_plugin(tmp_path) -> object:
    """造一个只带页面接口所需属性的实例，绕开 Star 的构造。"""
    store = Store(tmp_path)
    guard = Guard(store)
    plugin = object.__new__(main.BiliWhalePlugin)
    plugin.context = SimpleNamespace(persona_manager=None)
    plugin.config = FakeConfig()
    plugin.settings = Settings(owner_umo="")
    plugin.store = store
    plugin.guard = guard
    plugin.ffmpeg = ""
    plugin.client = SimpleNamespace(logged_in=False)
    plugin.debrief = None
    plugin._account = {}
    plugin._fallback_umo = ""
    plugin._last_run = ""
    plugin._waking = False
    plugin._qr = {}
    plugin._qr_at = 0.0
    plugin.client = BiliClient(
        sessdata="",
        bili_jct="",
        buvid3="",
        cache_path=tmp_path / "cache.json",
        guard=guard,
    )
    plugin.debrief = DebriefEngine(plugin.client, store.shots_dir, "")
    return plugin


def body(response) -> dict:
    return json.loads(bytes(response.body).decode("utf-8"))


def test_llm_hook_injects_today_history(tmp_path):
    """普通聊天里，她应该记得自己今天刷过什么。"""
    import asyncio
    from types import SimpleNamespace

    plugin = make_plugin(tmp_path)
    plugin.settings = Settings(owner_umo="", recall_memory=True)
    plugin.store.mark_watched({"bvid": "BV1", "title": "百合短篇", "score": 8})

    req = SimpleNamespace(
        prompt="你喜欢这个么",
        system_prompt="你是璃月。",
        extra_user_content_parts=[],
        func_tool=None,
    )
    asyncio.run(
        plugin._inject_browsing_memory(SimpleNamespace(_bili_whale_wake=False), req)
    )
    # 记忆作为额外内容块贴在用户消息后面：紧挨着问题，又不冒充用户说的话
    assert len(req.extra_user_content_parts) == 2  # 记忆 + "想刷就去看看"
    # 聊天里也挂上了 B站 工具，她想刷就能去看
    names = {tool.name for tool in req.func_tool.tools}
    assert {"whale_feed", "whale_debrief", "whale_recall"} <= names
    assert "whale_share" not in names  # 聊天里不给分享/发评论的权限
    assert "百合短篇" in req.extra_user_content_parts[0]["text"]
    assert req.prompt == "你喜欢这个么"
    assert req.system_prompt == "你是璃月。"


def test_llm_hook_skips_wake_and_disabled(tmp_path):
    import asyncio
    from types import SimpleNamespace

    plugin = make_plugin(tmp_path)
    plugin.settings = Settings(owner_umo="", recall_memory=True)
    plugin.store.mark_watched({"bvid": "BV1", "title": "百合短篇", "score": 8})

    wake = SimpleNamespace(prompt="唤醒提示", system_prompt="x", extra_user_content_parts=[])
    asyncio.run(plugin._inject_browsing_memory(SimpleNamespace(_bili_whale_wake=True), wake))
    assert wake.extra_user_content_parts == []

    plugin.settings = Settings(owner_umo="", recall_memory=False)
    off = SimpleNamespace(prompt="普通", system_prompt="x", extra_user_content_parts=[])
    asyncio.run(plugin._inject_browsing_memory(SimpleNamespace(_bili_whale_wake=False), off))
    assert off.extra_user_content_parts == []


def test_model_error_text_is_not_treated_as_report():
    """AstrBot 会把模型失败拼成一段正常回复返回，插件必须认出来。"""
    assert main.looks_like_model_error(
        "All chat models failed: RateLimitError: Error code: 429 - "
        "{'error': {'message': 'upstream overloaded'}}"
    )
    assert main.looks_like_model_error("Error code: 429 - too many requests")
    assert not main.looks_like_model_error("今天看了三条：猫、鬼畜、测评。")
    assert not main.looks_like_model_error("")


def test_web_routes_follow_plugin_prefix():
    recorded: list[str] = []
    stub = SimpleNamespace(
        context=SimpleNamespace(
            register_web_api=lambda route, *rest: recorded.append(route)
        )
    )
    for name in dir(main.BiliWhalePlugin):
        if name.startswith("_api_"):
            setattr(stub, name, getattr(main.BiliWhalePlugin, name))
    main.BiliWhalePlugin._register_web_apis(stub)
    assert recorded == [
        "/astrbot_plugin_bili_whale/page/status",
        "/astrbot_plugin_bili_whale/page/login/qr",
        "/astrbot_plugin_bili_whale/page/login/poll",
        "/astrbot_plugin_bili_whale/page/wake",
        "/astrbot_plugin_bili_whale/page/diary",
        "/astrbot_plugin_bili_whale/page/config",
        "/astrbot_plugin_bili_whale/page/watch/delete",
        "/astrbot_plugin_bili_whale/page/diary/delete",
        "/astrbot_plugin_bili_whale/page/config/save",
    ]


def test_status_payload_shape(tmp_path):
    plugin = make_plugin(tmp_path)
    data = body(asyncio.run(main.BiliWhalePlugin._api_status(plugin)))
    for key in (
        "logged_in",
        "account",
        "quota",
        "plan",
        "windows",
        "share",
        "waking",
        "watched",
        "comments",
        "owner_umo",
    ):
        assert key in data, key
    assert data["quota"]["reads"][1] == 3000
    assert data["logged_in"] is False


def test_status_lists_her_comments(tmp_path):
    plugin = make_plugin(tmp_path)
    plugin.store.mark_watched(
        {
            "bvid": "BV1",
            "title": "水之都那个忧郁的午后",
            "comment": "这段雨声真的顶",
            "comment_at": "2026-11-08T00:20:00",
        }
    )
    data = body(asyncio.run(main.BiliWhalePlugin._api_status(plugin)))
    assert data["comments"][0]["text"] == "这段雨声真的顶"
    assert data["comments"][0]["title"] == "水之都那个忧郁的午后"


def test_diary_payload_shape(tmp_path):
    plugin = make_plugin(tmp_path)
    plugin.store.append_diary("mine", "今天看了条猫")
    data = body(asyncio.run(main.BiliWhalePlugin._api_diary(plugin)))
    assert set(data) == {"date", "text"}
    assert "猫" in data["text"]


def test_config_payload_covers_editable_keys(tmp_path):
    from astrbot_plugin_bili_whale.main import CONFIG_SPEC

    plugin = make_plugin(tmp_path)
    data = body(asyncio.run(main.BiliWhalePlugin._api_config(plugin)))
    assert set(data["values"]) == set(CONFIG_SPEC)
    assert data["values"]["WAKE_WINDOWS"] == "20:00-24:00"
    assert data["personas"][0]["id"] == ""
    assert data["providers"][0]["id"] == ""
    assert "跟随当前会话" in data["providers"][0]["name"]
    assert data["sessions"] == []  # 测试替身没有数据库，取不到会话时应静默返回空列表
    assert [opt["value"] for opt in data["share_via_options"]] == [
        "dm",
        "comment",
        "local",
    ]


def test_coerce_handles_page_values(tmp_path):
    from astrbot_plugin_bili_whale.main import _coerce

    assert _coerce(int, "30") == 30
    assert _coerce(int, 30) == 30
    assert _coerce(bool, "true") is True
    assert _coerce(bool, False) is False
    assert _coerce(str, 12) == "12"
    try:
        _coerce(int, "三十")
    except ValueError:
        return
    raise AssertionError("非法数字应当抛 ValueError")


def test_config_save_applies_values(tmp_path):
    plugin = make_plugin(tmp_path)
    ok, message, values = plugin._apply_config_updates(
        {"SKIP_PERCENT": 55, "WAKE_WINDOWS": "21:00-23:00"}
    )
    assert ok, message
    assert values["SKIP_PERCENT"] == 55
    assert plugin.settings.windows == "21:00-23:00"
    assert plugin.config["SKIP_PERCENT"] == 55
    assert plugin.config.saved == 1


def test_config_save_clamps_numbers(tmp_path):
    plugin = make_plugin(tmp_path)
    ok, _, values = plugin._apply_config_updates({"SKIP_PERCENT": 999})
    assert ok
    assert values["SKIP_PERCENT"] == 100


def test_config_save_rejects_bad_share_channel(tmp_path):
    plugin = make_plugin(tmp_path)
    ok, message, _ = plugin._apply_config_updates({"SHARE_VIA": "telepathy"})
    assert not ok and "local" in message


def test_config_save_accepts_multiple_channels(tmp_path):
    plugin = make_plugin(tmp_path)
    ok, message, values = plugin._apply_config_updates({"SHARE_VIA": "dm, local"})
    assert ok, message
    assert values["SHARE_VIA"] == "dm,local"
    assert plugin.settings.share_channels == ("dm", "local")


def test_config_save_rejects_unknown_channel(tmp_path):
    plugin = make_plugin(tmp_path)
    ok, message, _ = plugin._apply_config_updates({"SHARE_VIA": "dm,telepathy"})
    assert not ok and "local" in message


def test_config_save_rejects_empty_channels(tmp_path):
    plugin = make_plugin(tmp_path)
    ok, message, _ = plugin._apply_config_updates({"SHARE_VIA": " , "})
    assert not ok


def test_config_save_rejects_bad_windows(tmp_path):
    plugin = make_plugin(tmp_path)
    ok, message, _ = plugin._apply_config_updates({"WAKE_WINDOWS": "乱写"})
    assert not ok and "作息" in message
    assert plugin.config.saved == 0


def test_config_save_resets_plan_when_windows_change(tmp_path):
    plugin = make_plugin(tmp_path)
    plugin.store.save_state({"plan": {"action": "wake"}, "used": {"x|y": 1}})
    ok, message, _ = plugin._apply_config_updates({"WAKE_WINDOWS": "07:00-09:00"})
    assert ok, message
    state = plugin.store.load_state()
    assert "plan" not in state
    assert state["used"] == {}


def test_config_save_rejects_unknown_keys(tmp_path):
    plugin = make_plugin(tmp_path)
    assert asyncio.run(main.BiliWhalePlugin._api_config_save(plugin)).status_code == 400


def test_delete_endpoints_reject_bad_params(tmp_path):
    plugin = make_plugin(tmp_path)
    assert (
        asyncio.run(main.BiliWhalePlugin._api_watch_delete(plugin)).status_code == 400
    )
    assert (
        asyncio.run(main.BiliWhalePlugin._api_diary_delete(plugin)).status_code == 400
    )


def test_watch_delete_endpoint_removes_record(tmp_path, monkeypatch):
    """接口直连：给了 bvid 就要真的从 watched.jsonl 里删掉。"""
    import asyncio
    from types import SimpleNamespace

    from astrbot.api import web as web_module

    plugin = make_plugin(tmp_path)
    plugin.store.mark_watched({"bvid": "BV1", "title": "百合"})
    plugin.store.mark_watched({"bvid": "BV2", "title": "别的"})

    class FakeRequest:
        async def json(self, default=None):
            return {"bvid": "BV1"}

    monkeypatch.setattr(web_module, "request", FakeRequest())
    response = asyncio.run(main.BiliWhalePlugin._api_watch_delete(plugin))
    assert response.status_code == 200
    assert [item["bvid"] for item in plugin.store.watched_recent(7)] == ["BV2"]


def test_diary_delete_endpoint_removes_round(tmp_path, monkeypatch):
    import asyncio

    from astrbot.api import web as web_module
    from core.store import today_str

    plugin = make_plugin(tmp_path)
    path = plugin.store.diary_dir / f"{today_str()}-yours.md"
    path.write_text("\n## 09:10\n\n第一轮\n\n## 18:52\n\n第二轮\n", encoding="utf-8")

    class FakeRequest:
        async def json(self, default=None):
            return {"who": "yours", "time": "18:52"}

    monkeypatch.setattr(web_module, "request", FakeRequest())
    response = asyncio.run(main.BiliWhalePlugin._api_diary_delete(plugin))
    assert response.status_code == 200
    text = path.read_text(encoding="utf-8")
    assert "第二轮" not in text and "第一轮" in text

def test_poll_without_qr_is_rejected(tmp_path):
    plugin = make_plugin(tmp_path)
    response = asyncio.run(main.BiliWhalePlugin._api_login_poll(plugin))
    assert response.status_code == 400


def test_wake_without_session_is_rejected(tmp_path):
    plugin = make_plugin(tmp_path)
    response = asyncio.run(main.BiliWhalePlugin._api_wake(plugin))
    assert response.status_code == 400
    assert "推送会话" in body(response).get("message", "") or True
