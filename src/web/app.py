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
from src.core.config import AUDIENCE_LABELS, AUDIENCES, Recipient
from src.core.notification_factory import NotificationFactory
from src.notification.sender_wxpusher import clear_followers_cache
from src.web.auth import TokenAuthMiddleware
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
    lunar_display,
    next_solar_for_lunar,
    normalize_solar_input,
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
from src.web.settings_schemas import (
    ResendSettingsForm,
    WxPusherSettingsForm,
    validate_channel_requirements,
)

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
_WEB_TEMPLATES = _ROOT / "templates" / "web"
_STATIC = Path(__file__).resolve().parent / "static"

#: 发送器类名 → 面向使用者的渠道称呼。
#: 界面上不该出现 ``ResendSender`` 这种内部标识。
_CHANNEL_LABELS = {
    "ResendSender": "邮件",
    "EmailSender": "邮件",
    "WxPusherSender": "手机推送",
    "ServerChanSender": "微信推送",
}

#: 配置里的渠道标识 → 界面称呼。与上面的类名表分开：一个在模板里用（配置层），
#: 一个在发送结果提示里用（运行层），两层的关键字来源不同。
_CHANNEL_NAMES = {
    "resend": "邮件",
    "email": "邮件",
    "wxpusher": "手机推送",
    "serverchan": "微信推送",
}


def _channel_label(class_name: str) -> str:
    return _CHANNEL_LABELS.get(class_name, class_name)


def _channel_to_label(name: str) -> str:
    return _CHANNEL_NAMES.get(str(name), str(name))


def create_app(config_path: Optional[str] = None, token: Optional[str] = None) -> FastAPI:
    """应用工厂。

    ``token`` 非空时启用访问令牌鉴权（见 ``src/web/auth.py``）。默认 ``None``
    表示不鉴权 —— 桌面端与测试用同一套路由，它们不需要这一层；
    命令行 ``web`` 命令会总是传一个 token。
    """
    config_manager = ConfigManager(config_path)
    config_manager.config  # 触发加载，配置有问题时尽早失败

    repo = ConfigRepository(config_manager.config_path)

    app = FastAPI(title="BirthdayRS 管理台", version="0.1.0", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(_WEB_TEMPLATES))
    templates.env.filters["dash"] = lambda v: v if v not in (None, "") else "—"
    # 渠道名 → 界面称呼。模板里不要出现 resend / wxpusher 这类内部标识。
    templates.env.globals["channel_label"] = _channel_to_label
    if _STATIC.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    if token:
        # 加在最外层：所有路由（含以后新增的）自动受保护。
        app.add_middleware(TokenAuthMiddleware, token=token)
        app.state.token = token

    app.state.repo = repo
    app.state.config_manager = config_manager

    # 配置**不缓存**在 app.state 里。
    #
    # 曾经这里放着 `app.state.config = config_manager.config`，而配置页保存后
    # 只有设置页那几个路由会去刷新它 —— 于是"改了设置，收件人页的测试发送仍在用
    # 旧配置"：界面显示已改成只发手机推送，实际还在发邮件。
    # 单例式缓存和"配置随时可能被改"这件事天然冲突，索性每次都从 ConfigManager 取，
    # 而 ConfigManager 自己负责缓存与失效。
    app.state.current_config = lambda: config_manager.config

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
            # 表单里「提前几天提醒」留空时用的值，只作提示展示。
            default_reminder_days=get_reminder_default(),
        )
        return templates.TemplateResponse(
            request, "form.html", context, status_code=status_code
        )

    def blank_form_values() -> Dict[str, Any]:
        # reminder_days 留空：表示"不单独设置"，用设置页里的全局默认。
        # 原来这里填的是全局默认值本身，于是新建时会把当时的默认值固化到这个人身上，
        # 之后改全局默认对他就再也不生效了。
        return {
            "name": "",
            "solar_birthday": "",
            "reminder_days": "",
            # 默认"只发给我自己"：私人朋友是常态，广播出去的信息收不回来。
            # 想广播必须显式选一次。
            "audience": "self",
            "note": "",
        }

    def with_lunar_preview(values: Dict[str, Any]) -> Dict[str, Any]:
        """给表单值补上农历展示字段与阳历校验结论，供模板渲染。

        阳历不合理时不推导农历 —— 与 ``/api/lunar`` 保持同一判断，
        避免服务端渲染出一个前端不会显示的假农历值。

        注意：YAML 里裸写的 ``1990-01-20`` 会被解析成 ``datetime.date`` 而不是字符串，
        所以这里必须先归一化成字符串，否则 ``.strip()`` 会炸。

        **输入框里回填的是 8 位数字**（``19900120``），与使用者要填的写法一致；
        存储与校验仍用 ``YYYY-MM-DD``。转换集中在这一个入口，模板只负责展示。
        """
        values = dict(values)
        solar = as_iso_text(values.get("solar_birthday"))
        # 校验用存储格式；回填用 8 位数字。两者都从同一个原始值算出来。
        values["solar_birthday"] = format_solar_input(solar) or solar

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
        """设置页：编辑让项目能正常发提醒所需的配置。

        密钥类字段（API Key / SPT）只显示打码值；留空提交表示不修改
        （见 ``update_resend_settings`` / ``update_wxpusher_settings``）。
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

        # 勾选框未选中时不会出现在表单里，所以要显式取列表。
        enabled_types = [t for t in form.getlist("enabled_types") if t]

        # 允许用 checkbox 显式清空密钥
        clear_key = bool(values.get("clear_api_key"))
        clear_spt = bool(values.get("clear_spt"))
        clear_app_token = bool(values.get("clear_app_token"))
        if clear_key:
            values["api_key"] = ""
        if clear_spt:
            values["spt"] = ""
        if clear_app_token:
            values["app_token"] = ""

        raw = repo.get_notification_raw()
        errors: Dict[str, str] = {}

        try:
            resend_payload = ResendSettingsForm.model_validate(values)
        except ValidationError as exc:
            errors.update(collect_form_errors(exc))

        try:
            wxpusher_payload = WxPusherSettingsForm.model_validate(values)
        except ValidationError as exc:
            errors.update(collect_form_errors(exc))

        # 跨字段：被启用的渠道必须有凭据。放在字段校验之后，两类错误一起回填。
        errors.update(
            validate_channel_requirements(
                enabled_types,
                values,
                existing={
                    "has_resend_key": raw.get("has_resend_key", False),
                    "has_wxpusher_app_token": raw.get("has_wxpusher_app_token", False),
                    "has_wxpusher_spt": raw.get("has_wxpusher_spt", False),
                },
            )
        )

        if errors:
            # 校验失败：把输入回填，但密钥字段不回填明文（用户没填就是没填）。
            merged = dict(raw)
            merged.update(
                {
                    "types": enabled_types or raw.get("types") or [],
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
            repo_values = resend_payload.to_repository_values()
            # 显式勾选"清除"时把意图传下去 —— schema 会把空 key 规范成"不改动"，
            # 这里补回用户的真实意图，否则勾选框看起来生效实际被覆盖。
            if clear_key:
                repo_values["clear_api_key"] = True
            repo.update_resend_settings(repo_values)

            wx_values = wxpusher_payload.to_repository_values()
            if clear_spt:
                wx_values["clear_spt"] = True
            if clear_app_token:
                wx_values["clear_app_token"] = True
            repo.update_wxpusher_settings(wx_values)

            # 换过 appToken 后关注者列表就不可信了，清掉缓存让它重新拉。
            if clear_app_token or (wxpusher_payload.app_token or "").strip():
                clear_followers_cache()

            # 渠道启用状态与默认天数统一在这一层落盘：
            # start_notification 只能有一个来源，分散在各渠道里会互相覆盖。
            repo.update_notification_settings(
                {
                    "enabled_types": enabled_types,
                    "default_reminder_days": int(resend_payload.default_reminder_days),
                }
            )
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

        # 让缓存失效：否则时间轴与收件人页仍按旧渠道渲染/发送。
        app.state.config_manager.reload()

        if clear_key or clear_spt or clear_app_token:
            return RedirectResponse(
                "/settings?notice=已清除密钥，对应的渠道现在发不出去&saved=1", status_code=303
            )
        return RedirectResponse("/settings?notice=设置已保存&saved=1", status_code=303)

    @app.post("/settings/test")
    async def settings_test_send(request: Request) -> RedirectResponse:
        """给**所有已启用的渠道**各发一条测试消息。

        这是真实发送 —— 设置改完后需要一条"能不能通"的即时反馈，
        否则只能等到某人生日到达才发现配错了。

        走全部启用渠道而不是只发邮件：配置页刚改完，使用者要验证的是
        "这套配置整体能不能送到我手上"。只验邮件会让"推送配错了"漏到生日当天。
        """
        summary = repo.get_notification_summary()
        types = summary.get("types") or []

        if not types:
            return RedirectResponse(
                "/settings?error=还没启用任何通知渠道，请在下方勾选至少一个", status_code=303
            )

        # 重新加载配置：设置可能刚被改动过
        try:
            config = app.state.config_manager.reload()
        except Exception as exc:
            logger.exception("重新加载配置失败")
            return RedirectResponse(f"/settings?error=重新加载配置失败：{exc}", status_code=303)

        target = summary.get("resend_receive_email") or summary.get("default_receive_email")
        # 收件邮箱只对邮件渠道有意义；只开推送时它可以是空的。
        probe = Recipient(
            name="测试提醒",
            email=str(target) if target else None,
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
            ).create_senders(config)
        except Exception as exc:
            logger.exception("创建发送器失败")
            return RedirectResponse(f"/settings?error=创建发送器失败：{exc}", status_code=303)

        if not senders:
            return RedirectResponse(
                "/settings?error=没有可用的发送渠道，请检查已启用渠道的配置是否完整",
                status_code=303,
            )

        results = await send_reminder_now(probe, extra, senders)
        ok = [r for r in results if r["ok"]]
        bad = [r for r in results if not r["ok"]]

        if not ok:
            detail = "；".join(f"{r['channel']}：{r['detail']}" for r in bad)
            return RedirectResponse(f"/settings?error=测试发送失败：{detail}", status_code=303)
        if bad:
            detail = "；".join(f"{r['channel']}：{r['detail']}" for r in bad)
            return RedirectResponse(
                f"/settings?notice=已通过 {len(ok)} 个渠道发出测试提醒；{detail}", status_code=303
            )

        return RedirectResponse(
            f"/settings?notice=已通过 {len(ok)} 个渠道发出测试提醒，请查收（邮件可能进垃圾箱）",
            status_code=303,
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
            # 只有配置里**显式写过** reminder_days 才回填；没写就是"沿用全局默认"，
            # 表单留空比回填一个具体数字更贴近事实。
            "reminder_days": raw.get("reminder_days", ""),
            # 旧配置没有这个字段，语义上就是"只发给我自己"
            "audience": raw.get("audience") or "self",
            "note": raw.get("note") or "",
        }
        return render_form(request, values, {}, "edit", index=index)

    @app.get("/api/lunar")
    async def api_lunar(solar: str = "") -> Dict[str, Any]:
        """阳历 → 农历查询，供表单实时校验与预览。

        接受 8 位数字（``19900120``，界面上的写法）或 ``1990-01-20``，
        统一归一化后再算 —— 前端可以原样把输入框内容发过来。

        **必须校验**：lunar_python 会把 ``1990-01-32`` 静默归一化成 1990-02-01，
        于是非法输入也能算出一个看似正常的农历（正月初六）。这里先用
        ``parse_iso_date`` 拒绝，避免把假数据当成结果显示给使用者。
        """
        raw = (solar or "").strip()
        if not raw:
            return {"ok": False, "display": None, "hint": ""}

        stored_input = normalize_solar_input(raw)
        parsed = parse_iso_date(stored_input)
        if parsed is None:
            return {
                "ok": False,
                "display": None,
                "hint": INVALID_SOLAR_HINT,
            }

        stored = solar_to_lunar(stored_input)
        if not stored:
            return {"ok": False, "display": None, "hint": "无法换算农历，请检查日期"}
        if stored == stored_input:
            # 极端情况下若换算原地返回，说明输入被上游归一化过。
            return {"ok": False, "display": None, "hint": INVALID_SOLAR_HINT}

        nxt = next_solar_for_lunar(stored)
        # 阳历生日（身份证月/日）的下一次 —— 与上面是**两个不同的日期**。
        nxt_solar = next_solar_occurrence(parsed)
        return {
            "ok": True,
            "display": lunar_display(stored),
            "stored": stored,
            # 归一化后的存储格式，前端可用来对齐（8 位数字 → 1990-01-20）
            "solar": stored_input,
            # 农历生日对应的阳历日期
            "next_solar": nxt.isoformat() if nxt else None,
            # 阳历生日的下一次
            "next_solar_birthday": nxt_solar.isoformat() if nxt_solar else None,
            "hint": "",
        }

    @app.post("/recipients/audience")
    async def set_audience_bulk(request: Request) -> RedirectResponse:
        """批量把勾选的记录改成某个受众。

        **这是有后果的操作**：把记录标成"团体"意味着下次它过生日时会广播给
        推送应用里的所有人，而消息发出去收不回来。所以：
        - 表单里的索引要重新查一遍，越界的跳过（可能有人并行删了条目）
        - 返回时如实说改了几条，不说"成功"这种含糊话
        """
        form = await request.form()
        raw_indices = form.getlist("indices")
        audience = (form.get("audience") or "").strip()

        if audience not in AUDIENCES:
            return RedirectResponse("/?error=请选择要设成哪个受众", status_code=303)
        if not raw_indices:
            return RedirectResponse("/?error=没有勾选任何记录", status_code=303)

        try:
            indices = [int(i) for i in raw_indices]
        except (TypeError, ValueError):
            return RedirectResponse("/?error=勾选的数据不对，请重试", status_code=303)

        try:
            changed = repo.set_audience_many(indices, audience)
        except Exception as exc:
            logger.exception("批量修改受众失败")
            return RedirectResponse(f"/?error=保存失败：{exc}", status_code=303)

        if changed == 0:
            return RedirectResponse(
                f"/?notice=勾选的 {len(indices)} 条已经是这个设置，无需改动", status_code=303
            )

        label = AUDIENCE_LABELS.get(audience, audience)
        skipped = len(indices) - changed
        note = f"已把 {changed} 条改为「{label}」"
        if skipped:
            note += f"（{skipped} 条无需改动或已不存在）"
        if audience == "group":
            note += "。注意：它们下次过生日会广播给所有人"
        return RedirectResponse(f"/?notice={note}", status_code=303)

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

        # 实时取配置：使用者可能刚在设置页改过渠道或凭据，
        # 用启动时的快照会让「界面显示的」和「实际发送的」不一致。
        config = app.state.current_config()

        try:
            recipient = build_sendable_recipient(raw, resolve_receive_email(config))
        except (ValueError, TypeError) as exc:
            return RedirectResponse(
                f"/?error={name} 的配置不完整，无法发送：{exc}", status_code=303
            )

        extra = render_reminder_for(recipient, datetime.now().date())
        if extra is None:
            return RedirectResponse(f"/?error={name} 的生日日期无法计算", status_code=303)

        try:
            senders = NotificationFactory(app.state.config_manager.get_templates_dir()).create_senders(
                config
            )
        except Exception as exc:
            logger.exception("创建发送器失败")
            return RedirectResponse(f"/?error=创建发送器失败：{exc}", status_code=303)

        if not senders:
            return RedirectResponse(
                "/?error=没有可用的发送渠道，请到设置页检查已启用渠道的凭据是否填好",
                status_code=303,
            )

        # 邮箱只对邮件类渠道是必需的：只开手机推送时它可以是空的。
        # 这里按**实际要用的发送器**判断，而不是无条件要求邮箱 ——
        # 否则推送渠道永远走不到发送那一步就被拦下了。
        needs_email = any(
            type(s).__name__ in ("ResendSender", "EmailSender") for s in senders
        )
        if needs_email and not recipient.email:
            return RedirectResponse(
                "/?error=邮件渠道需要收件邮箱。请到设置页填写接收提醒的邮箱",
                status_code=303,
            )

        results = await send_reminder_now(recipient, extra, senders)

        sent = [r["channel"] for r in results if r["ok"]]
        failed = [r for r in results if not r["ok"]]

        if failed and not sent:
            detail = "；".join(f"{r['channel']}: {r['detail']}" for r in failed)
            return RedirectResponse(f"/?error=发送失败：{detail}", status_code=303)

        # 送达描述按渠道说清楚：推送没有"收件地址"这个概念。
        # 邮箱取**当前配置**里的接收邮箱，而不是收件人条目上那个可能过期的值 ——
        # 提醒本来就是发给使用者自己的，条目上的 email 只是历史遗留字段。
        current_email = resolve_receive_email(config)
        where_parts = []
        if any(type(s).__name__ in ("ResendSender", "EmailSender") for s in senders):
            where_parts.append(str(current_email or recipient.email or "邮箱未填"))
        if any(type(s).__name__ == "WxPusherSender" for s in senders):
            where_parts.append("手机")
        where = " 和 ".join(where_parts) if where_parts else "已启用的渠道"

        if failed:
            detail = "；".join(f"{r['channel']}: {r['detail']}" for r in failed)
            return RedirectResponse(
                f"/?notice=已通过 {', '.join(sent)} 把 {name} 的提醒发到 {where}；{detail}",
                status_code=303,
            )

        channels = "、".join(_channel_label(c) for c in sent)
        return RedirectResponse(
            f"/?notice=已通过{channels}把 {name} 的提醒发到 {where}",
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
