"""Resend 邮件发送器。

用 Resend 的 HTTP API 发提醒邮件（https://resend.com）。

与 ``EmailSender``（SMTP）并存，由 ``notification.start_notification`` 选择用哪个。

正文用 ``html`` 字段发送。Resend 的 API 只有 ``html`` 与 ``text`` 两个正文字段，
不接受 ``markdown`` —— 邮件协议本身也没有 Markdown 正文类型，客户端只会把它
当普通文字显示。要写 Markdown 必须先转成 HTML 再发。

三个必须知道的约束：

1. **发件地址受域名验证限制。** Resend 要求 ``from`` 使用已在后台验证过的域名；
   没有自有域名时只能用 ``onboarding@resend.dev``。
2. **但发件人显示名可以自定义**，写成 ``显示名 <地址>`` 即可。否则收件人看到的
   发件人就是 "onboarding"，很难看。由 ``resend.from_name`` 配置。
3. **受限 API Key 只能发给 key 持有者自己的邮箱。** Resend 的测试用 key（权限为
   "only send emails"）会拒绝其他收件人，返回 403 并说明允许的地址。
   要发给任意邮箱，必须在 resend.com/domains 验证域名并把 ``from`` 换成该域名。
   这不是代码问题，改配置或换 key 即可。
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from src.core.config import DEFAULT_RESEND_FROM, ResendConfig
from src.notification.notification_base import NotificationBase
from src.notification.sender_email import retry_on_failure

logger = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"
#: 兼容旧引用
DEFAULT_FROM = DEFAULT_RESEND_FROM


class ResendSendError(RuntimeError):
    """Resend 返回了错误响应，带上原始信息便于排查。"""


class ResendSender(NotificationBase):
    """通过 Resend HTTP API 发送 HTML 邮件。"""

    def __init__(self, resend_config: ResendConfig, templates_dir: str):
        self.config = resend_config
        self.templates_dir = templates_dir
        # 复用邮件模板，保证两种渠道内容一致。
        # autoescape=True：模板产出 HTML，变量里的特殊字符需要转义。
        from jinja2 import Environment, FileSystemLoader

        self.env = Environment(loader=FileSystemLoader(templates_dir), autoescape=True)

    def render_content(self, name: str, template_file: str, extra_info: dict) -> str:
        # 与 EmailSender 走同一个渲染入口：文案段落来自 build_greeting_lines，
        # 模板只排版。各自拼句子就是邮件内容漂移的根源。
        from src.notification.sender_email import render_template_with_greeting

        return render_template_with_greeting(self.env, name, template_file, extra_info)

    @retry_on_failure()
    async def send(self, recipient, content: str, days_until: int, age: int) -> None:
        from src.core.config import Recipient  # noqa: F401  (类型说明用)

        # 收件人以"提醒送达的邮箱"为准：提醒是发给使用者的，不是发给生日对象本人。
        target = self.config.default_receive_email or getattr(recipient, "email", None)
        if not target:
            raise ResendSendError(
                "没有可用的收件邮箱：请在 config.yml 的 resend.default_receive_email 填入你的邮箱"
            )

        payload = {
            "from": self.config.sender_field,
            "to": [target],
            "subject": f"生日提醒 - {recipient.name} - {age}岁",
            "html": content,
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(RESEND_ENDPOINT, headers=headers, json=payload)

        if resp.status_code >= 400:
            detail = self._explain(resp.status_code, resp.text, target)
            logger.error("Resend 发送失败 to=%s: %s", target, detail)
            raise ResendSendError(detail)

        try:
            email_id = resp.json().get("id")
        except Exception:
            email_id = None
        logger.info("Resend 已受理 to=%s id=%s", target, email_id)

    @staticmethod
    def _explain(status: int, body: str, target: str) -> str:
        """把 Resend 的英文错误翻成可操作的中文提示。"""
        if status == 401:
            return "API Key 无效或已被撤销，请检查 resend.api_key"
        if status == 403:
            if "own email address" in body or "testing emails" in body:
                return (
                    f"这个 API Key 是测试用 key，只能发给 key 持有者自己的邮箱，不能发给 {target}。"
                    "要发给任意邮箱，需在 resend.com/domains 验证一个域名，"
                    "并把 resend.from_email 改成该域名下的地址。"
                )
            return f"Resend 拒绝了这次请求（403）：{body}"
        if status == 422:
            return f"请求内容不被接受（422）：{body}"
        return f"Resend 返回 {status}：{body}"

    @staticmethod
    def preview_email(template: str = "birthday.html", web_open: bool = False) -> str:
        """渲染一份预览，行为与 EmailSender.preview_email 保持一致。"""
        from src.notification.sender_email import EmailSender

        return EmailSender.preview_email(template=template, web_open=web_open)


def resolve_target_email(config: ResendConfig, recipient: Optional[object] = None) -> str:
    """确定提醒实际发到哪个邮箱。

    优先用 ``resend.default_receive_email``（使用者自己的邮箱），
    其次才退回收件人身上的 email 字段。
    """
    target = getattr(config, "default_receive_email", None)
    if target:
        return str(target)
    return str(getattr(recipient, "email", "") or "")
