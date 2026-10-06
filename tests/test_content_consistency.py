"""邮件与推送的内容一致性。

两条渠道的措辞分别写在模板（HTML）与代码（纯文本）里，天然容易漂移。
同一个生日，手机和邮箱收到的说法必须一样 —— 否则使用者会怀疑哪个是对的。

这里用同一组 extra_info 渲染两边，抽出文字比对：
排版不同（HTML vs 纯文本）是允许的，**措辞不同不允许**。
"""

import re
from pathlib import Path

import pytest

from src.core.config import SMTPConfig
from src.notification.sender_email import EmailSender
from src.notification.sender_serverchan import render_plain_text

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = str(REPO_ROOT / "templates")

#: 刻意带上生肖/星座等字段，验证它们**不再**出现在正文里。
_BASE_EXTRA = {
    "days_until": 3,
    "age": 36,
    "solar_match": True,
    "lunar_match": False,
    "zodiac": "虎",
    "constellation": "天秤",
    "solar_term": "寒露",
    "lunar_festival": "",
    "solar_festival": "",
}

_SMTP = SMTPConfig(host="x", port=1, username="u", password="p")


def _email_text(name: str, extra: dict) -> str:
    """渲染邮件并压成纯文本，去掉标签只为比对措辞。"""
    html = EmailSender(_SMTP, TEMPLATES).render_content(name, "birthday.html", extra)
    text = re.sub(r"<[^>]+>", "\n", html)
    # 去掉标题与页脚标题，它们只存在于邮件
    text = text.replace("生日提醒", "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)


def _push_text(name: str, extra: dict) -> str:
    return render_plain_text(name, extra)


def _sentences(text: str) -> set:
    """把文本压成"句子集合"，忽略空白差异。"""
    text = text.replace("：", ":").replace("亲爱的", "")
    parts = re.split(r"[。\n]+", text)
    return {re.sub(r"\s+", "", p) for p in parts if p.strip()}


@pytest.mark.parametrize(
    "extra,label",
    [
        ({"days_until": 3, "solar_match": True, "lunar_match": False}, "3天后·阳历"),
        ({"days_until": 7, "solar_match": False, "lunar_match": True}, "7天后·农历"),
        ({"days_until": 1, "solar_match": True, "lunar_match": True}, "1天后·双历"),
        ({"days_until": 0, "solar_match": True, "lunar_match": False}, "当天·阳历"),
        ({"days_until": 0, "solar_match": False, "lunar_match": True}, "当天·农历"),
        ({"days_until": 0, "solar_match": True, "lunar_match": True}, "当天·双历"),
    ],
)
def test_email_and_push_say_the_same_thing(extra, label):
    """同一个生日，邮件与推送的措辞必须一致。"""
    full = dict(_BASE_EXTRA)
    full.update(extra)

    email_sentences = _sentences(_email_text("张三", full))
    push_sentences = _sentences(_push_text("张三", full))

    missing_in_push = email_sentences - push_sentences
    missing_in_email = push_sentences - email_sentences

    assert not missing_in_push, f"[{label}] 推送缺少邮件里的话：{missing_in_push}"
    assert not missing_in_email, f"[{label}] 邮件缺少推送里的话：{missing_in_email}"


def test_content_has_no_zodiac_or_constellation():
    """正文不带生肖、星座、节气、节日 —— 这是给寿星的祝福，不是黄历。"""
    text = _push_text("张三", _BASE_EXTRA)
    for word in ("生肖", "星座", "节气", "节日", "虎", "天秤", "寒露"):
        assert word not in text, f"正文里还有「{word}」：{text!r}"

    email = _email_text("张三", _BASE_EXTRA)
    for word in ("生肖", "星座", "节气", "节日", "虎", "天秤", "寒露"):
        assert word not in email, f"邮件里还有「{word}」：{email!r}"


def test_push_mentions_days_and_greeting():
    """推送正文要有"几天后"和祝福语。"""
    text = _push_text("张三", _BASE_EXTRA)
    assert "3 天后" in text
    assert "阳历生日" in text
    assert "生日快乐" in text


def test_same_day_wording():
    """当天时要说"今天"，不能还说"0 天后"。"""
    text = _push_text("张三", dict(_BASE_EXTRA, days_until=0))
    assert "今天（2026年10月6日）是您的" in text
    assert "0 天后" not in text


def test_both_calendars_matched():
    """阳历农历都命中时**分开两行**，各自带公历日期。"""
    extra = dict(_BASE_EXTRA, solar_match=True, lunar_match=True,
                 year=2026, month=10, day=6, days_until=16)
    text = _push_text("张三", extra)
    assert "是您的阳历生日。" in text
    assert "是您的农历生日。" in text
    # 两行必须都带同一个日期
    assert text.count("2026年10月22日") == 2


def test_birthday_date_is_today_plus_days():
    """带公历日期 = 今天 + days_until，两边一致。"""
    email = _email_text("张三", dict(_BASE_EXTRA, days_until=16))
    push = _push_text("张三", dict(_BASE_EXTRA, days_until=16))
    # _BASE_EXTRA 没有 year/month/day，走系统今天兜底 —— 所以两边必然同一天
    assert "16 天后（" in push
    assert "16 天后（" in email


def test_birthday_date_uses_extra_year_month_day():
    """有 year/month/day 时用它算生日日期，不依赖系统时钟（可测）。"""
    text = _push_text("张三", dict(_BASE_EXTRA, year=2026, month=10, day=6, days_until=16))
    assert "16 天后（2026年10月22日）是您的阳历生日。" in text


def test_neither_calendar_matched_does_not_lie():
    """两种都没命中时不能瞎说一种日历（理论上不会发生，兜底别说错话）。"""
    text = _push_text("张三", dict(_BASE_EXTRA, solar_match=False, lunar_match=False))
    assert "生日" in text
    assert "阳历生日" not in text
    assert "农历生日" not in text
