"""阳历 ↔ 农历转换。

背景（容易搞错，值得记下来）：

``config.yml`` 里的 ``lunar_birthday`` 存的是**【农历年月日】**，不是"对应的阳历日期"。
例如 ``1989-12-24`` 表示农历 1989 年腊月廿四。
``BirthdayChecker`` 直接拿字符串里的月、日与农历月日比较（见 checker.py 的农历分支），
不做任何转换。config.example.yml 里"对应的阳历日期"那句注释是错的，会误导使用者 ——
本模块的目的就是让使用者只需要填阳历生日，农历由此处自动推导。

**校验必须用 ``datetime.date``，不能交给 lunar_python。**
``Solar.fromYmd(1990, 1, 32)`` 不会报错，它会静默归一化成 1990-02-01，于是
"1月32日"能算出一个看起来正常的农历（正月初六）—— 这是假数据，比报错更危险。
因此本模块所有入口先用 ``datetime.date`` 做权威校验。

闰月的处理：lunar_python 用负月份表示闰月（闰四月 -> ``getMonth() == -4``）。
本模块把闰月当作正常月处理（存绝对值），理由：
  - 非闰年不存在该月，若保留负数，闰月出生的人几十年才匹配到一次生日；
  - 使用者的预期是"每年过一次生日"。
取舍记录在 DESIGN.md。
"""

from __future__ import annotations

import datetime
import logging
from functools import lru_cache
from typing import Optional, Tuple

from lunar_python import Lunar, Solar

logger = logging.getLogger(__name__)

#: 查找"下一次农历生日对应的阳历日期"时试算的农历年数。
#: 农历年与阳历年可能差 1（春节前后），前后各试一年已足够。
_YEAR_WINDOW = 1

#: 相对年份的说法。索引是「目标年份 - 当前年份」。
_YEAR_WORDS = {0: "今年", 1: "明年", 2: "后年"}

#: 阳历日期不合理时的统一提示。
#: 措辞与表单标签保持一致（都用"身份证出生年月日"），避免标签与报错对不上号。
INVALID_SOLAR_HINT = "身份证出生年月日不合理，请检查格式和日期"


def relative_year_label(target: datetime.date, today: Optional[datetime.date] = None) -> str:
    """把日期归到"今年 / 明年 / 后年 / 2031 年"这类说法。

    界面要告诉使用者的是"今年农历生日落在阳历哪天"，只给一个裸日期
    （如 2027-02-10）会让人以为那是今年的日期。
    """
    base = (today or datetime.date.today()).year
    diff = target.year - base
    if diff in _YEAR_WORDS:
        return _YEAR_WORDS[diff]
    return f"{target.year} 年"


def as_iso_text(value: object) -> str:
    """把配置里读到的日期值归一化成 ``YYYY-MM-DD`` 字符串。

    YAML 里裸写的 ``1990-01-20`` 会被解析成 ``datetime.date``；带引号或由界面写入的
    才是字符串。两种都要能处理，否则 ``.strip()`` / ``.split()`` 会炸。
    非法或空值返回空字符串。
    """
    if value is None:
        return ""
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    return str(value).strip()


def parse_iso_date(value: object) -> Optional[datetime.date]:
    """严格解析 ``YYYY-MM-DD``（也接受 YAML 解析出的 date 对象）。

    这是唯一的权威校验入口：它会拒绝 1990-01-32、1990-02-30、1990-13-01 这类
    不存在的日期，而 lunar_python 会把其中一部分静默归一化。
    """
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value

    text = as_iso_text(value)
    if not text:
        return None

    parts = text.split("-")
    # 必须是严格的三段 YYYY-MM-DD：拒绝 1990-1-1（未补零）与 90-1-1（年份两位）。
    if len(parts) != 3 or len(parts[0]) != 4 or len(parts[1]) != 2 or len(parts[2]) != 2:
        return None
    try:
        year, month, day = (int(part) for part in parts)
    except (ValueError, TypeError):
        return None

    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def normalize_solar_input(value: object) -> str:
    """把使用者输入的生日归一化成**存储格式** ``YYYY-MM-DD``。

    界面上要求填 8 位数字（``19900120``，身份证上的写法），但配置文件里存的
    始终是带连字符的 ``1990-01-20`` —— ``config.yml`` 里已有数据、CLI 的
    ``BirthdayChecker`` 也按这个格式解析，存储格式不能动。
    所以转换只发生在**输入层**，由本函数统一负责。

    同时容忍带连字符的写法：已有配置、旧书签里的表单值、以及 API 调用方
    都可能传 ``1990-01-20``，不该因此报错。

    **不做合法性判断** —— 日期是否真实存在由 ``parse_iso_date`` 决定，
    这里只管格式转换。既不是 8 位数字也不是 ``YYYY-MM-DD`` 时原样返回，
    交给后续校验去报错，避免在这里吞掉使用者的输入。
    """
    text = as_iso_text(value)
    if not text:
        return ""

    # 已经是存储格式就直接用
    if parse_iso_date(text) is not None:
        return text

    # 8 位纯数字：YYYYMMDD
    digits = text.replace(" ", "")
    if len(digits) == 8 and digits.isdigit():
        # 格式对但日期可能不存在（如 19901320）—— 照样转成存储格式，
        # 由 parse_iso_date 在校验层拒绝，这样报错信息能说清是"日期不合理"。
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"

    return text


def format_solar_input(value: object) -> str:
    """把存储格式 ``YYYY-MM-DD`` 反向格式化成输入框里的 8 位数字。

    与 ``normalize_solar_input`` 是一对：一个负责存进去，一个负责填回表单。
    无法识别时原样返回，避免把使用者的数据弄丢。
    """
    parsed = parse_iso_date(value)
    if parsed is None:
        return as_iso_text(value)
    return f"{parsed.year:04d}{parsed.month:02d}{parsed.day:02d}"


@lru_cache(maxsize=512)
def solar_to_lunar(solar_str: str) -> Optional[str]:
    """阳历 ``YYYY-MM-DD`` → 农历 ``YYYY-MM-DD``（存储格式，与 lunar_birthday 一致）。

    返回的年份是**农历年**，可能比阳历年份小 1（春节前出生的情况）。
    **日期非法时返回 None**（不做归一化），由调用方决定如何提示。
    """
    parsed = parse_iso_date(solar_str)
    if parsed is None:
        return None

    try:
        lunar = Solar.fromYmd(parsed.year, parsed.month, parsed.day).getLunar()
    except Exception as exc:  # lunar_python 对超范围输入可能抛异常
        logger.debug("阳历转农历失败 %r: %s", solar_str, exc)
        return None

    return f"{lunar.getYear()}-{abs(lunar.getMonth()):02d}-{lunar.getDay():02d}"


def lunar_to_solar(lunar_str: str) -> Optional[str]:
    """农历 ``YYYY-MM-DD`` → 阳历 ``YYYY-MM-DD``。

    仅用于展示与校验，不参与匹配逻辑。配置里存的是农历月日，因此这里按农历解释输入。
    """
    parsed = parse_iso_date(lunar_str)
    if parsed is None:
        return None

    try:
        # 存储时闰月用了绝对值，这里按正常月解释，与写入口径一致。
        solar = Lunar.fromYmd(parsed.year, parsed.month, parsed.day).getSolar()
    except Exception as exc:
        logger.debug("农历转阳历失败 %r: %s", lunar_str, exc)
        return None

    return f"{solar.getYear()}-{solar.getMonth():02d}-{solar.getDay():02d}"


@lru_cache(maxsize=512)
def lunar_display(lunar_str: str) -> Optional[str]:
    """农历存储格式 → 中文表示，如 ``1989-12-24`` → ``腊月廿四``。"""
    parsed = parse_iso_date(lunar_str)
    if parsed is None:
        return None

    try:
        lunar = Lunar.fromYmd(parsed.year, parsed.month, parsed.day)
    except Exception as exc:
        logger.debug("农历中文转换失败 %r: %s", lunar_str, exc)
        return None

    return f"{lunar.getMonthInChinese()}月{lunar.getDayInChinese()}"


def to_lunar_display_from_solar(solar_str: str) -> Optional[str]:
    """阳历生日 → 农历中文表示，一步到位。非法日期返回 None。"""
    lunar = solar_to_lunar(solar_str)
    return lunar_display(lunar) if lunar else None


@lru_cache(maxsize=1024)
def _lunar_date_to_solar(lunar_year: int, month: int, day: int) -> Optional[datetime.date]:
    """(农历年, 月, 日) → 阳历 date。带缓存，同一天只在首次调用时计算。"""
    try:
        solar = Lunar.fromYmd(lunar_year, month, day).getSolar()
    except Exception:
        # 该农历年没有这个月日（闰月不存在、或该月天数不足）。
        return None
    return datetime.date(solar.getYear(), solar.getMonth(), solar.getDay())


def next_solar_for_lunar(
    lunar_str: str, today: Optional[datetime.date] = None
) -> Optional[datetime.date]:
    """农历月日 → **不早于今天**的下一个阳历日期。

    用于告诉使用者"这个农历生日接下来落在阳历哪一天"，比只看"腊月廿四"更有用。

    **实现要点（性能）**：不要逐日扫描。``Solar.fromYmd(...).getLunar()`` 每次都会
    重算整年农历（``LunarYear.compute``），扫 400 天约 700 ms；而本函数用
    ``Lunar.fromYmd(农历年, 月, 日).getSolar()`` 直接反查，最多试 3 个农历年，
    实测快 20 倍以上。两者结果在跨年、闰月等边界上完全一致（有测试覆盖）。

    匹配口径与 ``BirthdayChecker`` 一致：只看月、日，年份不参与。
    """
    parsed = parse_iso_date(lunar_str)
    if parsed is None:
        return None

    month, day = parsed.month, parsed.day
    start = today or datetime.date.today()

    # 农历年与阳历年可能差 1（春节前后），前后各试一年已足够覆盖。
    for lunar_year in range(start.year - _YEAR_WINDOW, start.year + _YEAR_WINDOW + 1):
        candidate = _lunar_date_to_solar(lunar_year, month, day)
        if candidate is not None and candidate >= start:
            return candidate

    return None


def validate_solar_date(value: object) -> Tuple[bool, str]:
    """给表单实时校验用：返回 ``(是否有效, 提示文案)``。

    空值视为"未填"而非"错误"，由必填校验单独处理。
    """
    text = as_iso_text(value)
    if not text:
        return False, "请填写身份证出生年月日"

    if parse_iso_date(text) is None:
        return False, INVALID_SOLAR_HINT

    return True, "有效"
