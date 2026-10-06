"""
配置管理模块
"""

import logging
from dataclasses import dataclass
from typing import List, Optional
import yaml

logger = logging.getLogger(__name__)

#: Resend 在没有自有域名时唯一可用的发件地址。
DEFAULT_RESEND_FROM = "onboarding@resend.dev"

#: 每条记录可以选择的「受众」。
#:
#: 这个区分的由来：使用者记着两类人的生日 ——
#:   - **团体成员**：大家互相认识，谁的生日到了应该让所有人都知道 → ``group``
#:   - **私人朋友**：与团体无关，只有使用者自己需要被提醒 → ``self``
#: 私人朋友的信息不该被广播出去，所以默认值是 ``self``（也保证旧配置行为不变）。
AUDIENCES = ("self", "group")

#: 受众的中文说法。**所有面向使用者的文案都用这一对词**：
#: 「团体」与「自己」。同一个概念在界面、按钮、提示、文档里必须一个叫法，
#: 否则使用者在"团体/只发给我/团体所有人"之间来回对照，容易选错 ——
#: 而选错的后果（把私人提醒广播出去）是不可撤销的。
AUDIENCE_LABELS = {
    "self": "自己",
    "group": "团体",
}


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
class WxPusherConfig:
    """WxPusher 推送配置（https://wxpusher.zjiecode.com）。

    支持两种模式，按填了哪些字段自动判断：

    **极简模式（SPT）** —— 只填 ``spt``。
    扫码即得一个 ``SPT_xxx``，它同时标识"用哪个身份发"和"发给谁"——
    发信人与收信人是同一个人。适合"只发给自己"。

    **标准模式（appToken + UID）** —— 填 ``app_token``。
    用自己创建的应用发消息，可以发给**所有关注该应用的人**。
    ``self_uid`` 是使用者自己的 UID：标记为"只发给我"的记录靠它定向投递，
    否则这类私人提醒会被广播给团体里所有人。

    两种可以同时填：标准模式负责团体广播，SPT 作为"自己的那条路"的兜底。
    一个都不填时该渠道视为未配置。
    """

    #: 极简推送令牌，形如 ``SPT_xxxxx``。等同于密钥，不要进日志。
    spt: Optional[str] = None
    #: 标准模式的应用密钥，形如 ``AT_xxxxx``。等同于密钥，不要进日志。
    app_token: Optional[str] = None
    #: 使用者自己的 UID（``UID_xxxxx``）。"只发给我"的记录发给它。
    #: 在 WxPusher 里关注应用后，从应用后台的用户列表可以看到。
    self_uid: Optional[str] = None
    default_reminder_days: int = 0

    @property
    def has_standard(self) -> bool:
        """是否配好了标准模式（能群发、也能定向发给 self_uid）。"""
        return bool((self.app_token or "").strip())

    @property
    def has_spt(self) -> bool:
        """是否配好了极简模式。"""
        return bool((self.spt or "").strip())

    @property
    def group_broadcast_ready(self) -> bool:
        """能否把 ``audience=group`` 的记录广播出去。"""
        return self.has_standard

    @property
    def self_only_ready(self) -> bool:
        """能否把 ``audience=self`` 的记录定向发出去。

        标准模式要有 ``self_uid`` 才能定向；没有 self_uid 时只能靠 SPT
        （SPT 天然只发给持有者自己）。
        """
        return self.has_spt or (self.has_standard and bool((self.self_uid or "").strip()))


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
    #: 提醒发给谁看。见 ``AUDIENCES``。
    #: - ``self``（默认）：只发给使用者自己 —— 私人朋友，与团体无关。
    #: - ``group``：广播给推送应用里的**所有**关注者 —— 团体成员，大家互相提醒。
    #: 缺省为 ``self``：旧配置没有这个字段，语义正好就是"只发给自己"。
    audience: str = "self"

    def __post_init__(self):
        """验证至少有一个生日日期"""
        if not self.solar_birthday and not self.lunar_birthday:
            raise ValueError(
                "At least one of solar_birthday or lunar_birthday must be provided"
            )
        if self.audience not in AUDIENCES:
            raise ValueError(
                f"audience 只能是 {' 或 '.join(AUDIENCES)}，收到：{self.audience!r}"
            )


@dataclass
class Config:
    """应用配置"""

    smtp_config: Optional[SMTPConfig]
    serverchan_config: Optional[ServerChanConfig]
    recipients: List[Recipient]
    notification_types: List[str]
    resend_config: Optional[ResendConfig] = None
    wxpusher_config: Optional[WxPusherConfig] = None

    @classmethod
    def from_yaml(cls, config_path: str) -> "Config":
        """从YAML文件加载配置"""
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        notification = data.get("notification", {})
        notification_types = [
            t.strip() for t in notification.get("start_notification", "email").split(",") if t.strip()
        ]

        def section(name: str) -> Optional[dict]:
            """取一个渠道的配置段。

            **空段（``resend: {}`` 或 ``resend:``）视为未配置**，返回 ``None``。
            否则会因为缺少必填字段抛 ``TypeError``，把整个配置加载搞崩 ——
            而空段是正常状态：使用者清空 API Key 后就是这个样子。
            """
            block = notification.get(name)
            return block if isinstance(block, dict) and block else None

        smtp_block = section("smtp")
        serverchan_block = section("serverchan")
        resend_block = section("resend")
        wxpusher_block = section("wxpusher")

        # 有必填字段的渠道，缺字段时降级为"未配置"而不是崩溃。
        # 少一个渠道只是不发那种通知，不该让整个程序起不来。
        smtp_config = SMTPConfig(**smtp_block) if smtp_block else None
        serverchan_config = ServerChanConfig(**serverchan_block) if serverchan_block else None
        resend_config = None
        if resend_block and (resend_block.get("api_key") or "").strip():
            resend_config = ResendConfig(**resend_block)
        elif resend_block:
            logger.warning("resend 段存在但没有 api_key，按未配置处理")
        wxpusher_config = None
        if wxpusher_block and (
            (wxpusher_block.get("spt") or "").strip()
            or (wxpusher_block.get("app_token") or "").strip()
        ):
            wxpusher_config = WxPusherConfig(**wxpusher_block)
        elif wxpusher_block:
            logger.warning("wxpusher 段存在但没有 spt / app_token，按未配置处理")

        # 渠道标识 → 对应的配置对象。``email`` 用的是 smtp 段，名字不一致是历史遗留。
        channel_configs = {
            "smtp": smtp_config,
            "resend": resend_config,
            "serverchan": serverchan_config,
            "wxpusher": wxpusher_config,
        }
        channel_names = {
            "email": "smtp",
            "resend": "resend",
            "serverchan": "serverchan",
            "wxpusher": "wxpusher",
        }

        # 默认值按**生效渠道顺序**继承，而不是写死 smtp 优先。
        # 写死会导致 start_notification: resend 时收件人仍按 smtp 的
        # default_reminder_days 发送，与设置页里填的值对不上。
        ordered = []
        for t in notification_types:
            name = channel_names.get(t)
            if name and channel_configs.get(name) is not None and name not in ordered:
                ordered.append(name)
        # 生效渠道都没配：退到「配置里存在」的渠道，顺序固定，不依赖 YAML 键序。
        for name in ("resend", "wxpusher", "smtp", "serverchan"):
            if channel_configs.get(name) is not None and name not in ordered:
                ordered.append(name)

        recipients = []
        for r in data.get("recipients", []):
            for name in ordered:
                cfg = channel_configs[name]
                # 邮件类渠道才有 default_receive_email / default_template_file；
                # 推送类渠道没有这两个字段（getattr 兜底 None）。
                if "email" not in r and getattr(cfg, "default_receive_email", None):
                    r["email"] = cfg.default_receive_email
                if "template_file" not in r and getattr(cfg, "default_template_file", None):
                    r["template_file"] = cfg.default_template_file
                if "reminder_days" not in r:
                    r["reminder_days"] = cfg.default_reminder_days
            # 一个渠道都没配时也要给出可用值：checker 会做 reminder_days + 1，
            # None 会直接抛 TypeError。
            r.setdefault("reminder_days", 0)
            recipients.append(Recipient(**r))

        return cls(
            smtp_config=smtp_config,
            serverchan_config=serverchan_config,
            recipients=recipients,
            notification_types=notification_types,
            resend_config=resend_config,
            wxpusher_config=wxpusher_config,
        )


if __name__ == "__main__":
    config = Config.from_yaml("config.example.yml")
    print(config)
