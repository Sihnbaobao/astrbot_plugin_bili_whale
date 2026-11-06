"""B站扫码登录。

手抄 Cookie 是最容易出错的一步：SESSDATA 是 HttpOnly，在控制台敲 document.cookie 读不到；
抄错一个空格就变成"填了但还是未登录"。扫码登录把这一步变成扫一下。

流程（和 B站 网页一致）：
    1. generate  取 qrcode_key + 二维码地址
    2. 本地把地址画成二维码
    3. poll      每两秒问一次状态：86101 未扫码 / 86090 已扫码待确认 / 0 成功 / 86038 过期
    4. 成功时跳转链接的查询串里就带着 SESSDATA、bili_jct、DedeUserID
"""

from __future__ import annotations

import base64
import http.cookies
import io
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp

from .bili import UA

_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

# 轮询返回的 data.code
STATE_OK = "confirmed"
STATE_WAITING = "waiting"
STATE_SCANNED = "scanned"
STATE_EXPIRED = "expired"

_CODE_MAP = {
    0: STATE_OK,
    86101: STATE_WAITING,
    86090: STATE_SCANNED,
    86038: STATE_EXPIRED,
}


class LoginError(Exception):
    """登录流程本身出错（不是"还没扫码"）。"""


def parse_set_cookies(headers: list[str]) -> dict[str, str]:
    """从响应头里取出 B站 下发的 Cookie（保留原始编码）。

    B站 扫码确认后，凭据是通过轮询响应的 Set-Cookie 下发的，不在返回的跳转链接里。
    链接只是给浏览器做跨域跳转用的，很多情况下里面根本没有 SESSDATA。

    Args:
        headers: 响应里所有 Set-Cookie 头。

    Returns:
        cookie 名到原始值的映射（不 unquote —— 浏览器也是原样回传的）。
    """
    out: dict[str, str] = {}
    for raw in headers:
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            continue
        for name, morsel in jar.items():
            out[name] = morsel.value
    return out


def parse_login_url(url: str) -> dict:
    """从登录成功后的跳转链接里取出 Cookie 值。

    成功跳转长这样：
        https://passport.biligame.com/crossDomain?DedeUserID=123&DedeUserID__ckMd5=..&
        Expires=..&SESSDATA=abc%2Cdef&bili_jct=xyz&gourl=https%3A%2F%2Fwww.bilibili.com

    Args:
        url: 轮询返回的 data.url。

    Returns:
        含 sessdata / bili_jct / mid 的字典，取不到的键为空串。
    """
    query = parse_qs(urlparse(url or "").query)
    if not query and "SESSDATA=" in (url or ""):
        # 少数情况下返回的是不带 scheme 的查询串
        query = parse_qs(url.split("?", 1)[-1])

    def pick(key: str) -> str:
        values = query.get(key) or []
        return unquote(values[0]).strip() if values else ""

    return {
        "sessdata": pick("SESSDATA"),
        "bili_jct": pick("bili_jct"),
        "mid": pick("DedeUserID"),
        "refresh_token": pick("refresh_token"),
    }


class BiliLogin:
    """扫码登录客户端。

    Args:
        proxy: 可选代理地址。
    """

    def __init__(self, proxy: str = "") -> None:
        self.proxy = (proxy or "").strip()
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        """关掉这次扫码用的会话。"""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """整个扫码流程复用同一个会话。

        生成二维码时 B站 会下发 buvid 一类的基础 Cookie，轮询时带上它们才跟浏览器一致；
        每次新建会话等于每两秒换一个"设备"，容易被判异常。
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "user-agent": UA,
                    "referer": "https://www.bilibili.com/",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def generate(self) -> dict:
        """生成一张二维码。

        Returns:
            含 key / url / image（PNG data URL）的字典。画不出图时 image 为空串，
            前端可以退回显示 url。

        Raises:
            LoginError: 取不到二维码地址。
        """
        payload, _ = await self._get(_GENERATE)
        data = payload.get("data") or {}
        url = str(data.get("url") or "")
        key = str(data.get("qrcode_key") or "")
        if not url or not key:
            raise LoginError(f"取二维码失败：{payload.get('message') or payload.get('code')}")
        return {"key": key, "url": url, "image": _render_qr(url)}

    async def poll(self, key: str) -> dict:
        """查询一次扫码状态。

        Args:
            key: generate 返回的 qrcode_key。

        Returns:
            含 state / message / cookie 的字典。state 为 confirmed 时 cookie 才有内容；
            如果 Cookie 值可能被百分号编码过，还会带一个解码后的 cookie_alt 供调用方兜底。

        Raises:
            LoginError: 接口调用失败，或扫码成功却没拿到凭据。
        """
        if not key:
            raise LoginError("缺少 qrcode_key")
        payload, headers = await self._get(_POLL, params={"qrcode_key": key})
        data = payload.get("data") or {}
        code = int(data.get("code", -1))
        state = _CODE_MAP.get(code, STATE_WAITING)
        result = {
            "state": state,
            "message": str(data.get("message") or ""),
            "cookie": {},
        }
        if state != STATE_OK:
            return result

        jar = parse_set_cookies(headers)
        fallback = parse_login_url(str(data.get("url") or ""))
        sessdata = jar.get("SESSDATA") or fallback["sessdata"]
        bili_jct = jar.get("bili_jct") or fallback["bili_jct"]
        if not sessdata or not bili_jct:
            names = "、".join(sorted(jar)) or "一个都没有"
            raise LoginError(
                f"扫码成功了，但 B站 没有下发登录凭据（收到的 Cookie：{names}）。"
                "重新生成二维码再扫一次；如果反复出现，把这句话发给我。"
            )

        cookie = {
            "sessdata": sessdata,
            "bili_jct": bili_jct,
            "mid": jar.get("DedeUserID") or fallback["mid"],
            "buvid3": jar.get("buvid3", ""),
        }
        result["cookie"] = cookie
        result["refresh_token"] = str(data.get("refresh_token") or "")

        # 不同客户端下发的值可能是百分号编码过也可能是解码过的，两种都留着，
        # 由调用方用 nav 校验后决定用哪一套
        decoded = dict(cookie)
        decoded["sessdata"] = unquote(cookie["sessdata"])
        decoded["bili_jct"] = unquote(cookie["bili_jct"])
        if decoded["sessdata"] != cookie["sessdata"]:
            result["cookie_alt"] = decoded
        return result

    async def _get(self, url: str, params: dict | None = None) -> tuple[dict, list[str]]:
        """发一次登录相关请求。

        Args:
            url: 接口地址。
            params: 查询参数。

        Returns:
            (JSON 负载, 所有 Set-Cookie 头)。

        Raises:
            LoginError: 网络或接口异常。
        """
        try:
            session = await self._ensure_session()
            async with session.get(
                url, params=params, proxy=self.proxy or None
            ) as resp:
                headers = resp.headers.getall("Set-Cookie", [])
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, aiohttp.ContentTypeError) as exc:
            raise LoginError(f"连不上 B站 登录服务：{exc}") from exc
        if not isinstance(payload, dict):
            raise LoginError("B站 登录服务返回了非 JSON 内容")
        if int(payload.get("code", -1)) != 0 and "data" not in payload:
            raise LoginError(f"B站 登录接口错误：{payload.get('message')}")
        return payload, headers


def _render_qr(text: str) -> str:
    """把文本画成二维码，返回 PNG 的 data URL。没装 qrcode 库时返回空串。

    Args:
        text: 要编码的二维码内容。

    Returns:
        data:image/png;base64,... 或空串。
    """
    try:
        import qrcode
    except ImportError:
        return ""
    image = qrcode.make(text)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"
