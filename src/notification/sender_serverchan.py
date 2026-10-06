"""ServerChan 推送发送器（https://sct.ftqq.com）。

用微信服务号把提醒推到微信。SendKey 同时标识发送者与接收者 —— 提醒是发给
使用者自己的。

**SendKey 有两种前缀，接口地址不同**（官方文档）：

- ``SCT`` 开头（Turbo 版）→ ``https://sctapi.ftqq.com/{key}.send``
- ``sctp`` 开头（Server酱³）→ ``https://{uid}.push.ft07.com/send/{key}.send``，
  其中 ``uid`` 是 key 里 ``sctp`` 与 ``t`` 之间的数字

**限制**（官方文档）：免费版每天 5 条；``title`` 不能含换行且超过 32 字符会被截断；
正文 ``desp`` 支持 Markdown。

**注意**：失败时它返回 HTTP 400（不是 200），所以状态码与 ``code`` 都要看。
"""

from datetime import date
from typing import Dict

import httpx
import logging

from src.notification.notification_base import NotificationBase
from src.notification.sender_email import retry_on_failure
from src.core.checker import Recipient

logger = logging.getLogger(__name__)

#: ServerChan Turbo 版端点（``SCT`` 开头的 SendKey）。
SCT_ENDPOINT = "https://sctapi.ftqq.com/{key}.send"

#: Server酱³ 端点模板（``sctp`` 开头的 SendKey）。``uid`` 从 key 里解析。
SC3_ENDPOINT = "https://{uid}.push.ft07.com/send/{key}.send"

#: ``title`` 超过这个长度会被服务端截断。
TITLE_LIMIT = 32


def render_plain_text(name: str, extra_info: Dict) -> str:
    """渲染推送用的纯文本正文。

    ServerChan / WxPusher / 邮件模板共用同一套措辞（见 templates/birthday.html），
    差别只在排版：推送是纯文本，邮件是 HTML。**内容必须一致** ——
    否则同一个生日，手机和邮箱收到的说法不一样，使用者会怀疑哪个是对的。

    只讲两件事：**哪天是生日**（带公历日期）、**祝福**。不带生肖、星座、
    节气、节日 —— 这是"给寿星的祝福短信"，不是黄历。

    格式（两边统一，一字不差）：
        亲爱的{name}：
        {n} 天后（{yyyy年m月d日}）是您的阳历生日。
        {n} 天后（{yyyy年m月d日}）是您的农历生日。   ← 阳历农历都命中时两行都写

        祝您生日快乐，身体健康，万事如意！
    """
    return "\n".join(build_greeting_lines(name, extra_info))


def build_greeting_lines(name: str, extra_info: Dict) -> list:
    """整条问候的段落。推送与邮件共用这一份，保证措辞一致。

    返回的段落：称呼、一句或多句生日提醒（带公历日期）、祝福语。
    排版交给各自渠道（推送 \n 连接，邮件 <p> 包裹），内容这里定死。
    """
    lines = [f"亲爱的{name}："]
    lines.extend(birthday_sentences(extra_info))
    lines.append(birthday_greeting())
    return lines


def birthday_sentences(extra_info: Dict) -> list:
    """「今天 / X 天后（YYYY年M月D日）是您的<哪种>生日」，**两行都写**。

    只要这个人有阳历生日和农历生日，就各自写一行，带各自的日期与天数 ——
    这是给寿星的祝福短信，两条都告诉他，他才知道哪个日子对得上自己。

    日期与天数由调用方算好放进 ``extra_info``：

    - ``solar_next_date``：阳历生日的下一次（``YYYY-MM-DD``）
    - ``lunar_next_solar_date``：农历生日下一次对应的阳历日期

    缺哪个就只写哪个（例如纯农历旧记录没有阳历）。
    """
    sentences = []
    solar_date = extra_info.get("solar_next_date")
    if solar_date:
        sentences.append(_birthday_line("阳历生日", solar_date, extra_info))
    lunar_date = extra_info.get("lunar_next_solar_date")
    if lunar_date:
        sentences.append(_birthday_line("农历生日", lunar_date, extra_info))

    if not sentences:
        # 兜底：日期字段缺失（旧调用方）。退回只写命中的那个，不说错话。
        return _legacy_sentences(extra_info)
    return sentences


def _birthday_line(kind: str, date_text: str, extra_info: Dict) -> str:
    """单行：今天 / X 天后（YYYY年M月D日）是您的<哪种>生日。"""
    parsed = date.fromisoformat(date_text) if isinstance(date_text, str) else date_text
    days_until = _days_from_today(parsed, extra_info)
    head = "今天" if days_until == 0 else f"{days_until} 天后"
    return f"{head}（{parsed.year}年{parsed.month}月{parsed.day}日）是您的{kind}。"


def _days_from_today(target: date, extra_info: Dict) -> int:
    """目标日期距今天的天数。今天 = extra_info 的 year/month/day，缺失用系统今天。"""
    today = _today_from_extra(extra_info)
    return (target - today).days


def _today_from_extra(extra_info: Dict) -> date:
    if extra_info.get("year"):
        try:
            return date(
                int(extra_info["year"]),
                int(extra_info["month"] or 1),
                int(extra_info["day"] or 1),
            )
        except (TypeError, ValueError):
            pass
    return date.today()


def _legacy_sentences(extra_info: Dict) -> list:
    """旧调用方没提供日期字段时：只写命中的那个，天数用 days_until。"""
    days_until = extra_info.get("days_until", 0)
    date_text = _birthday_date_text(extra_info)
    head = "今天" if days_until == 0 else f"{days_until} 天后"

    sentences = []
    if extra_info.get("solar_match"):
        sentences.append(f"{head}（{date_text}）是您的阳历生日。")
    if extra_info.get("lunar_match"):
        sentences.append(f"{head}（{date_text}）是您的农历生日。")
    if not sentences:
        sentences.append(f"{head}（{date_text}）是您的生日。")
    return sentences


def _birthday_date_text(extra_info: Dict) -> str:
    """生日那天的公历日期。今天 + days_until。

    需要 ``year / month / day``（今天）与 ``days_until``；缺了就用系统今天兜底，
    保证预览等场景也能渲染。
    """
    from datetime import date, timedelta

    today = None
    if extra_info.get("year"):
        try:
            today = date(
                int(extra_info["year"]),
                int(extra_info["month"] or 1),
                int(extra_info["day"] or 1),
            )
        except (TypeError, ValueError):
            today = None
    if today is None:
        today = date.today()

    birthday = today + timedelta(days=int(extra_info.get("days_until") or 0))
    return f"{birthday.year}年{birthday.month}月{birthday.day}日"


def birthday_greeting() -> str:
    """祝福语。与邮件模板里那句保持一字不差。"""
    return "祝您生日快乐，身体健康，万事如意！"


def resolve_endpoint(sckey: str) -> str:
    """按 SendKey 前缀选端点。识别不出就按 Turbo 版处理（也是主流用法）。

    两种 key 的用户体系不通用，发错端点会得到含糊的鉴权错误，所以这里显式判断。
    """
    key = (sckey or "").strip()
    if key.startswith("sctp"):
        # 形如 sctp1234tXXXX，uid 是 sctp 与 t 之间的数字
        rest = key[len("sctp"):]
        uid = rest.split("t", 1)[0]
        if uid.isdigit():
            return SC3_ENDPOINT.format(uid=uid, key=key)
        logger.warning("SendKey 以 sctp 开头但解析不出 uid，按 Turbo 版端点尝试")
    return SCT_ENDPOINT.format(key=key)


class ServerChanSender(NotificationBase):
    def __init__(self, sckey: str):
        self.sckey = sckey

    def render_content(self, name: str, template_file: str, extra_info: Dict) -> str:
        # template_file 在此渠道用不上：推送没有"模板"概念，文案由代码生成。
        return render_plain_text(name, extra_info)

    @retry_on_failure()
    async def send(self, recipient: Recipient, content: str, days_until: int, age: int):
        url = resolve_endpoint(self.sckey)
        title = f"生日提醒- {recipient.name} - {age}岁 - {days_until}天后"
        # title 不能含换行，超长会被服务端截断 —— 主动截断，避免截出半个字。
        data = {"title": title[:TITLE_LIMIT], "desp": content}
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, data=data)
            if resp.status_code == 200 and resp.json().get("code") == 0:
                logger.info(f"Server酱推送成功: {recipient.name}")
            else:
                # 不回显原始响应体：它可能回显含 SendKey 的内容，而异常会进日志。
                logger.error(
                    "Server酱推送失败: %s, HTTP %s", recipient.name, resp.status_code
                )
                raise Exception(
                    f"Server酱推送失败（HTTP {resp.status_code}）。"
                    "常见原因：免费额度每天 5 条已用完、SendKey 无效或被停用。"
                )
