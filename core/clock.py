"""生物钟：决定"今天几点醒、要不要醒"。

不是定时任务。三件事让它像过日子而不是像 cron：

1. 在作息窗口里**随机**挑一个时刻，不整点。
2. 有概率今天就是不想看（默认 30%）—— 但那一刻仍然会醒一次，让她把"不想看"写进日记。
3. 同一个窗口里醒多次时，两次之间保证最小间隔，避免全挤在十分钟里。

所有时间都是本地日历时间；跨午夜的窗口（如 22:00-02:00）按昨天那次机会算。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Window:
    """一个作息窗口。start/end 是当天零点起的分钟数，end 可超过 1440。"""

    label: str
    start: int
    end: int
    wakes: int


def _parse_clock(text: str) -> int:
    """把 HH:MM 解析成分钟数，24:00 合法。"""
    hours, _, minutes = str(text).strip().partition(":")
    hour, minute = int(hours), int(minutes or 0)
    if not 0 <= hour <= 24 or not 0 <= minute <= 59 or (hour == 24 and minute):
        raise ValueError(f"无效时间：{text}")
    return hour * 60 + minute


def parse_windows(spec: str, wakes_per_window: int = 1) -> list[Window]:
    """解析作息窗口配置。

    Args:
        spec: 形如 "20:00-24:00" 或 "09:00-11:00,20:00-24:00" 的字符串。
        wakes_per_window: 每个窗口默认醒几次。

    Returns:
        窗口列表，格式不合法的项被跳过。

    Raises:
        ValueError: 一个合法窗口都没有。
    """
    windows: list[Window] = []
    for chunk in str(spec or "").replace("，", ",").split(","):
        chunk = chunk.strip()
        if not chunk or "-" not in chunk:
            continue
        head, _, tail = chunk.partition("-")
        try:
            start, end = _parse_clock(head), _parse_clock(tail)
        except (ValueError, TypeError):
            continue
        if end <= start:
            end += 24 * 60
        windows.append(
            Window(
                label=f"{head.strip()}-{tail.strip()}",
                start=start,
                end=end,
                wakes=max(0, int(wakes_per_window)),
            )
        )
    if not windows:
        raise ValueError(f"没有可用的作息窗口：{spec!r}")
    return windows


class Clock:
    """按作息窗口产出"下一次醒来的计划"。

    Args:
        windows: 作息窗口。
        skip_percent: 今天不想看的概率（0-100）。
        min_gap_min: 同一窗口内两次醒来之间的最小间隔（分钟）。
        rng: 随机源，测试时注入固定种子。
    """

    def __init__(
        self,
        windows: list[Window],
        *,
        skip_percent: int = 30,
        min_gap_min: int = 10,
        rng: random.Random | None = None,
    ) -> None:
        self.windows = windows
        self.skip_percent = min(100, max(0, int(skip_percent)))
        self.min_gap = max(1, int(min_gap_min))
        self.rng = rng or random.Random()

    def _occurrences(self, now: datetime) -> list[dict]:
        """列出今天与昨天（跨午夜窗口）的窗口实例。"""
        out: list[dict] = []
        for window in self.windows:
            for offset in (0, -1):
                day = (now + timedelta(days=offset)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                start = day + timedelta(minutes=window.start)
                end = day + timedelta(minutes=window.end)
                if end <= now:
                    continue
                key = f"{day.strftime('%Y-%m-%d')}|{window.label}"
                out.append(
                    {
                        "key": key,
                        "label": window.label,
                        "start": start,
                        "end": end,
                        "wakes": window.wakes,
                    }
                )
        return sorted(out, key=lambda item: item["end"])

    @staticmethod
    def _cleanup(state: dict, now: datetime) -> dict:
        """丢掉三天前的窗口计数，避免状态文件无限增长。"""
        cutoff = (now - timedelta(days=3)).strftime("%Y-%m-%d")
        used = {
            key: value
            for key, value in (state.get("used") or {}).items()
            if str(key)[:10] >= cutoff
        }
        state["used"] = used
        return state

    def plan(self, state: dict, now: datetime | None = None) -> dict:
        """算出当前的计划。

        Args:
            state: 插件状态（会被原地清理旧计数）。
            now: 当前时间，默认此刻。

        Returns:
            计划字典：action 为 wake / skip / done，含 at(时间戳)、window、reason。
        """
        now = now or datetime.now()
        self._cleanup(state, now)
        today = now.strftime("%Y-%m-%d")

        current = state.get("plan") or {}
        if (
            current.get("date") == today
            and not current.get("fired")
            and current.get("action") in ("wake", "skip")
        ):
            return current

        candidates = [
            item
            for item in self._occurrences(now)
            if int(state["used"].get(item["key"], 0)) < item["wakes"]
        ]
        if not candidates:
            plan = {
                "date": today,
                "action": "done",
                "reason": "今天的窗口都过了或已经醒够了",
                "fired": True,
            }
            state["plan"] = plan
            return plan

        last_woke = float(state.get("woke_at", 0))
        for item in candidates:
            earliest = max(now, item["start"])
            if last_woke:
                earliest = max(
                    earliest,
                    datetime.fromtimestamp(last_woke) + timedelta(minutes=self.min_gap),
                )
            latest = item["end"] - timedelta(minutes=1)
            if earliest >= latest:
                continue
            span = (latest - earliest).total_seconds()
            at = earliest + timedelta(seconds=self.rng.uniform(0, max(1.0, span)))
            skip = self.rng.uniform(0, 100) < self.skip_percent
            plan = {
                "date": today,
                "action": "skip" if skip else "wake",
                "at": at.timestamp(),
                "window": item["label"],
                "key": item["key"],
                "reason": "今天不想看（掷硬币）" if skip else f"在 {item['label']} 里随机醒来",
                "fired": False,
            }
            state["plan"] = plan
            return plan

        plan = {
            "date": today,
            "action": "done",
            "reason": "剩余窗口放不下一次唤醒（离结束不到最小间隔）",
            "fired": True,
        }
        state["plan"] = plan
        return plan

    def due(self, plan: dict, now: datetime | None = None) -> bool:
        """计划到点了吗。"""
        now = now or datetime.now()
        return (
            plan.get("action") in ("wake", "skip")
            and not plan.get("fired")
            and float(plan.get("at", 0)) <= now.timestamp()
        )

    @staticmethod
    def consume(state: dict, plan: dict, now: datetime | None = None) -> dict:
        """记下这一次唤醒已经发生。

        Args:
            state: 插件状态。
            plan: 本次计划。
            now: 当前时间。

        Returns:
            更新后的状态。
        """
        now = now or datetime.now()
        key = plan.get("key")
        if key:
            used = state.setdefault("used", {})
            used[key] = int(used.get(key, 0)) + 1
        state["woke_at"] = now.timestamp()
        plan["fired"] = True
        state["plan"] = plan
        return state

    @staticmethod
    def describe(plan: dict) -> str:
        """给状态命令用的一句话。"""
        action = plan.get("action")
        if action == "done":
            return f"今天不再醒：{plan.get('reason', '')}"
        at = float(plan.get("at", 0))
        when = datetime.fromtimestamp(at).strftime("%m-%d %H:%M") if at else "?"
        if action == "skip":
            return f"下一次 {when} 醒来后会写「今天不想看」（{plan.get('window', '')}）"
        return f"下一次 {when} 醒来（{plan.get('window', '')}）"
