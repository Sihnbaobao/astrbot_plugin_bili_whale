"""配置解析：缺失取默认、列表归一、空串当没填。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import Settings, _as_list


def test_defaults_when_empty():
    settings = Settings.from_config({})
    assert settings.windows == "20:00-24:00"
    assert settings.skip_percent == 30
    assert settings.logged_in is False


def test_blank_string_falls_back_to_default():
    settings = Settings.from_config({"SKIP_PERCENT": "", "OWNER_NAME": ""})
    assert settings.skip_percent == 30
    assert settings.owner_name == "主人"


def test_logged_in_requires_both_cookies():
    assert Settings.from_config({"BILI_SESSDATA": "x"}).logged_in is False
    assert Settings.from_config({"BILI_SESSDATA": "x", "BILI_JCT": "y"}).logged_in is True


def test_share_channels_parsing():
    assert Settings.from_config({}).share_channels == ("local",)
    assert Settings.from_config({"SHARE_VIA": "dm,local"}).share_channels == (
        "dm",
        "local",
    )
    # 不认识的渠道要丢掉，但不能把整串清空
    assert Settings.from_config({"SHARE_VIA": "dm,telepathy"}).share_channels == ("dm",)
    assert Settings.from_config({"SHARE_VIA": "乱写"}).share_channels == ("local",)


def test_list_field_normalisation():
    assert _as_list("猫, 狗，鱼") == ["猫", "狗", "鱼"]
    assert _as_list(["a", " b "]) == ["a", "b"]
    assert _as_list("") == []


def test_typed_values_are_coerced():
    settings = Settings.from_config(
        {"SKIP_PERCENT": "45", "ENABLE_FRAMES": False, "PREFER_LIKE": "猫"}
    )
    assert settings.skip_percent == 45
    assert settings.frames is False
    assert settings.likes == ("猫",)
