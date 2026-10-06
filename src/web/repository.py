"""config.yml 的唯一读写出口。

设计要点：

1. **round-trip 保留注释**：使用 ruamel.yaml 的 round-trip 模式。config.yml 是使用者
   手写维护的文件，带中文注释；用 PyYAML 回写会静默丢掉全部注释，属于数据损失。
2. **原子替换**：先写同目录临时文件，再 ``os.replace``。任一步失败都不会留下半截配置。
3. **单出口**：界面所有写操作都经过本类，便于加锁与审计。
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from ruamel.yaml import YAML

logger = logging.getLogger(__name__)

# 收件人允许出现的字段。写入时会按此白名单过滤，避免表单夹带意外键进配置。
RECIPIENT_FIELDS = (
    "name",
    "email",
    "solar_birthday",
    "lunar_birthday",
    "reminder_days",
    "template_file",
    "note",
)


#: 表单直接管理的字段。这些字段以表单提交为准（不填即视为清空）。
FORM_FIELDS = ("name", "solar_birthday", "lunar_birthday", "reminder_days", "note")

#: 不在表单里、但必须原样保留的字段，避免编辑时把配置里已有的值弄丢。
PRESERVED_FIELDS = ("email", "template_file")


class ConfigNotFoundError(FileNotFoundError):
    """配置文件不存在。"""


class RecipientNotFoundError(IndexError):
    """按索引找不到收件人。"""


def mask_secret(value: Any) -> Optional[str]:
    """把密钥打码成 ``re_L5Y6...TFF3`` 形式。

    规则：保留前 6 位与后 4 位，中间用 ``...`` 代替；太短的整串替换成 ``••••``。

    为什么不在设置页显示完整密钥：模板渲染结果会出现在浏览器、截图、录屏和
    调试日志里。密钥一旦离开 ``config.yml`` 就多了一条泄露路径，
    而使用者的真实需求只是"确认填过、能认出是哪一个"。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= 10:
        return "••••"
    return f"{text[:6]}...{text[-4:]}"


class ConfigRepository:
    """读写 config.yml，保留注释与键顺序。"""

    def __init__(self, config_path: str) -> None:
        self.path = Path(config_path)
        self._lock = threading.Lock()
        self._yaml = YAML()
        self._yaml.preserve_quotes = True
        # 放宽折行宽度，避免中文长行被拆断后难以人工阅读。
        self._yaml.width = 4096
        self._yaml.allow_unicode = True
        self._yaml.indent(mapping=2, sequence=4, offset=2)

    # ---------- 读取 ----------

    def _load(self) -> Any:
        if not self.path.exists():
            raise ConfigNotFoundError(f"配置文件不存在: {self.path}")
        with open(self.path, "r", encoding="utf-8") as f:
            data = self._yaml.load(f)
        if data is None:
            raise ValueError(f"配置文件为空: {self.path}")
        return data

    def exists(self) -> bool:
        return self.path.exists()

    def get_recipients(self) -> List[Dict[str, Any]]:
        """返回收件人的普通 dict 列表（脱离 ruamel 类型，便于模板渲染）。"""
        with self._lock:
            data = self._load()
        recipients = data.get("recipients") or []
        return [self._to_plain(r) for r in recipients]

    def get_notification_summary(self) -> Dict[str, Any]:
        """通知配置摘要，供设置页与列表页展示。

        包含**生效渠道**与各渠道的关键参数。密钥类字段只返回打码值（见 ``mask_secret``），
        绝不把完整密钥送到模板层 —— 模板可能被日志、调试输出或截屏带走。
        """
        with self._lock:
            data = self._load()
        notification = data.get("notification") or {}
        smtp = notification.get("smtp") or {}
        serverchan = notification.get("serverchan") or {}
        resend = notification.get("resend") or {}
        raw_types = str(notification.get("start_notification", "email"))
        return {
            "types": [t.strip() for t in raw_types.split(",") if t.strip()],
            "smtp_host": smtp.get("host"),
            "smtp_port": smtp.get("port"),
            "smtp_username": smtp.get("username"),
            "smtp_configured": bool(smtp),
            # 提醒送达的邮箱 —— 是使用者自己的，不是收件人的
            "default_receive_email": smtp.get("default_receive_email"),
            "serverchan_configured": bool(serverchan.get("default_sckey")),
            # Resend
            "resend_configured": bool(resend.get("api_key")),
            "resend_key_masked": mask_secret(resend.get("api_key")),
            "resend_from": resend.get("from_email"),
            "resend_from_name": resend.get("from_name"),
            "resend_receive_email": resend.get("default_receive_email"),
            "default_reminder_days": smtp.get(
                "default_reminder_days",
                resend.get(
                    "default_reminder_days", serverchan.get("default_reminder_days", 0)
                ),
            ),
        }

    # ---------- 通知设置写入 ----------

    def get_notification_raw(self) -> Dict[str, Any]:
        """读取通知配置的原始值，供设置表单回填。

        密钥**不返回完整值**，只返回打码值；表单留空即表示"不修改"。
        """
        with self._lock:
            data = self._load()
        notification = data.get("notification") or {}
        resend = notification.get("resend") or {}
        smtp = notification.get("smtp") or {}
        raw_types = str(notification.get("start_notification", "resend"))

        reminder = resend.get("default_reminder_days")
        if reminder is None:
            reminder = smtp.get("default_reminder_days", 0)

        return {
            "types": [t.strip() for t in raw_types.split(",") if t.strip()],
            "resend_from_name": resend.get("from_name") or "",
            "resend_from_email": resend.get("from_email") or "",
            "resend_receive_email": resend.get("default_receive_email") or "",
            "default_reminder_days": reminder,
            "resend_key_masked": mask_secret(resend.get("api_key")),
            "has_resend_key": bool(resend.get("api_key")),
        }

    def update_resend_settings(self, values: Dict[str, Any]) -> None:
        """更新 Resend 相关设置。

        - 只覆盖传入的键，未传的保持原样（避免表单没渲染的字段被清掉）。
        - ``api_key`` 为空字符串 / ``None`` 时**保留原值** —— 表单里密钥是打码显示的，
          留空表达的是"不改"，不是"清空"。要清空需显式传 ``clear_api_key=True``。
        - ``start_notification`` 固定写 ``resend``：界面只管理 Resend，
          写死在这里比让表单决定更不容易配错。
        """
        with self._lock:
            data = self._load()
            notification = data.setdefault("notification", {})
            resend = notification.setdefault("resend", {})

            if values.get("clear_api_key"):
                resend.pop("api_key", None)
            else:
                new_key = values.get("api_key")
                if new_key:
                    resend["api_key"] = new_key

            if "from_name" in values:
                self._set_or_drop(resend, "from_name", values["from_name"])
            if "from_email" in values:
                self._set_or_drop(resend, "from_email", values["from_email"])
            if "default_receive_email" in values:
                self._set_or_drop(
                    resend, "default_receive_email", values["default_receive_email"]
                )
            if values.get("default_reminder_days") is not None:
                resend["default_reminder_days"] = int(values["default_reminder_days"])

            if "resend" in (values.get("enabled_types") or []) or not values.get(
                "enabled_types"
            ):
                notification["start_notification"] = "resend"

            self._save(data)

    @staticmethod
    def _set_or_drop(mapping: Any, key: str, value: Any) -> None:
        """写入值；空字符串表示"清空这个键"，直接删掉而不是留空留串。"""
        if value is None or (isinstance(value, str) and not value.strip()):
            mapping.pop(key, None)
        else:
            mapping[key] = value.strip() if isinstance(value, str) else value

    @staticmethod
    def _to_plain(node: Any) -> Dict[str, Any]:
        """把 ruamel 的 CommentedMap 转成普通 dict。"""
        return {k: node[k] for k in node.keys()}

    # ---------- 写入 ----------

    def _save(self, data: Any) -> None:
        """原子写回：临时文件 + os.replace。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".config-", suffix=".yml.tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                self._yaml.dump(data, f)
            os.replace(tmp_name, self.path)
        except BaseException:
            # 失败时清掉临时文件，不让它污染目录。
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

    @staticmethod
    def _clean(values: Dict[str, Any]) -> Dict[str, Any]:
        """按白名单过滤，并丢弃空值（空字符串 / None 不写进配置）。"""
        cleaned: Dict[str, Any] = {}
        for field in RECIPIENT_FIELDS:
            if field not in values:
                continue
            value = values[field]
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            cleaned[field] = value
        return cleaned

    def add_recipient(self, values: Dict[str, Any]) -> int:
        """追加一个收件人，返回其索引。"""
        with self._lock:
            data = self._load()
            recipients = data.setdefault("recipients", [])
            recipients.append(self._clean(values))
            self._save(data)
            return len(recipients) - 1

    def update_recipient(self, index: int, values: Dict[str, Any]) -> None:
        """按索引就地更新一个收件人。

        采用「表单字段覆盖 + 其余保留」的语义：表单已不再暴露 ``email`` /
        ``template_file``，若整体替换会把配置里已有的值静默删掉。
        """
        with self._lock:
            data = self._load()
            recipients = data.get("recipients") or []
            if not 0 <= index < len(recipients):
                raise RecipientNotFoundError(index)

            existing = self._to_plain(recipients[index])
            merged: Dict[str, Any] = {
                k: v for k, v in existing.items() if k in PRESERVED_FIELDS
            }
            merged.update(self._clean(values))
            recipients[index] = merged
            self._save(data)

    def delete_recipient(self, index: int) -> str:
        """按索引删除，返回被删者的姓名（用于提示文案）。"""
        with self._lock:
            data = self._load()
            recipients = data.get("recipients") or []
            if not 0 <= index < len(recipients):
                raise RecipientNotFoundError(index)
            removed = recipients.pop(index)
            name = str(self._to_plain(removed).get("name", "")) or "未命名"
            self._save(data)
            return name
