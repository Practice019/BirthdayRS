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

from src.core.config import AUDIENCE_LABELS, AUDIENCES, Recipient
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
    format_solar_input,
    normalize_solar_input,
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
from src.notification.sender_wxpusher import clear_followers_cache
from src.web.settings_schemas import (
    ResendSettingsForm,
    WxPusherSettingsForm,
    validate_channel_requirements,
)

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
        return self._config_manager.reload()

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
                    "reminder_days_inherited": v.reminder_days_inherited,
                    "audience": v.audience,
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
        # reminder_days 没单独设置过时返回 None，界面留空（= 沿用全局默认），
        # 而不是回填一个会把默认值固化下来的数字。
        # 生日回填 8 位数字，与输入框要求的写法一致（校验仍用存储格式）。
        return {
            "ok": True,
            "values": {
                "name": raw.get("name", ""),
                "solar_birthday": format_solar_input(solar),
                "reminder_days": raw.get("reminder_days"),
                # 旧记录没有这个字段，语义上就是"只发给我自己"
                "audience": raw.get("audience") or "self",
                "note": raw.get("note") or "",
            },
            "lunar_display": to_lunar_display_from_solar(solar) if solar else None,
        }

    def validate_solar(self, solar: str) -> Dict[str, Any]:
        """身份证出生年月日的即时校验 + 农历预览。

        接受 8 位数字（界面上的写法）或 ``1990-01-20``，统一归一化后再算。
        """
        raw = (solar or "").strip()
        if not raw:
            return {"ok": False, "state": "empty", "error": ""}

        normalized = normalize_solar_input(raw)
        parsed = parse_iso_date(normalized)
        if parsed is None:
            return {"ok": False, "state": "bad", "error": INVALID_SOLAR_HINT}

        stored = solar_to_lunar(normalized)
        if not stored or stored == normalized:
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

    def set_audience_bulk(self, indices: List[int], audience: str) -> Dict[str, Any]:
        """批量把若干条记录的受众改成 ``audience``。与 web 版同一套口径。"""
        if audience not in AUDIENCES:
            return {"ok": False, "error": "请选择要设成哪个受众"}
        if not indices:
            return {"ok": False, "error": "没有勾选任何记录"}

        try:
            changed = self._repo.set_audience_many(indices, audience)
        except Exception as exc:
            logger.exception("批量修改受众失败")
            return {"ok": False, "error": f"保存失败：{exc}"}

        # 配置已变，刷新进程内对象
        try:
            self._refresh_config()
        except Exception as exc:
            logger.exception("重新加载配置失败")
            return {"ok": False, "error": f"已写入，但重新加载失败：{exc}"}

        label = AUDIENCE_LABELS.get(audience, audience)
        if changed == 0:
            return {"ok": True, "notice": f"勾选的 {len(indices)} 条已经是这个设置，无需改动"}

        note = f"已把 {changed} 条改为「{label}」"
        skipped = len(indices) - changed
        if skipped:
            note += f"（{skipped} 条无需改动或已不存在）"
        if audience == "group":
            note += "。注意：它们下次过生日会广播给所有人"
        return {"ok": True, "notice": note}

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
            "push_text": result.get("push_text"),
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

        # 邮箱只对邮件类渠道是必需的：只开手机推送时它可以是空的。
        needs_email = any(
            type(s).__name__ in ("ResendSender", "EmailSender") for s in senders
        )
        if needs_email and not recipient.email:
            return {
                "ok": False,
                "error": "邮件渠道需要收件邮箱，请到设置页填写接收提醒的邮箱",
            }

        results = await send_reminder_now(recipient, extra, senders)
        sent = [r for r in results if r["ok"]]
        failed = [r for r in results if not r["ok"]]

        if not sent:
            detail = "；".join(f"{r['channel']}: {r['detail']}" for r in failed)
            return {"ok": False, "error": f"发送失败：{detail}"}

        # 推送没有"收件地址"，说成"发到你的手机"才准确。
        # 邮件地址取**当前配置**里的接收邮箱，而不是收件人条目上可能过期的值。
        current_email = resolve_receive_email(self._config)
        where_parts = []
        if any(type(s).__name__ in ("ResendSender", "EmailSender") for s in senders):
            where_parts.append(str(current_email or recipient.email or "邮箱未填"))
        if any(type(s).__name__ == "WxPusherSender" for s in senders):
            where_parts.append("手机")
        where = " 和 ".join(where_parts) if where_parts else "已启用的渠道"
        notice = f"已把 {name} 的提醒发到 {where}"
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
                "has_wxpusher_spt": values.get("has_wxpusher_spt", False),
                "wxpusher_spt_masked": values.get("wxpusher_spt_masked") or "",
                # 启用中的渠道，供勾选框回填
                "enabled_types": values.get("types", []),
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

        密钥类字段（API Key / SPT）留空表示不修改（界面显示的是打码值）；
        显式传 ``clear_api_key`` / ``clear_spt`` 才清除。
        与 web 版走同一套 schema 与校验，两端行为保持一致。
        """
        form_values = dict(values or {})
        clear_key = bool(form_values.pop("clear_api_key", False))
        clear_spt = bool(form_values.pop("clear_spt", False))
        clear_app_token = bool(form_values.pop("clear_app_token", False))
        if clear_key:
            form_values["api_key"] = ""
        if clear_spt:
            form_values["spt"] = ""
        if clear_app_token:
            form_values["app_token"] = ""

        enabled_types = list(form_values.get("enabled_types") or [])
        raw = self._repo.get_notification_raw()
        errors: Dict[str, str] = {}

        try:
            payload = ResendSettingsForm.model_validate(form_values)
        except ValidationError as exc:
            errors.update(_form_errors(exc))

        try:
            wx_payload = WxPusherSettingsForm.model_validate(form_values)
        except ValidationError as exc:
            errors.update(_form_errors(exc))

        errors.update(
            validate_channel_requirements(
                enabled_types,
                form_values,
                existing={
                    "has_resend_key": raw.get("has_resend_key", False),
                    "has_wxpusher_app_token": raw.get("has_wxpusher_app_token", False),
                    "has_wxpusher_spt": raw.get("has_wxpusher_spt", False),
                },
            )
        )

        if errors:
            return {"ok": False, "errors": errors, "error": _first_error(errors)}

        try:
            repo_values = payload.to_repository_values()
            if clear_key:
                repo_values["clear_api_key"] = True
            self._repo.update_resend_settings(repo_values)

            wx_values = wx_payload.to_repository_values()
            if clear_spt:
                wx_values["clear_spt"] = True
            if clear_app_token:
                wx_values["clear_app_token"] = True
            self._repo.update_wxpusher_settings(wx_values)

            # 换过 appToken 后关注者列表就不可信了
            if clear_app_token or (wx_payload.app_token or "").strip():
                clear_followers_cache()

            self._repo.update_notification_settings(
                {
                    "enabled_types": enabled_types,
                    "default_reminder_days": int(payload.default_reminder_days),
                }
            )
        except Exception as exc:
            logger.exception("保存设置失败")
            return {"ok": False, "error": f"保存失败：{exc}"}

        # 配置已变，刷新进程内对象，否则后续发送仍用旧值
        try:
            self._refresh_config()
        except Exception as exc:
            logger.exception("重新加载配置失败")
            return {"ok": False, "error": f"配置已写入，但重新加载失败：{exc}"}

        if clear_key or clear_spt or clear_app_token:
            return {"ok": True, "notice": "已清除密钥，对应的渠道现在发不出去"}
        return {"ok": True, "notice": "设置已保存"}

    async def test_settings_send(self) -> Dict[str, Any]:
        """给**所有已启用的渠道**各发一条测试提醒。真实发送。

        与 web 版一致：配置页要验证的是"整套配置能不能送到我手上"，
        只验邮件会让"推送配错了"漏到生日当天才发现。
        """
        summary = self._repo.get_notification_summary()
        types = summary.get("types") or []

        if not types:
            return {"ok": False, "error": "还没启用任何通知渠道，请先勾选至少一个"}

        target = summary.get("resend_receive_email") or summary.get("default_receive_email")

        try:
            self._refresh_config()
        except Exception as exc:
            return {"ok": False, "error": f"重新加载配置失败：{exc}"}

        probe = Recipient(
            name="测试提醒",
            email=str(target) if target else None,
            solar_birthday=datetime.now().strftime("%Y-%m-%d"),
            reminder_days=0,
            template_file="birthday.html",
        )
        extra = render_reminder_for(probe, datetime.now().date())
        if extra is None:
            return {"ok": False, "error": "无法生成测试内容"}

        try:
            senders = self._senders()
        except Exception as exc:
            logger.exception("创建发送器失败")
            return {"ok": False, "error": f"创建发送器失败：{exc}"}

        if not senders:
            return {"ok": False, "error": "没有可用的发送渠道，请检查已启用渠道的凭据是否完整"}

        results = await send_reminder_now(probe, extra, senders)
        failed = [r for r in results if not r["ok"]]
        if len(failed) == len(results):
            detail = "；".join(f"{r['channel']}：{r['detail']}" for r in failed)
            return {"ok": False, "error": f"测试发送失败：{detail}"}

        sent = len(results) - len(failed)
        if failed:
            detail = "；".join(f"{r['channel']}：{r['detail']}" for r in failed)
            return {"ok": True, "notice": f"已通过 {sent} 个渠道发出测试提醒；{detail}"}
        return {"ok": True, "notice": f"已通过 {sent} 个渠道发出测试提醒，请查收"}
