"""养鱼 · 自己刷B站。

一个「她自己过日子」的插件：按作息随机醒来，挑几条视频真看懂，写两本日记，
再决定要不要叫你。判断交给模型（工具循环），代码只管三件事：喂素材、守红线、按时叫醒。

与旧版刷B站插件的区别不在功能多少，而在分工：
这里没有硬编码的选题流水线，也没有几十个互相影响的开关。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .core.agent import WakeAgent
from .core.bili import BiliClient
from .core.clock import Clock, parse_windows
from .core.config import Settings
from .core.debrief import DebriefEngine
from .core.login import BiliLogin, LoginError
from .core.store import Guard, Store, today_str

PLUGIN_NAME = "astrbot_plugin_bili_whale"
TICK_SECONDS = 60
PUSH_CHUNK = 1500
RETRY_DELAY_SECONDS = 900
MAX_WAKE_RETRIES = 2

# AstrBot 的 agent runner 在模型全部失败时，会把错误拼成一段正常回复返回。
# 不识别的话就会把这段东西当成"她的汇报"推给用户。
_ERROR_MARKERS = (
    "All chat models failed",
    "RateLimitError",
    "Upstream request failed",
    "Error code: 429",
    "Error code: 500",
    "Error code: 502",
    "Error code: 503",
)


def looks_like_model_error(text: str) -> bool:
    """判断这段文本是不是模型报错被当成了回复。

    Args:
        text: agent 返回的正文。

    Returns:
        像报错返回 True。
    """
    head = str(text or "").strip()[:200]
    return any(marker in head for marker in _ERROR_MARKERS)

# 插件页能改的配置项：配置键 -> (Settings 字段, 类型)
# 用 Settings 做唯一事实来源，页面读到的值和实际生效的值就不会分叉。
CONFIG_SPEC: dict[str, tuple[str, type]] = {
    "WAKE_WINDOWS": ("windows", str),
    "WAKES_PER_WINDOW": ("wakes_per_window", int),
    "SKIP_PERCENT": ("skip_percent", int),
    "MIN_GAP_MINUTES": ("min_gap_min", int),
    "MAX_SCAN": ("max_scan", int),
    "MAX_DEEP_READ": ("max_deep", int),
    "FEED_PRIORITY": ("feed_priority", str),
    "ENABLE_LIKE": ("enable_like", bool),
    "ENABLE_MEMORY": ("recall_memory", bool),
    "ENABLE_COMMENT": ("enable_comment", bool),
    "ENABLE_FRAMES": ("frames", bool),
    "SHARE_VIA": ("share_channels", str),
    "SHARE_DAILY": ("share_daily", int),
    "SHARE_MIN_SCORE": ("share_min_score", int),
    "READ_DAILY": ("read_daily", int),
    "WRITE_DAILY": ("write_daily", int),
    "WRITE_COOLDOWN": ("write_cooldown", int),
    "OWNER_UMO": ("owner_umo", str),
    "OWNER_MID": ("owner_mid", str),
    "OWNER_NAME": ("owner_name", str),
    "PROVIDER_ID": ("provider_id", str),
    "PERSONA_ID": ("persona_id", str),
}

# 数值项的上下限，防止页面上手滑填出个 0 或者 99999
CONFIG_RANGE: dict[str, tuple[int, int]] = {
    "WAKES_PER_WINDOW": (0, 12),
    "SKIP_PERCENT": (0, 100),
    "MIN_GAP_MINUTES": (1, 240),
    "MAX_SCAN": (1, 20),
    "MAX_DEEP_READ": (0, 10),
    "SHARE_DAILY": (0, 20),
    "SHARE_MIN_SCORE": (0, 10),
    "READ_DAILY": (50, 20000),
    "WRITE_DAILY": (0, 200),
    "WRITE_COOLDOWN": (0, 3600),
}


class BiliWhalePlugin(Star):
    """插件入口：生命周期、生物钟驱动、推送与命令。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.settings = Settings.from_config(config)
        self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.store = Store(self.data_dir)
        self.guard = self._build_guard(self.settings)
        self.ffmpeg = self._find_ffmpeg()
        self.client = self._build_client(self.settings, self.guard)
        self.debrief = DebriefEngine(self.client, self.store.shots_dir, self.ffmpeg)
        self.clock = self._build_clock(self.settings)
        self._task: asyncio.Task | None = None
        self._running = False
        self._fallback_umo = ""
        self._last_run: str = ""
        self._wake_ok: bool = False
        self._qr: dict = {}
        self._qr_at: float = 0.0
        self._login: BiliLogin | None = None
        self._waking = False
        self._account: dict = {}

    # ── 构造与热更新 ──

    def _build_guard(self, settings: Settings) -> Guard:
        """按配置造预算控制器。全插件只应有一个实例，否则额度会被两份状态各记一次。"""
        return Guard(
            self.store,
            read_daily=settings.read_daily,
            write_daily=settings.write_daily,
            write_cooldown=settings.write_cooldown,
            share_daily=settings.share_daily,
            share_min_score=settings.share_min_score,
        )

    def _build_client(self, settings: Settings, guard: Guard) -> BiliClient:
        """按配置造 B站 客户端，复用同一个预算控制器。"""
        return BiliClient(
            sessdata=settings.sessdata,
            bili_jct=settings.bili_jct,
            buvid3=settings.buvid3,
            cache_path=self.store.cache_dir / "api-cache.json",
            guard=guard,
            proxy=settings.proxy,
            throttle_ms=settings.throttle_ms,
        )

    @staticmethod
    def _build_clock(settings: Settings) -> Clock:
        return Clock(
            parse_windows(settings.windows, settings.wakes_per_window),
            skip_percent=settings.skip_percent,
            min_gap_min=settings.min_gap_min,
        )

    @staticmethod
    def _find_ffmpeg() -> str:
        """找 ffmpeg：先看环境变量，再看 PATH。"""
        explicit = os.environ.get("FFMPEG", "").strip()
        if explicit and Path(explicit).exists():
            return explicit
        return shutil.which("ffmpeg") or ""

    def _refresh(self) -> None:
        """每次 tick 前重读配置，让改配置不必重启插件。

        只重建真的变了的部件：凭据变了才换客户端（旧的要先关连接池）。
        """
        latest = Settings.from_config(self.config)
        if (
            latest.sessdata,
            latest.bili_jct,
            latest.buvid3,
            latest.proxy,
        ) != (
            self.settings.sessdata,
            self.settings.bili_jct,
            self.settings.buvid3,
            self.settings.proxy,
        ):
            old = self.client
            self.client = self._build_client(latest, self.guard)
            self.debrief = DebriefEngine(self.client, self.store.shots_dir, self.ffmpeg)
            asyncio.create_task(old.close())
        self.settings = latest
        self.guard.read_daily = latest.read_daily
        self.guard.write_daily = latest.write_daily
        self.guard.write_cooldown = latest.write_cooldown
        self.guard.share_daily = latest.share_daily
        self.guard.share_min_score = latest.share_min_score
        # 客户端持有一份守卫引用，刷新后要重新指向，否则预算约束会在换客户端后失效
        self.client.guard = self.guard
        self.clock = self._build_clock(latest)

    # ── 生命周期 ──

    async def initialize(self) -> None:
        """插件激活时启动生物钟。"""
        if not self.settings.logged_in:
            logger.warning("[bili-whale] 还没配 Cookie，只读功能可用，写操作会被拒绝")
        if not self.settings.owner_umo:
            logger.warning("[bili-whale] 还没配 OWNER_UMO，唤醒后无法推送（先发一次 /鲸鱼 状态 可自动记住会话）")
        dropped = self.store.prune(180)
        if dropped:
            logger.info("[bili-whale] 清掉了 %d 条超过半年的观看记录", dropped)
        shots = self.store.prune_shots(30, 200)
        if shots:
            logger.info("[bili-whale] 清掉了 %d 张过期的截图", shots)
        pending = self.store.take_pending_report()
        if pending:
            logger.info("[bili-whale] 发现上一轮没推出去的汇报，尝试补发")
            asyncio.create_task(self._deliver_pending(pending))
        self._register_web_apis()
        self._running = True
        self._task = asyncio.create_task(self._clock_loop())
        logger.info("[bili-whale] 生物钟已启动，%s", Clock.describe(self._plan_now()))

    async def terminate(self) -> None:
        """插件被禁用或重载时收尾。"""
        self._running = False
        if self._login is not None:
            await self._login.close()
            self._login = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.client.close()

    # ── 生物钟 ──

    def _plan_now(self) -> dict:
        state = self.store.load_state()
        plan = self.clock.plan(state)
        self.store.save_state(state)
        return plan

    async def _clock_loop(self) -> None:
        """每分钟看一次：该醒了吗。"""
        while self._running:
            try:
                self._refresh()
                state = self.store.load_state()
                plan = self.clock.plan(state)
                if self.clock.due(plan):
                    skip = plan.get("action") == "skip"
                    await self._wake(manual=False, skip=skip)
                    # 唤醒期间额度和计划都被写过，这里必须重新读，否则旧快照会覆盖掉新状态
                    state = self.store.load_state()
                    plan = state.get("plan") or plan
                    if self._wake_ok:
                        self.store.save_state(Clock.consume(state, plan))
                    elif int(plan.get("retries", 0)) < MAX_WAKE_RETRIES:
                        # 模型抽风 / 网络抖动不该白占掉今天的这一次机会
                        plan["retries"] = int(plan.get("retries", 0)) + 1
                        plan["fired"] = False
                        plan["at"] = time.time() + RETRY_DELAY_SECONDS
                        state["plan"] = plan
                        self.store.save_state(state)
                        logger.warning(
                            "[bili-whale] 这一轮没跑成，%d 分钟后重试（第 %d 次）",
                            RETRY_DELAY_SECONDS // 60,
                            plan["retries"],
                        )
                    else:
                        logger.warning("[bili-whale] 连续 %d 次没跑成，这次机会作废", MAX_WAKE_RETRIES + 1)
                        self.store.save_state(Clock.consume(state, plan))
                else:
                    self.store.save_state(state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 一次失败不该让生物钟停摆
                logger.error("[bili-whale] 生物钟异常：%s", exc, exc_info=True)
            await asyncio.sleep(TICK_SECONDS)

    async def _wake(self, *, manual: bool, skip: bool = False) -> str:
        """真的唤醒一次：跑 agent、推送汇报。

        Args:
            manual: 是否人工触发。
            skip: 今天掷到「不想看」。

        Returns:
            模型写出的汇报正文，失败时返回空串。
        """
        left = self.guard.pause_left()
        if left:
            logger.warning("[bili-whale] 风控熔断中，跳过这次唤醒（剩 %s 秒）", left)
            return ""
        umo = self.settings.owner_umo or self._fallback_umo
        if not umo:
            logger.warning("[bili-whale] 没有推送会话，放弃这次唤醒")
            return ""

        agent = WakeAgent(
            context=self.context,
            settings=self.settings,
            store=self.store,
            guard=self.guard,
            client=self.client,
            debrief=self.debrief,
            ffmpeg=self.ffmpeg,
        )
        note = ""
        if skip:
            note = (
                "今天掷到了「不想看」。不用 whale_feed，直接写两本日记说明今天为什么不想看，"
                "然后汇报。这是被允许的，不必硬找理由去看。"
            )
        self._wake_ok = False
        logger.info(
            "[bili-whale] 醒来（%s），开始这一轮",
            "手动" if manual else ("跳过" if skip else "自然"),
        )
        try:
            report = await agent.run(manual=manual, note=note)
        except Exception as exc:
            logger.error("[bili-whale] 唤醒失败：%s", exc, exc_info=True)
            return ""

        if report and looks_like_model_error(report):
            logger.warning(
                "[bili-whale] 这一轮拿回来的是模型报错，不是汇报：%s", report[:120]
            )
            return ""

        self._wake_ok = True
        self._last_run = datetime.now().strftime("%Y-%m-%d %H:%M")
        # 先落盘再推送：推送要等 agent 写完最后一句，那一刻最容易被重启打断
        # 只推她真正发出去的东西。唤醒汇报那种"我今天看了什么"的流水账不再推送。
        share_block = agent.shared if "local" in self.settings.share_channels else []
        if share_block:
            self.store.save_pending_report("", share_block)
        if await self._push(umo, share_block):
            self.store.mark_pending_report_pushed()
        else:
            logger.warning("[bili-whale] 汇报没送出去，下次启动会补发")
        return report

    async def _deliver_pending(self, report: dict) -> None:
        """补发上一轮没能推送的汇报。

        Args:
            report: store.take_pending_report() 的返回值。
        """
        umo = self.settings.owner_umo or self._fallback_umo
        if not umo:
            return
        if await self._push(umo, report.get("shared") or []):
            self.store.mark_pending_report_pushed()
            logger.info("[bili-whale] 补发成功")
        else:
            logger.warning("[bili-whale] 补发失败，下次启动再试")

    async def _push(self, umo: str, shared: list[dict]) -> bool:
        """把汇报推给对方，分享内容附在后面。

        Args:
            umo: 目标会话。
            shared: 本次通过闸门的分享。

        Returns:
            是否送达。没东西可发也算送达，免得无限重试。
        """
        if not shared:
            return True
        # 不套模板：她的原话 + 链接，一条一条发。标题、分数都是内部信息。
        # 链接在前、正文在后：链接单独一行更整齐，也方便直接点开
        chunks = [
            f"https://www.bilibili.com/video/{item['bvid']}/\n{item['message']}"
            for item in shared
        ]

        text = "\n\n".join(chunks)
        logger.info("[bili-whale] 推送 %d 条分享 → %s", len(shared), umo)
        for start in range(0, len(text), PUSH_CHUNK):
            try:
                await self.context.send_message(
                    umo, MessageChain(chain=[Plain(text[start : start + PUSH_CHUNK])])
                )
            except Exception as exc:
                logger.error("[bili-whale] 推送失败：%s", exc)
                return False
        return True

    # ── 命令 ──

    @filter.command("鲸鱼")
    async def whale_command(self, event: AstrMessageEvent):
        """养鱼插件入口：/鲸鱼 [状态|现在|日记|历史]。

        Args:
            event: 消息事件。
        """
        self._fallback_umo = event.unified_msg_origin
        await self._remember_session(event.unified_msg_origin)
        argument = event.message_str.strip().split(maxsplit=1)
        sub = argument[1].strip() if len(argument) > 1 else ""

        if not sub or sub in ("状态", "status"):
            yield event.plain_result(self._status_text())
            return

        if sub in ("现在", "now"):
            yield event.plain_result("叫醒一次，正在刷……（会真的发消息，稍等）")
            await self._wake(manual=True)
            if not self._wake_ok:
                yield event.plain_result("这次没醒成，看日志里的原因。")
            return

        if sub.startswith("日记"):
            who = "yours" if "给你" in sub or "yours" in sub else "mine"
            body = self.store.read_diary(who).strip()
            label = "写给你那本" if who == "yours" else "写给自己那本"
            yield event.plain_result(
                f"今天{label}：\n\n{body or '（今天还没写）'}"
            )
            return

        if sub in ("历史", "history"):
            items = self.store.watched_recent(7)
            if not items:
                yield event.plain_result("最近七天什么都没看。")
                return
            lines = [f"最近七天看过 {len(items)} 条："]
            for item in items[:20]:
                mark = f"（{item['score']}/10）" if item.get("score") is not None else ""
                lines.append(f"- {item.get('bvid', '')} {item.get('title', '')}{mark}")
            yield event.plain_result("\n".join(lines))
            return

        yield event.plain_result(
            "用法：/鲸鱼 状态 | /鲸鱼 现在 | /鲸鱼 日记 | /鲸鱼 日记 给你 | /鲸鱼 历史"
        )

    async def _remember_session(self, umo: str) -> None:
        """第一次收到 /鲸鱼 命令时，把当前会话记成推送目标。

        Args:
            umo: 当前消息的会话标识。
        """
        if self.settings.owner_umo or not umo:
            return
        self.config["OWNER_UMO"] = umo
        try:
            self.config.save_config()
        except Exception as exc:
            logger.error("[bili-whale] 保存推送会话失败：%s", exc)
            return
        self.settings = Settings.from_config(self.config)
        logger.info("[bili-whale] 已把当前会话记为推送目标：%s", umo)

    def _status_text(self) -> str:
        """状态命令要显示的东西，尽量一眼看出哪里没配好。"""
        state = self.store.load_state()
        plan = state.get("plan") or {}
        lines = [
            "🐟 养鱼 · 自己刷B站",
            f"额度：{self.guard.summary()}",
            f"作息：{Clock.describe(plan)}",
            f"窗口：{self.settings.windows}（每窗口 {self.settings.wakes_per_window} 次，"
            f"{self.settings.skip_percent}% 概率今天不想看）",
            f"分享：{'/'.join(self.settings.share_channels)}，每天 ≤{self.settings.share_daily} 条，"
            f"评分 ≥{self.settings.share_min_score}",
        ]
        lines.append(f"登录态：{'有（可写）' if self.settings.logged_in else '没有（只能读）'}")
        lines.append(f"ffmpeg：{self.ffmpeg or '没有（deep 模式只看文字）'}")
        lines.append(f"推送会话：{self.settings.owner_umo or '未配置'}")
        lines.append(f"上次唤醒：{self._last_run or '还没跑过'}")
        recent = self.store.watched_recent(1)
        lines.append(f"今天已看：{len(recent)} 条")
        return "\n".join(lines)

    # ── 插件页 Web API ──

    def _register_web_apis(self) -> None:
        """注册插件页用的接口。

        路由前缀必须是插件名：AstrBot 按 /api/plug/<插件名>/<endpoint> 分发，
        页面侧用 bridge.apiGet("page/status") 这样的相对路径调用。
        """
        routes = [
            ("page/status", self._api_status, ["GET"], "养鱼：状态总览"),
            ("page/login/qr", self._api_login_qr, ["POST"], "养鱼：生成登录二维码"),
            ("page/login/poll", self._api_login_poll, ["POST"], "养鱼：查询扫码状态"),
            ("page/wake", self._api_wake, ["POST"], "养鱼：手动唤醒一次"),
            ("page/diary", self._api_diary, ["GET"], "养鱼：今日两本日记"),
            ("page/config", self._api_config, ["GET"], "养鱼：读取可编辑配置"),
            ("page/watch/delete", self._api_watch_delete, ["POST"], "养鱼：删除一条观看记录"),
            ("page/diary/delete", self._api_diary_delete, ["POST"], "养鱼：删除某一轮日记"),
            ("page/config/save", self._api_config_save, ["POST"], "养鱼：保存可编辑配置"),
        ]
        for endpoint, handler, methods, desc in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{endpoint}", handler, methods, desc
            )
        logger.info("[bili-whale] 插件页接口已注册：%s", "、".join(r[0] for r in routes))

    @filter.on_llm_request(priority=-5)
    async def _inject_browsing_memory(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """把「最近刷过什么」接进普通聊天。

        唤醒那一轮里她本来就有完整上下文，而且事件带着 _bili_whale_wake 标记，
        这里跳过，避免同一份记录注两遍。

        Args:
            event: 当前消息事件。
            req: 即将发往模型的请求。
        """
        if getattr(event, "_bili_whale_wake", False) or not self.settings.recall_memory:
            return
        try:
            persona = str(self.settings.persona_id or "").strip()
            block = self.store.memory_block(
                days=1,
                self_name=persona if persona and persona != "default" else "她",
                other_name=self.settings.owner_name or "对方",
            )
        except Exception as exc:  # 读历史失败不该影响正常对话
            logger.warning("[bili-whale] 拼浏览记忆失败：%s", exc)
            return
        if not block:
            return
        # 贴在用户这一条消息后面，而不是塞进 system_prompt：
        # system_prompt 前面隔着整段人格和规则，模型基本用不上；挨着问题才管用。
        # 用 extra_user_content_parts 而不是改 prompt —— 那样会被当成用户说的话。
        # priority=-5 比人格插件（-1）更晚执行，所以不会被它重写的 prompt 覆盖。
        parts = getattr(req, "extra_user_content_parts", None)
        if isinstance(parts, list):
            parts.append({"type": "text", "text": block})
        else:
            current = (req.prompt or "").strip()
            req.prompt = f"{current}\n\n{block}" if current else block
        logger.info("[bili-whale] 已把浏览记录贴到本轮消息后面")

    async def _api_status(self):
        """插件页顶部的全部状态。"""
        from astrbot.api.web import json_response

        state = self.store.load_state()
        plan = state.get("plan") or {}
        quota = self.guard.snapshot()
        recent = self.store.watched_recent(7)
        account = await self._account_info()
        return json_response(
            {
                "logged_in": self.settings.logged_in,
                "account": account,
                "quota": {
                    "reads": list(quota["reads"]),
                    "writes": list(quota["writes"]),
                    "shares": list(quota["shares"]),
                    "pause_left": quota["pause_left"],
                },
                "plan": Clock.describe(plan),
                "windows": self.settings.windows,
                "skip_percent": self.settings.skip_percent,
                "share": {
                    "via": ",".join(self.settings.share_channels),
                    "daily": self.settings.share_daily,
                    "min_score": self.settings.share_min_score,
                },
                "logged_in_hint": (
                    "已登录，可以写评论"
                    if self.settings.logged_in
                    else "只能读：刷、看、写日记都可以，分享到评论区需要登录"
                ),
                "ffmpeg": self.ffmpeg or "",
                "owner_umo": self.settings.owner_umo or self._fallback_umo,
                "last_run": self._last_run,
                "waking": self._waking,
                "watched": [
                    {
                        "bvid": item.get("bvid", ""),
                        "title": item.get("title", ""),
                        "at": str(item.get("at", ""))[:16].replace("T", " "),
                        "score": item.get("score"),
                        "tags": [str(tag) for tag in (item.get("tags") or [])[:3]],
                        "note": str(item.get("note") or "")[:80],
                    }
                    for item in recent[:12]
                ],
                "watched_total": len(recent),
                "comments": [
                    {
                        "bvid": item.get("bvid", ""),
                        "title": item.get("title", ""),
                        "text": item.get("comment", ""),
                        "at": str(item.get("comment_at", ""))[:16].replace("T", " "),
                    }
                    for item in recent
                    if item.get("comment")
                ][:10],
            }
        )

    async def _account_info(self) -> dict:
        """当前登录的 B站 账号，带十分钟缓存。

        状态接口会被页面轮询，每次都去问 B站 既浪费读预算也容易触发风控。
        """
        if not self.settings.logged_in:
            return {}
        fresh = self._account and time.time() - float(self._account.get("at", 0)) < 600
        if fresh:
            return self._account
        try:
            nav = await self.client.nav()
        except Exception as exc:  # 网络问题不该让整个状态接口失败
            logger.warning("[bili-whale] 查询登录账号失败：%s", exc)
            return self._account or {}
        self._account = {
            "name": str(nav.get("uname") or ""),
            "mid": str(nav.get("mid") or ""),
            "face": str(nav.get("face") or ""),
            "at": time.time(),
        }
        return self._account

    async def _api_login_qr(self):
        """生成登录二维码。"""
        from astrbot.api.web import error_response, json_response

        if self._login is not None:
            await self._login.close()
        self._login = BiliLogin(proxy=self.settings.proxy)
        try:
            data = await self._login.generate()
        except LoginError as exc:
            await self._login.close()
            self._login = None
            return error_response(str(exc))
        self._qr = data
        self._qr_at = time.time()
        return json_response(
            {"key": data["key"], "image": data["image"], "url": data["url"]}
        )

    async def _api_login_poll(self):
        """查询一次扫码状态，成功后直接写入配置并热切换客户端。"""
        from astrbot.api.web import error_response, json_response, request

        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        key = str((payload or {}).get("key") or self._qr.get("key") or "")
        if not key:
            return error_response("没有正在进行的登录，请先点扫码登录")
        if self._qr_at and time.time() - self._qr_at > 300:
            return json_response({"state": "expired", "message": "二维码过期了，请重新生成"})

        login = self._login or BiliLogin(proxy=self.settings.proxy)
        try:
            result = await login.poll(key)
        except LoginError as exc:
            await login.close()
            self._login = None
            return error_response(str(exc))

        if result["state"] != "confirmed":
            if result["state"] == "expired":
                await login.close()
                self._login = None
            return json_response(
                {"state": result["state"], "message": result.get("message", "")}
            )

        ok, name, mid = await self._apply_login(
            result["cookie"], result.get("cookie_alt")
        )
        await login.close()
        self._login = None
        self._qr = {}
        return json_response(
            {
                "state": "confirmed",
                "name": name,
                "mid": mid,
                "verified": ok,
                "logged_in": ok,
            }
        )

    async def _apply_login(
        self, cookie: dict, alt: dict | None = None
    ) -> tuple[bool, str, str]:
        """把扫码拿到的凭据写进配置并立即生效。

        B站 下发的 Cookie 可能被百分号编码过、也可能没有。先用原始值校验，
        校验不过就换解码后的那套再试一次，哪套真能登录就用哪套。

        Args:
            cookie: 轮询结果里的 cookie。
            alt: 可选，解码版本的 cookie。

        Returns:
            (是否校验通过, B站昵称, mid)。
        """
        ok, name, mid = await self._login_with(cookie)
        if not ok and alt:
            ok, name, mid = await self._login_with(alt)
        if not ok:
            logger.warning(
                "[bili-whale] 凭据已写入配置，但 nav 校验没通过 —— 可能是确认还没同步，稍后再刷一次状态看看"
            )
        return ok, name, mid

    async def _login_with(self, cookie: dict) -> tuple[bool, str, str]:
        """写入一套凭据，重建客户端并用 nav 校验。

        Args:
            cookie: 含 sessdata / bili_jct / mid / buvid3 的字典。

        Returns:
            (是否登录成功, 昵称, mid)。
        """
        if cookie.get("buvid3") and not str(self.settings.buvid3 or "").strip():
            self.config["BILI_BUVID3"] = cookie["buvid3"]
        self.config["BILI_SESSDATA"] = cookie.get("sessdata", "")
        self.config["BILI_JCT"] = cookie.get("bili_jct", "")
        try:
            self.config.save_config()
        except Exception as exc:
            logger.error("[bili-whale] 保存登录凭据失败：%s", exc)

        self.settings = Settings.from_config(self.config)
        old = self.client
        self.client = self._build_client(self.settings, self.guard)
        self.debrief = DebriefEngine(self.client, self.store.shots_dir, self.ffmpeg)
        asyncio.create_task(old.close())

        try:
            nav = await self.client.nav()
        except Exception as exc:
            logger.warning("[bili-whale] 登录后校验失败：%s", exc)
            return False, "", ""
        ok = bool(nav.get("isLogin"))
        name = str(nav.get("uname") or "")
        mid = str(nav.get("mid") or cookie.get("mid") or "")
        if ok:
            self._account = {
                "name": name,
                "mid": mid,
                "face": str(nav.get("face") or ""),
                "at": time.time(),
            }
            logger.info("[bili-whale] 已登录 B站 账号：%s (%s)", name or "未知", mid or "?")
        return ok, name, mid

    async def _api_wake(self):
        """手动唤醒一次。跑一次要几分钟，所以立刻返回、让页面轮询状态。"""
        from astrbot.api.web import error_response, json_response

        if self._waking:
            return error_response("已经有一次唤醒在跑了，等它跑完")
        if not (self.settings.owner_umo or self._fallback_umo):
            return error_response("还没有推送会话：先在聊天里给机器人发一次 /鲸鱼 状态")

        async def runner() -> None:
            try:
                await self._wake(manual=True)
            finally:
                self._waking = False

        self._waking = True
        asyncio.create_task(runner())
        return json_response({"started": True})

    async def _api_diary(self):
        """今天的两本日记。"""
        from astrbot.api.web import json_response

        return json_response(
            {
                "date": today_str(),
                "mine": self.store.read_diary("mine"),
                "yours": self.store.read_diary("yours"),
            }
        )

    async def _api_config(self):
        """页面要渲染的可编辑配置。"""
        from astrbot.api.web import json_response

        return json_response(
            {
                "values": {
                    key: self._setting_value(attr) for key, (attr, _) in CONFIG_SPEC.items()
                },
                "personas": await self._persona_options(),
                "providers": self._provider_options(),
                "sessions": await self._session_options(),
                "feed_priority_options": [
                    {"value": "following", "label": "关注的人优先"},
                    {"value": "recommend", "label": "首页推荐优先"},
                    {"value": "popular", "label": "热门榜优先"},
                ],
                "share_via_options": [
                    {"value": "dm", "label": "B站 私信"},
                    {"value": "comment", "label": "视频评论区"},
                    {"value": "local", "label": "只发到聊天窗口"},
                ],
                "mission": {
                    "owner_mid": self.settings.owner_mid,
                    "channels": list(self.settings.share_channels),
                },
            }
        )

    async def _session_options(self) -> list[dict]:
        """最近和机器人说过话的会话，让「推送会话」可以直接选而不是手填。

        AstrBot 的会话标识是 平台id:消息类型:会话id，比如 星野璃月:FriendMessage:1756758977。
        平台 id 是机器人昵称而不是适配器名，手填很容易写错，所以优先给选项。

        Returns:
            [{"value": 会话标识, "label": 展示名}, ...]，取最近 8 个。
        """
        options: list[dict] = []
        try:
            database = self.context.get_db()
            conversations = await database.get_conversations()
        except Exception as exc:
            logger.warning("[bili-whale] 读取会话列表失败：%s", exc)
            return options

        seen: set[str] = set()
        ordered = sorted(
            conversations or [],
            key=lambda item: str(getattr(item, "updated_at", "") or ""),
            reverse=True,
        )
        for conversation in ordered:
            umo = str(getattr(conversation, "user_id", "") or "")
            if not umo or umo in seen:
                continue
            seen.add(umo)
            title = str(getattr(conversation, "title", "") or "").strip()
            label = f"{title} · {umo}" if title else umo
            options.append({"value": umo, "label": label})
            if len(options) >= 8:
                break
        return options

    def _setting_value(self, attr: str) -> object:
        """把 Settings 字段转成页面能直接用的值（元组转逗号串）。"""
        value = getattr(self.settings, attr)
        return ",".join(value) if isinstance(value, tuple) else value

    def _provider_options(self) -> list[dict]:
        """可选的聊天模型。留空项表示跟随当前会话。

        Returns:
            [{"id": "", "name": "跟随当前会话的聊天模型"}, ...]
        """
        options = [{"id": "", "name": "跟随当前会话的聊天模型"}]
        getter = getattr(self.context, "get_all_providers", None)
        if getter is None:
            return options
        try:
            providers = getter() or []
        except Exception as exc:
            logger.warning("[bili-whale] 读取模型列表失败：%s", exc)
            return options
        for provider in providers:
            try:
                if not provider.provider_config.get("enable", True):
                    continue
                meta = provider.meta()
            except Exception:
                continue
            provider_id = str(getattr(meta, "id", "") or "")
            if not provider_id:
                continue
            model = str(getattr(meta, "model", "") or "")
            label = f"{provider_id}（{model}）" if model else provider_id
            options.append({"id": provider_id, "name": label})
        return options

    async def _persona_options(self) -> list[dict]:
        """AstrBot 人格库的选项。取不到就只给「不使用人格」。"""
        options = [{"id": "", "name": "不使用人格（中性设定）"}]
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return options
        try:
            personas = await manager.get_all_personas()
        except Exception as exc:
            logger.warning("[bili-whale] 读取人格列表失败：%s", exc)
            return options
        for persona in personas or []:
            persona_id = str(getattr(persona, "persona_id", "") or "").strip()
            if not persona_id:
                continue
            preview = " ".join(str(getattr(persona, "system_prompt", "") or "").split())
            label = persona_id if not preview else f"{persona_id} — {preview[:24]}"
            options.append({"id": persona_id, "name": label})
        return options

    async def _api_watch_delete(self):
        """删掉一条观看记录。"""
        from astrbot.api.web import error_response, json_response, request

        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        bvid = str((payload or {}).get("bvid") or "").strip()
        if not bvid:
            return error_response("缺少 bvid")
        removed = self.store.delete_watched(bvid)
        if not removed:
            return error_response("没找到这条记录")
        logger.info("[bili-whale] 删除观看记录 %s（%d 条）", bvid, removed)
        return json_response({"removed": removed})

    async def _api_diary_delete(self):
        """删掉日记里的某一轮。"""
        from astrbot.api.web import error_response, json_response, request

        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        who = str((payload or {}).get("who") or "").strip()
        stamp = str((payload or {}).get("time") or "").strip()
        if who not in ("mine", "yours") or not stamp:
            return error_response("参数不对")
        if not self.store.delete_diary_round(who, stamp):
            return error_response("没找到这一轮")
        logger.info("[bili-whale] 删掉日记 %s 的 %s 那一轮", who, stamp)
        return json_response({"deleted": True})

    async def _api_config_save(self):
        """保存页面上的配置，并让新值立刻生效（不用重启）。

        只接受 CONFIG_SPEC 里的键，其余静默忽略 —— 页面不该成为绕过校验的后门。
        """
        from astrbot.api.web import error_response, json_response, request

        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        updates = (payload or {}).get("updates")
        if not isinstance(updates, dict) or not updates:
            return error_response("没有要保存的内容")

        ok, message, values = self._apply_config_updates(updates)
        if not ok:
            return error_response(message)
        return json_response({"saved": True, "values": values})

    def _apply_config_updates(self, updates: dict) -> tuple[bool, str, dict]:
        """校验并应用页面提交的配置，同时让新值立刻生效。

        Args:
            updates: 配置键到新值的映射，未知键会被忽略。

        Returns:
            (是否成功, 错误信息, 全部可编辑项的当前值)。
        """
        applied: dict[str, object] = {}
        for key, raw in updates.items():
            spec = CONFIG_SPEC.get(key)
            if spec is None:
                continue
            _, kind = spec
            try:
                value = _coerce(kind, raw)
            except (TypeError, ValueError):
                return False, f"「{key}」的值不合法", {}
            if kind is int and key in CONFIG_RANGE:
                low, high = CONFIG_RANGE[key]
                value = max(low, min(high, int(value)))
            if key == "SHARE_VIA":
                picked = [
                    item.strip()
                    for item in str(value).replace("，", ",").split(",")
                    if item.strip()
                ]
                allowed = {"local", "comment", "dm"}
                if not picked or any(item not in allowed for item in picked):
                    return False, "分享渠道只能从 local / comment / dm 里选", {}
                value = ",".join(dict.fromkeys(picked))
            if key == "FEED_PRIORITY" and value not in (
                "following",
                "recommend",
                "popular",
            ):
                return False, "候选来源只能是 following / recommend / popular", {}
            applied[key] = value

        if not applied:
            return False, "没有可保存的项", {}

        # 先校验再落盘：作息窗口写错了不该把配置留在半坏状态
        if "WAKE_WINDOWS" in applied:
            try:
                parse_windows(str(applied["WAKE_WINDOWS"]), 1)
            except ValueError as exc:
                return False, f"作息窗口不合法：{exc}", {}

        old_windows = self.settings.windows
        for key, value in applied.items():
            self.config[key] = value
        try:
            self.config.save_config()
        except Exception as exc:
            logger.error("[bili-whale] 保存配置失败：%s", exc)
            return False, f"写入配置失败：{exc}", {}

        self.settings = Settings.from_config(self.config)
        self.clock = self._build_clock(self.settings)
        self.guard.read_daily = self.settings.read_daily
        self.guard.write_daily = self.settings.write_daily
        self.guard.write_cooldown = self.settings.write_cooldown
        self.guard.share_daily = self.settings.share_daily
        self.guard.share_min_score = self.settings.share_min_score
        self.client.guard = self.guard

        if self.settings.windows != old_windows:
            # 作息改了要重排计划，否则会按旧窗口一直等到明天
            state = self.store.load_state()
            state.pop("plan", None)
            state["used"] = {}
            self.store.save_state(state)

        logger.info("[bili-whale] 插件页保存配置：%s", "、".join(applied))
        return True, "", {
            key: self._setting_value(attr) for key, (attr, _) in CONFIG_SPEC.items()
        }


def _coerce(kind: type, raw: object) -> object:
    """把页面传来的值转成配置要的类型。

    Args:
        kind: 目标类型（str / int / bool）。
        raw: 页面传来的原始值。

    Returns:
        转换后的值。

    Raises:
        ValueError: 无法转换。
    """
    if kind is bool:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if kind is int:
        return int(str(raw).strip())
    return str(raw).strip()


__all__ = ["BiliWhalePlugin"]
