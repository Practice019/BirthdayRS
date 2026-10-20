"""每日自动发送的定时器。

**为什么需要它**：``run`` 命令只是"跑一次检查就退出"，它本身不带定时能力。
容器原先只跑 ``web``（管界面），于是从来没有任何东西去调 ``run`` ——
配置写得再对，也一次都不会自动发出去。使用者只会看到"测试发送能用，
但生日那天没收到"。

**为什么做在 web 进程里**：容器只跑一个进程最省心。独立 cron 容器意味着
多一个镜像、多一份配置、多一处会忘的地方；而 web 进程本来就常驻，
容器又配了 ``restart: unless-stopped``，进程挂了会自动拉起。

**为什么不是精确的 9:00:00**：这里用"每 30 秒醒来一次、看今天这一刻
该不该发"。精确到秒没有意义（提醒不是秒级业务），而轮询换来的好处是
**不用处理时区夏令时、系统休眠、时钟跳变**这些边角情况。用一个
标记文件记录"今天发过了"，重启也不会重复发。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: 默认发送时刻（本地时区）。
#: 早上 9 点：符合"提前提醒"的用意，不会在半夜打扰别人。
DEFAULT_SEND_HOUR = 9
DEFAULT_SEND_MINUTE = 0

#: 轮询间隔（秒）。30 秒足够让 9:00 的发送在 9:00:30 内完成，
#: 又不会让日志被刷屏。
POLL_INTERVAL = 30


def _sent_marker_path(log_dir: Path) -> Path:
    return log_dir / ".last_auto_send"


def already_sent_today(marker: Path, today: date) -> bool:
    """今天是否已经发过。

    用一个文件记住"最近一次自动发送的日期"。比在内存里记更可靠 ——
    容器重启（部署、升级、崩溃拉起）后内存状态会丢，重新发一遍
    会让收件人一天收到两条同样的提醒。
    """
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return False
    return text == today.isoformat()


def mark_sent(marker: Path, today: date) -> None:
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(today.isoformat(), encoding="utf-8")
    except OSError as exc:
        # 标记写不进去不该让整个发送失败 —— 那条提醒已经发出去了。
        # 代价是重启后可能重复发一次，比"因为写不了文件就不发"轻。
        logger.warning("写不下自动发送标记（%s）：%s", marker, exc)


def should_send_now(
    now: datetime,
    *,
    hour: int = DEFAULT_SEND_HOUR,
    minute: int = DEFAULT_SEND_MINUTE,
) -> bool:
    """现在这一刻是否已经到发送时间。

    不要求"正好整点"：过了 9:00 就一直算到点。这样容器 9:30 才启动、
    或者进程中途重启，当天仍然会补发 —— 而不是默默错过一整天。
    """
    return (now.hour, now.minute) >= (hour, minute)


class DailyScheduler:
    """每天在指定时刻调用一次 ``job``。

    ``job`` 可以是普通函数，也可以是**协程函数**。定时器本身跑在
    FastAPI 的事件循环里，所以发送流程必须是 ``await`` 而不是
    ``asyncio.run()`` —— 后者在已有事件循环时会抛
    ``RuntimeError: asyncio.run() cannot be called from a running event loop``。
    """

    def __init__(
        self,
        job: Callable[[], Any],
        log_dir: Path,
        *,
        hour: int = DEFAULT_SEND_HOUR,
        minute: int = DEFAULT_SEND_MINUTE,
        poll_interval: int = POLL_INTERVAL,
    ) -> None:
        self.job = job
        self.marker = _sent_marker_path(log_dir)
        self.hour = hour
        self.minute = minute
        self.poll_interval = poll_interval
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    async def _loop(self) -> None:
        logger.info(
            "自动发送已启用：每天 %02d:%02d 检查一次（标记文件 %s）",
            self.hour,
            self.minute,
            self.marker,
        )
        while not self._stopping:
            try:
                now = datetime.now()
                today = now.date()
                if should_send_now(now, hour=self.hour, minute=self.minute):
                    if already_sent_today(self.marker, today):
                        logger.debug("今天已经发过了，跳过")
                    else:
                        logger.info("到点，开始每日自动发送")
                        # 先打标记再发送：发送中途崩溃时宁可少发一次，
                        # 也不要因为崩溃重启而重复发（重复发送打扰使用者）。
                        mark_sent(self.marker, today)
                        # job 可能是协程：直接 await，不能 asyncio.run() ——
                        # 这里已经在事件循环里了。
                        sent = self.job()
                        if asyncio.iscoroutine(sent):
                            sent = await sent
                        logger.info("每日自动发送完成：%s 条", sent)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 单次失败不能让定时器死掉 —— 明天还得继续发。
                logger.exception("自动发送出错，已跳过本次，明天继续")

            await asyncio.sleep(self.poll_interval)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
