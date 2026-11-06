"""扫码登录：从跳转链接里取凭据。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.login import (
    STATE_EXPIRED,
    STATE_OK,
    STATE_SCANNED,
    STATE_WAITING,
    parse_login_url,
    parse_set_cookies,
)


def test_parse_typical_crossdomain_url():
    url = (
        "https://passport.biligame.com/crossDomain?"
        "DedeUserID=123456&DedeUserID__ckMd5=abcdef&Expires=1999999999"
        "&SESSDATA=abc%2C1799999999%2Cxyz%2A11&bili_jct=deadbeef&gourl=https%3A%2F%2Fwww.bilibili.com"
    )
    cookie = parse_login_url(url)
    assert cookie["sessdata"] == "abc,1799999999,xyz*11"
    assert cookie["bili_jct"] == "deadbeef"
    assert cookie["mid"] == "123456"


def test_parse_missing_fields_returns_empty():
    cookie = parse_login_url("https://example.com/nothing")
    assert cookie["sessdata"] == ""
    assert cookie["bili_jct"] == ""


def test_parse_empty_url():
    assert parse_login_url("")["sessdata"] == ""


def test_parse_set_cookies_keeps_raw_value():
    """Set-Cookie 里的值要原样保留 —— 浏览器也是原样回传的。"""
    headers = [
        "SESSDATA=abc%2C1799999999%2Cxyz%2A11; Path=/; HttpOnly",
        "bili_jct=deadbeef; Path=/",
        "DedeUserID=123456; Path=/",
    ]
    jar = parse_set_cookies(headers)
    assert jar["SESSDATA"] == "abc%2C1799999999%2Cxyz%2A11"
    assert jar["bili_jct"] == "deadbeef"
    assert jar["DedeUserID"] == "123456"


def test_parse_set_cookies_ignores_attributes():
    jar = parse_set_cookies(["buvid3=abc; Path=/; Domain=.bilibili.com; Max-Age=999"])
    assert jar == {"buvid3": "abc"}


def test_parse_set_cookies_tolerates_garbage():
    assert parse_set_cookies(["not a cookie", ""]) == {}
    assert parse_set_cookies([]) == {}


def test_state_codes_are_stable():
    # 这四个状态值是页面轮询时的契约，改名会让前端静默停在"未扫码"
    assert (STATE_OK, STATE_WAITING, STATE_SCANNED, STATE_EXPIRED) == (
        "confirmed",
        "waiting",
        "scanned",
        "expired",
    )
