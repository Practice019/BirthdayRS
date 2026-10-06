"""
配置管理模块
"""

from dataclasses import dataclass
from typing import List, Optional
import yaml

#: Resend 在没有自有域名时唯一可用的发件地址。
DEFAULT_RESEND_FROM = "onboarding@resend.dev"


@dataclass
class SMTPConfig:
    """SMTP服务器配置"""

    host: str
    port: int
    username: str
    password: str
    use_tls: bool = True
    default_receive_email: Optional[str] = None
    default_template_file: str = "birthday.html"
    default_reminder_days: int = 0


@dataclass
class ServerChanConfig:
    default_sckey: Optional[str] = None
    default_reminder_days: int = 0


@dataclass
class ResendConfig:
    """Resend HTTP API 配置。

    ``default_receive_email`` 是**提醒实际送达的邮箱**（使用者自己的），
    与收件人本人的邮箱无关 —— 提醒是发给使用者的。

    ``from_name`` 是发件人**显示名**（收件人邮箱里看到的那个名字）。
    Resend 的 ``from`` 字段支持 ``显示名 <地址>`` 格式；没有自有域名时地址只能是
    ``onboarding@resend.dev``，但显示名可以自定义 —— 否则收件人看到的是
    "onboarding"，观感很差。
    """

    api_key: str
    from_email: Optional[str] = None
    from_name: Optional[str] = None
    default_receive_email: Optional[str] = None
    default_template_file: str = "birthday.html"
    default_reminder_days: int = 0

    @property
    def sender_field(self) -> str:
        """组出 Resend ``from`` 字段的值。

        - 显式给了 ``from_name`` → ``显示名 <地址>``
        - ``from_email`` 本身已含 ``<...>`` → 原样使用（允许完全手控）
        - 只给了显示名、没给地址 → 显示名 + 默认地址（显示名仍然生效）
        - 都没给 → 默认地址
        """
        address = (self.from_email or "").strip()
        name = (self.from_name or "").strip()

        if address and "<" in address and ">" in address:
            # 使用者已自行写成 "Name <addr>" 形式，尊重原样。
            return address

        final_address = address or DEFAULT_RESEND_FROM
        if name:
            return f"{name} <{final_address}>"
        return final_address


@dataclass
class Recipient:
    """收件人信息"""

    name: str
    email: Optional[str] = None
    solar_birthday: Optional[str] = None  # YYYY-MM-DD 格式
    lunar_birthday: Optional[str] = None  # YYYY-MM-DD 格式（农历年月日）
    reminder_days: Optional[int] = None
    template_file: Optional[str] = None
    #: 使用者自己写的备注（这个人的相关信息）。界面展示用，不参与生日匹配，
    #: 也不进通知正文。可选，保持对旧配置的兼容。
    note: Optional[str] = None

    def __post_init__(self):
        """验证至少有一个生日日期"""
        if not self.solar_birthday and not self.lunar_birthday:
            raise ValueError(
                "At least one of solar_birthday or lunar_birthday must be provided"
            )


@dataclass
class Config:
    """应用配置"""

    smtp_config: Optional[SMTPConfig]
    serverchan_config: Optional[ServerChanConfig]
    recipients: List[Recipient]
    notification_types: List[str]
    resend_config: Optional[ResendConfig] = None

    @classmethod
    def from_yaml(cls, config_path: str) -> "Config":
        """从YAML文件加载配置"""
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        notification = data.get("notification", {})
        notification_types = [
            t.strip() for t in notification.get("start_notification", "email").split(",") if t.strip()
        ]
        smtp_config = (
            SMTPConfig(**notification["smtp"]) if "smtp" in notification else None
        )
        serverchan_config = (
            ServerChanConfig(**notification["serverchan"])
            if "serverchan" in notification
            else None
        )
        resend_config = (
            ResendConfig(**notification["resend"]) if "resend" in notification else None
        )

        recipients = []
        for r in data.get("recipients", []):
            # 邮件相关默认
            if smtp_config:
                if "email" not in r and smtp_config.default_receive_email:
                    r["email"] = smtp_config.default_receive_email
                if "reminder_days" not in r:
                    r["reminder_days"] = smtp_config.default_reminder_days
                if "template_file" not in r:
                    r["template_file"] = smtp_config.default_template_file
            # Resend 相关默认
            if resend_config:
                if "email" not in r and resend_config.default_receive_email:
                    r["email"] = resend_config.default_receive_email
                if "reminder_days" not in r:
                    r["reminder_days"] = resend_config.default_reminder_days
                if "template_file" not in r:
                    r["template_file"] = resend_config.default_template_file
            # Server酱相关默认
            if serverchan_config:
                if "reminder_days" not in r:
                    r["reminder_days"] = serverchan_config.default_reminder_days
            recipients.append(Recipient(**r))

        return cls(
            smtp_config=smtp_config,
            serverchan_config=serverchan_config,
            recipients=recipients,
            notification_types=notification_types,
            resend_config=resend_config,
        )


if __name__ == "__main__":
    config = Config.from_yaml("config.example.yml")
    print(config)
