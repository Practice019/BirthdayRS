"""Resend 发送器测试。

全部用假的 httpx 客户端，绝不发起真实网络请求。
真实端到端验证由命令行手动执行（见 DESIGN.md）。
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config import Recipient, ResendConfig
from src.notification.sender_resend import ResendSender, ResendSendError

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = str(REPO_ROOT / "templates")


def make_config(**overrides) -> ResendConfig:
    base = {
        "api_key": "re_test_key",
        "from_email": "onboarding@resend.dev",
        "from_name": None,
        "default_receive_email": "me@example.com",
        "default_template_file": "birthday.html",
        "default_reminder_days": 3,
    }
    base.update(overrides)
    return ResendConfig(**base)


def make_recipient(**overrides) -> Recipient:
    base = {
        "name": "张三",
        "solar_birthday": "1990-01-20",
        "reminder_days": 3,
    }
    base.update(overrides)
    return Recipient(**base)


def fake_response(status: int = 200, payload=None, text: str = ""):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text or ('{"id":"abc-123"}' if status == 200 else "{}")
    resp.json = MagicMock(return_value=payload if payload is not None else {"id": "abc-123"})
    return resp


@pytest.mark.asyncio
async def test_send_posts_expected_payload():
    """发送请求应包含正确的收件人、发件人与 HTML 内容。"""
    sender = ResendSender(make_config(), TEMPLATES)
    content = "<p>hello</p>"

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=fake_response())
        client_cls.return_value.__aenter__.return_value = client

        await sender.send(make_recipient(), content, days_until=2, age=36)

    client.post.assert_awaited_once()
    args, kwargs = client.post.call_args
    url = args[0] if args else kwargs["url"]
    assert url == "https://api.resend.com/emails"

    payload = kwargs["json"]
    assert payload["to"] == ["me@example.com"]
    assert payload["from"] == "onboarding@resend.dev"
    assert payload["html"] == content
    assert "张三" in payload["subject"]
    assert kwargs["headers"]["Authorization"] == "Bearer re_test_key"


@pytest.mark.asyncio
async def test_send_uses_default_from_when_absent():
    sender = ResendSender(make_config(from_email=None), TEMPLATES)

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=fake_response())
        client_cls.return_value.__aenter__.return_value = client

        await sender.send(make_recipient(), "<p>x</p>", 0, 30)

    assert client.post.call_args.kwargs["json"]["from"] == "onboarding@resend.dev"


# ---------- 发件人显示名 ----------


def test_sender_field_with_display_name():
    """配置显示名后，from 应为「显示名 <地址>」，收件人不再看到 onboarding。"""
    config = make_config(from_name="生日提醒")
    assert config.sender_field == "生日提醒 <onboarding@resend.dev>"


def test_sender_field_without_display_name():
    config = make_config(from_name=None)
    assert config.sender_field == "onboarding@resend.dev"


def test_sender_field_respects_explicit_angle_brackets():
    """使用者自己写成 Name <addr> 时原样使用，不重复包裹。"""
    config = make_config(from_email="提醒 <noreply@mydomain.com>", from_name="别的名字")
    assert config.sender_field == "提醒 <noreply@mydomain.com>"


def test_sender_field_defaults_when_email_missing():
    """只给了显示名、没给地址时，显示名仍应生效（用默认地址）。"""
    config = make_config(from_email=None, from_name="生日提醒")
    assert config.sender_field == "生日提醒 <onboarding@resend.dev>"


def test_sender_field_defaults_when_all_missing():
    config = make_config(from_email=None, from_name=None)
    assert config.sender_field == "onboarding@resend.dev"


def test_sender_field_with_custom_domain():
    config = make_config(from_email="noreply@mydomain.com", from_name="生日提醒")
    assert config.sender_field == "生日提醒 <noreply@mydomain.com>"


@pytest.mark.asyncio
async def test_send_includes_display_name():
    sender = ResendSender(make_config(from_name="生日提醒"), TEMPLATES)

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=fake_response())
        client_cls.return_value.__aenter__.return_value = client

        await sender.send(make_recipient(), "<p>x</p>", 0, 30)

    assert client.post.call_args.kwargs["json"]["from"] == "生日提醒 <onboarding@resend.dev>"


@pytest.mark.asyncio
async def test_send_raises_with_actionable_message_on_403():
    """受限 key 的 403 必须给出可操作的中文说明。"""
    sender = ResendSender(make_config(), TEMPLATES)
    # 用示例地址，不写任何真实邮箱 —— 测试文件会进公开仓库
    body = (
        '{"message":"You can only send testing emails to your own email address '
        '(key-owner@example.com).","statusCode":403}'
    )

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=fake_response(403, text=body))
        client_cls.return_value.__aenter__.return_value = client

        with pytest.raises(ResendSendError) as exc:
            await sender.send(make_recipient(), "<p>x</p>", 0, 30)

    msg = str(exc.value)
    assert "测试用 key" in msg
    assert "resend.com/domains" in msg
    # Resend 在 403 正文里回显了 key 持有者的邮箱；提示文案不应把它透传给使用者
    # （原始报错的 JSON 里有这个地址，翻译后的提示只应描述约束，不回显地址）
    assert "key-owner@example.com" not in msg


@pytest.mark.asyncio
async def test_send_raises_on_401():
    sender = ResendSender(make_config(api_key="bad"), TEMPLATES)

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=fake_response(401, text='{"message":"bad key"}'))
        client_cls.return_value.__aenter__.return_value = client

        with pytest.raises(ResendSendError) as exc:
            await sender.send(make_recipient(), "<p>x</p>", 0, 30)

    assert "API Key 无效" in str(exc.value)


@pytest.mark.asyncio
async def test_send_without_target_email_fails_clearly():
    """没有接收邮箱时给出明确提示，而不是发到空地址。"""
    sender = ResendSender(make_config(default_receive_email=None), TEMPLATES)
    recipient = make_recipient(email=None)

    with pytest.raises(ResendSendError) as exc:
        await sender.send(recipient, "<p>x</p>", 0, 30)

    assert "default_receive_email" in str(exc.value)


@pytest.mark.asyncio
async def test_send_falls_back_to_recipient_email():
    sender = ResendSender(make_config(default_receive_email=None), TEMPLATES)
    recipient = make_recipient(email="someone@example.com")

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=fake_response())
        client_cls.return_value.__aenter__.return_value = client

        await sender.send(recipient, "<p>x</p>", 0, 30)

    assert client.post.call_args.kwargs["json"]["to"] == ["someone@example.com"]


def test_render_content_uses_real_template():
    sender = ResendSender(make_config(), TEMPLATES)
    html = sender.render_content(
        "李四",
        "birthday.html",
        {
            "days_until": 0,
            "age": 30,
            "solar_match": True,
            "lunar_match": False,
            "year": 2026,
            "month": 10,
            "day": 6,
            "lunar_month": "腊月",
            "lunar_day": "初五",
            "week_name": "二",
            "constellation": "摩羯",
            "gz_year": "己巳",
            "gz_month": "丙子",
            "gz_day": "甲子",
            "gz_hour": "甲子",
            "zodiac": "蛇",
            "solar_term": "",
            "lunar_festival": "",
            "solar_festival": "",
        },
    )
    assert "李四" in html
    assert "生日快乐" in html


@pytest.mark.asyncio
async def test_retry_repeats_on_failure():
    """失败应重试 3 次（复用 EmailSender 的指数退避装饰器）。"""
    sender = ResendSender(make_config(), TEMPLATES)

    with patch("httpx.AsyncClient") as client_cls, patch(
        "asyncio.sleep", new=AsyncMock()
    ):
        client = AsyncMock()
        client.post = AsyncMock(return_value=fake_response(401, text='{"message":"bad"}'))
        client_cls.return_value.__aenter__.return_value = client

        with pytest.raises(ResendSendError):
            await sender.send(make_recipient(), "<p>x</p>", 0, 30)

    assert client.post.await_count == 3, "应尝试 3 次"
