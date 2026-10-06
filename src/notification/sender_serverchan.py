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

    ServerChan 与 WxPusher 共用这一份：换渠道时文案一致，使用者不用适应两种格式，
    也只需要在一处维护。
    """
    lines = [f"亲爱的{name}："]
    if extra_info.get("days_until", 0) == 0:
        if extra_info.get("solar_match") and extra_info.get("lunar_match"):
            lines.append("今天是您的阳历和农历生日，祝您生日快乐！🎉")
        elif extra_info.get("solar_match"):
            lines.append("今天是您的阳历生日，祝您生日快乐！🎉")
        else:
            lines.append("今天是您的农历生日，祝您生日快乐！🎉")
    else:
        if extra_info.get("solar_match") and extra_info.get("lunar_match"):
            lines.append(f"{extra_info['days_until']}天后是您的阳历和农历生日！")
        elif extra_info.get("solar_match"):
            lines.append(f"{extra_info['days_until']}天后是您的阳历生日！")
        else:
            lines.append(f"{extra_info['days_until']}天后是您的农历生日！")
    # 追加命理和节日信息
    lines.append(f"生肖：{extra_info.get('zodiac', '')}")
    lines.append(f"星座：{extra_info.get('constellation', '')}")
    if extra_info.get("solar_term"):
        lines.append(f"节气：{extra_info['solar_term']}")
    if extra_info.get("lunar_festival"):
        lines.append(f"农历节日：{extra_info['lunar_festival']}")
    if extra_info.get("solar_festival"):
        lines.append(f"阳历节日：{extra_info['solar_festival']}")
    return "\n".join(lines)


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
