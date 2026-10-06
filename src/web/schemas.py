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

from src.core.config import AUDIENCES
from src.web.lunar import normalize_solar_input, parse_iso_date, solar_to_lunar

#: 存储格式（也是 config.yml 里的格式）。
DATE_FORMAT = "%Y-%m-%d"
#: 提示要按**使用者实际输入的写法**说，否则他会照着提示去填连字符。
DATE_HINT = "请填 8 位数字，例如 19900120"

#: 这个字段在界面上的名字。
#: 标签与所有校验提示必须用同一个说法，否则使用者对不上号。
SOLAR_FIELD_LABEL = "身份证出生年月日"

MAX_REMINDER_DAYS = 365
MAX_NOTE_LENGTH = 500


def _clean_solar_date(value: Optional[str]) -> str:
    """校验必填的身份证出生年月日，返回**存储格式** ``YYYY-MM-DD``。

    输入允许两种写法：8 位数字（界面上要求的 ``19900120``）与带连字符的
    ``1990-01-20``（已有配置、API 调用方）。统一由 ``normalize_solar_input``
    归一化后再校验 —— 校验和存储都只认一种格式，多格式共存只在入口这一层。

    这里用 ``parse_iso_date`` 而不是 ``strptime``：前者会拒绝 1990-02-30
    这类不存在的日期，是项目里唯一的权威日期校验入口。
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError(f"请填写{SOLAR_FIELD_LABEL}")

    stored = normalize_solar_input(raw)
    parsed = parse_iso_date(stored)
    if parsed is None:
        raise ValueError(f"{SOLAR_FIELD_LABEL}不正确，{DATE_HINT}")

    if parsed.year < 1900:
        raise ValueError(f"{SOLAR_FIELD_LABEL}的年份看起来不对，请填四位年份")
    if parsed.year > datetime.now().year:
        raise ValueError(f"{SOLAR_FIELD_LABEL}不能是未来的日期")
    # 返回**归一化后**的值：存储格式永远是 YYYY-MM-DD，与使用者输入什么写法无关。
    return stored


class RecipientForm(BaseModel):
    """新增 / 编辑收件人的表单。

    ``lunar_birthday`` 不接受输入：它由 ``solar_birthday`` 自动推导，
    用于让 CLI 的 ``run`` 命令能在农历生日当天也发出提醒。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(max_length=64)
    solar_birthday: str = ""
    #: 留空 = **不单独设置**，用设置页里的全局默认值。这是常态，也是默认行为。
    #: 只有使用者显式填了数字，才按这个数字提醒这个人。
    reminder_days: str = ""
    #: 提醒发给谁看：``self``（只发给我）或 ``group``（发给团体所有人）。
    #:
    #: **默认 ``None`` 表示"这次提交没带这个字段"**，而不是"等于 self"。
    #: 两者必须区分：编辑一条团体记录时，没渲染该字段的调用方（旧客户端、
    #: 脚本）不该把它**悄悄降级**成私人 —— 该广播的没广播，且不报错。
    #: 新建时 None 会被 ``to_config`` 落成 ``self``（新记录的合理默认）。
    audience: Optional[str] = None
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
        # 空 = 继承全局默认（最常见的用法），保留空串本身，不折叠成 0。
        if not raw:
            return ""
        try:
            days = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("提前提醒的天数要填整数，比如 3") from exc
        if days < 0:
            raise ValueError("提前提醒的天数不能是负数")
        if days > MAX_REMINDER_DAYS:
            raise ValueError(f"提前提醒的天数最多 {MAX_REMINDER_DAYS} 天")
        return str(days)

    @field_validator("audience")
    @classmethod
    def check_audience(cls, v: Optional[str]) -> Optional[str]:
        # 空 / 未提供 → 保持 None，交由仓储层决定是"新建用默认"还是"编辑保留原值"
        if v is None or not str(v).strip():
            return None
        value = str(v).strip()
        if value not in AUDIENCES:
            raise ValueError("请选择这条提醒发给谁")
        return value

    @field_validator("note")
    @classmethod
    def check_note(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) > MAX_NOTE_LENGTH:
            raise ValueError(f"备注太长了，请控制在 {MAX_NOTE_LENGTH} 个字符以内")
        return v

    @property
    def reminder_days_value(self) -> Optional[int]:
        """单独设置的天数；``None`` 表示不设置，用全局默认。"""
        return int(self.reminder_days) if self.reminder_days else None

    @property
    def computed_lunar_birthday(self) -> Optional[str]:
        """由身份证出生年月日推导出的农历生日（存储格式）。"""
        return solar_to_lunar(self.solar_birthday)

    def to_config(self) -> dict:
        """转成写入 config.yml 的字段。

        只返回表单管理的字段；``email`` / ``template_file`` 由仓储层保留原值，
        不出现在这里，以免被清空。

        - ``lunar_birthday`` 写自动推导值：CLI 的 run 命令靠它才能在农历生日当天提醒。
        - ``reminder_days`` 留空时**整个键都不写**：配置里没有这个键，运行时就会退回
          全局默认值（见 ``Config.from_yaml``）。显式填 0 则写 0 —— 0 是合法语义
          （"只在当天提醒"），与"不设置"必须区分开。
        """
        data = {
            "name": self.name,
            "solar_birthday": self.solar_birthday,
            "lunar_birthday": self.computed_lunar_birthday,
            "reminder_days": self.reminder_days_value,
            # 本次提交带了就写出来（含显式的 self —— 那压过配置里的原值）；
            # 没带就完全不出现在这个 dict 里，由仓储层保留原值。
            # 新建时下面补一个 self 作为新记录的合理默认。
            "audience": self.audience,
            "note": self.note or None,
        }
        return {k: v for k, v in data.items() if v is not None}
