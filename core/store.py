"""插件本地状态：预算、配额、观看历史、日记、风控熔断。

全部是普通 JSON / JSONL / Markdown 文件 —— 没有数据库。出问题时人可以直接打开看，
换机器把整个目录拷走就带走了。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path


def _atomic_write(path: Path, text: str) -> None:
    """原子写文件。

    进程在写一半时被杀掉会留下半截 JSON，下次启动就整份状态失效。
    先写同目录临时文件再 os.replace，代价一个 inode，换掉整类问题。

    Args:
        path: 目标文件。
        text: 完整内容。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def today_str() -> str:
    """返回本地日期，格式 YYYY-MM-DD。"""
    return datetime.now().strftime("%Y-%m-%d")


class Store:
    """所有落盘状态的唯一入口。

    Args:
        root: 插件数据目录。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.state_path = self.root / "state.json"
        self.watched_path = self.root / "watched.jsonl"
        self.diary_dir = self.root / "diary"
        self.shots_dir = self.root / "shots"
        self.cache_dir = self.root / "cache"
        for directory in (self.root, self.diary_dir, self.shots_dir, self.cache_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def load_state(self) -> dict:
        """读取 state.json，损坏或缺失时返回空 dict。"""
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_state(self, state: dict) -> None:
        """整份写回 state.json。"""
        _atomic_write(self.state_path, json.dumps(state, ensure_ascii=False, indent=2))

    def memory_block(self, days: int = 1, limit: int = 3) -> str:
        """拼一段"今天刷过什么"的简短上下文。

        给普通聊天用：她醒来那一轮结束之后，上下文就没了，
        所以在别的对话里她应该还"记得"自己今天看了什么 —— 像人一样。

        Args:
            days: 回看天数。
            limit: 最多提几条。

        Returns:
            文本块；这些天什么都没看就返回空串。
        """
        items = self.watched_recent(days)
        if not items:
            return ""

        def clean(text: str, width: int) -> str:
            body = re.sub(r"^【[^】]*】", "", str(text or "")).strip()
            body = re.sub(r"\s*[—－-]{2,}\s*", " ", body).strip()
            return body.rstrip("？！。 ，")[:width]

        def recall(item: dict) -> str:
            """把一条记录压成"标题（印象）"。"""
            title = clean(item.get("title") or "", 18) or "（无标题）"
            note = re.sub(r"\s+", " ", str(item.get("note") or "")).strip()
            pieces = [piece for piece in re.split(r"[。！？]", note) if piece.strip()]
            pick = (pieces[-1] if pieces else "").strip()[:16]
            if item.get("shared"):
                pick = f"{pick}，已发给对方" if pick else "已发给对方"
            return f"{title}（{pick}）" if pick else title

        # 刚发出去的那条要单独点名：用户下一句说"这个"时，她得有指代对象
        fresh: dict | None = None
        for item in items:
            stamp = str(item.get("shared_at") or "")
            if not item.get("shared") or not stamp:
                continue
            try:
                age = (datetime.now() - datetime.fromisoformat(stamp)).total_seconds()
            except ValueError:
                continue
            if age < 8 * 3600:
                fresh = item
                break

        parts: list[str] = []
        lead = ""
        if fresh is not None:
            title = clean(fresh.get("title") or "", 20) or "（无标题）"
            said = str(fresh.get("message") or "").strip()[:32]
            # 全程第三人称：这段是贴在"用户侧"的附注，用"你/他"会被读成对方在说话
            lead = f"她刚给对方发过 {title}"
            if said:
                lead += f"，发的时候说「{said}」"
            lead += "。"
            # 被问到"这个视频讲了什么"时，光有标题和一句感慨不够，把她当时记的展开一段
            remembered = re.sub(r"\s+", " ", str(fresh.get("note") or "")).strip()
            if remembered:
                # 60 字以内按句号截断，别切成"…懒得改。今。"这种残句
                head = remembered[:60]
                stop = head.rfind("。")
                if len(remembered) > 60:
                    head = head[: stop + 1] if stop >= 20 else head.rstrip("，、；：") + "…"
                lead += f"当时记的：{head}"
            # 对方接下来说"这个/刚才那个"，指的就是这一条
            lead += '对方说"这个"就是指这条。'

        for item in items:
            if fresh is not None and item is fresh:
                continue
            if len(parts) >= limit:
                break
            parts.append(recall(item))

        body = lead + (("今天还刷过：" + "；".join(parts) + "。") if parts else "")
        return "（记忆：" + body + "）"
    def liked_before(self, bvid: str) -> bool:
        """这条是不是已经点过赞了。"""
        return str(bvid) in (self.load_state().get("liked") or {})

    def likes_today(self) -> int:
        """今天已经点了几条。"""
        liked = self.load_state().get("liked") or {}
        start = (
            datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        )
        return sum(1 for ts in liked.values() if float(ts) >= start)

    def last_like_at(self) -> float:
        """最近一次点赞的时间戳，用来做点赞之间的间隔。"""
        liked = self.load_state().get("liked") or {}
        return max((float(value) for value in liked.values()), default=0.0)

    def note_like(self, bvid: str, limit: int = 200) -> None:
        """记录一次点赞，顺带把太老的记录剪掉。

        Args:
            bvid: 视频号。
            limit: 最多保留多少条点赞记录。
        """
        state = self.load_state()
        liked = state.setdefault("liked", {})
        liked[str(bvid)] = time.time()
        if len(liked) > limit:
            stale = sorted(liked.items(), key=lambda kv: float(kv[1]))[: len(liked) - limit]
            for key, _value in stale:
                liked.pop(key, None)
        self.save_state(state)

    @property
    def pending_path(self) -> Path:
        """待推送汇报单独存一个文件。

        Args:
            无。

        Returns:
            文件路径。
        """
        return self.root / "pending_report.json"

    def save_pending_report(self, text: str, shared: list[dict]) -> None:
        """把这一轮的汇报先落盘。

        推送要等 agent 写完最后一句话，而进程被杀、重启、断网都可能发生在那一刻。
        先存下来，推成功再标记，下次启动就能补发。

        Args:
            text: 汇报正文。
            shared: 本次分享出去的条目。
        """
        payload = {
            "at": time.time(),
            "text": text,
            "shared": shared or [],
            "pushed": False,
        }
        _atomic_write(self.pending_path, json.dumps(payload, ensure_ascii=False, indent=2))

    def take_pending_report(self, max_age: float = 7200) -> dict | None:
        """取还没推送出去的汇报。

        Args:
            max_age: 超过这个秒数就当作过期（内容已经没意义了）。

        Returns:
            汇报字典，没有则 None。
        """
        try:
            report = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(report, dict) or report.get("pushed"):
            return None
        if time.time() - float(report.get("at", 0)) > max_age:
            return None
        return report

    def mark_pending_report_pushed(self) -> None:
        """标记汇报已经送达，避免重复补发。"""
        try:
            report = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(report, dict):
            return
        report["pushed"] = True
        _atomic_write(self.pending_path, json.dumps(report, ensure_ascii=False, indent=2))

    def mark_watched(self, entry: dict) -> None:
        """追加一条观看记录。

        Args:
            entry: 至少含 bvid / title，可选 score / note。
        """
        record = {"at": datetime.now().isoformat(timespec="seconds"), **entry}
        with self.watched_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")

    def watched_recent(self, days: int = 30) -> list[dict]:
        """按时间倒序返回最近 N 天的观看记录。

        Args:
            days: 回看天数。

        Returns:
            观看记录列表，解析失败的行被静默跳过。
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        if not self.watched_path.exists():
            return []
        latest: dict[str, dict] = {}
        for line in self.watched_path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict) or str(item.get("at", "")) < cutoff:
                continue
            # 同一条视频会被 debrief 和 share 各记一次，历史里只保留最新那条。
            # 先 pop 再插回：这样它在字典里的位置也跟着更新，倒序才是真的按时间倒序
            # （同一秒内的两次记录时间戳相同，靠插入顺序区分才稳定）。
            key = str(item.get("bvid", ""))
            latest.pop(key, None)
            latest[key] = item
        return list(reversed(list(latest.values())))

    def remember(
        self, bvid: str, note: str, tags: list[str] | None = None
    ) -> bool:
        """给她看过的某条视频补一句自己的话。

        UP 主的简介经常是空的或者纯引流，所以真正有用的是她读完之后自己写的那两句。
        文件是追加写的，这里取该条最近一次记录合并后追加一条完整记录；
        读取时按 bvid 去重（保留最后一条），所以外面看到的始终是合并后的结果。

        Args:
            bvid: 视频号。
            note: 她自己写的两三句话。
            tags: 额外标签，可选。

        Returns:
            找到原记录并写入返回 True，否则 False。
        """
        bvid = str(bvid or "").strip()
        if not bvid:
            return False
        latest: dict | None = None
        for item in self.watched_recent(3650):
            if item.get("bvid") == bvid:
                latest = item
                break
        if latest is None:
            return False

        merged = dict(latest)
        merged["note"] = str(note or "").strip()[:200]
        if tags:
            existing = [str(tag) for tag in (merged.get("tags") or [])]
            for tag in tags:
                name = str(tag).strip()
                if name and name not in existing:
                    existing.append(name)
            merged["tags"] = existing[:16]
        merged["noted_at"] = datetime.now().isoformat(timespec="seconds")
        with self.watched_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(merged, ensure_ascii=False) + "\n")
        return True

    def stats(self, days: int = 30) -> dict:
        """最近一段时间的观看统计。

        给唤醒提示用：让她自己看一眼"最近都在看什么、有没有真喜欢"。
        纯本地计算，不打接口也不花模型钱。

        Args:
            days: 统计窗口天数。

        Returns:
            含 count / shared / avg_score / tags / tnames 的字典。
        """
        items = self.watched_recent(days)
        tag_counts: dict[str, int] = {}
        tname_counts: dict[str, int] = {}
        scores: list[float] = []
        shared = 0
        for item in items:
            for tag in item.get("tags") or []:
                name = str(tag).strip()
                if name:
                    tag_counts[name] = tag_counts.get(name, 0) + 1
            tname = str(item.get("tname") or "").strip()
            if tname:
                tname_counts[tname] = tname_counts.get(tname, 0) + 1
            if item.get("score") is not None:
                scores.append(float(item["score"]))
            if item.get("shared"):
                shared += 1
        return {
            "count": len(items),
            "shared": shared,
            "avg_score": round(sum(scores) / len(scores), 1) if scores else 0.0,
            "tags": sorted(tag_counts.items(), key=lambda kv: kv[1], reverse=True)[:8],
            "tnames": sorted(tname_counts.items(), key=lambda kv: kv[1], reverse=True)[:5],
        }

    def prune_shots(self, days: int = 30, max_mb: int = 200) -> int:
        """清理旧的截图。

        深读留下的封面、九宫格、字幕条只是当时的证据：给她自己看图用的，
        看过之后没有长期价值，不清理会一直堆着。

        Args:
            days: 保留天数。
            max_mb: 目录体积上限，超出时按最旧的先删。

        Returns:
            删除的文件数。
        """
        if not self.shots_dir.exists():
            return 0

        cutoff = datetime.now().timestamp() - days * 86400
        removed = 0
        for path in list(self.shots_dir.iterdir()):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue

        remaining: list[tuple[float, int, Path]] = []
        for path in self.shots_dir.iterdir():
            if not path.is_file():
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            remaining.append((info.st_mtime, info.st_size, path))
        remaining.sort()

        total = sum(size for _mtime, size, _path in remaining)
        limit = max_mb * 1024 * 1024
        for _mtime, size, path in remaining:
            if total <= limit:
                break
            try:
                path.unlink()
                total -= size
                removed += 1
            except OSError:
                continue
        return removed

    def prune(self, days: int = 180) -> int:
        """丢掉太老的观看记录。

        视频不在本地，留着只是统计素材；超过半年的记录参考价值已经很低，
        而且这个文件会无限长下去。

        Args:
            days: 保留天数。

        Returns:
            被删掉的条数。
        """
        if not self.watched_path.exists():
            return 0
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        kept: list[str] = []
        dropped = 0
        for line in self.watched_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                dropped += 1
                continue
            if str(item.get("at", "")) >= cutoff:
                kept.append(json.dumps(item, ensure_ascii=False))
            else:
                dropped += 1
        if dropped:
            _atomic_write(
                self.watched_path, "\n".join(kept) + ("\n" if kept else "")
            )
        return dropped

    def recall(self, query: str, limit: int = 5, days: int = 120) -> list[dict]:
        """按关键词回忆看过的视频。

        匹配标题、简介、标签和分区；命中的词越多越靠前，同分时新近的优先。

        Args:
            query: 关键词，空格或逗号分隔。
            limit: 返回条数。
            days: 回看天数。

        Returns:
            命中的记录，按相关度排序。
        """
        terms = [
            term.strip().lower()
            for term in str(query or "").replace("，", " ").replace(",", " ").split()
            if term.strip()
        ]
        if not terms:
            return []

        scored: list[tuple[int, dict]] = []
        for item in self.watched_recent(days):
            haystack = " ".join(
                [
                    str(item.get("title", "")),
                    str(item.get("note", "")),
                    str(item.get("desc", "")),
                    " ".join(str(tag) for tag in (item.get("tags") or [])),
                    str(item.get("tname", "")),
                ]
            ).lower()
            hits = sum(1 for term in terms if term in haystack)
            if hits:
                scored.append((hits, item))
        scored.sort(key=lambda pair: (pair[0], str(pair[1].get("at", ""))), reverse=True)
        return [item for _hits, item in scored[: max(1, int(limit))]]

    def seen(self, bvid: str, days: int = 7) -> bool:
        """这条视频最近 N 天是否已经看过。

        Args:
            bvid: 视频号。
            days: 回看天数。

        Returns:
            看过为 True。
        """
        return any(item.get("bvid") == bvid for item in self.watched_recent(days))

    def append_diary(self, who: str, text: str, day: str | None = None) -> Path:
        """把一段日记追加到当天的文件里。

        Args:
            who: mine（写给自己）或 yours（写给你）。
            text: 日记正文。
            day: 目标日期，默认今天。

        Returns:
            写入的文件路径。
        """
        day = day or today_str()
        path = self.diary_dir / f"{day}-{who}.md"
        stamp = datetime.now().strftime("%H:%M")
        with path.open("a", encoding="utf-8") as fp:
            fp.write(f"\n## {stamp}\n\n{text.strip()}\n")
        return path

    def delete_watched(self, bvid: str) -> int:
        """删掉某条视频的全部观看记录。

        Args:
            bvid: 视频号。

        Returns:
            删掉的记录条数。
        """
        bvid = str(bvid or "").strip()
        if not bvid or not self.watched_path.exists():
            return 0
        kept: list[str] = []
        removed = 0
        for line in self.watched_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if str(item.get("bvid") or "") == bvid:
                removed += 1
            else:
                kept.append(line)
        if removed:
            _atomic_write(
                self.watched_path, "\n".join(kept) + ("\n" if kept else "")
            )
        return removed

    def delete_diary_round(
        self, who: str, stamp: str, day: str | None = None
    ) -> bool:
        """删掉日记里的某一轮。

        文件按 `## HH:MM` 分段，这里把命中的那一段整个去掉再写回。

        Args:
            who: mine 或 yours。
            stamp: 轮次时间，形如 18:52。
            day: 目标日期，默认今天。

        Returns:
            删掉了返回 True。
        """
        target_day = day or today_str()
        path = self.diary_dir / f"{target_day}-{who}.md"
        if not path.exists():
            return False

        lines = path.read_text(encoding="utf-8").splitlines()
        head = f"## {stamp}".strip()
        kept: list[str] = []
        skipping = False
        removed = False
        for line in lines:
            if line.strip() == head:
                skipping = True
                removed = True
                continue
            if skipping and line.startswith("## "):
                skipping = False
            if not skipping:
                kept.append(line)
        if not removed:
            return False
        _atomic_write(path, "\n".join(kept).strip() + "\n")
        return True

    def read_diary(self, who: str, day: str | None = None) -> str:
        """读取某天某本日记，没有则返回空串。"""
        path = self.diary_dir / f"{day or today_str()}-{who}.md"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""


class Guard:
    """读/写预算、分享闸门与风控熔断。

    这里是插件唯一的硬约束实现处：工具层只来问能不能、然后记一笔做了。
    绕过这里就等于没有约束，所以所有写操作都必须经过 write_gate。

    Args:
        store: 状态存储。
        read_daily: 每日读请求上限。
        write_daily: 每日写操作上限（评论/收藏）。
        write_cooldown: 两次写之间的最小间隔（秒）。
        share_daily: 每天最多分享几条。
        comment_daily: 每天最多自己发几条评论。
        share_min_score: 分享所需最低评分（0-10）。
        video_cooldown_days: 同一条视频多少天内不重复分享。
    """

    def __init__(
        self,
        store: Store,
        *,
        read_daily: int = 3000,
        write_daily: int = 30,
        write_cooldown: int = 120,
        share_daily: int = 3,
        comment_daily: int = 2,
        share_min_score: int = 7,
        video_cooldown_days: int = 7,
    ) -> None:
        self.store = store
        self.read_daily = max(1, int(read_daily))
        self.write_daily = max(1, int(write_daily))
        self.write_cooldown = max(0, int(write_cooldown))
        self.share_daily = max(0, int(share_daily))
        self.comment_daily = max(0, int(comment_daily))
        self.share_min_score = max(0, int(share_min_score))
        self.video_cooldown_days = max(1, int(video_cooldown_days))

    def _state(self) -> dict:
        """取状态并跨天归零。"""
        state = self.store.load_state()
        if state.get("day") != today_str():
            state["day"] = today_str()
            state["reads"] = 0
            state["writes"] = 0
            state["shares"] = 0
            state["comments"] = 0
        state.setdefault("reads", 0)
        state.setdefault("writes", 0)
        state.setdefault("shares", 0)
        state.setdefault("comments", 0)
        state.setdefault("commented", {})
        state.setdefault("last_write_at", 0.0)
        state.setdefault("pause_until", 0.0)
        state.setdefault("share_bvids", {})
        return state

    def _flush(self, state: dict) -> None:
        self.store.save_state(state)

    def pause_left(self) -> int:
        """返回熔断剩余秒数，0 表示没在熔断。"""
        state = self._state()
        return max(0, int(float(state.get("pause_until", 0)) - time.time()))

    def pause(self, seconds: int, why: str) -> None:
        """熔断一段时间。

        Args:
            seconds: 时长，至少 60 秒。
            why: 原因，会写进状态供状态命令显示。
        """
        state = self._state()
        state["pause_until"] = time.time() + max(60, int(seconds))
        state["pause_why"] = why
        self._flush(state)

    def read_budget(self) -> tuple[int, int]:
        """返回 (今日已用, 上限)。"""
        state = self._state()
        return int(state["reads"]), self.read_daily

    def can_read(self, n: int = 1) -> tuple[bool, str]:
        """判断还能不能发 N 次读请求。

        Args:
            n: 本次预计消耗次数。

        Returns:
            (是否允许, 拒绝原因)。
        """
        left = self.pause_left()
        if left:
            return False, f"B站风控熔断中，约 {left // 60 + 1} 分钟后恢复"
        used, limit = self.read_budget()
        if used + n > limit:
            return False, f"今日读预算用完（{used}/{limit}）"
        return True, ""

    def note_read(self, n: int = 1) -> None:
        """记一次读请求消耗。"""
        state = self._state()
        state["reads"] = int(state["reads"]) + n
        self._flush(state)

    def write_gate(
        self,
        *,
        score: int | None = None,
        reason: str = "",
        bvid: str | None = None,
        is_share: bool = False,
        is_comment: bool = False,
    ) -> tuple[bool, str]:
        """判断一次写操作能否放行。

        分享比普通写多三道：每日额度、评分门槛、同一视频冷却。

        Args:
            score: 自评分数 0-10，分享时必填。
            reason: 至少 8 个字的理由，分享时必填。
            bvid: 目标视频号，分享时用于查重。
            is_share: 是否属于分享通道。
            is_comment: 是否属于"她自己想评论"，与分享各算各的额度。

        Returns:
            (是否允许, 拒绝原因)。
        """
        left = self.pause_left()
        if left:
            return False, f"B站风控熔断中，约 {left // 60 + 1} 分钟后恢复"

        state = self._state()
        now = time.time()
        gap = now - float(state.get("last_write_at", 0))
        if gap < self.write_cooldown:
            return False, f"写冷却中，还需等 {int(self.write_cooldown - gap)} 秒"

        if int(state["writes"]) >= self.write_daily:
            return False, f"今日写预算用完（{state['writes']}/{self.write_daily}）"

        if is_comment:
            if int(state["comments"]) >= self.comment_daily:
                return False, f"今天评论够多了（{state['comments']}/{self.comment_daily}）"
            if bvid:
                last = float(state["commented"].get(bvid, 0))
                if now - last < self.video_cooldown_days * 86400:
                    return False, "这条已经评论过了"
            return True, ""

        if not is_share:
            return True, ""

        if int(state["shares"]) >= self.share_daily:
            return False, f"今日分享额度用完（{state['shares']}/{self.share_daily}）"
        if score is None or not 0 <= int(score) <= 20:
            return False, "分享必须带 score（0-10），说清值得在哪"
        if int(score) < self.share_min_score:
            return False, f"评分 {score} 低于门槛 {self.share_min_score}，这条不值得发"
        if len(str(reason).strip()) < 8:
            return False, "reason 太短（至少 8 个字），说不清就别发"
        if bvid:
            last = float(state["share_bvids"].get(bvid, 0))
            if now - last < self.video_cooldown_days * 86400:
                return False, f"{bvid} 在 {self.video_cooldown_days} 天内已经分享过"
        return True, ""

    def note_write(
        self,
        *,
        bvid: str | None = None,
        is_share: bool = False,
        is_comment: bool = False,
    ) -> None:
        """记一次写操作。"""
        state = self._state()
        state["writes"] = int(state["writes"]) + 1
        state["last_write_at"] = time.time()
        if is_share:
            state["shares"] = int(state["shares"]) + 1
            if bvid:
                state["share_bvids"][bvid] = time.time()
        if is_comment:
            state["comments"] = int(state["comments"]) + 1
            if bvid:
                state["commented"][bvid] = time.time()
        self._flush(state)

    def mark_commented(self, bvid: str) -> None:
        """记下"这条已经评论过"，不额外计额度。

        分享到评论区时也要记一笔，否则她会在同一条视频下面评两次。

        Args:
            bvid: 视频号。
        """
        if not bvid:
            return
        state = self._state()
        state["commented"][bvid] = time.time()
        self._flush(state)

    def snapshot(self) -> dict:
        """返回当日额度快照，给状态命令和插件页共用。

        Returns:
            含 reads/writes/shares 三个 (已用, 上限) 元组与 pause_left 的字典。
        """
        state = self._state()
        return {
            "reads": (int(state["reads"]), self.read_daily),
            "writes": (int(state["writes"]), self.write_daily),
            "shares": (int(state["shares"]), self.share_daily),
            "pause_left": self.pause_left(),
        }

    def summary(self) -> str:
        """一行人类可读的当日额度摘要，给状态命令和唤醒提示用。"""
        state = self._state()
        left = self.pause_left()
        parts = [
            f"读 {state['reads']}/{self.read_daily}",
            f"写 {state['writes']}/{self.write_daily}",
            f"分享 {state['shares']}/{self.share_daily}",
        ]
        if left:
            parts.append(f"熔断 {left // 60 + 1} 分钟")
        return " · ".join(parts)
