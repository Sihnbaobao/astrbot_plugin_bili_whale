"""B站接口客户端：节流、元信息缓存、风控熔断、WBI 签名。

三条设计约束，都是从「小号被限流」这个实际风险倒推出来的：

1. 两次请求之间强制间隔（默认 1.2 秒）—— 连打是最容易被判机器人的特征。
2. 元信息缓存 24 小时 —— 一条视频在摘要/深读/分享查重里会被反复查，不缓存就是三倍流量。
3. 撞到 412/429 或 -412 一类风控码就熔断 —— 硬顶只会把熔断时间越拖越长。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
import zlib
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# B站 WBI 签名用的固定置换表
_MIXIN_TABLE = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
)

# 这些接口层错误码和 HTTP 412 同义：被平台限流了
_BAN_CODES = {-412, -352, -799, -509, -10403}
_VIEW_CACHE_TTL = 24 * 3600
_MIXIN_TTL = 10 * 60


def _seconds(value: object) -> int:
    """把各种时长写法转成秒。

    搜索接口返回的是 "54:57" 这种字符串，热门接口返回的是整数 —— 两种都得吃。

    Args:
        value: 整数秒、纯数字字符串或 mm:ss / hh:mm:ss。

    Returns:
        秒数，解析不了算 0。
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value or "").strip()
    if not text:
        return 0
    if ":" in text:
        total = 0
        for part in text.split(":"):
            total = total * 60 + int(part or 0)
        return total
    try:
        return max(0, int(float(text)))
    except ValueError:
        return 0


def _count(value: object) -> int:
    """把 "12.3万" / "1.2亿" / "12345" 转成整数。

    Args:
        value: 原始播放量/点赞数。

    Returns:
        解析后的整数，解析不了算 0。
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value or "").strip()
    if not text:
        return 0
    unit = 1
    if text.endswith("万"):
        unit, text = 10_000, text[:-1]
    elif text.endswith("亿"):
        unit, text = 100_000_000, text[:-1]
    try:
        return max(0, int(float(text) * unit))
    except ValueError:
        return 0


class BiliError(Exception):
    """B站接口不可用。消息是给人看的，可以直接进日志或回复。"""


class BiliClient:
    """一个账号的 B站读写入口。

    Args:
        sessdata: Cookie SESSDATA，为空时只能做匿名读。
        bili_jct: Cookie bili_jct，写操作需要的 csrf。
        buvid3: Cookie buvid3，缺少时很容易触发风控。
        cache_path: 元信息缓存文件。
        guard: 预算与熔断控制器。
        proxy: 可选代理地址。
        throttle_ms: 两次请求的最小间隔（毫秒）。
    """

    def __init__(
        self,
        *,
        sessdata: str,
        bili_jct: str,
        buvid3: str,
        cache_path: Path,
        guard: Any,
        proxy: str = "",
        throttle_ms: int = 1200,
    ) -> None:
        self.sessdata = (sessdata or "").strip()
        self.bili_jct = (bili_jct or "").strip()
        self.buvid3 = (buvid3 or "").strip()
        self.cache_path = Path(cache_path)
        self.guard = guard
        self.proxy = (proxy or "").strip()
        self.throttle = max(0, int(throttle_ms)) / 1000
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._mixin: str = ""
        self._mixin_at: float = 0.0
        self._buvid_tried = False

    @property
    def logged_in(self) -> bool:
        """是否配置了可用于写操作的登录态。"""
        return bool(self.sessdata and self.bili_jct)

    def cookie_header(self) -> str:
        """拼 Cookie 头，空项自动省略。"""
        pairs = [
            ("SESSDATA", self.sessdata),
            ("bili_jct", self.bili_jct),
            ("buvid3", self.buvid3),
        ]
        return "; ".join(f"{k}={v}" for k, v in pairs if v)

    async def close(self) -> None:
        """关闭底层连接池。"""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _bootstrap_buvid(self) -> None:
        """没有配 buvid3 时自动取一个并落盘。

        B站 对没有设备号的请求会明显更容易判成机器 —— 数据中心 IP 上尤其如此。
        这个接口不需要登录，取到的值长期有效，所以只需要成功一次。
        注意：这里直接发裸请求、不走 _request，避免签名流程递归。
        """
        self._buvid_tried = True
        store = self._cache_read()
        cached = str(store.get("__buvid3", "") or "")
        if cached:
            self.buvid3 = cached
            return
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.bilibili.com/x/frontend/finger/spi",
                    headers={"user-agent": UA, "referer": "https://www.bilibili.com/"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    payload = await resp.json(content_type=None)
            value = str(((payload or {}).get("data") or {}).get("b_3") or "")
            if value:
                self.buvid3 = value
                store["__buvid3"] = value
                self._cache_write(store)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            pass

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "user-agent": UA,
                    "referer": "https://www.bilibili.com/",
                    "origin": "https://www.bilibili.com",
                    "accept": "application/json, text/plain, */*",
                    "accept-language": "zh-CN,zh;q=0.9",
                },
                timeout=aiohttp.ClientTimeout(total=30),
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        return self._session

    async def _throttled(self) -> None:
        """确保两次请求之间至少隔 throttle 秒。"""
        async with self._lock:
            gap = time.monotonic() - self._last_request
            if gap < self.throttle:
                await asyncio.sleep(self.throttle - gap)
            self._last_request = time.monotonic()

    def _cache_read(self) -> dict:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _cache_write(self, store: dict) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(store, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    async def _sign(self, params: dict) -> str:
        """生成 WBI 签名查询串。

        Args:
            params: 待签名的业务参数。

        Returns:
            形如 a=1&wts=...&w_rid=... 的查询串。

        Raises:
            BiliError: 拿不到签名密钥。
        """
        mixin = await self._mixin_key()
        parts: list[str] = []
        for key, value in sorted({**params, "wts": int(time.time())}.items()):
            cleaned = re.sub(r"[!'()*]", "", str(value))
            parts.append(f"{quote(str(key), safe='')}={quote(cleaned, safe='')}")
        query = "&".join(parts)
        w_rid = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
        return f"{query}&w_rid={w_rid}"

    async def _mixin_key(self) -> str:
        if self._mixin and time.time() - self._mixin_at < _MIXIN_TTL:
            return self._mixin
        nav = await self._request(
            "GET", "https://api.bilibili.com/x/web-interface/nav", use_cache=False
        )
        img = (nav.get("wbi_img") or {}).get("img_url", "")
        sub = (nav.get("wbi_img") or {}).get("sub_url", "")
        raw = img.rsplit("/", 1)[-1].split(".")[0] + sub.rsplit("/", 1)[-1].split(".")[0]
        if len(raw) < 64:
            raise BiliError("拿不到 WBI 签名密钥，登录态可能已失效")
        self._mixin = "".join(raw[i] for i in _MIXIN_TABLE)[:32]
        self._mixin_at = time.time()
        return self._mixin

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        sign: bool = False,
        use_cache: bool = False,
        referer: str = "",
        allow_codes: set[int] | None = None,
    ) -> dict:
        """发一次请求并统一处理风控、预算和缓存。

        Args:
            method: GET 或 POST。
            url: 完整地址。
            params: 查询参数。
            data: 表单参数。
            sign: 是否走 WBI 签名。
            use_cache: 是否允许命中元信息缓存。
            referer: 覆盖默认 Referer。
            allow_codes: 除 0 之外允许的返回码（如 nav 匿名时的 -101）。

        Returns:
            接口返回的 data 字段；非 0 返回码抛 BiliError。

        Raises:
            BiliError: 网络、风控、调用次数或接口返回码异常。
        """
        if not self.buvid3 and not self._buvid_tried:
            await self._bootstrap_buvid()

        cache_key = ""
        if use_cache:
            cache_key = method + url + json.dumps(params or {}, sort_keys=True)
            store = self._cache_read()
            hit = store.get(cache_key)
            if isinstance(hit, dict) and time.time() - float(hit.get("at", 0)) < _VIEW_CACHE_TTL:
                return hit.get("data", {})

        ok, why = self.guard.can_read(1)
        if not ok:
            raise BiliError(why)
        self.guard.note_read(1)

        if sign:
            url = f"{url}?{await self._sign(params or {})}"
            params = None

        session = await self._ensure_session()
        headers = {"referer": referer} if referer else {}
        cookie = self.cookie_header()
        if cookie:
            headers["cookie"] = cookie
        await self._throttled()

        try:
            async with session.request(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                proxy=self.proxy or None,
            ) as resp:
                if resp.status in (412, 429):
                    self.guard.pause(300, f"HTTP {resp.status}")
                    raise BiliError(f"B站返回 HTTP {resp.status}（风控），已暂停 5 分钟")
                if resp.status != 200:
                    raise BiliError(f"B站返回 HTTP {resp.status}")
                payload = await resp.json(content_type=None)
        except asyncio.TimeoutError as exc:
            raise BiliError("B站请求超时") from exc
        except aiohttp.ClientError as exc:
            raise BiliError(f"B站请求失败：{exc}") from exc

        if not isinstance(payload, dict):
            raise BiliError("B站返回了非 JSON 内容，可能触发了风控")

        code = int(payload.get("code", -1))
        if code in _BAN_CODES:
            self.guard.pause(300, f"code={code}")
            raise BiliError(f"B站风控 code={code}，已暂停 5 分钟")
        if code != 0 and code not in (allow_codes or set()):
            raise BiliError(f"B站接口错误 code={code}：{payload.get('message', '')}")

        body = payload.get("data") or {}
        if use_cache and cache_key:
            store = self._cache_read()
            store[cache_key] = {"at": time.time(), "data": body}
            if len(store) > 400:
                stale = sorted(store.items(), key=lambda kv: float(kv[1].get("at", 0)))[:100]
                for key, _ in stale:
                    store.pop(key, None)
            self._cache_write(store)
        return body

    # ── 常用接口 ──

    async def nav(self) -> dict:
        """当前登录账号信息。

        匿名访问会返回 code=-101，但 wbi_img 仍然在 data 里 —— 签名要靠它，
        所以这个码必须放行，不能当成错误。
        """
        return await self._request(
            "GET",
            "https://api.bilibili.com/x/web-interface/nav",
            allow_codes={0, -101},
        )

    async def view(self, bvid: str) -> dict:
        """视频元信息。

        优先走 WBI 签名的 wbi/view —— 未签名的旧端点在数据中心 IP 上更容易返回 412，
        失败时退回旧端点再试一次。

        Args:
            bvid: 视频号。

        Returns:
            view 接口的 data，含 aid/cid/title/owner/stat/pages。

        Raises:
            BiliError: 两个端点都拿不到。
        """
        referer = f"https://www.bilibili.com/video/{bvid}/"
        try:
            return await self._request(
                "GET",
                "https://api.bilibili.com/x/web-interface/wbi/view",
                params={"bvid": bvid},
                sign=True,
                use_cache=True,
                referer=referer,
            )
        except BiliError:
            return await self._request(
                "GET",
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bvid},
                use_cache=True,
                referer=referer,
            )

    async def tags(self, bvid: str) -> list[str]:
        """视频的官方标签。

        这等于 B站 已经替我们做好的「标签分析」：UP 主和观众一起打的，
        比让模型重新生成更准，也不额外花钱。

        Args:
            bvid: 视频号。

        Returns:
            标签名列表，取不到时返回空列表。
        """
        try:
            data = await self._request(
                "GET",
                "https://api.bilibili.com/x/tag/archive/tags",
                params={"bvid": bvid},
                use_cache=True,
                referer=f"https://www.bilibili.com/video/{bvid}/",
            )
        except BiliError:
            return []
        out: list[str] = []
        for item in data or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("tag_name") or "").strip()
            if name and name not in out:
                out.append(name)
        return out[:12]

    async def play_url(self, bvid: str, cid: int) -> tuple[str, int]:
        """取 mp4 直链。

        用 platform=pc + qn=80 才能拿到 720p；html5 档位会被压到 360p。

        Args:
            bvid: 视频号。
            cid: 分 P 的 cid。

        Returns:
            (直链, 实际画质代码)，取不到直链时抛 BiliError。
        """
        data = await self._request(
            "GET",
            "https://api.bilibili.com/x/player/playurl",
            params={
                "bvid": bvid,
                "cid": cid,
                "qn": 80,
                "fnval": 1,
                "platform": "pc",
                "high_quality": 1,
            },
            referer=f"https://www.bilibili.com/video/{bvid}/",
        )
        urls = (data.get("durl") or [{}])[0].get("url")
        if not urls:
            raise BiliError("没有视频直链（这条可能要大会员或只有 dash 流）")
        return urls, int(data.get("quality") or 0)

    async def subtitle(self, bvid: str, cid: int) -> tuple[str, list[dict]]:
        """拉官方字幕。

        subtitle_url 里的 auth_key 是分钟级有效的，所以拿到就立刻下载。

        Args:
            bvid: 视频号。
            cid: 分 P 的 cid。

        Returns:
            (轨道语言, 字幕行列表[from/to/content])，没有字幕时返回空列表。
        """
        try:
            data = await self._request(
                "GET",
                "https://api.bilibili.com/x/player/wbi/v2",
                params={"bvid": bvid, "cid": cid},
                sign=True,
                referer=f"https://www.bilibili.com/video/{bvid}/",
            )
        except BiliError:
            return "", []
        tracks = ((data.get("subtitle") or {}).get("subtitles")) or []
        if not tracks:
            return "", []
        track = next((t for t in tracks if t.get("lan") == "ai-zh"), None)
        track = track or next((t for t in tracks if t.get("lan") == "zh-CN"), None)
        track = track or tracks[0]
        url = str(track.get("subtitle_url", ""))
        if not url:
            return "", []
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = "https://api.bilibili.com" + url
        session = await self._ensure_session()
        try:
            async with session.get(url, proxy=self.proxy or None) as resp:
                if resp.status != 200:
                    return str(track.get("lan", "")), []
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return str(track.get("lan", "")), []
        body = payload.get("body") if isinstance(payload, dict) else None
        if not isinstance(body, list):
            return str(track.get("lan", "")), []
        lines = [
            {
                "from": float(item.get("from", 0)),
                "to": float(item.get("to", 0)),
                "content": str(item.get("content", "")).strip(),
            }
            for item in body
            if isinstance(item, dict) and str(item.get("content", "")).strip()
        ]
        return str(track.get("lan", "")), lines

    async def danmaku(self, cid: int) -> list[dict]:
        """拉视频弹幕。

        该接口返回的是不带 content-encoding 头的 deflate 流，需要手动解压。

        Args:
            cid: 分 P 的 cid。

        Returns:
            弹幕列表，每项含 t(秒) 与 text。
        """
        session = await self._ensure_session()
        ok, why = self.guard.can_read(1)
        if not ok:
            raise BiliError(why)
        self.guard.note_read(1)
        await self._throttled()
        try:
            async with session.get(
                f"https://comment.bilibili.com/{cid}.xml",
                proxy=self.proxy or None,
            ) as resp:
                if resp.status != 200:
                    return []
                raw = await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return []

        text = raw.decode("utf-8", errors="ignore")
        if "<d " not in text:
            for wbits in (-15, 15, 47):
                try:
                    text = zlib.decompress(raw, wbits).decode("utf-8", errors="ignore")
                    break
                except zlib.error:
                    continue
        out: list[dict] = []
        for match in re.finditer(r'<d p="([^"]*)">([^<]*)</d>', text):
            try:
                t = float(match.group(1).split(",")[0])
            except (ValueError, IndexError):
                continue
            body = (
                match.group(2)
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"')
            )
            out.append({"t": t, "text": body})
        return out

    async def comments(self, aid: int, limit: int = 8) -> list[dict]:
        """取热门评论。

        Args:
            aid: 视频 aid。
            limit: 条数。

        Returns:
            每项含 uname/like/message。
        """
        data = await self._request(
            "GET",
            "https://api.bilibili.com/x/v2/reply",
            params={"type": 1, "oid": aid, "sort": 2, "ps": limit, "pn": 1},
        )
        out: list[dict] = []
        for item in data.get("replies") or []:
            content = (item.get("content") or {}).get("message", "")
            out.append(
                {
                    "uname": (item.get("member") or {}).get("uname", "?"),
                    "like": int(item.get("like") or 0),
                    "message": re.sub(r"\s+", " ", str(content))[:200],
                }
            )
        return out

    async def feed(
        self, source: str, *, keyword: str = "", limit: int = 10
    ) -> list[dict]:
        """拉候选视频列表。

        Args:
            source: popular（热门）/ ranking（排行榜）/ search（按关键词搜）/ following（关注动态）。
            keyword: search 源的关键词。
            limit: 返回条数上限。

        Returns:
            规整后的视频摘要列表，字段可能不全（缺的由 view 补）。
        """
        if source == "recommend":
            # 首页推荐流：这是 B站 给「这个人」排的，最接近真人刷首页。
            # 需要登录态才有个人化结果，匿名只能拿到泛化内容。
            if not self.logged_in:
                raise BiliError("首页推荐需要登录态，先用 popular")
            data = await self._request(
                "GET",
                "https://api.bilibili.com/x/web-interface/wbi/index/top/feed/rcmd",
                params={
                    "ps": max(limit, 12),
                    "fresh_type": 4,
                    "fresh_idx": 1,
                    "fresh_idx_1h": 1,
                    "feed_version": "V8",
                    "homepage_ver": 1,
                    "brush": 0,
                },
                sign=True,
            )
            out: list[dict] = []
            for item in data.get("item") or []:
                if not isinstance(item, dict):
                    continue
                bvid = str(item.get("bvid") or item.get("id") or "")
                if not bvid.startswith("BV"):
                    continue  # 直播、广告等非视频卡片
                owner = item.get("owner") or {}
                stat = item.get("stat") or {}
                out.append(
                    {
                        "bvid": bvid,
                        "aid": int(item.get("aid") or 0),
                        "title": str(item.get("title") or ""),
                        "up": str(owner.get("name") or ""),
                        "duration": int(item.get("duration") or 0),
                        "views": int(stat.get("view") or 0),
                        "likes": int(stat.get("like") or 0),
                        "pubdate": int(item.get("pubdate") or 0),
                        "desc": re.sub(r"\s+", " ", str(item.get("desc") or ""))[:200],
                        "tname": str(item.get("tname") or ""),
                    }
                )
            return out[:limit]

        if source == "ranking":
            data = await self._request(
                "GET",
                "https://api.bilibili.com/x/web-interface/ranking/v2",
                params={"rid": 0, "type": "all"},
            )
            items = data.get("list") or []
            return [self._slim(item) for item in items[:limit]]

        if source == "search":
            if not keyword:
                raise BiliError("search 源需要 keyword")
            if not self.logged_in:
                # B站 的搜索接口对匿名请求直接返回 -101，与其把错误码抛给模型，
                # 不如在本地就说清楚该换哪一路
                raise BiliError("搜索需要登录态（配好 Cookie 才能用），先用 popular 或 ranking")
            data = await self._request(
                "GET",
                "https://api.bilibili.com/x/web-interface/wbi/search/type",
                params={"search_type": "video", "keyword": keyword, "page": 1},
                sign=True,
            )
            items = (data.get("result") or []) or []
            return [
                self._slim({**item, "bvid": item.get("bvid")})
                for item in items
                if isinstance(item, dict) and item.get("bvid")
            ][:limit]

        if source == "following":
            if not self.logged_in:
                raise BiliError("关注动态需要登录态，先用 popular 或 ranking")
            data = await self._request(
                "GET",
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all",
                params={"type": "video"},
            )
            out: list[dict] = []
            for item in data.get("items") or []:
                modules = item.get("modules") or {}
                major = (modules.get("module_dynamic") or {}).get("major") or {}
                archive = major.get("archive") or {}
                if not archive.get("bvid"):
                    continue
                author = modules.get("module_author") or {}
                stat = archive.get("stat") or {}
                out.append(
                    {
                        "bvid": archive["bvid"],
                        "aid": int(archive.get("aid") or 0),
                        "title": str(archive.get("title") or ""),
                        "up": str(author.get("name") or ""),
                        "duration": _seconds(archive.get("duration_text")),
                        "views": _count(stat.get("play")),
                        "likes": 0,
                        "pubdate": int(author.get("pub_ts") or 0),
                        "desc": re.sub(r"s+", " ", str(archive.get("desc") or ""))[:200],
                        "tname": "",
                    }
                )
            return out[:limit]

        data = await self._request(
            "GET",
            "https://api.bilibili.com/x/web-interface/popular",
            params={"ps": max(limit, 20), "pn": 1},
        )
        items = data.get("list") or []
        return [self._slim(item) for item in items[:limit]]

    @staticmethod
    def _slim(item: dict) -> dict:
        """把不同源的视频对象压成同一套精简字段。

        各接口的字段类型并不统一：热门给整数，搜索给 "54:57" 和 "12.3万"。
        统一在这里兜住，别让类型差异炸到调用方。
        """
        stat = item.get("stat") or {}
        owner = item.get("owner") or {}
        title = re.sub(r"<[^>]+>", "", str(item.get("title", "")))
        return {
            "bvid": str(item.get("bvid") or ""),
            "aid": _count(item.get("aid")),
            "title": title.replace("&quot;", '"').strip(),
            "up": str(owner.get("name") or item.get("author") or ""),
            "duration": _seconds(item.get("duration")),
            "views": _count(stat.get("view") or item.get("play")),
            "likes": _count(stat.get("like") or item.get("like")),
            "pubdate": _count(item.get("pubdate") or item.get("senddate")),
            "desc": re.sub(r"\s+", " ", str(item.get("desc") or item.get("description") or ""))[:200],
            "tname": str(item.get("tname") or ""),
        }

    async def like(self, bvid: str) -> None:
        """给一条视频点赞。

        Args:
            bvid: 视频号。

        Raises:
            BiliError: 未登录或接口失败。
        """
        if not self.logged_in:
            raise BiliError("没有登录态，点不了赞")
        await self._request(
            "POST",
            "https://api.bilibili.com/x/web-interface/archive/like",
            data={"bvid": bvid, "like": 1, "csrf": self.bili_jct},
            referer=f"https://www.bilibili.com/video/{bvid}/",
        )

    async def relation(self, fid: str) -> dict:
        """和某个用户的关系。

        Args:
            fid: 对方 UID。

        Returns:
            relation 接口的 data；attribute 为 6 表示互相关注。

        Raises:
            BiliError: 接口调用失败。
        """
        return await self._request(
            "GET",
            "https://api.bilibili.com/x/relation",
            params={"fid": str(fid)},
            referer=f"https://space.bilibili.com/{fid}",
        )

    async def send_dm(self, receiver_mid: str, text: str) -> str:
        """给指定用户发一条 B站 私信。

        只发给主人是刻意的：私信是 B站 风控最凶的接口，能力越窄越安全。

        Args:
            receiver_mid: 收信人的 B站 UID。
            text: 正文。

        Returns:
            平台返回的提示。

        Raises:
            BiliError: 未登录、未互关或发送失败。
        """
        if not self.logged_in:
            raise BiliError("没有登录态，发不了私信")
        receiver = str(receiver_mid or "").strip()
        if not receiver:
            raise BiliError("没有配置收信人的 B站 UID（OWNER_MID）")

        me = await self.nav()
        sender = str(me.get("mid") or "")
        if not sender:
            raise BiliError("拿不到自己的 UID，登录态可能已失效")

        relation = await self.relation(receiver)
        if int(relation.get("attribute", -1)) != 6:
            raise BiliError("和对方不是互相关注，B站 不允许发私信")

        body = {
            "msg[sender_uid]": sender,
            "msg[receiver_id]": receiver,
            "msg[receiver_type]": "1",
            "msg[msg_type]": "1",
            "msg[msg_status]": "0",
            "msg[timestamp]": str(int(time.time())),
            # 同一账号永远用同一个设备号：频繁换设备号是私信被封的典型特征
            "msg[dev_id]": str(uuid.uuid5(uuid.NAMESPACE_URL, f"bili-whale:{sender}")),
            "msg[content]": json.dumps({"content": text}, ensure_ascii=False),
            "csrf": self.bili_jct,
        }
        data = await self._request(
            "POST",
            "https://api.vc.bilibili.com/web_im/v1/web_im/send_msg",
            data=body,
            referer="https://message.bilibili.com/",
        )
        if not isinstance(data, dict) or not data.get("msg_key"):
            raise BiliError("私信没发出去：平台没有返回消息 ID")
        return "已送达"

    async def post_comment(self, aid: int, message: str, bvid: str = "") -> dict:
        """在视频下发一条评论。

        尽量贴近浏览器的发法：先打开一次视频页拿到会话 cookie（b_nut/buvid4 之类），
        再带着完整的头和 cookie 发评论。裸调接口容易被单独判定成机器。

        Args:
            aid: 视频 aid。
            message: 正文，纯文本。
            bvid: 视频号，用于预热会话；缺省则跳过预热。

        Returns:
            接口返回的 data。

        Raises:
            BiliError: 未登录或发送失败。
        """
        if not self.logged_in:
            raise BiliError("没有登录态，发不了评论")

        session = await self._ensure_session()
        cookies = self.cookie_header()
        referer = f"https://www.bilibili.com/video/{bvid}/" if bvid else "https://www.bilibili.com/"

        if bvid:
            # 先像浏览器那样把视频页打开一次，让 B站 下发会话 cookie
            try:
                await self._throttled()
                async with session.get(
                    referer,
                    headers={"user-agent": UA, "cookie": cookies, "referer": "https://www.bilibili.com/"},
                    proxy=self.proxy or None,
                ) as page:
                    extra = {key: morsel.value for key, morsel in page.cookies.items()}
                    await page.read()
                if extra:
                    cookies = cookies + "; " + "; ".join(f"{k}={v}" for k, v in extra.items())
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass

        headers = {
            "user-agent": UA,
            "cookie": cookies,
            "referer": referer,
            "origin": "https://www.bilibili.com",
            "accept": "application/json, text/plain, */*",
            "x-requested-with": "XMLHttpRequest",
        }
        data = {
            "type": 1,
            "oid": aid,
            "message": message,
            "plat": 1,
            "csrf": self.bili_jct,
            "gaia_source": "main_web",
            "scene": "main",
            "from_spmid": "",
            "root": 0,
            "parent": 0,
        }

        await self._throttled()
        try:
            async with session.post(
                "https://api.bilibili.com/x/v2/reply/add",
                data=data,
                headers=headers,
                proxy=self.proxy or None,
            ) as resp:
                if resp.status in (412, 429):
                    self.guard.pause(300, f"HTTP {resp.status}")
                    raise BiliError(f"B站返回 HTTP {resp.status}（风控），已暂停 5 分钟")
                payload = await resp.json(content_type=None)
        except asyncio.TimeoutError as exc:
            raise BiliError("评论请求超时") from exc
        except aiohttp.ClientError as exc:
            raise BiliError(f"评论请求失败：{exc}") from exc

        code = int((payload or {}).get("code", -1))
        if code != 0:
            reason = (payload or {}).get("message", "")
            raise BiliError(f"评论没发出去：{reason}（code={code}）")
        return payload.get("data") or {}
