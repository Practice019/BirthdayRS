"""界面用的领域计算：下一次生日时间轴、提醒预览。

**为什么不用 ``BirthdayChecker`` 逐日扫描**

最初实现让 checker 用 ``reminder_days=366`` 扫一整年，这是主页面加载要 2 秒的根因：
checker 对窗口内**每一天**都调用 ``Solar.getLunar()``，而 lunar_python 每次都会重算
整年农历（``LunarYear.compute``），没有缓存。2 个人 × 2 种日历 × 366 天 ≈ 1464 次重算。

「下一次生日」其实可以直接算出来：

- 阳历：今年的月/日，已过则取明年（2 月 29 日需向后找到闰年）。
- 农历：``Lunar.fromYmd(农历年, 月, 日).getSolar()`` 直接反查，最多试 3 个农历年。

结果与逐日扫描完全一致（``tests/test_web.py`` 有对拍测试覆盖跨年、闰月等边界），
但把 1464 次重算降到个位数。

**与之保持一致的口径**：匹配只看月/日、年份不参与；年龄 = 命中日期的阳历年 - 出生年。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from lunar_python import Solar

from src.core.config import Recipient, SMTPConfig
from src.notification.sender_email import EmailSender
from src.notification.sender_serverchan import ServerChanSender
from src.web.lunar import (
    lunar_display,
    next_solar_for_lunar,
    parse_iso_date,
    solar_to_lunar,
)

logger = logging.getLogger(__name__)

#: 阳历生日为 2 月 29 日时，需要向后找到闰年。9 年足够覆盖任何情况。
_SOLAR_YEAR_SEARCH = 9

# 预览用的占位配置：render_content 不读取 SMTP 凭据与 sckey，
# 它们只在 send() 里用到，因此这里不触碰真实凭据。端口用 0 避免任何误连。
_PREVIEW_SMTP = SMTPConfig(
    host="preview.invalid",
    port=0,
    username="preview@invalid",
    password="",
)

_KNOWN_FIELDS = {
    "name",
    "email",
    "solar_birthday",
    "lunar_birthday",
    "reminder_days",
    "template_file",
}

#: 中文星期，索引对应 date.weekday()（0 = 周一）。
_WEEKDAYS = "一二三四五六日"


@dataclass(frozen=True)
class RecipientView:
    """一条收件人视图。

    口径：**阳历生日是唯一事实来源**，农历由其自动推导。
    ``lunar_birthday`` 是配置里存的值（用于兼容 CLI），``lunar_from_solar`` 是
    界面实际展示的推导结果。两者不一致时（历史手填值有误）以推导值为准。
    """

    index: int
    name: str
    email: Optional[str]
    solar_birthday: Optional[str]
    lunar_birthday: Optional[str]
    reminder_days: int
    template_file: str

    days_until: Optional[int]
    next_birthday: Optional[date]
    age: Optional[int]
    week_name: Optional[str]
    #: 是否会真的收到提醒：取决于 days_until 是否落在 reminder_days 窗口内。
    will_trigger: bool
    #: 由阳历推导的农历生日（存储格式），界面以它为准。
    lunar_from_solar: Optional[str] = None
    #: 农历的中文表示，如"腊月廿四"。
    lunar_text: Optional[str] = None
    #: **下次农历生日**落在阳历哪一天（农历月日反查的结果）。
    #: 例：农历正月初五 -> 2027-02-10。
    lunar_next_solar: Optional[date] = None
    #: **下次阳历生日**（身份证上的月/日，每年重复）。
    #: 例：01-31 -> 2027-01-31。与 lunar_next_solar 是两回事，不可互相代用。
    next_solar_birthday: Optional[date] = None
    #: 配置里手填的农历值与推导值是否不一致（历史数据可能填错）。
    lunar_mismatch: bool = False
    #: 阳历生日是否「填了但不合法」（如 1990-01-32）。
    #: 注意与「没填阳历」（纯农历旧条目）区分：后者仍可计算。
    solar_invalid: bool = False
    #: 使用者自己写的备注（这个人的相关信息）。只在自己界面看，不进通知正文。
    note: Optional[str] = None
    #: 提醒窗口是否来自全局默认值（配置里没单独给这个人写 ``reminder_days``）。
    #: 界面上要能区分"我给他单独设了 3 天"和"他跟着全局默认走"。
    reminder_days_inherited: bool = False
    #: 提醒发给谁看：``self``（只发给我）或 ``group``（发给团体所有人）。
    #: 时间轴上必须能看出来 —— 广播给团体是不可撤销的，混在一起很容易发错。
    audience: str = "self"

    @property
    def solar_valid(self) -> bool:
        """阳历生日是否可用（填了且合法）。"""
        return bool(self.solar_birthday) and not self.solar_invalid

    @property
    def birthday_kinds(self) -> List[str]:
        """该收件人配置了哪几种生日。"""
        kinds = []
        if self.solar_birthday:
            kinds.append("阳历")
        if self.lunar_birthday:
            kinds.append("农历")
        return kinds

    @property
    def is_lunar_only(self) -> bool:
        """只有农历、没有阳历的历史条目。

        新口径以阳历为唯一来源，这类条目无法自动推导，需要在界面上明确标出，
        否则会把农历值误当成阳历日期看。
        """
        return bool(self.lunar_birthday) and not self.solar_birthday

    @property
    def next_is_lunar(self) -> Optional[bool]:
        """「下一次生日」取的是农历还是阳历。

        阳历与农历两个日期都可能存在，取更近的那个（``_next_birthday_fast`` 已排序）。
        界面必须标出中标的是哪一个，否则使用者看到两个不同日期会以为算错了。

        - ``True``  → 农历生日更近
        - ``False`` → 阳历生日更近
        - ``None``  → 无法判断（日期缺失或不可识别）
        """
        if self.next_birthday is None:
            return None
        if self.next_solar_birthday == self.next_birthday:
            return False
        if self.lunar_next_solar == self.next_birthday:
            return True
        # 两个候选都不等于中点（理论上不该发生），按阳历记。
        return False

    @property
    def next_kind_text(self) -> str:
        """下一次生日的日历标注，如"阳历"、"农历"。"""
        if self.next_is_lunar is None:
            return ""
        return "农历" if self.next_is_lunar else "阳历"

    @property
    def status(self) -> str:
        """状态标签，用于界面样式与文案。"""
        if not self.solar_valid:
            return "unknown"
        if self.days_until is None:
            return "unknown"
        if self.days_until == 0:
            return "today"
        if self.will_trigger:
            return "soon"
        return "idle"

    @property
    def status_text(self) -> str:
        """面向使用者的状态描述（不暴露 days_until 之类的内部概念）。"""
        if self.solar_invalid:
            return "身份证出生年月日不合理"
        if self.days_until is None:
            return "日期无法识别"
        if self.days_until == 0:
            return "今天发送"
        if self.will_trigger:
            return f"还有 {self.days_until} 天 · 会发送"
        return f"还有 {self.days_until} 天"


def _recipient_from_raw(raw: Dict[str, Any]) -> Recipient:
    """把配置里的普通 dict 转成 ``Recipient``。

    只传 config 里真实存在的键，至少一个生日的校验交给 ``Recipient.__post_init__``。
    """
    kwargs = {k: v for k, v in raw.items() if k in _KNOWN_FIELDS}
    return Recipient(**kwargs)


def _reminder_days(raw: Dict[str, Any], fallback: int) -> int:
    value = raw.get("reminder_days")
    if value is None:
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _parse_iso(value: object) -> Optional[date]:
    """严格解析 YYYY-MM-DD（也接受 YAML 解析出的 date 对象）。

    拒绝 1990-01-32 这类不存在的日期 —— lunar_python 会静默归一化它。
    统一的实现在 ``src.web.lunar.parse_iso_date``，这里只是语义化的别名。
    """
    return parse_iso_date(value)


def _next_solar_occurrence(birth: date, today: date) -> Optional[date]:
    """阳历生日的下一次出现日期（只看月/日）。"""
    for offset in range(_SOLAR_YEAR_SEARCH):
        year = today.year + offset
        try:
            candidate = date(year, birth.month, birth.day)
        except ValueError:
            # 2 月 29 日在非闰年不存在，继续找下一个闰年。
            continue
        if candidate >= today:
            return candidate
    return None


def next_solar_occurrence(birth: date, today: Optional[date] = None) -> Optional[date]:
    """阳历生日的下一次出现日期。``_next_solar_occurrence`` 的公开包装。

    与 ``next_solar_for_lunar`` 是**两个不同的概念**，不要互相代用：
      - 本函数：阳历月/日每年重复，如 01-31 -> 2027-01-31
      - ``next_solar_for_lunar``：农历月日对应的阳历日期，如 正月初五 -> 2027-02-10
    """
    return _next_solar_occurrence(birth, today or date.today())


def _today_meta(today: date) -> Dict[str, Any]:
    """某个日期的农历/干支/节日信息。单次转换，不扫描。"""
    solar = Solar.fromYmd(today.year, today.month, today.day)
    lunar = solar.getLunar()

    lunar_festivals = lunar.getFestivals() or []
    solar_festivals = solar.getFestivals() or []

    return {
        # 日期本身（供模板直出"今天是什么日子"）
        "year": today.year,
        "month": today.month,
        "day": today.day,
        "gz_year": lunar.getYearInGanZhi(),
        "gz_month": lunar.getMonthInGanZhi(),
        "gz_day": lunar.getDayInGanZhi(),
        "gz_hour": lunar.getTimeInGanZhi(),
        "lunar_month": f"{lunar.getMonthInChinese()}月",
        "lunar_day": lunar.getDayInChinese(),
        "week_name": _WEEKDAYS[today.weekday()],
        "constellation": solar.getXingZuo(),
        "lunar_festival": "、".join(lunar_festivals),
        "solar_festival": "、".join(solar_festivals),
        "solar_term": lunar.getJieQi() or "",
    }


def _next_birthday_fast(
    recipient: Recipient, today: date
) -> Optional[Dict[str, Any]]:
    """算出下一次生日，返回与 checker 同形的信息。

    与逐日扫描等价（有对拍测试），但只做常数次农历转换。
    """
    candidates: List[Dict[str, Any]] = []

    # 阳历分支
    birth_solar = _parse_iso(recipient.solar_birthday)
    if birth_solar is not None:
        hit = _next_solar_occurrence(birth_solar, today)
        if hit is not None:
            candidates.append(
                {
                    "date": hit,
                    "days_until": (hit - today).days,
                    "age": hit.year - birth_solar.year,
                    "solar_match": True,
                    "lunar_match": False,
                }
            )

    # 农历分支
    lunar_source = recipient.lunar_birthday
    if lunar_source:
        # next_solar_for_lunar 用农历月日反查，最多 3 次转换。
        lunar_birth = _parse_iso(lunar_source)
        if lunar_birth is not None:
            hit = next_solar_for_lunar(lunar_source, today)
            if hit is not None:
                candidates.append(
                    {
                        "date": hit,
                        "days_until": (hit - today).days,
                        # 与 checker 一致：命中日期的阳历年 - 农历字符串的年份
                        "age": hit.year - lunar_birth.year,
                        "solar_match": False,
                        "lunar_match": True,
                    }
                )

    if not candidates:
        return None

    candidates.sort(key=lambda c: c["days_until"])
    nearest = dict(candidates[0])

    # 把**两个**生日各自的下一次日期都带上（不只命中的那个）。
    # 通知文案要两行都写：阳历生日、农历生日各一行 —— 给寿星的祝福短信
    # 两条都告诉他，他才知道哪个日子对得上自己。日期独立于"哪个先到"。
    for c in candidates:
        if c["solar_match"]:
            nearest["solar_next_date"] = c["date"].isoformat()
        if c["lunar_match"]:
            nearest["lunar_next_solar_date"] = c["date"].isoformat()
    return nearest


def build_recipient_view(
    index: int,
    raw: Dict[str, Any],
    default_reminder_days: int,
    today: date,
) -> RecipientView:
    """计算单个收件人的下一次生日。"""
    reminder_days = _reminder_days(raw, default_reminder_days)
    # 配置里没写 = 跟着全局默认走。界面上要标出来，否则使用者分不清
    # "这个人的 3 天" 是单独设的还是继承来的。
    reminder_days_inherited = raw.get("reminder_days") is None
    solar = raw.get("solar_birthday")
    configured_lunar = raw.get("lunar_birthday")

    solar_date = _parse_iso(solar)
    solar_valid = bool(solar) and solar_date is not None

    # 阳历不合理时**不推导农历** —— 否则会算出"1990-01-32 -> 正月初六"这类假数据。
    derived_lunar = solar_to_lunar(solar) if solar_valid else None
    lunar_text = lunar_display(derived_lunar) if derived_lunar else None

    def base(**overrides: Any) -> RecipientView:
        data: Dict[str, Any] = {
            "index": index,
            "name": str(raw.get("name") or "未命名"),
            "email": raw.get("email"),
            "solar_birthday": solar,
            "lunar_birthday": configured_lunar,
            "reminder_days": reminder_days,
            "template_file": str(raw.get("template_file") or "birthday.html"),
            "days_until": None,
            "next_birthday": None,
            "age": None,
            "week_name": None,
            "will_trigger": False,
            "lunar_from_solar": derived_lunar,
            "lunar_text": lunar_text,
            "lunar_next_solar": None,
            "next_solar_birthday": None,
            "note": (raw.get("note") or None),
            "lunar_mismatch": bool(
                configured_lunar and derived_lunar and configured_lunar != derived_lunar
            ),
            "solar_invalid": bool(solar) and not solar_date,
            "reminder_days_inherited": reminder_days_inherited,
            "audience": (raw.get("audience") or "self"),
        }
        data.update(overrides)
        return RecipientView(**data)

    try:
        recipient = _recipient_from_raw(raw)
    except (ValueError, TypeError):
        # 缺两种生日（如配置写漏了）：保留在列表里并标注，不让整页崩掉。
        return base()

    # 阳历**填了但不合法**（如 1990-01-32）→ 到此为止：不推导农历、不算倒计时，
    # 由界面提示。注意"没填阳历"（纯农历旧条目）不在此列，那种情况仍应能算。
    if solar and not solar_valid:
        return base()

    if not solar_valid and not configured_lunar:
        return base()

    # 农历以推导值为准：即使配置里手填了错误值，匹配也用推导值。
    effective = recipient
    if derived_lunar and configured_lunar != derived_lunar:
        effective = Recipient(
            name=recipient.name,
            email=recipient.email,
            solar_birthday=recipient.solar_birthday,
            lunar_birthday=derived_lunar,
            reminder_days=recipient.reminder_days,
            template_file=recipient.template_file,
        )

    hit = _next_birthday_fast(effective, today)
    if hit is None:
        return base()

    next_birthday = hit["date"]
    days_until = hit["days_until"]
    # 两个不同的"阳历日期"，必须分别算：
    #   lunar_next_solar    = 农历生日落在阳历哪天（农历月日反查）
    #   next_solar_birthday = 阳历生日（身份证月/日）的下一次
    lunar_next = next_solar_for_lunar(derived_lunar, today) if derived_lunar else None
    next_solar_birthday = (
        _next_solar_occurrence(solar_date, today) if solar_date else None
    )

    return base(
        days_until=days_until,
        next_birthday=next_birthday,
        age=hit["age"],
        week_name=_WEEKDAYS[next_birthday.weekday()],
        will_trigger=days_until <= reminder_days,
        lunar_next_solar=lunar_next,
        next_solar_birthday=next_solar_birthday,
    )


def build_timeline(
    recipients: List[Dict[str, Any]],
    default_reminder_days: int,
    today: Optional[datetime] = None,
) -> List[RecipientView]:
    """构建全部收件人的时间轴，**最快过生日的排在最上面**。

    排序键是三级，缺一不可：

    1. ``days_until is None`` —— 日期认不出来的排最后（它们没有倒计时可比）
    2. ``days_until`` —— 主键，升序。这是"最上面是最快过生日的"的来源
    3. ``index`` —— 同一天生日时按配置里的先后，保证顺序**稳定**

    第三级不是可有可无的：只按前两级排的话，同一天生日的几条谁在上取决于
    Python 排序的稳定性与输入顺序，一旦上游顺序变化（比如批量改了受众后
    重新读盘），页面顺序就会莫名其妙地跳。
    """
    today_date = (today or datetime.now()).date()
    views = [
        build_recipient_view(i, raw, default_reminder_days, today_date)
        for i, raw in enumerate(recipients)
    ]
    return sorted(
        views,
        key=lambda v: (
            v.days_until is None,
            v.days_until if v.days_until is not None else 0,
            v.index,
        ),
    )


def _extra_from_hit(hit: Dict[str, Any], today_date: date) -> Dict[str, Any]:
    """把 ``_next_birthday_fast`` 的结果合并成 extra_info。

    **两处渲染入口（预览 render_preview、测试发送 render_reminder_for）
    都必须走这里。** 曾经它们各自挑字段，新增字段时漏改一处就会出现
    "预览与实际发送内容不一致" —— 而预览的意义正是"所见即将发"。
    """
    extra: Dict[str, Any] = dict(_today_meta(today_date))
    extra["days_until"] = hit["days_until"]
    extra["age"] = hit["age"]
    # 不再往 extra 里填生肖 —— 通知文案是祝福短信，不是黄历。
    extra["solar_match"] = hit["solar_match"]
    extra["lunar_match"] = hit["lunar_match"]
    # 两个生日各自的下一次日期：文案两行都写（见 build_greeting_lines）
    for key in ("solar_next_date", "lunar_next_solar_date"):
        if key in hit:
            extra[key] = hit[key]
    return extra


def render_preview(
    raw: Dict[str, Any],
    templates_dir: str,
    today: Optional[datetime] = None,
) -> Dict[str, Any]:
    """用真实的发送器渲染"如果现在发送，会发出去什么"。

    复用 ``EmailSender`` / ``ServerChanSender``：预览与实际发送走同一套模板与文案逻辑，
    预览才有参考价值。
    """
    today_date = (today or datetime.now()).date()

    solar_raw = raw.get("solar_birthday")
    if solar_raw and _parse_iso(solar_raw) is None:
        return {
            "ok": False,
            "reason": f"阳历生日「{solar_raw}」不合理，请改成 1990-01-20 这样的日期。",
        }

    try:
        recipient = _recipient_from_raw(raw)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "reason": f"这条收件人配置不完整：{exc}"}

    # 与时间轴保持同一口径：农历一律用阳历推导值。
    derived_lunar = solar_to_lunar(recipient.solar_birthday)
    if derived_lunar:
        recipient = Recipient(
            name=recipient.name,
            email=recipient.email,
            solar_birthday=recipient.solar_birthday,
            lunar_birthday=derived_lunar,
            reminder_days=recipient.reminder_days,
            template_file=recipient.template_file,
        )

    hit = _next_birthday_fast(recipient, today_date)
    if hit is None:
        return {"ok": False, "reason": "无法计算这个人的下一次生日，请检查日期格式。"}

    # 邮件里的「今日信息」块描述的是发送当天，因此用今天的元信息。
    extra = _extra_from_hit(hit, today_date)

    template_file = recipient.template_file or "birthday.html"

    # 邮件正文是 HTML（见 DESIGN.md §6.3），渲染出来就是会发出去的原文。
    email_html: Optional[str] = None
    email_error: Optional[str] = None
    try:
        email_html = EmailSender(_PREVIEW_SMTP, templates_dir).render_content(
            recipient.name, template_file, extra
        )
    except Exception as exc:  # 模板缺失或语法错误
        email_error = f"渲染邮件模板失败：{type(exc).__name__}"

    try:
        push_text = ServerChanSender("preview").render_content(
            recipient.name, template_file, extra
        )
    except Exception:
        push_text = None

    return {
        "ok": True,
        "email_html": email_html,
        "email_error": email_error,
        "push_text": push_text,
        "days_until": extra["days_until"],
        "age": extra["age"],
    }


def render_reminder_for(recipient: Recipient, today: date) -> Optional[Dict[str, Any]]:
    """为某个收件人生成提醒所需的 extra_info（含真实的 days_until / age）。

    测试发送与预览共用它，保证「测试发出去的」和「实际会发的」是同一种内容。
    """
    hit = _next_birthday_fast(recipient, today)
    if hit is None:
        return None

    return _extra_from_hit(hit, today)


def build_sendable_recipient(raw: Dict[str, Any], fallback_email: Optional[str]) -> Recipient:
    """把配置条目转成可发送的 ``Recipient``。

    - 农历用阳历推导值（与时间轴、预览同一口径）
    - 生日对象本人没有邮箱时，用配置里的接收邮箱顶上 —— 提醒是发给使用者的，
      收件人记录里不再有邮箱字段，所以这一步是常态而非例外
    """
    recipient = _recipient_from_raw(raw)

    derived_lunar = solar_to_lunar(recipient.solar_birthday)
    email = recipient.email or fallback_email or ""

    return Recipient(
        name=recipient.name,
        email=email,
        solar_birthday=recipient.solar_birthday,
        lunar_birthday=derived_lunar or recipient.lunar_birthday,
        reminder_days=recipient.reminder_days,
        template_file=recipient.template_file,
        note=recipient.note,
    )


def resolve_receive_email(config) -> Optional[str]:
    """找出提醒实际送达的邮箱（使用者自己的）。

    按生效顺序找：resend 配置 → smtp 配置。与选择哪个渠道无关地兜底，
    因为测试发送与真实发送都必须有个明确的收件人。
    """
    if getattr(config, "resend_config", None):
        target = config.resend_config.default_receive_email
        if target:
            return str(target)
    if getattr(config, "smtp_config", None):
        target = config.smtp_config.default_receive_email
        if target:
            return str(target)
    return None


async def send_reminder_now(
    recipient: Recipient,
    extra_info: Dict[str, Any],
    senders: List[Any],
) -> List[Dict[str, Any]]:
    """立即通过给定发送器发出一条提醒，返回每个渠道的结果。

    **这是真实发送**，会真的发出邮件/推送。调用方需自行确认这是使用者的意图。
    逐个渠道独立处理：一个失败不影响其他渠道，与 ``BirthdayReminder.run`` 的行为一致。
    """
    results: List[Dict[str, Any]] = []
    for sender in senders:
        name = type(sender).__name__
        try:
            content = sender.render_content(
                name=recipient.name,
                template_file=recipient.template_file or "birthday.html",
                extra_info=extra_info,
            )
            await sender.send(
                recipient=recipient,
                content=content,
                days_until=extra_info["days_until"],
                age=extra_info["age"],
            )
            results.append({"channel": name, "ok": True, "detail": "已发送"})
        except Exception as exc:
            logger.warning("测试发送失败 channel=%s: %s", name, exc)
            results.append({"channel": name, "ok": False, "detail": str(exc)})
    return results
