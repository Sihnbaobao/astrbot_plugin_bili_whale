"""看懂一条视频：六路互补的素材采集。

六路各回答一个问题，缺哪路都能靠其它路补：

    元信息简介 -> 它自称是什么
    官方字幕   -> 它到底说了什么（最准最省）
    弹幕热区   -> 观众在哪炸了（对齐到字幕时间轴后就是事件点）
    热评       -> 看完的人怎么看
    封面        -> 第一印象
    画面抽帧   -> 长什么样（九宫格一眼九帧；没官方字幕时再看底部字幕条）

这个模块只负责把素材备齐、对齐时间轴、剔掉刷屏噪声。**不作判断** ——
"这值不值得发"必须由模型看着素材决定。
"""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .bili import BiliClient, BiliError

# 超过这个时长不下载整片抽帧：一条 40 分钟的视频能吃掉几百 MB 和几分钟
MAX_FRAME_DURATION = 20 * 60


def _fmt(seconds: float) -> str:
    """秒数转 mm:ss。"""
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


@dataclass
class Debrief:
    """一条视频的六路素材。"""

    bvid: str
    aid: int = 0
    cid: int = 0
    title: str = ""
    up: str = ""
    tname: str = ""
    duration: int = 0
    pubdate: int = 0
    views: int = 0
    likes: int = 0
    desc: str = ""
    tags: list[str] = field(default_factory=list)
    subtitle_lang: str = ""
    subtitle_lines: list[dict] = field(default_factory=list)
    danmaku_total: int = 0
    danmaku_claimed: int = 0
    hot_zones: list[dict] = field(default_factory=list)
    top_danmaku: list[str] = field(default_factory=list)
    comments: list[dict] = field(default_factory=list)
    cover_path: str = ""
    sheet_path: str = ""
    strip_path: str = ""
    notes: list[str] = field(default_factory=list)

    def images(self) -> list[str]:
        """交给视觉模型看的图片，顺序是封面在前、画面在后。"""
        return [p for p in (self.cover_path, self.sheet_path, self.strip_path) if p]

    def subtitle_at(self, moment: float) -> str:
        """取某个时间点的字幕内容，用于给弹幕热区配台词。"""
        best = ""
        for line in self.subtitle_lines:
            if line["from"] <= moment <= (line["to"] or line["from"] + 3):
                return line["content"]
            if line["from"] <= moment:
                best = line["content"]
        return best

    def materials(self, *, subtitle_chars: int = 4000) -> str:
        """把素材拼成一段给模型读的纯文本。

        Args:
            subtitle_chars: 字幕正文最多取多少字；超长时分段采样而不是直接截尾，
                否则长视频永远只能看到开头。

        Returns:
            多行文本，六路各一节。
        """
        lines: list[str] = []
        lines.append("【元信息】")
        lines.append(
            f"{self.title}  |  UP {self.up}  |  {self.tname or '-'}  |  "
            f"时长 {_fmt(self.duration)}  |  播放 {self.views} 赞 {self.likes}"
        )
        if self.tags:
            lines.append(f"标签：{' / '.join(self.tags)}")
        if self.desc:
            lines.append(f"简介：{self.desc}")

        lines.append("")
        lines.append("【官方字幕】")
        if self.subtitle_lines:
            text = "\n".join(
                f"[{_fmt(line['from'])}] {line['content']}"
                for line in self.subtitle_lines
            )
            if len(text) > subtitle_chars:
                head = text[: subtitle_chars // 2]
                tail = text[-subtitle_chars // 2 :]
                text = f"{head}\n…（中间省略）…\n{tail}"
            lines.append(f"（轨道 {self.subtitle_lang}，共 {len(self.subtitle_lines)} 行）")
            lines.append(text)
        else:
            lines.append("（无官方字幕轨道 —— 只能靠弹幕、热评和画面）")

        lines.append("")
        lines.append("【弹幕热区】")
        if self.hot_zones:
            sample = (
                f"，接口标称 {self.danmaku_claimed} 条"
                if self.danmaku_claimed
                else ""
            )
            lines.append(f"（拿到 {self.danmaku_total} 条{sample}）")
            for zone in self.hot_zones:
                mark = "🔥" if zone.get("hot") else "  "
                lines.append(
                    f"{mark} [{_fmt(zone['start'])}-{_fmt(zone['end'])}] "
                    f"有效 {zone['count']} 条"
                )
                if zone.get("line"):
                    lines.append(f"     字幕：{zone['line']}")
                if zone.get("texts"):
                    lines.append(f"     弹幕：{' | '.join(zone['texts'])}")
            if self.top_danmaku:
                lines.append(f"全程高频：{' | '.join(self.top_danmaku)}")
        else:
            lines.append("（没取到弹幕）")

        lines.append("")
        lines.append("【热评】")
        if self.comments:
            for item in self.comments:
                lines.append(f"👍{item['like']} {item['uname']}：{item['message']}")
        else:
            lines.append("（没取到热评）")

        if self.notes:
            lines.append("")
            lines.append("【采集备注】")
            lines.extend(f"- {note}" for note in self.notes)
        return "\n".join(lines)


class DebriefEngine:
    """把一条视频从"标题"读成"内容"。

    Args:
        client: B站客户端。
        shots_dir: 截图与中间产物目录。
        ffmpeg: ffmpeg 可执行文件路径，空串表示没装（跳过画面那一路）。
    """

    def __init__(self, client: BiliClient, shots_dir: Path, ffmpeg: str = "ffmpeg") -> None:
        self.client = client
        self.shots_dir = Path(shots_dir)
        self.ffmpeg = ffmpeg
        self.shots_dir.mkdir(parents=True, exist_ok=True)

    async def gather(
        self,
        bvid: str,
        *,
        frames: bool = True,
        top: int = 4,
        bucket: int = 30,
        ncomments: int = 8,
        grid: tuple[int, int] = (3, 3),
    ) -> Debrief:
        """采集一条视频的六路素材。

        Args:
            bvid: 视频号。
            frames: 是否抽帧看画面（需要 ffmpeg 且视频不太长）。
            top: 弹幕热区取前几段。
            bucket: 弹幕按多少秒一个桶聚合。
            ncomments: 热评条数。
            grid: 九宫格的列数与行数。

        Returns:
            素材对象。任何一路失败都只记进 notes，不影响其它路。

        Raises:
            BiliError: 元信息都取不到（这时这条视频没法看）。
        """
        view = await self.client.view(bvid)
        pages = view.get("pages") or [{}]
        cid = int(pages[0].get("cid") or view.get("cid") or 0)
        result = Debrief(
            bvid=bvid,
            aid=int(view.get("aid") or 0),
            cid=cid,
            title=str(view.get("title") or ""),
            up=str((view.get("owner") or {}).get("name") or ""),
            tname=str(view.get("tname") or ""),
            duration=int(view.get("duration") or 0),
            pubdate=int(view.get("pubdate") or 0),
            views=int((view.get("stat") or {}).get("view") or 0),
            likes=int((view.get("stat") or {}).get("like") or 0),
            danmaku_claimed=int((view.get("stat") or {}).get("danmaku") or 0),
            desc=re.sub(r"\s+", " ", str(view.get("desc") or ""))[:300],
        )

        try:
            result.tags = await self.client.tags(bvid)
        except BiliError as exc:
            result.notes.append(f"标签拉取失败：{exc}")

        pic = str(view.get("pic") or "")
        if pic:
            result.cover_path = await self._download(pic, f"{bvid}-cover")

        try:
            lang, lines = await self.client.subtitle(bvid, cid)
            result.subtitle_lang, result.subtitle_lines = lang, lines
        except BiliError as exc:
            result.notes.append(f"字幕拉取失败：{exc}")

        try:
            danmaku = await self.client.danmaku(cid)
            self._analyze_danmaku(result, danmaku, top=top, bucket=bucket)
        except BiliError as exc:
            result.notes.append(f"弹幕拉取失败：{exc}")

        try:
            result.comments = await self.client.comments(result.aid, ncomments)
        except BiliError as exc:
            result.notes.append(f"热评拉取失败：{exc}")

        if frames:
            if not self.ffmpeg:
                result.notes.append("没有 ffmpeg，跳过抽帧（画面这一路缺失）")
            elif result.duration > MAX_FRAME_DURATION:
                result.notes.append(
                    f"视频长 {_fmt(result.duration)}，超过 {_fmt(MAX_FRAME_DURATION)} 不抽帧"
                )
            else:
                try:
                    await self._frames(result, grid)
                except (BiliError, OSError, asyncio.TimeoutError) as exc:
                    result.notes.append(f"抽帧失败：{exc}")

        return result

    async def _download(self, url: str, name: str) -> str:
        """下载一张图片到 shots 目录，失败返回空串。"""
        if url.startswith("//"):
            url = "https:" + url
        session = await self.client._ensure_session()
        path = self.shots_dir / f"{name}.jpg"
        try:
            async with session.get(url, proxy=self.client.proxy or None) as resp:
                if resp.status != 200:
                    return ""
                path.write_bytes(await resp.read())
        except Exception:  # 网络、磁盘、超时都只算这一路失败
            return ""
        return str(path)

    def _analyze_danmaku(
        self, result: Debrief, danmaku: list[dict], *, top: int, bucket: int
    ) -> None:
        """剔除刷屏波、按时间分桶、挑出最热的几段并配上台词。

        Args:
            result: 结果对象，原地写入。
            danmaku: 原始弹幕。
            top: 取前几段。
            bucket: 分桶秒数。
        """
        if not danmaku:
            return
        result.danmaku_total = len(danmaku)

        # 刷屏波：归一化后同一句吃掉全片三成以上（开场仪式弹幕常能到 80%）
        def normalize(text: str) -> str:
            return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)

        counts: dict[str, int] = {}
        for item in danmaku:
            key = normalize(item["text"]) or "<纯符号>"
            counts[key] = counts.get(key, 0) + 1
        spam = {
            key
            for key, count in counts.items()
            if count >= 20 and count / len(danmaku) > 0.3
        }

        def is_spam(text: str) -> bool:
            key = normalize(text)
            return not key or any(key and key in spam_key for spam_key in spam)

        buckets: dict[int, dict] = {}
        for item in danmaku:
            slot = int(item["t"] // bucket) * bucket
            entry = buckets.setdefault(slot, {"raw": [], "real": []})
            entry["raw"].append(item)
            if not is_spam(item["text"]):
                entry["real"].append(item)

        real_total = sum(len(entry["real"]) for entry in buckets.values())
        average = real_total / max(1, len(buckets))
        ranked = sorted(
            buckets.items(), key=lambda kv: len(kv[1]["real"]), reverse=True
        )[:top]
        for slot, entry in ranked:
            zone_counts: dict[str, int] = {}
            for item in entry["real"]:
                zone_counts[item["text"]] = zone_counts.get(item["text"], 0) + 1
            texts = [
                f"{text}×{count}" if count > 1 else text
                for text, count in sorted(
                    zone_counts.items(), key=lambda kv: kv[1], reverse=True
                )[:6]
            ]
            result.hot_zones.append(
                {
                    "start": slot,
                    "end": slot + bucket,
                    "count": len(entry["real"]),
                    "raw": len(entry["raw"]),
                    "hot": len(entry["real"]) >= average * 1.5,
                    "line": result.subtitle_at(slot + bucket / 2),
                    "texts": texts,
                }
            )

        overall: dict[str, int] = {}
        for item in danmaku:
            if not is_spam(item["text"]) and len(item["text"]) >= 2:
                overall[item["text"]] = overall.get(item["text"], 0) + 1
        result.top_danmaku = [
            f"{text}×{count}" if count > 1 else text
            for text, count in sorted(overall.items(), key=lambda kv: kv[1], reverse=True)[:10]
        ]

    async def _frames(self, result: Debrief, grid: tuple[int, int]) -> None:
        """下载整片、抽帧、拼九宫格；没有官方字幕时再拼一张底部字幕条。

        Args:
            result: 结果对象，原地写入 sheet_path / strip_path。
            grid: (列, 行)。

        Raises:
            BiliError: 拿不到直链或 ffmpeg 失败。
        """
        cols, rows = grid
        count = cols * rows
        url, quality = await self.client.play_url(result.bvid, result.cid)
        mp4 = self.shots_dir / f"{result.bvid}.mp4"

        session = await self.client._ensure_session()
        async with session.get(
            url,
            headers={"referer": "https://www.bilibili.com/"},
            proxy=self.client.proxy or None,
        ) as resp:
            if resp.status != 200:
                raise BiliError(f"下载失败 HTTP {resp.status}")
            with mp4.open("wb") as fp:
                async for chunk in resp.content.iter_chunked(1 << 20):
                    fp.write(chunk)

        temp = self.shots_dir / f".frames-{result.bvid}"
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)
        temp.mkdir(parents=True, exist_ok=True)
        try:
            duration = max(1, result.duration)
            times = [(i + 0.5) * duration / count for i in range(count)]
            for index, moment in enumerate(times):
                await self._run_ffmpeg(
                    [
                        "-y", "-loglevel", "error",
                        "-ss", f"{moment:.2f}",
                        "-i", str(mp4),
                        "-frames:v", "1", "-q:v", "3",
                        str(temp / f"f{index:02d}.jpg"),
                    ]
                )
            sheet = self.shots_dir / f"{result.bvid}-{cols}x{rows}.jpg"
            await self._run_ffmpeg(
                [
                    "-y", "-loglevel", "error",
                    "-i", str(temp / "f%02d.jpg"),
                    "-vf", f"scale=720:-1,tile={cols}x{rows}",
                    "-frames:v", "1", str(sheet),
                ]
            )
            if sheet.exists():
                result.sheet_path = str(sheet)
                result.notes.append(f"抽帧画质 {quality}")
            if not result.subtitle_lines:
                strip = self.shots_dir / f"{result.bvid}-subs.jpg"
                await self._run_ffmpeg(
                    [
                        "-y", "-loglevel", "error",
                        "-i", str(temp / "f%02d.jpg"),
                        "-vf",
                        f"crop=iw:ih*0.17:0:ih*0.83,scale=1280:-1,tile=1x{count}",
                        "-frames:v", "1", str(strip),
                    ]
                )
                if strip.exists():
                    result.strip_path = str(strip)
                    result.notes.append("无官方字幕，附带底部字幕条")
        finally:
            shutil.rmtree(temp, ignore_errors=True)
            mp4.unlink(missing_ok=True)

    async def _run_ffmpeg(self, args: list[str]) -> None:
        """跑一次 ffmpeg，失败时带 stderr 抛错。

        Args:
            args: 除可执行文件外的参数。

        Raises:
            BiliError: ffmpeg 返回非 0。
        """
        proc = await asyncio.create_subprocess_exec(
            self.ffmpeg,
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            raise BiliError("ffmpeg 超时")
        if proc.returncode != 0:
            detail = (stderr or b"").decode("utf-8", errors="ignore")[-200:]
            raise BiliError(f"ffmpeg 失败：{detail}")
