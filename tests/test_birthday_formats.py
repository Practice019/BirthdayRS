"""生日字段的三种填法。

需求：不强制填出生年。支持

1. 只填公历（完整，含年）→ 自动算农历、显示年龄
2. 只填公历（只有月日）→ 只按公历提醒，不算农历、不显示年龄
3. 只填农历 → 只按农历提醒（有些人只过农历生日）

核心约束（实测确认）：**农历无法从"公历月日"推导** ——
1990-06-06 与 1991-06-06 的农历完全不同（农历与公历的对应逐年漂移）。
所以无年时绝不推导，宁可让使用者手填。
"""

from datetime import date, datetime

import pytest

from src.core.checker import BirthdayChecker
from src.core.config import Recipient
from src.web.domain import _parse_birth_value, build_timeline
from src.web.lunar import parse_month_day
from src.web.schemas import RecipientForm


# ---------- 解析层 ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0606", (6, 6)),
        ("06-06", (6, 6)),
        ("6-6", (6, 6)),
        ("1224", (12, 24)),
        ("0229", (2, 29)),   # 闰日在无年时合法：四年过一次
    ],
)
def test_parse_month_day_accepts(raw, expected):
    assert parse_month_day(raw) == expected


@pytest.mark.parametrize("raw", ["0230", "1301", "0000", "1990-06-06", "19900606", ""])
def test_parse_month_day_rejects(raw):
    """非法月日与完整日期都不该被当成"只有月日"。

    0230 / 1301 这类不存在的月日必须拒绝 —— 与 parse_iso_date 同一原则：
    宁可报错，不给假数据。
    """
    assert parse_month_day(raw) is None


def test_parse_birth_value_year_and_month_day():
    """完整日期带出年份（能算年龄），只有月日则年份为 None。"""
    d, y = _parse_birth_value("1990-06-06")
    assert (d, y) == (date(1990, 6, 6), 1990)

    d, y = _parse_birth_value("0606")
    assert d == date(2000, 6, 6)   # 占位年（闰年，容纳 0229）
    assert y is None, "无年时不该编造一个年份"

    assert _parse_birth_value("0230") is None


# ---------- 匹配层：无年也能提醒 ----------


def test_yearless_solar_matches():
    """只有月日的记录也要能触发提醒。

    注意提醒窗口**不含生日当天**，所以用生日前一天验证。
    """
    checker = BirthdayChecker()
    r = Recipient(name="无年", solar_birthday="0606", reminder_days=3)

    ok, extra = checker._check_birthday(r, datetime(2027, 6, 5))
    assert extra["solar_match"] is True
    assert extra["days_until"] == 1
    assert extra["age"] == 0, "无年时算不出年龄，应保持默认值"


def test_yearless_has_no_age():
    """无年记录不该显示年龄 —— 编造一个比不显示更糟。"""
    checker = BirthdayChecker()
    r = Recipient(name="无年", solar_birthday="0606", reminder_days=3)
    _, extra = checker._check_birthday(r, datetime(2027, 6, 4))
    assert extra["solar_match"] is True
    assert extra["days_until"] == 2
    assert extra["age"] == 0


def test_yearless_lunar_matches():
    """只有农历月日（如 1224 = 腊月廿四）也要能提醒。

    2027-01-31 是农历腊月廿四，窗口不含当天 → 用 01-30 验证。
    """
    checker = BirthdayChecker()
    r = Recipient(name="农历", lunar_birthday="1224", reminder_days=3)
    _, extra = checker._check_birthday(r, datetime(2027, 1, 30))
    assert extra["lunar_match"] is True
    assert extra["days_until"] == 1


def test_lunar_only_record_works():
    """只填农历、完全不填公历 —— 有些人只过农历生日。"""
    checker = BirthdayChecker()
    r = Recipient(name="只农历", lunar_birthday="1224", reminder_days=3)
    ok, extra = checker._check_birthday(r, datetime(2027, 1, 30))
    assert ok is True
    assert extra["lunar_match"] is True
    assert extra["solar_match"] is False


def test_both_calendars_give_both_dates():
    """都填时两个日期都算出来，文案才能两行都写。"""
    checker = BirthdayChecker()
    r = Recipient(
        name="都填", solar_birthday="1990-01-20", lunar_birthday="1989-12-24",
        reminder_days=3,
    )
    _, extra = checker._check_birthday(r, datetime(2026, 10, 6))
    assert "solar_next_date" in extra
    assert "lunar_next_solar_date" in extra
    # 是两个不同的日期（阳历 1/20 与农历腊月廿四 落在不同天）
    assert extra["solar_next_date"] != extra["lunar_next_solar_date"]


def test_yearless_next_date_computed():
    """无年记录的"下一次生日"也要算得出来。"""
    checker = BirthdayChecker()
    nxt = checker._next_solar_date("0606", datetime(2026, 10, 6))
    assert nxt == date(2027, 6, 6)

    nxt = checker._next_lunar_solar_date("1224", datetime(2026, 10, 6))
    assert nxt is not None and nxt > date(2026, 10, 6)


def test_leap_day_finds_next_leap_year():
    """2 月 29 日出生：非闰年要往后找到闰年，而不是报错或返回 None。"""
    checker = BirthdayChecker()
    nxt = checker._next_solar_date("0229", datetime(2026, 10, 6))
    assert nxt == date(2028, 2, 29)


# ---------- 时间轴 ----------


def test_timeline_handles_all_three_formats():
    """三种填法都要能在时间轴上算出倒计时。

    注意 ``days_until`` 取的是**阳历与农历中更近的那个** ——
    例如 1990-10-16 的农历是八月廿八，农历下一次落在 10-08，
    于是倒计时按 10-08 算（2 天）而不是阳历的 10-16（10 天）。
    所以这里只断言"算得出来且合理"，不写死具体天数。
    """
    today = datetime(2026, 10, 6)
    raw = [
        {"name": "完整", "solar_birthday": "1990-10-16"},
        {"name": "无年", "solar_birthday": "1016"},
        {"name": "只农历", "lunar_birthday": "0901"},
    ]
    views = build_timeline(raw, 3, today=today)
    by_name = {v.name: v for v in views}

    # 三种填法都要能算出下一次生日
    for name in ("完整", "无年", "只农历"):
        assert by_name[name].days_until is not None, f"{name} 没算出倒计时"
        assert by_name[name].days_until >= 0

    # 无年没有年龄，完整日期有
    assert by_name["无年"].age is None, "无年不该有年龄"
    assert by_name["完整"].age is not None

    # 阳历与农历各自的下一次日期都要算出来（文案两行都写靠它们）
    assert by_name["完整"].next_solar_birthday is not None
    assert by_name["完整"].lunar_next_solar is not None
    assert by_name["无年"].next_solar_birthday is not None


# ---------- 表单 ----------


def test_form_accepts_yearless_solar():
    f = RecipientForm.model_validate({"name": "A", "solar_birthday": "0606"})
    assert f.to_config()["solar_birthday"] == "0606"
    # 无年不推导农历（推出来是错的）
    assert "lunar_birthday" not in f.to_config()


def test_form_accepts_lunar_only():
    f = RecipientForm.model_validate({"name": "B", "lunar_birthday": "1224"})
    cfg = f.to_config()
    assert cfg["lunar_birthday"] == "1224"
    assert "solar_birthday" not in cfg


def test_form_manual_lunar_wins_over_derived():
    """手填的农历优先于自动推导 —— 使用者更清楚自己过哪个生日。"""
    f = RecipientForm.model_validate(
        {"name": "C", "solar_birthday": "19900606", "lunar_birthday": "0505"}
    )
    assert f.to_config()["lunar_birthday"] == "0505"


def test_form_derives_lunar_from_full_solar():
    f = RecipientForm.model_validate({"name": "D", "solar_birthday": "19900606"})
    # 1990-06-06 的农历是 1990-05-14
    assert f.to_config()["lunar_birthday"] == "1990-05-14"


def test_form_rejects_both_empty():
    with pytest.raises(Exception) as exc:
        RecipientForm.model_validate({"name": "E"})
    assert "至少填一个" in str(exc.value)


def test_form_rejects_invalid_month_day():
    with pytest.raises(Exception):
        RecipientForm.model_validate({"name": "F", "solar_birthday": "0230"})


# ---------- 提醒窗口：生日前 N 天到前一天，不含当天 ----------


def test_window_excludes_birthday_itself():
    """生日当天不再提醒 —— 那时祝福应该已经送到了。

    22 号生日、reminder_days=3：19/20/21 号早上各发一次，22 号不发。
    """
    checker = BirthdayChecker()
    r = Recipient(name="张三", solar_birthday="1990-10-22", reminder_days=3)

    fired = {}
    for day in range(18, 24):
        ok, extra = checker._check_birthday(r, datetime(2026, 10, day))
        fired[day] = (ok, extra.get("days_until"))

    assert fired[18] == (False, 0), "18 号还没进窗口"
    assert fired[19] == (True, 3)
    assert fired[20] == (True, 2)
    assert fired[21] == (True, 1)
    assert fired[22][0] is False, "生日当天不该再发"


def test_window_with_zero_days_is_never():
    """reminder_days=0（只在当天提醒）+ 不含当天 = 永远不发。

    这是「不含当天」带来的语义收缩，必须让人知道：填 0 等于关掉提醒。
    """
    checker = BirthdayChecker()
    r = Recipient(name="张三", solar_birthday="1990-10-22", reminder_days=0)
    for day in range(18, 24):
        ok, _ = checker._check_birthday(r, datetime(2026, 10, day))
        assert ok is False, f"{day} 号不该发"
