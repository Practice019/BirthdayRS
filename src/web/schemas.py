"""表单校验 schema（Pydantic v2）。

设计要点：

1. **只收「这个人是谁」需要的信息**：姓名、身份证出生年月日、提前几天提醒、备注。
   邮箱与邮件模板不在表单里 —— 提醒是发给使用者自己的（配置里的接收邮箱），
   模板属于全局设置，都不该按人重复填。
2. **农历生日由身份证日期自动推导**（见 ``src/web/lunar.py``），不作为输入。
3. **错误信息面向使用者**：字段一律按 ``str`` 接收并自己做转换与范围检查，
   避免 Pydantic 内建约束先抛出英文消息。
4. **校验失败不丢输入**：路由捕获 ``ValidationError`` 后把原始提交值填回模板。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.web.lunar import solar_to_lunar

DATE_FORMAT = "%Y-%m-%d"
DATE_HINT = "格式应为 1990-01-20（年-月-日）"

#: 这个字段在界面上的名字。
#: 标签与所有校验提示必须用同一个说法，否则使用者对不上号。
SOLAR_FIELD_LABEL = "身份证出生年月日"

MAX_REMINDER_DAYS = 365
MAX_NOTE_LENGTH = 500


def _clean_solar_date(value: Optional[str]) -> str:
    """校验必填的身份证出生年月日。"""
    value = (value or "").strip()
    if not value:
        raise ValueError(f"请填写{SOLAR_FIELD_LABEL}")

    try:
        parsed = datetime.strptime(value, DATE_FORMAT).date()
    except ValueError as exc:
        raise ValueError(f"{SOLAR_FIELD_LABEL}{DATE_HINT}") from exc

    if parsed.year < 1900:
        raise ValueError(f"{SOLAR_FIELD_LABEL}的年份看起来不对，请填四位年份")
    if parsed.year > datetime.now().year:
        raise ValueError(f"{SOLAR_FIELD_LABEL}不能是未来的日期")
    return value


class RecipientForm(BaseModel):
    """新增 / 编辑收件人的表单。

    ``lunar_birthday`` 不接受输入：它由 ``solar_birthday`` 自动推导，
    用于让 CLI 的 ``run`` 命令能在农历生日当天也发出提醒。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(max_length=64)
    solar_birthday: str = ""
    reminder_days: str = "0"
    note: str = ""

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("请填写姓名")
        if len(v) > 64:
            raise ValueError("姓名太长了，请控制在 64 个字符以内")
        return v

    @field_validator("solar_birthday")
    @classmethod
    def check_solar(cls, v: str) -> str:
        return _clean_solar_date(v)

    @field_validator("reminder_days")
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

    @field_validator("note")
    @classmethod
    def check_note(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) > MAX_NOTE_LENGTH:
            raise ValueError(f"备注太长了，请控制在 {MAX_NOTE_LENGTH} 个字符以内")
        return v

    @property
    def reminder_days_value(self) -> int:
        return int(self.reminder_days)

    @property
    def computed_lunar_birthday(self) -> Optional[str]:
        """由身份证出生年月日推导出的农历生日（存储格式）。"""
        return solar_to_lunar(self.solar_birthday)

    def to_config(self) -> dict:
        """转成写入 config.yml 的字段。

        只返回表单管理的字段；``email`` / ``template_file`` 由仓储层保留原值，
        不出现在这里，以免被清空。

        - ``lunar_birthday`` 写自动推导值：CLI 的 run 命令靠它才能在农历生日当天提醒。
        - ``reminder_days`` 为 0 时写 0 而非省略 —— 0 是合法语义（"只在当天提醒"），
          省略会退回默认值造成行为改变。
        """
        data = {
            "name": self.name,
            "solar_birthday": self.solar_birthday,
            "lunar_birthday": self.computed_lunar_birthday,
            "reminder_days": self.reminder_days_value,
            "note": self.note or None,
        }
        return {k: v for k, v in data.items() if v is not None}
