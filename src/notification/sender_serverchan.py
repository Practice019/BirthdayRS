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

    只讲两件事：**哪天是生日**、**祝福**。不带生肖、星座、节气、节日 ——
    这是"给寿星的祝福短信"，不是黄历；收件人是过生日的人，不需要知道
    自己那天属什么星座。
    """
    lines = [
        f"亲爱的{name}：",
        birthday_sentence(name, extra_info),
        "",
        birthday_greeting(),
    ]
    return "\n".join(lines)


def birthday_sentence(name: str, extra_info: Dict) -> str:
    """「今天 / X 天后是您的<哪种>生日」这一句。

    阳历、农历都命中时说"阳历和农历生日"；只有一个命中时只说那个。
    """
    days_until = extra_info.get("days_until", 0)
    solar = extra_info.get("solar_match")
    lunar = extra_info.get("lunar_match")

    if solar and lunar:
        kind = "阳历和农历生日"
    elif solar:
        kind = "阳历生日"
    elif lunar:
        kind = "农历生日"
    else:
        # 理论上不会走到：能被提醒就说明至少命中一种日历。
        # 兜底说"生日"，不说错话。
        kind = "生日"

    if days_until == 0:
        return f"今天是您的{kind}。"
    return f"{days_until} 天后是您的{kind}。"


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
