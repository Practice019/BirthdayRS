"""设置页表单校验（Pydantic v2）。

界面只管理 Resend —— 它是让项目能正常跑起来所需的全部配置：
一个 API Key、一个接收邮箱、可选的发件人显示名、默认提前天数。

字段按 ``str`` 接收并自行转换，理由与 ``schemas.py`` 一致：
避免 Pydantic 内建约束先抛出英文消息。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from src.web.schemas import MAX_REMINDER_DAYS

#: Resend 密钥的前缀。填错时能立刻发现，而不是等发送失败。
RESEND_KEY_PREFIX = "re_"


def _looks_like_email(value: str) -> bool:
    """形状校验即可，不引 email-validator 依赖（本地单人部署的内部工具）。"""
    if "@" not in value or " " in value:
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and not domain.endswith(".")


class ResendSettingsForm(BaseModel):
    """Resend 设置表单。

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
        if not v:
            raise ValueError("请填写接收提醒的邮箱")
        if not _looks_like_email(v):
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
        raw = (v or "").strip()
        if not raw:
            return "0"
        try:
            days = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("提前提醒的天数要填整数，比如 3") from exc
        if days < 0:
            raise ValueError("提前提醒的天数不能是负数")
        if days > MAX_REMINDER_DAYS:
            raise ValueError(f"提前提醒的天数最多 {MAX_REMINDER_DAYS} 天")
        return str(days)

    def to_repository_values(self) -> dict:
        """转成仓储层 ``update_resend_settings`` 需要的参数。"""
        return {
            "api_key": self.api_key,  # 空串 = 不修改，由仓储层处理
            "default_receive_email": self.default_receive_email,
            "from_name": self.from_name,
            "from_email": self.from_email,
            "default_reminder_days": int(self.default_reminder_days),
        }
