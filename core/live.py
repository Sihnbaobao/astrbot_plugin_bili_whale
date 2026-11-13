"""直播间的事：拿到播放地址、抓一段音频、本地转成文字。

音频只在临时目录里活几十秒，转完就删，不留在磁盘上。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from astrbot.api import logger

WORKER = Path(__file__).with_name("asr_worker.py")


def asr_ready(model_dir: Path) -> tuple[bool, str]:
    """检查语音识别能不能用。

    Args:
        model_dir: SenseVoice 模型目录。

    Returns:
        (能不能用, 原因)。
    """
    try:
        import sherpa_onnx  # noqa: F401
    except Exception:
        return False, "没装语音识别（pip install sherpa-onnx）"
    if not (model_dir / "model.int8.onnx").exists():
        return False, "语音模型还没下载"
    if not (model_dir / "tokens.txt").exists():
        return False, "语音模型缺 tokens.txt"
    return True, ""


async def pull_audio(
    url: str, seconds: int, ffmpeg: str, workdir: Path
) -> Path | None:
    """从直播流里抓一段音频，存成 16k 单声道的 wav。

    Args:
        url: 直播播放地址。
        seconds: 抓多少秒。
        ffmpeg: ffmpeg 路径，空串表示没有。
        workdir: 临时目录。

    Returns:
        wav 路径；失败返回 None。
    """
    if not ffmpeg:
        return None
    target = workdir / "live.wav"
    command = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-headers",
        "Referer: https://live.bilibili.com/\r\n",
        "-user_agent",
        "Mozilla/5.0",
        "-i",
        url,
        "-t",
        str(max(5, min(60, seconds))),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        str(target),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(process.communicate(), timeout=seconds + 90)
    except (OSError, asyncio.TimeoutError) as exc:
        logger.warning("[bili-whale] 抓直播音频失败：%s", exc)
        return None
    if process.returncode != 0 or not target.exists() or target.stat().st_size < 2000:
        logger.warning("[bili-whale] ffmpeg 没抓到音频：%s", err[-200:])
        return None
    return target


async def transcribe(wav: Path, model_dir: Path) -> str:
    """调本地 SenseVoice 把 wav 转成文字。

    Args:
        wav: 音频路径。
        model_dir: 模型目录。

    Returns:
        文字；失败返回空串。
    """
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(WORKER),
            str(model_dir),
            str(wav),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(process.communicate(), timeout=300)
    except (OSError, asyncio.TimeoutError) as exc:
        logger.warning("[bili-whale] 转写失败：%s", exc)
        return ""
    if process.returncode != 0:
        logger.warning("[bili-whale] 转写进程退出 %s：%s", process.returncode, err[-200:])
        return ""
    return out.decode("utf-8", "ignore").strip()


async def listen(
    url: str, seconds: int, ffmpeg: str, model_dir: Path
) -> str:
    """抓一段直播音频并转写，音频用完即删。

    Args:
        url: 播放地址。
        seconds: 抓多少秒。
        ffmpeg: ffmpeg 路径。
        model_dir: 模型目录。

    Returns:
        听到的话；失败返回空串。
    """
    with tempfile.TemporaryDirectory(prefix="bili-whale-live-") as tmp:
        wav = await pull_audio(url, seconds, ffmpeg, Path(tmp))
        if wav is None:
            return ""
        return await transcribe(wav, model_dir)
