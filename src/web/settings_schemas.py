"""设置页表单校验（Pydantic v2）。

界面管理**让项目能正常发提醒所必需**的配置：邮件渠道（Resend）与手机推送渠道
（WxPusher）的凭据、接收提醒的邮箱、默认提前天数、以及启用哪些渠道。

**渠道与必填项的关系**：只有被启用的渠道才校验它的凭据。只开手机推送时不该
被"请填写接收邮箱"拦住 —— 那是邮件渠道的要求。跨字段的这类判断放在
``validate_channel_requirements()``，而不是塞进单个字段的 validator：
字段级校验看不到"这个字段属于哪个渠道、那个渠道开没开"。

字段按 ``str`` 接收并自行转换，理由与 ``schemas.py`` 一致：
避免 Pydantic 内建约束先抛出英文消息。
"""

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, ConfigDict, field_validator

from src.web.schemas import MAX_REMINDER_DAYS

#: Resend 密钥的前缀。填错时能立刻发现，而不是等发送失败。
RESEND_KEY_PREFIX = "re_"

#: WxPusher 极简推送令牌的前缀。
WXPUSHER_SPT_PREFIX = "SPT_"
#: WxPusher 应用密钥的前缀（自己创建的应用）。
WXPUSHER_APP_TOKEN_PREFIX = "AT_"
#: WxPusher 用户标识的前缀。
WXPUSHER_UID_PREFIX = "UID_"


def _looks_like_email(value: str) -> bool:
    """形状校验即可，不引 email-validator 依赖（本地单人部署的内部工具）。"""
    if "@" not in value or " " in value:
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and not domain.endswith(".")


class ResendSettingsForm(BaseModel):
    """邮件渠道（Resend）设置。

    ``api_key`` 留空表示**不修改**（表单里显示的是打码值）。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    api_key: str = ""
    default_receive_email: str = ""
    from_name: str = ""
    from_email: str = ""
    default_reminder_days: str = "3"

    @field_validator("api_key")
    @classmethod
    def check_api_key(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            return ""  # 不改动
        if not v.startswith(RESEND_KEY_PREFIX):
            raise ValueError(f"Resend 的 API Key 应以 {RESEND_KEY_PREFIX} 开头")
        if len(v) < 20:
            raise ValueError("API Key 看起来太短，请检查是否复制完整")
        return v

    @field_validator("default_receive_email")
    @classmethod
    def check_receive_email(cls, v: str) -> str:
        v = (v or "").strip()
        # 非空就必须是合法邮箱；是否必填由渠道启用状态决定（见 validate_channel_requirements）。
        if v and not _looks_like_email(v):
            raise ValueError("邮箱地址看起来不对，例如 you@qq.com")
        return v

    @field_validator("from_email")
    @classmethod
    def check_from_email(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            return ""  # 留空则用默认 onboarding@resend.dev
        # 允许直接写 "显示名 <地址>" 形式
        if "<" in v and ">" in v:
            return v
        if not _looks_like_email(v):
            raise ValueError("发件地址看起来不对，例如 onboarding@resend.dev")
        return v

    @field_validator("from_name")
    @classmethod
    def check_from_name(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) > 60:
            raise ValueError("发件人名字太长了，请控制在 60 个字符以内")
        # 显示名里带 < > 会破坏 "Name <addr>" 的解析
        if "<" in v or ">" in v:
            raise ValueError("发件人名字里不能包含 < 或 >")
        return v

    @field_validator("default_reminder_days")
    @classmethod
    def check_reminder_days(cls, v: str) -> str:
        return _clean_reminder_days(v)

    def to_repository_values(self) -> dict:
        """转成仓储层 ``update_resend_settings`` 需要的参数。"""
        return {
            "api_key": self.api_key,  # 空串 = 不修改，由仓储层处理
            "default_receive_email": self.default_receive_email,
            "from_name": self.from_name,
            "from_email": self.from_email,
            "default_reminder_days": int(self.default_reminder_days),
        }


class WxPusherSettingsForm(BaseModel):
    """手机推送渠道（WxPusher）设置。

    三个密钥类字段留空都表示**不修改**；要清空需显式勾选对应的「清除」。
    区分它们的用途：

    - ``app_token``：自己创建的应用的密钥（``AT_`` 开头）。有它才能**广播给团体**。
    - ``self_uid``：使用者自己的 UID（``UID_`` 开头）。有它才能把"只发给我"的记录
      定向发出去。它不是密钥（别人知道你的 UID 也发不了消息），但仍属个人标识。
    - ``spt``：极简推送令牌。没有 ``self_uid`` 时作为"只发给我"的兜底。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    spt: str = ""
    app_token: str = ""
    self_uid: str = ""
    default_reminder_days: str = "3"

    @field_validator("spt")
    @classmethod
    def check_spt(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            return ""  # 不改动
        if not v.startswith(WXPUSHER_SPT_PREFIX):
            raise ValueError(f"SPT 应以 {WXPUSHER_SPT_PREFIX} 开头，扫码即可获取")
        if len(v) < 12:
            raise ValueError("SPT 看起来太短，请检查是否复制完整")
        return v

    @field_validator("app_token")
    @classmethod
    def check_app_token(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            return ""
        if not v.startswith(WXPUSHER_APP_TOKEN_PREFIX):
            raise ValueError(
                f"appToken 应以 {WXPUSHER_APP_TOKEN_PREFIX} 开头，在应用后台的 appToken 页面获取"
            )
        if len(v) < 12:
            raise ValueError("appToken 看起来太短，请检查是否复制完整")
        return v

    @field_validator("self_uid")
    @classmethod
    def check_self_uid(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            return ""
        if not v.startswith(WXPUSHER_UID_PREFIX):
            raise ValueError(
                f"UID 应以 {WXPUSHER_UID_PREFIX} 开头，在应用后台的用户列表里可以看到"
            )
        return v

    @field_validator("default_reminder_days")
    @classmethod
    def check_reminder_days(cls, v: str) -> str:
        return _clean_reminder_days(v)

    def to_repository_values(self) -> dict:
        return {
            # 空串 = 不修改，由仓储层处理
            "spt": self.spt,
            "app_token": self.app_token,
            "self_uid": self.self_uid,
            "default_reminder_days": int(self.default_reminder_days),
        }


def _clean_reminder_days(raw: str) -> str:
    """默认提前天数的共用校验。"""
    value = (raw or "").strip()
    if not value:
        return "0"
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("提前提醒的天数要填整数，比如 3") from exc
    if days < 0:
        raise ValueError("提前提醒的天数不能是负数")
    if days > MAX_REMINDER_DAYS:
        raise ValueError(f"提前提醒的天数最多 {MAX_REMINDER_DAYS} 天")
    return str(days)


def _effective(new_value: object, already_saved: bool) -> bool:
    """这个字段在保存后是否有值：本次填了，或之前就存过。

    表单里密钥是打码显示的，留空表示"不改动"，所以"已有值 + 本次留空"是合法的。
    """
    return bool(str(new_value or "").strip()) or already_saved


def validate_channel_requirements(
    enabled_types: List[str],
    values: Dict[str, object],
    existing: Dict[str, bool],
) -> Dict[str, str]:
    """校验被启用渠道的必填项，返回 ``{字段名: 中文提示}``。

    跨字段判断集中在这里：某个渠道没被启用，就不该要求它的凭据；
    一旦启用，缺凭据要**明确拦住并指出缺哪个**，而不是等到发送时才失败。

    ``existing`` 是配置里**已有**的凭据（``has_*`` 标志）。表单里密钥是打码显示的，
    留空表示"不改动"，所以"已有值 + 本次留空"是合法的，不能报错。

    **手机推送的判断依据是"能不能发出消息"，不是"某个字段填没填"。**
    WxPusher 有两条可选路径：

    - ``app_token``：能广播给团体（配 ``self_uid`` 还能定向）
    - ``spt``：只能发给自己

    任一条配好就够 —— 曾经这里写死要求 SPT，于是已经有 appToken + self_uid 的人
    一删掉 SPT 就再也保存不了设置（保存时被"请填写 SPT"拦住）。
    至于两条路径各自的覆盖缺口，界面上有独立的能力概览在提示，不该在这里拦保存。
    """
    errors: Dict[str, str] = {}

    if not enabled_types:
        errors["enabled_types"] = "请至少启用一个通知渠道，否则提醒发不出去"
        return errors

    if "resend" in enabled_types:
        if not _effective(values.get("api_key"), existing.get("has_resend_key", False)):
            errors["api_key"] = "启用了邮件渠道，请填写 Resend API Key"
        if not str(values.get("default_receive_email") or "").strip():
            errors["default_receive_email"] = "启用了邮件渠道，请填写接收提醒的邮箱"

    if "wxpusher" in enabled_types:
        has_app = _effective(
            values.get("app_token"), existing.get("has_wxpusher_app_token", False)
        )
        has_spt = _effective(
            values.get("spt"), existing.get("has_wxpusher_spt", False)
        )
        if not (has_app or has_spt):
            # 指向 appToken 那个框：它是现在的主路径，也最不容易配错。
            errors["app_token"] = (
                "启用了手机推送，请至少填写应用密钥（appToken）或推送令牌（SPT）中的一个"
            )

    return errors
