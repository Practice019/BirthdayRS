"""桌面应用后端：暴露给前端 JS 调用的 API。

**与 web 版的区别只在传输层**：web 版走 HTTP 路由 + 表单 POST，
这里走 pywebview 的 ``js_api``（进程内直接调用）。业务逻辑完全复用
``domain.py`` / ``repository.py`` / ``lunar.py``，两处不重复实现。

**零端口**：pywebview 以 ``http_server=False`` 启动、页面用 ``html=`` 直载，
进程不监听任何 socket。因此这里不需要任何 HTTP 相关代码。

前端约定：所有方法返回 JSON 可序列化的 dict，形如
``{"ok": True, ...}`` 或 ``{"ok": False, "error": "面向使用者的中文提示"}``。
异常在边界处被捕获并转成 ``error``，不让它穿透到 JS（穿透会让前端拿不到提示）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from src.core.config import Recipient
from src.core.config_manager import ConfigManager
from src.core.notification_factory import NotificationFactory
from src.web.domain import (
    build_sendable_recipient,
    build_timeline,
    next_solar_occurrence,
    render_preview,
    render_reminder_for,
    resolve_receive_email,
    send_reminder_now,
)
from src.web.lunar import (
    INVALID_SOLAR_HINT,
    as_iso_text,
    lunar_display,
    next_solar_for_lunar,
    parse_iso_date,
    solar_to_lunar,
    to_lunar_display_from_solar,
)
from src.web.repository import (
    ConfigNotFoundError,
    ConfigRepository,
    RecipientNotFoundError,
)
from src.web.schemas import RecipientForm
from src.web.settings_schemas import ResendSettingsForm

logger = logging.getLogger(__name__)


def _form_errors(exc: ValidationError) -> Dict[str, str]:
    """把 Pydantic 错误压成 {字段名: 中文提示}。

    与 web 版 ``collect_form_errors`` 同一逻辑；模型级校验的 ``loc`` 为空，
    归到 ``__all__``。
    """
    fallback = {
        "string_too_long": "内容太长了",
        "missing": "这一项必填",
        "string_type": "请填写文本",
        "int_parsing": "请填写整数",
    }
    errors: Dict[str, str] = {}
    for err in exc.errors():
        loc = err.get("loc") or ()
        field = str(loc[0]) if loc else "__all__"
        message = err.get("msg", "输入有误")
        message = message.replace("Value error, ", "").replace("Assertion failed, ", "")
        if message in fallback:
            message = fallback[message]
        errors.setdefault(field, message)
    return errors


def _first_error(errors: Dict[str, str], default: str = "输入有误") -> str:
    """取第一条可展示的错误文案。"""
    if not errors:
        return default
    for key in ("__all__", "name", "solar_birthday", "default_receive_email"):
        if key in errors:
            return errors[key]
    return next(iter(errors.values()))


class AppApi:
    """前端的全部后端能力。方法名与前端 JS 调用一一对应。

    **只暴露方法，不暴露内部状态**：pywebview 会把 ``js_api`` 对象的公开属性
    一并挂到 ``window.pywebview.api`` 上。``config_manager`` / ``repo`` 这类对象
    不该出现在前端可见面上（既无用处，也扩大了可被误用的接口）。
    为此内部状态统一用下划线前缀 —— pywebview 只暴露不以 ``_`` 开头的成员。
    """

    def __init__(self, config_path: Optional[str] = None) -> None:
        self._config_manager = ConfigManager(config_path)
        # 触发加载：配置有问题时尽早失败，而不是等用户点了按钮才报错
        self._config_manager.config
        self._repo = ConfigRepository(self._config_manager.config_path)

    # ---------- 内部工具 ----------

    def _refresh_config(self):
        """重新读取 config.yml。

        设置页改完配置后必须刷新，否则本进程里还是旧对象，
        测试发送会用旧的 key/邮箱，看起来"改了没生效"。
        """
        self._config_manager._config = None
        return self._config_manager.config

    @property
    def _config(self):
        """当前生效的配置对象。"""
        return self._config_manager.config

    def _templates_dir(self) -> str:
        return self._config_manager.get_templates_dir()

    def _senders(self) -> List[Any]:
        return NotificationFactory(self._templates_dir()).create_senders(self._config)

    # ---------- 时间轴 ----------

    def get_timeline(self) -> Dict[str, Any]:
        """首页数据：收件人列表 + 通知摘要。"""
        try:
            recipients = self._repo.get_recipients()
        except ConfigNotFoundError as exc:
            return {"ok": False, "error": f"找不到配置文件：{exc}"}
        except Exception as exc:
            logger.exception("读取配置失败")
            return {"ok": False, "error": f"读取配置失败：{exc}"}

        summary = self._repo.get_notification_summary()
        timeline = build_timeline(recipients, summary.get("default_reminder_days") or 0)

        return {
            "ok": True,
            "recipients": [
                {
                    "index": v.index,
                    "name": v.name,
                    "note": v.note or "",
                    "solar_birthday": as_iso_text(v.solar_birthday),
                    "lunar_text": v.lunar_text or "",
                    "lunar_next_solar": v.lunar_next_solar.isoformat() if v.lunar_next_solar else "",
                    "next_birthday": v.next_birthday.isoformat() if v.next_birthday else "",
                    "next_kind": v.next_kind_text,
                    "week_name": v.week_name or "",
                    "age": v.age,
                    "days_until": v.days_until,
                    "status": v.status,
                    "status_text": v.status_text,
                    "reminder_days": v.reminder_days,
                    "will_trigger": v.will_trigger,
                    "is_lunar_only": v.is_lunar_only,
                    "solar_invalid": v.solar_invalid,
                    "lunar_mismatch": v.lunar_mismatch,
                }
                for v in timeline
            ],
            "summary": summary,
            "total": len(timeline),
            "trigger_count": sum(1 for v in timeline if v.will_trigger),
        }

    # ---------- 收件人增删改 ----------

    def get_recipient(self, index: int) -> Dict[str, Any]:
        """编辑用：取单个收件人的表单值。"""
        try:
            raw = self._repo.get_recipients()[index]
        except (IndexError, ConfigNotFoundError):
            return {"ok": False, "error": "这条收件人不存在，可能已被删除"}

        solar = as_iso_text(raw.get("solar_birthday"))
        return {
            "ok": True,
            "values": {
                "name": raw.get("name", ""),
                "solar_birthday": solar,
                "reminder_days": raw.get("reminder_days", 0),
                "note": raw.get("note") or "",
            },
            "lunar_display": to_lunar_display_from_solar(solar) if solar else None,
        }

    def validate_solar(self, solar: str) -> Dict[str, Any]:
        """身份证出生年月日的即时校验 + 农历预览。"""
        solar = (solar or "").strip()
        if not solar:
            return {"ok": False, "state": "empty", "error": ""}

        parsed = parse_iso_date(solar)
        if parsed is None:
            return {"ok": False, "state": "bad", "error": INVALID_SOLAR_HINT}

        stored = solar_to_lunar(solar)
        if not stored or stored == solar:
            return {"ok": False, "state": "bad", "error": INVALID_SOLAR_HINT}

        nxt = next_solar_for_lunar(stored)
        nxt_solar = next_solar_occurrence(parsed)
        return {
            "ok": True,
            "state": "ok",
            "lunar_display": lunar_display(stored),
            "lunar_next_solar": nxt.isoformat() if nxt else "",
            "next_solar_birthday": nxt_solar.isoformat() if nxt_solar else "",
        }

    def _validate_recipient(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """共用校验：返回 (payload, errors)。"""
        try:
            payload = RecipientForm.model_validate(values)
            return {"payload": payload, "errors": {}}
        except ValidationError as exc:
            return {"payload": None, "errors": _form_errors(exc)}

    def save_recipient(self, values: Dict[str, Any], index: Optional[int] = None) -> Dict[str, Any]:
        """新增或更新收件人。``index`` 为 None 表示新增。"""
        result = self._validate_recipient(values)
        if result["errors"]:
            return {"ok": False, "errors": result["errors"], "error": _first_error(result["errors"])}

        payload = result["payload"]
        try:
            if index is None:
                self._repo.add_recipient(payload.to_config())
                return {"ok": True, "notice": f"已添加 {payload.name}"}
            self._repo.update_recipient(index, payload.to_config())
            return {"ok": True, "notice": f"已保存 {payload.name}"}
        except RecipientNotFoundError:
            return {"ok": False, "error": "这条收件人不存在，可能已被删除"}
        except Exception as exc:
            logger.exception("写入配置失败")
            return {"ok": False, "error": f"保存失败：{exc}"}

    def delete_recipient(self, index: int) -> Dict[str, Any]:
        try:
            name = self._repo.delete_recipient(index)
        except RecipientNotFoundError:
            return {"ok": False, "error": "这条收件人不存在，可能已被删除"}
        except Exception as exc:
            logger.exception("删除失败")
            return {"ok": False, "error": f"删除失败：{exc}"}
        return {"ok": True, "notice": f"已删除 {name}"}

    # ---------- 预览与测试发送 ----------

    def preview_recipient(self, index: int) -> Dict[str, Any]:
        """渲染这个人实际会收到的提醒内容（不发网络请求）。"""
        try:
            raw = self._repo.get_recipients()[index]
        except (IndexError, ConfigNotFoundError):
            return {"ok": False, "error": "这条收件人不存在，可能已被删除"}

        result = render_preview(raw, self._templates_dir())
        if not result.get("ok"):
            return {"ok": False, "error": result.get("reason", "无法预览")}

        return {
            "ok": True,
            "name": raw.get("name", "未命名"),
            "email_html": result.get("email_html"),
            "email_error": result.get("email_error"),
            "serverchan_text": result.get("serverchan_text"),
            "days_until": result.get("days_until"),
            "age": result.get("age"),
        }

    async def test_send(self, index: int) -> Dict[str, Any]:
        """立即发一条提醒，**真实发送**。

        ``run`` 命令只在提醒窗口内发送，这个能力让人随时自测，
        不必等到某人生日当天。用与 ``run`` 相同的发送器与模板。
        """
        try:
            raw = self._repo.get_recipients()[index]
        except (IndexError, ConfigNotFoundError):
            return {"ok": False, "error": "这条收件人不存在，可能已被删除"}

        name = raw.get("name") or "未命名"
        try:
            recipient = build_sendable_recipient(raw, resolve_receive_email(self._config))
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": f"{name} 的配置不完整，无法发送：{exc}"}

        if not recipient.email:
            return {
                "ok": False,
                "error": "没有可用的收件邮箱，请到设置页填写接收提醒的邮箱",
            }

        extra = render_reminder_for(recipient, datetime.now().date())
        if extra is None:
            return {"ok": False, "error": f"{name} 的生日日期无法计算"}

        try:
            senders = self._senders()
        except Exception as exc:
            logger.exception("创建发送器失败")
            return {"ok": False, "error": f"创建发送器失败：{exc}"}

        if not senders:
            return {"ok": False, "error": "没有可用的发送渠道，请检查设置"}

        results = await send_reminder_now(recipient, extra, senders)
        sent = [r for r in results if r["ok"]]
        failed = [r for r in results if not r["ok"]]

        if not sent:
            detail = "；".join(f"{r['channel']}: {r['detail']}" for r in failed)
            return {"ok": False, "error": f"发送失败：{detail}"}

        notice = f"已把 {name} 的提醒发到 {recipient.email}"
        if failed:
            detail = "；".join(f"{r['channel']}: {r['detail']}" for r in failed)
            notice += f"；{detail}"
        return {"ok": True, "notice": notice}

    # ---------- 设置 ----------

    def get_settings(self) -> Dict[str, Any]:
        values = self._repo.get_notification_raw()
        summary = self._repo.get_notification_summary()
        current_default = int(values.get("default_reminder_days") or 0)
        return {
            "ok": True,
            "values": {
                "resend_receive_email": values.get("resend_receive_email", ""),
                "resend_from_name": values.get("resend_from_name", ""),
                "resend_from_email": values.get("resend_from_email", ""),
                "default_reminder_days": current_default,
                "has_resend_key": values.get("has_resend_key", False),
                "resend_key_masked": values.get("resend_key_masked") or "",
            },
            "days_options": self._reminder_days_options(current_default),
            "summary": summary,
        }

    @staticmethod
    def _reminder_days_options(default: int) -> List[Dict[str, Any]]:
        """提前天数的候选值，供下拉选择（避免手填 365 这种离谱值）。"""
        common = [0, 1, 2, 3, 5, 7, 10, 14, 30]
        if default not in common:
            common.append(default)
        return [
            {"value": d, "label": "只在生日当天" if d == 0 else f"提前 {d} 天"}
            for d in sorted(set(common))
        ]

    def save_settings(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """保存设置。

        ``api_key`` 留空表示不修改（界面显示的是打码值）；
        显式传 ``clear_api_key`` 才清除。
        """
        form_values = dict(values or {})
        clear_key = bool(form_values.pop("clear_api_key", False))
        if clear_key:
            form_values["api_key"] = ""

        try:
            payload = ResendSettingsForm.model_validate(form_values)
        except ValidationError as exc:
            errors = _form_errors(exc)
            return {"ok": False, "errors": errors, "error": _first_error(errors)}

        repo_values = payload.to_repository_values()
        if clear_key:
            repo_values["clear_api_key"] = True

        try:
            self._repo.update_resend_settings(repo_values)
        except Exception as exc:
            logger.exception("保存设置失败")
            return {"ok": False, "error": f"保存失败：{exc}"}

        # 配置已变，刷新进程内对象，否则后续发送仍用旧值
        try:
            self._refresh_config()
        except Exception as exc:
            logger.exception("重新加载配置失败")
            return {"ok": False, "error": f"配置已写入，但重新加载失败：{exc}"}

        if clear_key:
            return {"ok": True, "notice": "已清除 API Key，现在无法发送邮件"}
        return {"ok": True, "notice": "设置已保存"}

    async def test_settings_send(self) -> Dict[str, Any]:
        """给当前设置的接收邮箱发一封测试邮件。真实发送。"""
        summary = self._repo.get_notification_summary()
        target = summary.get("resend_receive_email") or summary.get("default_receive_email")

        if not summary.get("resend_configured"):
            return {"ok": False, "error": "还没填 API Key，无法发送"}
        if not target:
            return {"ok": False, "error": "还没填接收邮箱，无法发送"}

        try:
            self._refresh_config()
        except Exception as exc:
            return {"ok": False, "error": f"重新加载配置失败：{exc}"}

        probe = Recipient(
            name="测试邮件",
            email=str(target),
            solar_birthday=datetime.now().strftime("%Y-%m-%d"),
            reminder_days=0,
            template_file="birthday.html",
        )
        extra = render_reminder_for(probe, datetime.now().date())
        if extra is None:
            return {"ok": False, "error": "无法生成测试内容"}

        try:
            senders = [
                s
                for s in self._senders()
                if type(s).__name__ in ("ResendSender", "EmailSender")
            ]
        except Exception as exc:
            logger.exception("创建发送器失败")
            return {"ok": False, "error": f"创建发送器失败：{exc}"}

        if not senders:
            return {"ok": False, "error": "当前没有可用的邮件渠道，请检查 Resend 配置"}

        results = await send_reminder_now(probe, extra, senders)
        failed = [r for r in results if not r["ok"]]
        if len(failed) == len(results):
            detail = "；".join(r["detail"] for r in failed)
            return {"ok": False, "error": f"测试发送失败：{detail}"}

        return {"ok": True, "notice": f"测试邮件已发到 {target}，请查收（可能进垃圾箱）"}
