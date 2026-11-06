"""插件配置的解析与默认值。

只做一件事：把 AstrBotConfig（本质是个 dict）解析成一个不可变的 Settings 对象，
让下游不用到处 config.get("XXX", 默认值)。新增配置项只改这里和 _conf_schema.json。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _as_list(value: Any) -> list[str]:
    """把配置里的列表/逗号串统一成字符串列表。"""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").replace("，", ",").split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """一份解析好的插件配置。"""

    sessdata: str = ""
    bili_jct: str = ""
    buvid3: str = ""
    provider_id: str = ""
    owner_umo: str = ""
    owner_mid: str = ""
    owner_name: str = "主人"
    persona_id: str = ""
    persona: str = ""
    windows: str = "20:00-24:00"
    wakes_per_window: int = 1
    skip_percent: int = 30
    min_gap_min: int = 10
    max_scan: int = 5
    max_deep: int = 3
    feed_priority: str = "following"
    read_daily: int = 3000
    write_daily: int = 30
    write_cooldown: int = 120
    share_daily: int = 3
    share_min_score: int = 7
    share_channels: tuple[str, ...] = ("local",)
    enable_like: bool = True
    enable_comment: bool = True
    recall_memory: bool = True
    frames: bool = True
    likes: tuple[str, ...] = ()
    dislikes: tuple[str, ...] = ()
    proxy: str = ""
    throttle_ms: int = 1200

    @property
    def logged_in(self) -> bool:
        """是否配了能写操作的登录态。"""
        return bool(self.sessdata and self.bili_jct)

    @classmethod
    def from_config(cls, config: Any) -> Settings:
        """从 AstrBotConfig 构造。

        Args:
            config: AstrBot 注入的插件配置。

        Returns:
            解析后的配置；缺失项取默认值。
        """

        def get(key: str, default: Any = "") -> Any:
            try:
                value = config.get(key, default)
            except AttributeError:
                value = default
            return default if value is None or value == "" else value

        return cls(
            sessdata=str(get("BILI_SESSDATA")).strip(),
            bili_jct=str(get("BILI_JCT")).strip(),
            buvid3=str(get("BILI_BUVID3")).strip(),
            provider_id=str(get("PROVIDER_ID")).strip(),
            owner_umo=str(get("OWNER_UMO")).strip(),
            owner_mid=str(get("OWNER_MID")).strip(),
            owner_name=str(get("OWNER_NAME", "主人")) or "主人",
            persona_id=str(get("PERSONA_ID")).strip(),
            persona=str(get("PERSONA")).strip(),
            windows=str(get("WAKE_WINDOWS", "20:00-24:00")),
            wakes_per_window=int(get("WAKES_PER_WINDOW", 1)),
            skip_percent=int(get("SKIP_PERCENT", 30)),
            min_gap_min=int(get("MIN_GAP_MINUTES", 10)),
            max_scan=int(get("MAX_SCAN", 5)),
            max_deep=int(get("MAX_DEEP_READ", 3)),
            feed_priority=str(get("FEED_PRIORITY", "following")).strip() or "following",
            read_daily=int(get("READ_DAILY", 3000)),
            write_daily=int(get("WRITE_DAILY", 30)),
            write_cooldown=int(get("WRITE_COOLDOWN", 120)),
            share_daily=int(get("SHARE_DAILY", 3)),
            share_min_score=int(get("SHARE_MIN_SCORE", 7)),
            share_channels=tuple(
                channel
                for channel in _as_list(get("SHARE_VIA", "local"))
                if channel in ("local", "comment", "dm")
            )
            or ("local",),
            frames=bool(get("ENABLE_FRAMES", True)),
            enable_like=bool(get("ENABLE_LIKE", True)),
            enable_comment=bool(get("ENABLE_COMMENT", True)),
            recall_memory=bool(get("ENABLE_MEMORY", True)),
            likes=tuple(_as_list(get("PREFER_LIKE"))),
            dislikes=tuple(_as_list(get("PREFER_DISLIKE"))),
            proxy=str(get("PROXY_URL")).strip(),
            throttle_ms=int(get("THROTTLE_MS", 1200)),
        )
