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

from src.core.config import AUDIENCES

logger = logging.getLogger(__name__)

# 收件人允许出现的字段。写入时会按此白名单过滤，避免表单夹带意外键进配置。
RECIPIENT_FIELDS = (
    "name",
    "email",
    "solar_birthday",
    "lunar_birthday",
    "reminder_days",
    "template_file",
    "audience",
    "note",
)


#: 表单直接管理的字段。这些字段以表单提交为准（不填即视为清空）。
FORM_FIELDS = (
    "name",
    "solar_birthday",
    "lunar_birthday",
    "reminder_days",
    "audience",
    "note",
)

#: 不在表单里、但必须原样保留的字段，避免编辑时把配置里已有的值弄丢。
PRESERVED_FIELDS = ("email", "template_file")


class ConfigNotFoundError(FileNotFoundError):
    """配置文件不存在。"""


class RecipientNotFoundError(IndexError):
    """按索引找不到收件人。"""


#: 渠道标识 → 它在 notification 里的配置段名。
#: 注意 ``email`` 用的是 ``smtp`` 段，这两个名字不一致是历史遗留，别想当然。
_CHANNEL_SECTIONS = {
    "resend": "resend",
    "email": "smtp",
    "serverchan": "serverchan",
    "wxpusher": "wxpusher",
}

#: 设置页允许启用的渠道，按界面展示顺序。``email``（SMTP）与 ``serverchan``
#: 只在配置文件里手工维护，界面不提供开关。
UI_CHANNELS = ("resend", "wxpusher")


def notification_types(notification: Any) -> List[str]:
    """解析 ``start_notification``，返回按生效顺序排列的渠道列表。"""
    raw = str((notification or {}).get("start_notification", "email"))
    return [t.strip() for t in raw.split(",") if t.strip()]


def effective_default_reminder_days(notification: Any) -> int:
    """取「默认提前几天提醒」，以**生效渠道**为准。

    这里的取值必须与真正发送时一致（``Config.from_yaml`` 给收件人套默认值、
    ``run`` 命令据此发送）。曾经这里写死 ``smtp`` 优先，于是
    ``start_notification: resend`` 时会出现：设置页显示 resend 的 1 天，
    时间轴却显示 smtp 的 3 天，而且 ``run`` 真按 3 天发 —— 界面与实际行为对不上。

    取值顺序：先按 ``start_notification`` 的渠道顺序找；都没写就退到任意一个
    配置了的渠道；最后兜 0（0 = 只在当天提醒）。
    """
    notification = notification or {}
    for channel in notification_types(notification):
        section = _CHANNEL_SECTIONS.get(channel)
        if not section:
            continue
        block = notification.get(section) or {}
        value = block.get("default_reminder_days")
        if value is not None:
            return int(value)

    # 生效渠道没写：退回「配置了这个键」的渠道，顺序固定，避免依赖 YAML 里的键序。
    for section in ("resend", "smtp", "serverchan"):
        block = notification.get(section) or {}
        value = block.get("default_reminder_days")
        if value is not None:
            return int(value)
    return 0


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
        wxpusher = notification.get("wxpusher") or {}
        return {
            "types": notification_types(notification),
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
            # WxPusher（手机推送）。两种模式分开报告：能广播 vs 能定向，
            # 界面据此提示"团体提醒还发不出去"这类缺口。
            "wxpusher_configured": bool(
                (wxpusher.get("spt") or "").strip()
                or (wxpusher.get("app_token") or "").strip()
            ),
            "wxpusher_spt_masked": mask_secret(wxpusher.get("spt")),
            "wxpusher_app_token_masked": mask_secret(wxpusher.get("app_token")),
            "wxpusher_self_uid": (wxpusher.get("self_uid") or "").strip(),
            "wxpusher_can_broadcast": bool((wxpusher.get("app_token") or "").strip()),
            "wxpusher_can_self": bool(
                (wxpusher.get("spt") or "").strip()
                or (
                    (wxpusher.get("app_token") or "").strip()
                    and (wxpusher.get("self_uid") or "").strip()
                )
            ),
            "default_reminder_days": effective_default_reminder_days(notification),
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
        wxpusher = notification.get("wxpusher") or {}

        return {
            "types": notification_types(notification),
            "resend_from_name": resend.get("from_name") or "",
            "resend_from_email": resend.get("from_email") or "",
            "resend_receive_email": resend.get("default_receive_email") or "",
            # 与时间轴同口径（见 effective_default_reminder_days），
            # 否则设置页显示一个值、时间轴显示另一个。
            "default_reminder_days": effective_default_reminder_days(notification),
            "resend_key_masked": mask_secret(resend.get("api_key")),
            "has_resend_key": bool(resend.get("api_key")),
            # WxPusher：密钥只回打码值，留空表示不修改。
            # self_uid 不是密钥（知道它也无法发消息），原样回填方便核对。
            "wxpusher_spt_masked": mask_secret(wxpusher.get("spt")),
            "has_wxpusher_spt": bool((wxpusher.get("spt") or "").strip()),
            "wxpusher_app_token_masked": mask_secret(wxpusher.get("app_token")),
            "has_wxpusher_app_token": bool((wxpusher.get("app_token") or "").strip()),
            "wxpusher_self_uid": (wxpusher.get("self_uid") or "").strip(),
            # 能力标志也放在这里：设置页的模板读的是 raw（values.*），
            # 只放在 summary 里的话页面拿不到，会一直显示"缺 appToken"。
            "wxpusher_can_broadcast": bool((wxpusher.get("app_token") or "").strip()),
            "wxpusher_can_self": bool(
                (wxpusher.get("spt") or "").strip()
                or (
                    (wxpusher.get("app_token") or "").strip()
                    and (wxpusher.get("self_uid") or "").strip()
                )
            ),
        }

    def _write_start_notification(self, notification: Any, enabled: list) -> None:
        """把启用的渠道列表写进 ``start_notification``。

        **至少要留一个渠道**：一个都不写程序跑起来也发不出任何提醒，
        而 ``validate_config`` 会因此判定配置无效。真删到只剩 0 个时保留原来的值，
        让使用者先看到保存成功、再去关掉最后一个 —— 而不是配置被悄悄改坏。
        """
        types = [t for t in UI_CHANNELS if t in enabled]
        if not types:
            return
        notification["start_notification"] = ",".join(types)

    def update_resend_settings(self, values: Dict[str, Any]) -> None:
        """更新 Resend（邮件）相关设置。

        - 只覆盖传入的键，未传的保持原样（避免表单没渲染的字段被清掉）。
        - ``api_key`` 为空字符串 / ``None`` 时**保留原值** —— 表单里密钥是打码显示的，
          留空表达的是"不改"，不是"清空"。要清空需显式传 ``clear_api_key=True``。
        - 渠道启用状态由 ``enabled_types`` 决定，这里只负责写自己那段配置；
          ``start_notification`` 统一在 ``update_notification_settings`` 里落盘。
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

            self._save(data)

    def update_wxpusher_settings(self, values: Dict[str, Any]) -> None:
        """更新 WxPusher（手机推送）相关设置。

        ``spt`` / ``app_token`` 的语义与 Resend 的 ``api_key`` 一致：
        留空 = 不修改，要清空需显式传 ``clear_spt`` / ``clear_app_token``。
        两个都是密钥，界面上始终只显示打码值。

        ``self_uid`` **不是密钥**：知道它也无法给别人发消息。它随表单提交覆盖，
        留空即清除（换应用后旧的 UID 就没意义了，留着反而误导）。
        """
        with self._lock:
            data = self._load()
            notification = data.setdefault("notification", {})
            wxpusher = notification.setdefault("wxpusher", {})

            for key, clear_flag in (
                ("spt", "clear_spt"),
                ("app_token", "clear_app_token"),
            ):
                if values.get(clear_flag):
                    wxpusher.pop(key, None)
                    continue
                new_value = (values.get(key) or "").strip()
                if new_value:
                    wxpusher[key] = new_value

            if "self_uid" in values:
                self._set_or_drop(wxpusher, "self_uid", values["self_uid"])

            if values.get("default_reminder_days") is not None:
                wxpusher["default_reminder_days"] = int(values["default_reminder_days"])

            self._save(data)

    def update_notification_settings(self, values: Dict[str, Any]) -> None:
        """更新通知渠道的整体设置：启用哪些渠道、默认提前天数。

        **渠道启用状态只由这里的 ``enabled_types`` 决定。** 之前 ``start_notification``
        被写死在 ``update_resend_settings`` 里，于是界面只能表示"只用 resend"；
        要支持多渠道勾选，就必须把它提到这一层统一落盘。

        ``enabled_types`` 为 ``None`` 表示"表单没提交这个字段"（例如桌面端旧调用），
        此时保持原值不动，而不是把渠道清空。
        """
        enabled = values.get("enabled_types")
        if enabled is None:
            return

        with self._lock:
            data = self._load()
            notification = data.setdefault("notification", {})
            self._write_start_notification(notification, list(enabled))

            days = values.get("default_reminder_days")
            if days is not None:
                days = int(days)
                # 默认提前天数写在**启用渠道**的段里：取值时按生效渠道顺序读
                # （见 effective_default_reminder_days），写错段落会读不到。
                # 同时写 resend 与 wxpusher，保证只启用其中一个时也生效。
                for name in ("resend", "wxpusher"):
                    section = notification.get(name)
                    if section is not None:
                        section["default_reminder_days"] = days

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

    def set_audience_many(self, indices, audience: str) -> int:
        """批量把若干条记录的受众改成 ``audience``，返回实际改动条数。

        **只动 ``audience`` 这一个键**，其余字段与文件里的注释原样保留 ——
        批量操作最怕"顺手把别的字段也重写了"。仓储层的 ruamel round-trip
        本来就保注释，这里只要不整体替换每个条目就行。

        越界的索引**静默跳过**而不是报错：界面上勾选后可能有人同时删了一条，
        剩下那些仍然该被改；因为一个失效索引让整批失败更糟。
        """
        if audience not in AUDIENCES:
            raise ValueError(f"受众只能是 {' 或 '.join(AUDIENCES)}，收到：{audience!r}")

        wanted = sorted({int(i) for i in indices})
        if not wanted:
            return 0

        with self._lock:
            data = self._load()
            recipients = data.get("recipients") or []
            changed = 0
            for index in wanted:
                if not 0 <= index < len(recipients):
                    continue
                item = recipients[index]
                if item.get("audience") == audience:
                    continue
                item["audience"] = audience
                changed += 1
            if changed:
                self._save(data)
            return changed

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
