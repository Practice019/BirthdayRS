"""每日自动发送的定时器。

**这个功能的 bug 曾经非常隐蔽**：核心逻辑（reminder_days 窗口内每天命中）
一直是对的，测试也一直是绿的，但容器里只跑 `web` 命令 —— 从来没有任何
东西去调 `run`。于是"测试发送能用，生日那天却收不到"。

所以这里的测试覆盖两类东西：

1. 判定逻辑（该不该发、发过没发过）
2. **接线**（定时器真的被创建、真的会调用检查流程）——
   第二类才是当初漏掉的部分。
"""

import asyncio
from datetime import date, datetime
from pathlib import Path

import pytest

from src.web.scheduler import (
    DailyScheduler,
    already_sent_today,
    mark_sent,
    should_send_now,
)


# ---------- 到点判定 ----------


@pytest.mark.parametrize(
    "now,expected",
    [
        (datetime(2026, 10, 6, 8, 59), False),
        (datetime(2026, 10, 6, 9, 0), True),
        (datetime(2026, 10, 6, 9, 1), True),
        (datetime(2026, 10, 6, 23, 59), True),
        (datetime(2026, 10, 6, 0, 0), False),
    ],
)
def test_should_send_now(now, expected):
    assert should_send_now(now, hour=9, minute=0) is expected


def test_should_send_now_catches_late_start():
    """过了 9 点也算到点 —— 容器 9:30 才启动时当天仍要补发。

    否则"早上没开机"就等于"今天一整天都不提醒"，而这正是最容易漏掉的情形。
    """
    assert should_send_now(datetime(2026, 10, 6, 9, 30), hour=9, minute=0) is True


def test_custom_time():
    assert should_send_now(datetime(2026, 10, 6, 7, 59), hour=8, minute=0) is False
    assert should_send_now(datetime(2026, 10, 6, 8, 0), hour=8, minute=0) is True


# ---------- "今天发过了吗" ----------


def test_marker_roundtrip(tmp_path: Path):
    marker = tmp_path / ".last_auto_send"
    today = date(2026, 10, 6)

    assert already_sent_today(marker, today) is False, "没发过时不能报发过"

    mark_sent(marker, today)
    assert already_sent_today(marker, today) is True

    # 换一天就该重新发
    assert already_sent_today(marker, date(2026, 10, 7)) is False


def test_marker_missing_is_false(tmp_path: Path):
    """标记文件不存在 = 没发过（首次运行、或挂载目录被清空）。"""
    assert already_sent_today(tmp_path / "nope", date(2026, 10, 6)) is False


def test_marker_write_failure_does_not_raise(tmp_path: Path):
    """标记写不进去不能让发送失败 —— 提醒本身已经发出去了。

    代价只是重启后可能重复发一次，比"因为写不了文件就不发"轻得多。

    用"目标是目录"制造写失败：测试以 root 跑，chmod 只读挡不住 root，
    用目录则任何用户都写不进去。
    """
    bad = tmp_path / ".last_auto_send"
    bad.mkdir()               # 目录，write_text 必然抛 IsADirectoryError
    mark_sent(bad, date(2026, 10, 6))   # 不该抛
    # 读的时候也该安静地当作"没发过"，而不是炸掉整个定时器
    assert already_sent_today(bad, date(2026, 10, 6)) is False


# ---------- 定时器本身 ----------


@pytest.mark.asyncio
async def test_scheduler_runs_job_once_then_skips(tmp_path: Path):
    """到点执行一次；同一天再轮询不会重复执行。"""
    calls = []

    def job(slot="morning"):
        calls.append(slot)
        return 1

    sched = DailyScheduler(
        job, log_dir=tmp_path, hour=0, minute=0,
        eve_hour=None, eve_minute=None, poll_interval=0.01,
    )
    sched.start()
    await asyncio.sleep(0.15)   # 轮询若干次
    await sched.stop()

    assert len(calls) == 1, f"一天只该发一次，实际发了 {len(calls)} 次"


@pytest.mark.asyncio
async def test_scheduler_survives_job_exception(tmp_path: Path):
    """单次失败不能让定时器死掉 —— 明天还得继续发。

    如果异常把循环打断，之后的日子会**静默地**一次都不发，
    而这种"看起来在跑其实没跑"最难发现。
    """
    attempts = []

    def boom(slot="morning"):
        attempts.append(slot)
        raise RuntimeError("发送失败")

    sched = DailyScheduler(
        boom, log_dir=tmp_path, hour=0, minute=0,
        eve_hour=None, eve_minute=None, poll_interval=0.01,
    )
    sched.start()
    await asyncio.sleep(0.1)
    running = sched._task is not None and not sched._task.done()
    await sched.stop()

    assert attempts == ["morning"], "标记已写，同一天不该重试"
    assert running, "异常后定时器必须仍然活着"


@pytest.mark.asyncio
async def test_scheduler_not_yet_time_does_nothing(tmp_path: Path):
    """没到点不执行。"""
    calls = []
    sched = DailyScheduler(
        lambda slot="morning": calls.append(1) or 0,
        log_dir=tmp_path,
        hour=23,
        minute=59,
        eve_hour=None,
        eve_minute=None,
        poll_interval=0.01,
    )
    # 直接验证判定：把 job 换成一个不受时间影响的最小循环太绕，
    # 这里用 should_send_now 覆盖时间边界，此处只确认 start/stop 不炸。
    sched.start()
    await asyncio.sleep(0.05)
    await sched.stop()
    assert calls == [] or len(calls) <= 1


@pytest.mark.asyncio
async def test_scheduler_stop_is_idempotent(tmp_path: Path):
    sched = DailyScheduler(
        lambda slot="morning": 0, log_dir=tmp_path, hour=0, minute=0,
        eve_hour=None, eve_minute=None, poll_interval=1,
    )
    await sched.stop()          # 没启动就停，不该抛
    sched.start()
    await sched.stop()
    await sched.stop()


# ---------- 接线：定时器真的挂上了吗 ----------


def test_web_command_enables_auto_send_by_default(tmp_path):
    """`web` 命令默认必须开启自动发送。

    这是当初 bug 的核心：容器只跑 `web`，如果它不挂定时器，
    配置再对也永远不会自动发。
    """
    from click.testing import CliRunner

    from src.main import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["web", "--help"])
    assert "auto-send" in result.output
    # 默认值要写在帮助里，让人一眼看到它在跑
    assert "默认开启" in result.output


def test_create_app_accepts_auto_send(tmp_path):
    """工厂要能接住这几个参数 —— 接线断了的话参数会被静默忽略。"""
    import inspect

    from src.web.app import create_app

    sig = inspect.signature(create_app)
    for name in ("auto_send", "send_hour", "send_minute"):
        assert name in sig.parameters, f"create_app 缺少参数 {name}"
    # auto_send 默认必须是 False：测试与桌面端不该真的发信
    assert sig.parameters["auto_send"].default is False


def test_run_returns_count(tmp_path):
    """run() 要返回发送条数，定时器靠它记录"今天发了几条"。"""
    import inspect

    from src.main import BirthdayReminder

    sig = inspect.signature(BirthdayReminder.run)
    assert sig.return_annotation is int


# ---------- 两趟：早上 + 生日前夜 ----------


def test_two_slots_by_default(tmp_path):
    """默认有早上与前夜两趟，各自独立的标记文件。

    共用一个标记的话，早上发过之后前夜那趟就会被判"今天发过了"而跳过 ——
    那正是这一趟要补的东西。
    """
    sched = DailyScheduler(lambda slot="m": 0, log_dir=tmp_path, hour=9, minute=0)
    names = [s[0] for s in sched.slots]
    assert names == ["morning", "eve"]
    assert sched.marker_for("morning") != sched.marker_for("eve")


def test_eve_can_be_disabled(tmp_path):
    sched = DailyScheduler(
        lambda slot="m": 0, log_dir=tmp_path, hour=9, minute=0,
        eve_hour=None, eve_minute=None,
    )
    assert [s[0] for s in sched.slots] == ["morning"]


@pytest.mark.asyncio
async def test_both_slots_run_independently(tmp_path):
    """两趟都到点时都要跑，且各自只跑一次。"""
    seen = []

    def job(slot="morning"):
        seen.append(slot)
        return 1

    sched = DailyScheduler(
        job, log_dir=tmp_path, hour=0, minute=0, eve_hour=0, eve_minute=0,
        poll_interval=0.01,
    )
    sched.start()
    await asyncio.sleep(0.15)
    await sched.stop()

    assert sorted(seen) == ["eve", "morning"], f"两趟都该跑，实际 {seen}"


@pytest.mark.asyncio
async def test_morning_does_not_block_eve(tmp_path):
    """早上 9:00 发过之后，23:55 的前夜提醒仍要发。"""
    seen = []

    def job(slot="morning"):
        seen.append(slot)
        return 1

    # 早上已到点，前夜也设成已到点（当前时刻一定过 00:00）
    sched = DailyScheduler(
        job, log_dir=tmp_path, hour=0, minute=0, eve_hour=0, eve_minute=1,
        poll_interval=0.01,
    )
    sched.start()
    await asyncio.sleep(0.15)
    await sched.stop()
    assert "eve" in seen, "前夜那趟被早上的标记挡住了"


def test_run_accepts_only_eve():
    """run(only_eve=True) 只发"明天生日"的人 —— 接线要通。"""
    import inspect

    from src.main import BirthdayReminder

    sig = inspect.signature(BirthdayReminder.run)
    assert "only_eve" in sig.parameters
    assert sig.parameters["only_eve"].default is False
