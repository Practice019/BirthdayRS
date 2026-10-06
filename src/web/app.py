"""FastAPI 应用：BirthdayRS 管理台。

结构遵循薄路由原则 —— 路由只做「取数据 → 校验 → 调仓储 → 渲染」，业务计算在
``domain.py``，文件读写在 ``repository.py``。

**表单解析刻意手写**（不走 ``Annotated[Model, Form()]``）：FastAPI 的内置表单校验会在进入
路由前抛 ``RequestValidationError``，此时拿不到用户提交的原始值，只能返回空表单让用户重填。
手写解析可以在校验失败时把输入原样填回并就地标注错误。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from src.core.config_manager import ConfigManager
from src.core.config import Recipient
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
    validate_solar_date,
)
from src.web.repository import (
    ConfigNotFoundError,
    ConfigRepository,
    RecipientNotFoundError,
)
from src.web.schemas import RecipientForm
from src.web.settings_schemas import ResendSettingsForm

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
_WEB_TEMPLATES = _ROOT / "templates" / "web"
_STATIC = Path(__file__).resolve().parent / "static"


def create_app(config_path: Optional[str] = None) -> FastAPI:
    """应用工厂。"""
    config_manager = ConfigManager(config_path)
    config_manager.config  # 触发加载，配置有问题时尽早失败

    repo = ConfigRepository(config_manager.config_path)

    app = FastAPI(title="BirthdayRS 管理台", version="0.1.0", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(_WEB_TEMPLATES))
    templates.env.filters["dash"] = lambda v: v if v not in (None, "") else "—"
    if _STATIC.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    app.state.repo = repo
    app.state.config_manager = config_manager
    app.state.config = config_manager.config

    # ---------- 辅助 ----------

    def page_context(request: Request, **extra: Any) -> Dict[str, Any]:
        """所有页面共享的上下文。"""
        notice = request.query_params.get("notice")
        error = request.query_params.get("error")
        return {
            "request": request,
            "notice": notice,
            "error": error,
            "active": None,
            **extra,
        }

    def get_reminder_default() -> int:
        return int(repo.get_notification_summary().get("default_reminder_days") or 0)

    # 兜底翻译：schema 已尽量给出中文提示，这里防住漏网的内建英文消息。
    _FALLBACK_MESSAGES = {
        "string_too_long": "内容太长了",
        "missing": "这一项必填",
        "string_type": "请填写文本",
        "int_parsing": "请填写整数",
    }

    def collect_form_errors(exc: ValidationError) -> Dict[str, str]:
        """把 Pydantic 错误压成 {字段名: 中文提示}。

        模型级校验（跨字段）的 ``loc`` 为空，归到 ``__all__``。
        """
        errors: Dict[str, str] = {}
        for err in exc.errors():
            loc = err.get("loc") or ()
            field = loc[0] if loc else "__all__"
            message = err.get("msg", "输入有误")
            # 去掉 Pydantic 附加的 "Value error, " 前缀，只留我们写的中文提示。
            message = message.replace("Value error, ", "").replace("Assertion failed, ", "")
            if message in _FALLBACK_MESSAGES:
                message = _FALLBACK_MESSAGES[message]
            errors.setdefault(str(field), message)
        return errors

    def render_form(
        request: Request,
        values: Dict[str, Any],
        errors: Dict[str, str],
        mode: str,
        index: Optional[int] = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        context = page_context(
            request,
            values=with_lunar_preview(values),
            errors=errors,
            mode=mode,
            index=index,
            active="new" if mode == "create" else None,
        )
        return templates.TemplateResponse(
            request, "form.html", context, status_code=status_code
        )

    def blank_form_values() -> Dict[str, Any]:
        return {
            "name": "",
            "solar_birthday": "",
            "reminder_days": get_reminder_default(),
            "note": "",
        }

    def with_lunar_preview(values: Dict[str, Any]) -> Dict[str, Any]:
        """给表单值补上农历展示字段与阳历校验结论，供模板渲染。

        阳历不合理时不推导农历 —— 与 ``/api/lunar`` 保持同一判断，
        避免服务端渲染出一个前端不会显示的假农历值。

        注意：YAML 里裸写的 ``1990-01-20`` 会被解析成 ``datetime.date`` 而不是字符串，
        所以这里必须先归一化成字符串，否则 ``.strip()`` 会炸。
        """
        values = dict(values)
        solar = as_iso_text(values.get("solar_birthday"))

        valid, message = validate_solar_date(solar) if solar else (False, "")
        values["solar_state"] = (
            "ok" if valid else ("bad" if solar else "empty")
        )
        values["solar_message"] = message

        values["lunar_display"] = (
            to_lunar_display_from_solar(solar) if valid else None
        )
        if valid:
            stored = solar_to_lunar(solar)
            # 农历生日对应的阳历日期（农历月日反查），显示在农历后面的括号里
            lunar_solar = next_solar_for_lunar(stored) if stored else None
            values["lunar_next_solar"] = lunar_solar.isoformat() if lunar_solar else None
            # 阳历生日（身份证月/日）的下一次 —— 与上面是两个不同的日期
            solar_date = parse_iso_date(solar)
            nxt_solar = next_solar_occurrence(solar_date) if solar_date else None
            values["next_solar_birthday"] = nxt_solar.isoformat() if nxt_solar else None
        else:
            values["lunar_next_solar"] = None
            values["next_solar_birthday"] = None
        return values

    # ---------- 路由 ----------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        try:
            recipients = repo.get_recipients()
        except ConfigNotFoundError:
            return templates.TemplateResponse(
                request,
                "error.html",
                page_context(
                    request,
                    title="找不到配置文件",
                    detail=f"期望的路径是 {repo.path}。先从 config.example.yml 复制一份再启动。",
                ),
                status_code=500,
            )
        except Exception as exc:
            logger.exception("读取配置失败")
            return templates.TemplateResponse(
                request,
                "error.html",
                page_context(request, title="读取配置失败", detail=str(exc)),
                status_code=500,
            )

        summary = repo.get_notification_summary()
        timeline = build_timeline(recipients, summary.get("default_reminder_days") or 0)

        return templates.TemplateResponse(
            request,
            "list.html",
            page_context(
                request,
                timeline=timeline,
                summary=summary,
                total=len(timeline),
                trigger_count=sum(1 for v in timeline if v.will_trigger),
                active="home",
            ),
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request) -> HTMLResponse:
        """设置页：编辑让项目正常运行的 Resend 配置。

        ``api_key`` 只显示打码值；留空提交表示不修改（见 ``update_resend_settings``）。
        """
        return templates.TemplateResponse(
            request,
            "settings.html",
            page_context(
                request,
                values=repo.get_notification_raw(),
                summary=repo.get_notification_summary(),
                errors={},
                saved=request.query_params.get("saved") == "1",
                test=None,
                active="settings",
            ),
        )

    @app.post("/settings", response_class=HTMLResponse)
    async def save_settings(request: Request) -> HTMLResponse:
        form = await request.form()
        values = dict(form)
        errors = {}

        # 允许用 checkbox 显式清空密钥
        if values.get("clear_api_key"):
            values["api_key"] = ""
            clear_key = True
        else:
            clear_key = False

        try:
            payload = ResendSettingsForm.model_validate(values)
        except ValidationError as exc:
            errors = collect_form_errors(exc)

        if errors:
            # 校验失败：把输入回填，但密钥字段不回填明文（用户没填就是没填）。
            merged = dict(repo.get_notification_raw())
            merged.update(
                {
                    "resend_receive_email": values.get("default_receive_email", ""),
                    "resend_from_name": values.get("from_name", ""),
                    "resend_from_email": values.get("from_email", ""),
                    "default_reminder_days": values.get("default_reminder_days", "3"),
                }
            )
            return templates.TemplateResponse(
                request,
                "settings.html",
                page_context(
                    request,
                    values=merged,
                    summary=repo.get_notification_summary(),
                    errors=errors,
                    saved=False,
                    test=None,
                    active="settings",
                ),
                status_code=400,
            )

        try:
            repo_values = payload.to_repository_values()
            # 显式勾选"清除"时把意图传下去 —— schema 会把空 key 规范成"不改动"，
            # 这里补回用户的真实意图，否则勾选框看起来生效实际被覆盖。
            if clear_key:
                repo_values["clear_api_key"] = True
            repo.update_resend_settings(repo_values)
        except Exception as exc:
            logger.exception("保存设置失败")
            return templates.TemplateResponse(
                request,
                "settings.html",
                page_context(
                    request,
                    values=repo.get_notification_raw(),
                    summary=repo.get_notification_summary(),
                    errors={"__all__": f"保存失败：{exc}"},
                    saved=False,
                    test=None,
                    active="settings",
                ),
                status_code=500,
            )

        if clear_key:
            return RedirectResponse(
                "/settings?notice=已清除 API Key，现在无法发送邮件&saved=1", status_code=303
            )
        return RedirectResponse("/settings?notice=设置已保存&saved=1", status_code=303)

    @app.post("/settings/test")
    async def settings_test_send(request: Request) -> RedirectResponse:
        """发一封测试邮件到当前配置的接收邮箱。

        这是真实发送 —— 设置改完后需要一条"能不能通"的即时反馈，
        否则只能等到某人生日到达才发现配错了。
        """
        summary = repo.get_notification_summary()
        target = summary.get("resend_receive_email") or summary.get("default_receive_email")

        if not summary.get("resend_configured"):
            return RedirectResponse("/settings?error=还没填 API Key，无法发送", status_code=303)
        if not target:
            return RedirectResponse("/settings?error=还没填接收邮箱，无法发送", status_code=303)

        # 重新加载配置：设置可能刚被改动过
        try:
            app.state.config_manager._config = None
            app.state.config = app.state.config_manager.config
        except Exception as exc:
            logger.exception("重新加载配置失败")
            return RedirectResponse(f"/settings?error=重新加载配置失败：{exc}", status_code=303)

        probe = Recipient(
            name="测试邮件",
            email=str(target),
            solar_birthday=datetime.now().strftime("%Y-%m-%d"),
            reminder_days=0,
            template_file="birthday.html",
        )
        extra = render_reminder_for(probe, datetime.now().date())
        if extra is None:
            return RedirectResponse("/settings?error=无法生成测试内容", status_code=303)

        try:
            senders = NotificationFactory(
                app.state.config_manager.get_templates_dir()
            ).create_senders(app.state.config)
        except Exception as exc:
            logger.exception("创建发送器失败")
            return RedirectResponse(f"/settings?error=创建发送器失败：{exc}", status_code=303)

        # 设置页的测试只走邮件类渠道
        mail_senders = [
            s for s in senders if type(s).__name__ in ("ResendSender", "EmailSender")
        ]
        if not mail_senders:
            return RedirectResponse(
                "/settings?error=当前没有可用的邮件渠道，请检查 Resend 配置", status_code=303
            )

        results = await send_reminder_now(probe, extra, mail_senders)
        ok = [r for r in results if r["ok"]]
        bad = [r for r in results if not r["ok"]]

        if not ok:
            detail = "；".join(r["detail"] for r in bad)
            return RedirectResponse(f"/settings?error=测试发送失败：{detail}", status_code=303)
        if bad:
            detail = "；".join(r["detail"] for r in bad)
            return RedirectResponse(
                f"/settings?notice=测试邮件已发到 {target}；{detail}", status_code=303
            )

        return RedirectResponse(
            f"/settings?notice=测试邮件已发到 {target}，请查收（可能进垃圾箱）", status_code=303
        )

    @app.get("/recipients/new", response_class=HTMLResponse)
    async def new_recipient(request: Request) -> HTMLResponse:
        return render_form(request, blank_form_values(), {}, "create")

    @app.post("/recipients")
    async def create_recipient(request: Request) -> HTMLResponse:
        form = await request.form()
        values = dict(form)
        try:
            payload = RecipientForm.model_validate(values)
        except ValidationError as exc:
            return render_form(request, values, collect_form_errors(exc), "create", status_code=400)

        try:
            repo.add_recipient(payload.to_config())
        except Exception as exc:
            logger.exception("写入配置失败")
            return render_form(
                request,
                values,
                {"__all__": f"保存失败：{exc}"},
                "create",
                status_code=500,
            )

        return RedirectResponse(f"/?notice=已添加 {payload.name}", status_code=303)

    @app.get("/recipients/{index}/edit", response_class=HTMLResponse)
    async def edit_recipient(request: Request, index: int) -> HTMLResponse:
        try:
            raw = repo.get_recipients()[index]
        except (IndexError, ConfigNotFoundError):
            return RedirectResponse("/?error=这条收件人不存在，可能已被删除", status_code=303)

        values = {
            "name": raw.get("name", ""),
            "solar_birthday": raw.get("solar_birthday") or "",
            "reminder_days": raw.get("reminder_days", get_reminder_default()),
            "note": raw.get("note") or "",
        }
        return render_form(request, values, {}, "edit", index=index)

    @app.get("/api/lunar")
    async def api_lunar(solar: str = "") -> Dict[str, Any]:
        """阳历 → 农历查询，供表单实时校验与预览。

        **必须校验**：lunar_python 会把 ``1990-01-32`` 静默归一化成 1990-02-01，
        于是非法输入也能算出一个看似正常的农历（正月初六）。这里先用
        ``parse_iso_date`` 拒绝，避免把假数据当成结果显示给使用者。
        """
        solar = (solar or "").strip()
        if not solar:
            return {"ok": False, "display": None, "hint": ""}

        parsed = parse_iso_date(solar)
        if parsed is None:
            return {
                "ok": False,
                "display": None,
                "hint": INVALID_SOLAR_HINT,
            }

        stored = solar_to_lunar(solar)
        if not stored:
            return {"ok": False, "display": None, "hint": "无法换算农历，请检查日期"}
        if stored == solar:
            # 极端情况下若换算原地返回，说明输入被上游归一化过。
            return {"ok": False, "display": None, "hint": INVALID_SOLAR_HINT}

        nxt = next_solar_for_lunar(stored)
        # 阳历生日（身份证月/日）的下一次 —— 与上面是**两个不同的日期**。
        nxt_solar = next_solar_occurrence(parsed)
        return {
            "ok": True,
            "display": lunar_display(stored),
            "stored": stored,
            # 农历生日对应的阳历日期
            "next_solar": nxt.isoformat() if nxt else None,
            # 阳历生日的下一次
            "next_solar_birthday": nxt_solar.isoformat() if nxt_solar else None,
            "hint": "",
        }

    @app.post("/recipients/{index}")
    async def update_recipient(request: Request, index: int) -> HTMLResponse:
        form = await request.form()
        values = dict(form)
        try:
            payload = RecipientForm.model_validate(values)
        except ValidationError as exc:
            return render_form(
                request, values, collect_form_errors(exc), "edit", index=index, status_code=400
            )

        try:
            repo.update_recipient(index, payload.to_config())
        except RecipientNotFoundError:
            return RedirectResponse("/?error=这条收件人不存在，可能已被删除", status_code=303)
        except Exception as exc:
            logger.exception("写入配置失败")
            return render_form(
                request,
                values,
                {"__all__": f"保存失败：{exc}"},
                "edit",
                index=index,
                status_code=500,
            )

        return RedirectResponse(f"/?notice=已保存 {payload.name}", status_code=303)

    @app.post("/recipients/{index}/delete")
    async def delete_recipient(request: Request, index: int) -> RedirectResponse:
        try:
            name = repo.delete_recipient(index)
        except RecipientNotFoundError:
            return RedirectResponse("/?error=这条收件人不存在，可能已被删除", status_code=303)
        except Exception as exc:
            logger.exception("删除失败")
            return RedirectResponse(f"/?error=删除失败：{exc}", status_code=303)

        return RedirectResponse(f"/?notice=已删除 {name}", status_code=303)

    @app.post("/recipients/{index}/test-send")
    async def test_send(request: Request, index: int) -> RedirectResponse:
        """立即给这个人发一条提醒，**真实发送**。

        存在的理由：``run`` 命令只在提醒窗口内发送，想看一条实际效果就得等到那天。
        这个按钮让人随时自测，不必改生日或改窗口。

        用与 ``run`` 相同的发送器与模板，所以「测试收到的」就是「将来会收到的」。
        """
        try:
            raw = repo.get_recipients()[index]
        except (IndexError, ConfigNotFoundError):
            return RedirectResponse("/?error=这条收件人不存在，可能已被删除", status_code=303)

        name = raw.get("name") or "未命名"

        try:
            recipient = build_sendable_recipient(raw, resolve_receive_email(app.state.config))
        except (ValueError, TypeError) as exc:
            return RedirectResponse(
                f"/?error={name} 的配置不完整，无法发送：{exc}", status_code=303
            )

        if not recipient.email:
            return RedirectResponse(
                "/?error=没有可用的收件邮箱。请在 config.yml 的 resend 里填 "
                "default_receive_email",
                status_code=303,
            )

        extra = render_reminder_for(recipient, datetime.now().date())
        if extra is None:
            return RedirectResponse(f"/?error={name} 的生日日期无法计算", status_code=303)

        try:
            senders = NotificationFactory(app.state.config_manager.get_templates_dir()).create_senders(
                app.state.config
            )
        except Exception as exc:
            logger.exception("创建发送器失败")
            return RedirectResponse(f"/?error=创建发送器失败：{exc}", status_code=303)

        if not senders:
            return RedirectResponse(
                "/?error=没有可用的发送渠道，请检查 config.yml 的 start_notification",
                status_code=303,
            )

        results = await send_reminder_now(recipient, extra, senders)

        sent = [r["channel"] for r in results if r["ok"]]
        failed = [r for r in results if not r["ok"]]

        if failed and not sent:
            detail = "；".join(f"{r['channel']}: {r['detail']}" for r in failed)
            return RedirectResponse(f"/?error=发送失败：{detail}", status_code=303)
        if failed:
            detail = "；".join(f"{r['channel']}: {r['detail']}" for r in failed)
            return RedirectResponse(
                f"/?notice=已通过 {', '.join(sent)} 发送给 {name}；{detail}", status_code=303
            )

        channels = "、".join("邮件" if "Resend" in c or "Email" in c else c for c in sent)
        return RedirectResponse(
            f"/?notice=已通过{channels}把 {name} 的提醒发到 {recipient.email}",
            status_code=303,
        )

    @app.get("/recipients/{index}/preview", response_class=HTMLResponse)
    async def preview_recipient(request: Request, index: int) -> HTMLResponse:
        try:
            raw = repo.get_recipients()[index]
        except (IndexError, ConfigNotFoundError):
            return RedirectResponse("/?error=这条收件人不存在，可能已被删除", status_code=303)

        result = render_preview(raw, str(_ROOT / "templates"))

        return templates.TemplateResponse(
            request,
            "preview.html",
            page_context(
                request,
                name=raw.get("name", "未命名"),
                index=index,
                result=result,
            ),
        )

    @app.exception_handler(ConfigNotFoundError)
    async def config_missing_handler(request: Request, exc: ConfigNotFoundError) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "error.html",
            page_context(request, title="找不到配置文件", detail=str(exc)),
            status_code=500,
        )

    return app
