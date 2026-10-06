"""Web 界面测试。

约定：每个测试用临时目录里的 config.yml，绝不触碰仓库里的真实配置。
"""

import shutil
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.web.app import create_app

# pytest-asyncio 为 strict 模式（与仓库既有测试一致），显式标记整个模块。
pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "config.example.yml"

MINIMAL_YAML = """\
notification:
  smtp:
    host: smtp.example.com
    port: 587
    username: you@example.com
    password: secret
    default_receive_email: "default@example.com"
    default_template_file: birthday.html
    default_reminder_days: 3
  resend:
    api_key: re_TESTKEY1234567890abcdef
    from_email: onboarding@resend.dev
    from_name: 生日提醒
    default_receive_email: "default@example.com"
    default_reminder_days: 3
  start_notification: resend
recipients:
  - name: 张三          # 保留这条注释用于验证 round-trip
    email: zhangsan@example.com
    solar_birthday: 1990-01-01
    reminder_days: 3
  - name: 李四
    lunar_birthday: 1991-02-15
    reminder_days: 7
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    """写一份最小可用的 config.yml。"""
    path = tmp_path / "config.yml"
    path.write_text(MINIMAL_YAML, encoding="utf-8")
    return path


@pytest_asyncio.fixture
async def client(config_file: Path):
    app = create_app(str(config_file))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------- 列表页 ----------


async def test_index_lists_recipients(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "张三" in body
    assert "李四" in body
    assert "时间轴" in body


async def test_index_shows_notification_summary(client):
    resp = await client.get("/")
    body = resp.text
    # 配置了 resend 时，摘要应显示 resend 的接收邮箱与发件人名字
    assert "default@example.com" in body
    assert "生日提醒" in body
    assert "邮件" in body


# ---------- 新建 ----------


async def test_new_form_renders(client):
    resp = await client.get("/recipients/new")
    assert resp.status_code == 200
    assert 'id="name"' in resp.text
    # 默认提前天数来自配置
    assert 'value="3"' in resp.text


async def test_create_recipient_writes_config(client, config_file):
    resp = await client.post(
        "/recipients",
        data={
            "name": "王五",
            "email": "wangwu@example.com",
            "solar_birthday": "1988-05-20",
            "reminder_days": "5",
            "template_file": "birthday.html",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # Location 是 URL 编码的中文，解一下再断言
    assert "王五" in unquote(resp.headers["location"])

    saved = config_file.read_text(encoding="utf-8")
    assert "王五" in saved
    assert "1988-05-20" in saved
    # 农历生日由阳历自动推导：1988-05-20 的农历是 1988年四月初五
    assert "lunar_birthday: '1988-04-05'" in saved or "lunar_birthday: 1988-04-05" in saved
    # 原有注释必须保留（round-trip 的关键验证）
    assert "保留这条注释用于验证" in saved


async def test_create_requires_solar_birthday(client, config_file):
    """身份证出生年月日是必填项，农历不再接受手工输入。"""
    before = config_file.read_text(encoding="utf-8")
    resp = await client.post(
        "/recipients",
        data={"name": "无生日", "solar_birthday": "", "reminder_days": "3"},
    )
    assert resp.status_code == 400
    assert "请填写身份证出生年月日" in resp.text
    # 校验失败不应写盘
    assert config_file.read_text(encoding="utf-8") == before


async def test_create_rejects_bad_date_and_keeps_input(client):
    resp = await client.post(
        "/recipients",
        data={
            "name": "格式错",
            "solar_birthday": "1990/10/08",
            "reminder_days": "3",
            "note": "这条备注必须保留",
        },
    )
    assert resp.status_code == 400
    assert "格式应为" in resp.text
    # 用户已填的其他字段必须回填，不能丢
    assert "格式错" in resp.text
    assert "1990/10/08" in resp.text
    assert "这条备注必须保留" in resp.text


async def test_create_rejects_blank_name(client):
    resp = await client.post(
        "/recipients",
        data={"name": "   ", "solar_birthday": "1990-01-01", "reminder_days": "3"},
    )
    assert resp.status_code == 400
    assert "请填写姓名" in resp.text


async def test_create_rejects_overlong_note(client):
    """备注有长度上限，超出时给中文提示。"""
    resp = await client.post(
        "/recipients",
        data={
            "name": "备注太长",
            "solar_birthday": "1990-01-20",
            "reminder_days": "3",
            "note": "x" * 501,
        },
    )
    assert resp.status_code == 400
    assert "备注太长了" in resp.text


async def test_form_has_no_email_or_template_field(client):
    """邮箱与邮件模板不再是按人配置的项。"""
    resp = await client.get("/recipients/new")
    assert resp.status_code == 200
    assert 'name="email"' not in resp.text
    assert 'name="template_file"' not in resp.text
    assert 'name="lunar_birthday"' not in resp.text
    # 应有的四个字段
    for field in ("name", "solar_birthday", "reminder_days", "note"):
        assert f'name="{field}"' in resp.text, f"缺少字段 {field}"


async def test_create_rejects_negative_reminder_days(client):
    resp = await client.post(
        "/recipients",
        data={"name": "负数", "solar_birthday": "1990-01-01", "reminder_days": "-1"},
    )
    assert resp.status_code == 400


# ---------- 编辑 ----------


async def test_edit_form_prefilled(client):
    resp = await client.get("/recipients/0/edit")
    assert resp.status_code == 200
    assert "张三" in resp.text
    assert "1990-01-01" in resp.text


async def test_update_recipient(client, config_file):
    resp = await client.post(
        "/recipients/0",
        data={
            "name": "张三改",
            "email": "new@example.com",
            "solar_birthday": "1990-01-01",
            "reminder_days": "10",
            "template_file": "birthday.html",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    saved = config_file.read_text(encoding="utf-8")
    assert "张三改" in saved
    assert "reminder_days: 10" in saved
    # 编辑不应新增或丢失条目
    assert saved.count("solar_birthday") == 1
    assert "李四" in saved
    # 农历按新的阳历生日重算：1990-01-01 的农历是 1989年腊月初五
    assert "1989-12-05" in saved


async def test_update_invalid_keeps_input(client):
    resp = await client.post(
        "/recipients/0",
        data={"name": "张三", "solar_birthday": "bad", "reminder_days": "3"},
    )
    assert resp.status_code == 400
    assert "张三" in resp.text
    assert "bad" in resp.text


async def test_update_missing_index_redirects(client):
    resp = await client.post(
        "/recipients/99",
        data={"name": "x", "solar_birthday": "1990-01-01", "reminder_days": "3"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


# ---------- 删除 ----------


async def test_delete_recipient(client, config_file):
    resp = await client.post("/recipients/0/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert "张三" in unquote(resp.headers["location"])

    saved = config_file.read_text(encoding="utf-8")
    assert "张三" not in saved
    assert "李四" in saved


async def test_delete_missing_index(client):
    resp = await client.post("/recipients/99/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


# ---------- 预览 ----------


async def test_preview_renders_email_and_serverchan(client):
    resp = await client.get("/recipients/0/preview")
    assert resp.status_code == 200
    body = resp.text
    assert "提醒预览" in body
    # 邮件模板里的实际内容应被渲染出来
    assert "生日提醒" in body
    # Server酱纯文本分支
    assert "亲爱的张三" in body


async def test_preview_does_not_send_anything(client):
    """预览不能触发任何网络发送 —— 只渲染。"""
    resp = await client.get("/recipients/0/preview")
    assert "预览不会真的发出通知" in resp.text


# ---------- 备注字段 ----------


async def test_note_is_saved_and_shown(client, config_file):
    resp = await client.post(
        "/recipients",
        data={
            "name": "带备注",
            "solar_birthday": "1993-06-15",
            "reminder_days": "5",
            "note": "大学同学，喜欢喝茶",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    saved = config_file.read_text(encoding="utf-8")
    assert "大学同学，喜欢喝茶" in saved

    listing = await client.get("/")
    assert "大学同学，喜欢喝茶" in listing.text


async def test_note_is_optional(client, config_file):
    """不填备注也要能保存 —— 它不该成为必填项。"""
    resp = await client.post(
        "/recipients",
        data={"name": "无备注", "solar_birthday": "1993-06-15", "reminder_days": "3"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    saved = config_file.read_text(encoding="utf-8")
    assert "无备注" in saved
    assert "note" not in saved


async def test_note_can_be_edited_and_cleared(client, config_file):
    base = {
        "name": "张三",
        "solar_birthday": "1990-01-01",
        "reminder_days": "3",
    }

    # 先写一条备注
    resp = await client.post(
        "/recipients/0", data={**base, "note": "旧备注"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "旧备注" in config_file.read_text(encoding="utf-8")

    # 再清空
    resp = await client.post(
        "/recipients/0", data={**base, "note": ""}, follow_redirects=False
    )
    assert resp.status_code == 303
    saved = config_file.read_text(encoding="utf-8")
    assert "旧备注" not in saved


# ---------- 编辑不得弄丢表单外的字段 ----------


async def test_edit_preserves_email_and_template(client, config_file):
    """表单不再暴露 email / template_file，编辑时必须保留配置里已有的值。

    早期实现整体替换会把这些字段静默删掉。
    """
    before = config_file.read_text(encoding="utf-8")
    assert "zhangsan@example.com" in before

    resp = await client.post(
        "/recipients/0",
        data={
            "name": "张三改名",
            "solar_birthday": "1990-01-01",
            "reminder_days": "3",
            "note": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    saved = config_file.read_text(encoding="utf-8")
    assert "张三改名" in saved
    assert "zhangsan@example.com" in saved, "email 不应被编辑操作删掉"


async def test_edit_preserves_note_of_other_recipients(client, config_file):
    """编辑一个人不能影响另一个人。"""
    await client.post(
        "/recipients", data={"name": "甲", "solar_birthday": "1990-02-02", "note": "甲的备注"}
    )
    resp = await client.post(
        "/recipients/0",
        data={"name": "张三", "solar_birthday": "1990-01-01", "reminder_days": "3"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "甲的备注" in config_file.read_text(encoding="utf-8")


# ---------- 设置页 ----------


async def test_settings_page_renders(client):
    resp = await client.get("/settings")
    assert resp.status_code == 200
    body = resp.text
    assert "设置" in body
    # 要说清提醒是发给使用者自己的
    assert "提醒是发给" in body
    # 应显示配置里的接收邮箱
    assert "default@example.com" in body
    # 应是可编辑表单
    assert 'name="api_key"' in body
    assert 'name="default_receive_email"' in body
    assert 'name="from_name"' in body
    assert 'name="default_reminder_days"' in body


async def test_list_shows_resend_channel_not_raw_type(client):
    """回归测试：配置了 resend 时，渠道应显示"邮件"，不能是原文 resend。

    也不能因为没配 SMTP 就误报"邮件未配置"。
    """
    resp = await client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "resend" not in body, "渠道名不应把内部标识暴露给使用者"
    assert "邮件未配置" not in body


async def test_settings_never_leaks_full_api_key(client, config_file):
    """设置页绝不能把完整密钥渲染到 HTML 里。"""
    # 先写入一个密钥
    # 用**假**密钥当测试数据。绝不要把真实密钥写进测试文件 ——
    # 测试文件会进版本库，密钥就随之泄露。
    fake_key = "re_FAKEKEYFORMASKING1234567890"
    await client.post(
        "/settings",
        data={
            "api_key": fake_key,
            "default_receive_email": "me@example.com",
            "from_name": "生日提醒",
            "from_email": "",
            "default_reminder_days": "3",
        },
        follow_redirects=False,
    )

    resp = await client.get("/settings")
    assert resp.status_code == 200
    assert fake_key not in resp.text, "完整密钥不得出现在页面里"
    # 但应显示打码值，让使用者能认出是哪一个
    assert "re_FAK..." in resp.text


async def test_settings_save_writes_config(client, config_file):
    resp = await client.post(
        "/settings",
        data={
            "api_key": "re_NEWKEY1234567890abcdefgh",
            "default_receive_email": "new@qq.com",
            "from_name": "我的提醒",
            "from_email": "",
            "default_reminder_days": "5",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "notice=" in unquote(resp.headers["location"])

    saved = config_file.read_text(encoding="utf-8")
    assert "re_NEWKEY1234567890abcdefgh" in saved
    assert "new@qq.com" in saved
    assert "我的提醒" in saved
    assert "default_reminder_days: 5" in saved


async def test_settings_blank_key_keeps_existing(client, config_file):
    """密钥字段留空表示"不修改"，不能被清空。"""
    # 先存一个密钥
    await client.post(
        "/settings",
        data={
            "api_key": "re_KEEPME1234567890abcdefgh",
            "default_receive_email": "keep@qq.com",
            "default_reminder_days": "3",
        },
        follow_redirects=False,
    )
    assert "re_KEEPME1234567890abcdefgh" in config_file.read_text(encoding="utf-8")

    # 只改接收邮箱，密钥留空
    resp = await client.post(
        "/settings",
        data={
            "api_key": "",
            "default_receive_email": "changed@qq.com",
            "default_reminder_days": "3",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    saved = config_file.read_text(encoding="utf-8")
    assert "re_KEEPME1234567890abcdefgh" in saved, "留空不应清空密钥"
    assert "changed@qq.com" in saved


async def test_settings_can_clear_key_explicitly(client, config_file):
    await client.post(
        "/settings",
        data={
            "api_key": "re_CLEARME1234567890abcdefgh",
            "default_receive_email": "x@qq.com",
            "default_reminder_days": "3",
        },
        follow_redirects=False,
    )

    resp = await client.post(
        "/settings",
        data={
            "api_key": "",
            "clear_api_key": "1",
            "default_receive_email": "x@qq.com",
            "default_reminder_days": "3",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    saved = config_file.read_text(encoding="utf-8")
    assert "re_CLEARME1234567890abcdefgh" not in saved


async def test_settings_validates_email(client):
    resp = await client.post(
        "/settings",
        data={
            "api_key": "",
            "default_receive_email": "not-an-email",
            "default_reminder_days": "3",
        },
    )
    assert resp.status_code == 400
    assert "邮箱地址看起来不对" in resp.text


async def test_settings_validates_key_prefix(client):
    resp = await client.post(
        "/settings",
        data={
            "api_key": "wrongprefix1234567890",
            "default_receive_email": "a@qq.com",
            "default_reminder_days": "3",
        },
    )
    assert resp.status_code == 400
    assert "re_ 开头" in resp.text


async def test_settings_validates_from_name_no_brackets(client):
    """显示名里带尖括号会破坏 "Name <addr>" 解析，必须拒绝。"""
    resp = await client.post(
        "/settings",
        data={
            "api_key": "",
            "default_receive_email": "a@qq.com",
            "from_name": "坏<名字>",
            "default_reminder_days": "3",
        },
    )
    assert resp.status_code == 400
    assert "不能包含" in resp.text


async def test_settings_preserves_comments(client, config_file):
    """设置写入也要保留 config.yml 里的注释。"""
    await client.post(
        "/settings",
        data={
            "api_key": "re_X1234567890abcdefghijkl",
            "default_receive_email": "c@qq.com",
            "default_reminder_days": "4",
        },
        follow_redirects=False,
    )
    saved = config_file.read_text(encoding="utf-8")
    assert "保留这条注释用于验证" in saved


async def test_settings_test_send_requires_key(client, config_file):
    """没配密钥时点测试发送要给明确提示，而不是报错。"""
    resp = await client.post("/settings/test", follow_redirects=False)
    assert resp.status_code == 303
    location = unquote(resp.headers["location"])
    assert "error=" in location
    assert "API Key" in location


async def test_settings_nav_link_present(client):
    resp = await client.get("/")
    assert 'href="/settings"' in resp.text


# ---------- 测试发送 ----------


async def test_test_send_button_present(client):
    resp = await client.get("/")
    assert "/test-send" in resp.text
    assert "测试发送" in resp.text


async def test_test_send_actually_sends(client, config_file):
    """测试发送必须真的调用发送器 —— 否则按钮毫无意义。

    用假的发送器替换工厂产物，避免真实网络请求。
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    sent = []

    class FakeSender:
        def render_content(self, name, template_file, extra_info):
            return f"<p>{name}</p>"

        async def send(self, recipient, content, days_until, age):
            sent.append(
                {
                    "name": recipient.name,
                    "email": recipient.email,
                    "days_until": days_until,
                    "age": age,
                }
            )

    with patch(
        "src.web.app.NotificationFactory.create_senders",
        return_value=[FakeSender()],
    ):
        resp = await client.post("/recipients/0/test-send", follow_redirects=False)

    assert resp.status_code == 303
    location = unquote(resp.headers["location"])
    assert "notice" in location
    assert "张三" in location

    assert len(sent) == 1, "应真的调用了 sender.send"
    assert sent[0]["name"] == "张三"
    # 张三记录里带 email（旧格式），应优先用它
    assert sent[0]["email"] == "zhangsan@example.com"
    # 张三生日还有一百多天，days_until 应是真实值而非 0
    assert sent[0]["days_until"] > 0
    assert sent[0]["age"] > 0


async def test_test_send_reports_channel_failure(client):
    """渠道失败时要如实报错，不能假装成功。"""
    from unittest.mock import patch

    class FailingSender:
        def render_content(self, name, template_file, extra_info):
            return "<p>x</p>"

        async def send(self, recipient, content, days_until, age):
            raise RuntimeError("模拟的发送失败")

    with patch(
        "src.web.app.NotificationFactory.create_senders",
        return_value=[FailingSender()],
    ):
        resp = await client.post("/recipients/0/test-send", follow_redirects=False)

    assert resp.status_code == 303
    location = unquote(resp.headers["location"])
    assert "error=" in location
    assert "模拟的发送失败" in location


async def test_test_send_missing_index(client):
    resp = await client.post("/recipients/99/test-send", follow_redirects=False)
    assert resp.status_code == 303
    assert "error=" in unquote(resp.headers["location"])


async def test_test_send_without_receive_email(client, tmp_path):
    """没有可用收件邮箱时给明确提示，而不是发到空地址。"""
    from unittest.mock import patch

    path = tmp_path / "no_mail.yml"
    path.write_text(
        "notification:\n"
        "  resend:\n"
        "    api_key: re_x\n"
        "    default_reminder_days: 3\n"
        "  start_notification: resend\n"
        "recipients:\n"
        "  - name: 无邮箱\n"
        "    solar_birthday: 1990-01-20\n",
        encoding="utf-8",
    )

    app = create_app(str(path))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        with patch("src.web.app.NotificationFactory.create_senders", return_value=[]):
            resp = await ac.post("/recipients/0/test-send", follow_redirects=False)

    assert resp.status_code == 303
    assert "error=" in unquote(resp.headers["location"])


def test_resolve_receive_email_prefers_resend():
    """按生效顺序找接收邮箱：resend 优先，其次 smtp。"""
    from src.core.config import ResendConfig, SMTPConfig
    from src.web.domain import resolve_receive_email

    class Cfg:
        resend_config = ResendConfig(api_key="k", default_receive_email="r@example.com")
        smtp_config = SMTPConfig(
            host="h", port=1, username="u", password="p", default_receive_email="s@example.com"
        )

    assert resolve_receive_email(Cfg()) == "r@example.com"

    class Cfg2:
        resend_config = None
        smtp_config = SMTPConfig(
            host="h", port=1, username="u", password="p", default_receive_email="s@example.com"
        )

    assert resolve_receive_email(Cfg2()) == "s@example.com"

    class Cfg3:
        resend_config = None
        smtp_config = None

    assert resolve_receive_email(Cfg3()) is None


def test_build_sendable_recipient_falls_back_to_config_email():
    """收件人记录里没有邮箱时，用配置里的接收邮箱顶上。"""
    from src.web.domain import build_sendable_recipient

    raw = {"name": "张三", "solar_birthday": "1990-01-20", "reminder_days": 3}
    recipient = build_sendable_recipient(raw, "fallback@example.com")
    assert recipient.email == "fallback@example.com"
    # 农历应已由阳历推导出来
    assert recipient.lunar_birthday == "1989-12-24"


# ---------- 领域计算 ----------


def test_weekday_is_birthday_not_today():
    """回归测试：星期必须取自生日当天，而不是今天。

    BirthdayChecker 返回的 week_name 描述的是**今天**；早期实现直接把它显示在
    "下一次生日"旁边，导致日期与星期对不上。
    """
    from datetime import datetime

    from src.web.domain import build_timeline

    raw = [{"name": "测试", "solar_birthday": "1990-10-08", "reminder_days": 3}]
    # 2026-10-06 是星期二，2026-10-08 是星期四
    views = build_timeline(raw, 3, today=datetime(2026, 10, 6))
    view = views[0]

    assert view.next_birthday is not None
    assert view.next_birthday.isoformat() == "2026-10-08"
    assert view.days_until == 2
    assert view.week_name == "四", "应显示生日当天的星期"


def test_timeline_sorted_by_days_until():
    from datetime import datetime

    from src.web.domain import build_timeline

    today = datetime(2026, 10, 6)
    raw = [
        {"name": "远", "solar_birthday": "1990-12-01", "reminder_days": 3},
        {"name": "近", "solar_birthday": "1990-10-07", "reminder_days": 3},
    ]
    views = build_timeline(raw, 3, today=today)
    assert [v.name for v in views] == ["近", "远"]


def test_will_trigger_respects_reminder_window():
    from datetime import datetime

    from src.web.domain import build_timeline

    today = datetime(2026, 10, 6)
    # 生日在 5 天后：窗口 3 天不触发，窗口 7 天触发
    raw = [{"name": "x", "solar_birthday": "1990-10-11"}]
    assert build_timeline(raw, 3, today=today)[0].will_trigger is False
    assert build_timeline(raw, 7, today=today)[0].will_trigger is True


def test_invalid_recipient_survives_timeline():
    """缺生日的配置条目不能让整页崩掉。"""
    from datetime import datetime

    from src.web.domain import build_timeline

    raw = [{"name": "缺生日"}]
    views = build_timeline(raw, 3, today=datetime(2026, 10, 6))
    assert len(views) == 1
    assert views[0].days_until is None
    assert views[0].status == "unknown"


async def test_preview_hides_zero_age(client, tmp_path):
    """生日快乐当年时 age 为 0，界面不应显示"满 0 岁"。"""
    year = datetime.now().year
    path = tmp_path / "zero_age.yml"
    target = (datetime.now() + timedelta(days=2)).strftime("%m-%d")
    path.write_text(
        "notification:\n"
        "  smtp:\n"
        "    host: h\n    port: 587\n    username: u\n    password: p\n"
        "    default_reminder_days: 3\n"
        "  start_notification: email\n"
        "recipients:\n"
        f"  - name: 当年出生\n    solar_birthday: {year}-{target}\n    reminder_days: 3\n",
        encoding="utf-8",
    )

    from src.web.app import create_app

    app = create_app(str(path))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/recipients/0/preview")
    assert resp.status_code == 200
    assert "满 0 岁" not in resp.text


def test_datetime_now_matches_checker():
    """确保字段计算与 checker 的口径一致（days_until 是未来天数偏移）。"""
    from datetime import datetime

    from src.web.domain import build_timeline

    today = datetime(2026, 10, 6)
    views = build_timeline(
        [{"name": "当天", "solar_birthday": "1990-10-06", "reminder_days": 0}], 0, today=today
    )
    assert views[0].days_until == 0
    assert views[0].status == "today"
    assert views[0].week_name == "二"


def test_next_birthday_is_earliest_of_both_calendars():
    """回归测试：「下一次生日」必须取阳历与农历中更近的那次。

    BirthdayChecker 的农历分支会覆盖阳历分支的 days_until，直接采信它的返回值
    会得到一个比真实下一次更晚的日期。
    """
    from datetime import datetime

    from src.web.domain import build_timeline

    today = datetime(2026, 10, 6)
    raw = [
        {
            "name": "双生日",
            "solar_birthday": "1990-01-01",
            # 阳历 1990-01-01 对应的农历，由界面自动推导后写入配置
            "lunar_birthday": "1989-12-05",
            "reminder_days": 3,
        }
    ]
    views = build_timeline(raw, 3, today=today)
    view = views[0]

    solar_days = (datetime(2027, 1, 1) - today).days  # 87
    assert view.days_until == solar_days, (
        f"应取更近的阳历生日 {solar_days} 天，实际 {view.days_until} 天"
    )
    assert view.next_birthday.isoformat() == "2027-01-01"


def test_preview_agrees_with_timeline():
    """预览页与时间轴必须给出同一个「下一次生日」，否则两处会互相矛盾。"""
    from datetime import datetime

    from src.web.domain import build_timeline, render_preview

    today = datetime(2026, 10, 6)
    raw = {
        "name": "双生日",
        "solar_birthday": "1990-01-01",
        "lunar_birthday": "1989-12-05",
        "reminder_days": 3,
    }
    timeline_days = build_timeline([raw], 3, today=today)[0].days_until
    preview = render_preview(raw, str(REPO_ROOT / "templates"), today=today)

    assert preview["ok"] is True
    assert preview["days_until"] == timeline_days


# ---------- 农历自动推导 ----------


def test_lunar_conversion_matches_checker_semantics():
    """推导出的农历必须能被 checker 的农历分支匹配回去。

    checker 把 lunar_birthday 的月/日当【农历月日】直接比较，所以存储值必须是
    农历的年月日，而不是"对应的阳历日期"（config.example.yml 的注释是错的）。
    """
    from lunar_python import Solar

    from src.web.lunar import solar_to_lunar

    solar_str = "1990-01-20"
    stored = solar_to_lunar(solar_str)
    assert stored == "1989-12-24"

    year, month, day = map(int, stored.split("-"))
    # 用该农历月日回查阳历，应能找到对应的一天
    found = False
    for offset in range(0, 400):
        import datetime as _dt

        probe = _dt.date(1991, 1, 1) + _dt.timedelta(days=offset)
        lunar = Solar.fromYmd(probe.year, probe.month, probe.day).getLunar()
        if lunar.getMonth() == month and lunar.getDay() == day:
            found = True
            break
    assert found, "推导出的农历应能在次年找到匹配日"


def test_lunar_display_is_chinese():
    from src.web.lunar import lunar_display

    # 1990-01-20 -> 农历 1989年腊月廿四
    assert lunar_display("1989-12-24") == "腊月廿四"


def test_leap_month_stored_as_normal_month():
    """闰月出生者存为普通月，保证每年都能提醒（见 lunar.py 的说明）。"""
    from src.web.lunar import solar_to_lunar

    # 2020-06-01 是农历闰四月初十
    stored = solar_to_lunar("2020-06-01")
    assert stored == "2020-04-10", "闰四月应存为 04，不带负号"
    # 单独检查月份字段：月份不能是负数
    month_part = stored.split("-")[1]
    assert int(month_part) > 0, f"月份应为正数，实际 {month_part}"


def test_timeline_uses_derived_lunar_over_stale_config():
    """配置里手填的农历值有误时，以阳历推导值为准，并标记不一致。"""
    from datetime import datetime

    from src.web.domain import build_timeline

    raw = [
        {
            "name": "历史错误值",
            "solar_birthday": "1990-01-20",
            # 早期手工填写，与阳历不符（1990-01-20 的农历是 1989-12-24）
            "lunar_birthday": "1989-12-05",
            "reminder_days": 3,
        }
    ]
    view = build_timeline(raw, 3, today=datetime(2026, 10, 6))[0]

    assert view.lunar_from_solar == "1989-12-24"
    assert view.lunar_mismatch is True
    # 匹配使用的是推导值，因此结果应与纯阳历一致
    assert view.days_until is not None


async def test_api_lunar_returns_display(client):
    resp = await client.get("/api/lunar", params={"solar": "1990-01-20"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["display"] == "腊月廿四"
    assert data["stored"] == "1989-12-24"
    assert data["next_solar"] == "2027-01-31"


async def test_api_lunar_rejects_invalid_calendar_date(client):
    """非法日历日期必须被拒绝，不得归一化后算出农历。"""
    for bad in ["1990-01-32", "1990-02-30", "1990-13-01"]:
        resp = await client.get("/api/lunar", params={"solar": bad})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False, f"{bad} 不应返回农历"
        assert data["display"] is None
        assert "不合理" in data["hint"]


async def test_api_lunar_handles_bad_input(client):
    resp = await client.get("/api/lunar", params={"solar": "not-a-date"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False

    resp = await client.get("/api/lunar", params={"solar": ""})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


async def test_form_has_no_lunar_input(client):
    """表单不应再暴露农历输入框 —— 它由阳历自动推导。"""
    resp = await client.get("/recipients/new")
    assert resp.status_code == 200
    assert 'name="lunar_birthday"' not in resp.text
    assert 'id="lunar_birthday"' not in resp.text
    # 字段标签应叫"身份证出生年月日"
    assert "身份证出生年月日" in resp.text
    # 旧的解释性小字已按需求移除
    assert "身份证上的出生日期，例如" not in resp.text
    # 应有农历预览占位
    assert "农历生日" in resp.text


async def test_form_shows_derived_lunar_on_edit(client):
    """编辑页应显示由阳历推导的农历，以及对应的阳历生日。"""
    resp = await client.get("/recipients/0/edit")
    assert resp.status_code == 200
    # 张三 solar=1990-01-01 -> 农历 1989年腊月初五
    assert "腊月初五" in resp.text
    # 新文案：用"下次"
    assert "下次农历生日" in resp.text
    assert "下次阳历生日" in resp.text


async def test_api_lunar_returns_next_solar(client):
    resp = await client.get("/api/lunar", params={"solar": "1990-01-20"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    # 腊月廿四的下一次落在次年 1 月
    assert data["next_solar"] is not None


async def test_lunar_only_recipient_is_flagged(client):
    """只有农历、没有阳历的历史条目必须在界面标出，不能冒充阳历日期。"""
    resp = await client.get("/")
    assert resp.status_code == 200
    body = resp.text
    # 李四在示例配置里只有 lunar_birthday
    assert "只有农历生日" in body

    from datetime import datetime

    from src.web.domain import build_timeline

    raw = [{"name": "旧条目", "lunar_birthday": "1991-02-15", "reminder_days": 7}]
    view = build_timeline(raw, 7, today=datetime(2026, 10, 6))[0]
    assert view.is_lunar_only is True
    # 经典条目仍能算出天数（checker 支持纯农历）
    assert view.days_until is not None


def test_solar_recipient_is_not_flagged_as_lunar_only():
    from datetime import datetime

    from src.web.domain import build_timeline

    raw = [{"name": "正常", "solar_birthday": "1990-01-20", "reminder_days": 3}]
    view = build_timeline(raw, 3, today=datetime(2026, 10, 6))[0]
    assert view.is_lunar_only is False


# ---------- 下一次生日：取更近的那个，并标注是哪种日历 ----------


def test_next_birthday_picks_the_nearer_calendar():
    """阳历与农历混在一起排序，取最近的那一个。"""
    from datetime import datetime

    from src.web.domain import build_timeline

    today = datetime(2026, 10, 6)

    # 张三：阳历 2027-01-20（106 天）比农历 2027-01-31（117 天）近 → 取阳历
    zhang = build_timeline(
        [{"name": "张三", "solar_birthday": "1990-01-20", "lunar_birthday": "1989-12-24"}],
        3,
        today=today,
    )[0]
    assert zhang.next_birthday.isoformat() == "2027-01-20"
    assert zhang.next_is_lunar is False
    assert zhang.next_kind_text == "阳历"

    # 李四：农历 2027-03-22（167 天）比阳历 2027-03-30（175 天）近 → 取农历
    li = build_timeline(
        [{"name": "李四", "solar_birthday": "1991-03-30", "lunar_birthday": "1991-02-15"}],
        3,
        today=today,
    )[0]
    assert li.next_birthday.isoformat() == "2027-03-22"
    assert li.next_is_lunar is True
    assert li.next_kind_text == "农历"


def test_next_kind_is_none_when_unresolvable():
    from datetime import datetime

    from src.web.domain import build_timeline

    view = build_timeline([{"name": "坏"}], 3, today=datetime(2026, 10, 6))[0]
    assert view.next_is_lunar is None
    assert view.next_kind_text == ""


def test_both_calendars_trigger_reminders():
    """回归测试：阳历与农历两个生日都必须能触发提醒，不能只认一个。

    checker 的农历分支会覆盖阳历分支的 days_until，因此它内部把两种日历分开判断；
    这里验证在各自生日当天都能命中。
    """
    from datetime import datetime

    from src.core.checker import BirthdayChecker
    from src.core.config import Recipient

    recipient = Recipient(
        name="张三",
        solar_birthday="1990-01-20",
        lunar_birthday="1989-12-24",  # 农历腊月廿四
        reminder_days=3,
    )
    checker = BirthdayChecker()

    # 阳历生日当天
    _, solar_extra = checker._check_birthday(recipient, datetime(2027, 1, 20))
    assert solar_extra["solar_match"] is True

    # 农历生日当天（2027-01-31 是农历腊月廿四）
    _, lunar_extra = checker._check_birthday(recipient, datetime(2027, 1, 31))
    assert lunar_extra["lunar_match"] is True

    # 农历生日前 3 天也应命中（提前提醒）
    _, early = checker._check_birthday(recipient, datetime(2027, 1, 28))
    assert early["lunar_match"] is True
    assert early["days_until"] == 3


# ---------- 非法阳历日期不得推导农历 ----------


def test_invalid_solar_date_produces_no_lunar():
    """回归测试：阳历不合理时不得算出农历。

    lunar_python 会把 ``Solar.fromYmd(1990, 1, 32)`` 静默归一化成 1990-02-01，
    于是"1月32日"能算出"正月初六"这种假数据 —— 比报错更危险。
    """
    from datetime import datetime

    from src.web.domain import build_timeline

    raw = [{"name": "错日期", "solar_birthday": "1990-01-32", "reminder_days": 3}]
    view = build_timeline(raw, 3, today=datetime(2026, 10, 6))[0]

    assert view.solar_invalid is True
    assert view.solar_valid is False
    assert view.lunar_text is None, "非法阳历不得推导农历"
    assert view.lunar_from_solar is None
    assert view.lunar_next_solar is None
    assert view.days_until is None, "非法阳历不得显示倒计时"
    assert view.status_text == "身份证出生年月日不合理"


def test_lunar_conversion_rejects_invalid_dates():
    from src.web.lunar import solar_to_lunar, validate_solar_date

    for bad in ["1990-01-32", "1990-02-30", "1990-13-01", "1990-00-10", "1990-1-1", "abc"]:
        assert solar_to_lunar(bad) is None, f"{bad} 不应算出农历"
        ok, _ = validate_solar_date(bad)
        assert ok is False, f"{bad} 应被判为无效"

    # 合法日期仍正常
    assert solar_to_lunar("1990-01-20") == "1989-12-24"
    ok, msg = validate_solar_date("1990-01-20")
    assert ok is True


def test_validate_solar_date_messages():
    from src.web.lunar import validate_solar_date

    ok, msg = validate_solar_date("")
    assert ok is False and "请填写" in msg

    ok, msg = validate_solar_date("1990-01-32")
    assert ok is False and "不合理" in msg

    ok, msg = validate_solar_date("1990-01-20")
    assert ok is True


# ---------- 相对年份（供内部使用，界面文案用"下次"） ----------


def test_relative_year_label():
    """只给裸日期会让人误以为是今年，因此保留年份相对说法供需要时使用。"""
    import datetime as dt

    from src.web.lunar import relative_year_label

    base = dt.date(2026, 10, 6)
    assert relative_year_label(dt.date(2026, 12, 1), base) == "今年"
    assert relative_year_label(dt.date(2027, 2, 10), base) == "明年"
    assert relative_year_label(dt.date(2028, 1, 5), base) == "后年"
    assert relative_year_label(dt.date(2031, 3, 3), base) == "2031 年"


def test_next_solar_date_may_land_in_next_year():
    """回归：农历生日若今年已过，对应的阳历日期会落到明年。

    界面必须如实说"明年"，不能硬写"今年"—— 否则日期与说法自相矛盾。
    """
    import datetime as dt

    from src.web.lunar import next_solar_for_lunar, relative_year_label

    today = dt.date(2026, 10, 6)
    # 正月初五：2026 年的已在 2 月过完，下一次落在 2027 年
    nxt = next_solar_for_lunar("1989-01-05", today)
    assert nxt is not None
    assert nxt.year == 2027
    assert relative_year_label(nxt, today) == "明年"


def test_next_solar_date_stays_in_current_year_when_upcoming():
    import datetime as dt

    from src.web.lunar import next_solar_for_lunar, relative_year_label

    today = dt.date(2026, 10, 6)
    # 八月廿六：2026 年 10 月 6 日当天
    nxt = next_solar_for_lunar("1990-08-26", today)
    assert nxt == today
    assert relative_year_label(nxt, today) == "今年"


def test_lunar_year_end_maps_to_next_solar_year():
    """农历年末（腊月/正月）在阳历上会跨年，所以标签是"明年"而非"今年"。

    这正是不能硬写"今年"的原因：腊月廿四在 2026-10-06 之后的下一次落在 2027-01-31。
    """
    import datetime as dt

    from src.web.lunar import next_solar_for_lunar, relative_year_label

    today = dt.date(2026, 10, 6)
    nxt = next_solar_for_lunar("1989-12-24", today)
    assert nxt == dt.date(2027, 1, 31)
    assert relative_year_label(nxt, today) == "明年"


def test_leap_day_birthday_does_not_crash():
    """2 月 29 日出生：非闰年不能崩。

    注意结果取阳历与农历中更近的一次：1992-02-29 的农历是正月廿六，
    它的下一次（如 2027-03-03）通常早于下一个闰年（2028-02-29），
    所以 next_birthday 不一定是 2 月 29 日 —— 只要不崩且能算出日期即可。
    """
    from datetime import datetime

    from src.web.domain import build_timeline

    raw = [{"name": "闰日", "solar_birthday": "1992-02-29", "reminder_days": 3}]
    view = build_timeline(raw, 3, today=datetime(2026, 10, 6))[0]

    assert view.solar_valid is True
    assert view.next_birthday is not None
    assert view.days_until is not None and view.days_until >= 0
    # 农历推导也要正常
    assert view.lunar_text is not None


def test_leap_day_solar_only_resolves_to_next_leap_year():
    """纯阳历分支：2 月 29 日应落到下一个闰年。"""
    from datetime import date

    from src.web.domain import _next_solar_occurrence

    hit = _next_solar_occurrence(date(1992, 2, 29), date(2026, 10, 6))
    assert hit == date(2028, 2, 29)


# ---------- 两个"下一个阳历日期"必须区分 ----------


def test_lunar_next_solar_and_next_solar_birthday_are_distinct():
    """回归测试：农历对应的阳历日期 ≠ 阳历生日的下一次。

    曾经把 ``lunar_next_solar`` 当作"下次阳历生日"显示，导致身份证 1990-01-31
    的人显示"下次阳历生日 2027-02-10"（那是农历正月初五对应的日期），
    而正确的阳历生日是 2027-01-31。
    """
    from datetime import datetime

    from src.web.domain import build_timeline

    today = datetime(2026, 10, 6)
    raw = [{"name": "测试", "solar_birthday": "1990-01-31", "reminder_days": 3}]
    view = build_timeline(raw, 3, today=today)[0]

    # 农历正月初五 -> 阳历 2027-02-10
    assert view.lunar_text == "正月初五"
    assert view.lunar_next_solar.isoformat() == "2027-02-10"

    # 阳历生日 01-31 -> 2027-01-31（与上面不同！）
    assert view.next_solar_birthday.isoformat() == "2027-01-31"
    assert view.lunar_next_solar != view.next_solar_birthday


def test_next_solar_birthday_is_annual_month_day():
    """下次阳历生日只看月/日，与农历无关。"""
    from datetime import date

    from src.web.domain import next_solar_occurrence

    # 今天是 10 月，1 月 31 日已过 -> 落到明年
    assert next_solar_occurrence(
        date(1990, 1, 31), date(2026, 10, 6)
    ) == date(2027, 1, 31)
    # 12 月 25 日还没到 -> 今年
    assert next_solar_occurrence(
        date(1990, 12, 25), date(2026, 10, 6)
    ) == date(2026, 12, 25)
    # 闰日 -> 下一个闰年
    assert next_solar_occurrence(
        date(1992, 2, 29), date(2026, 10, 6)
    ) == date(2028, 2, 29)


async def test_api_lunar_returns_both_dates(client):
    resp = await client.get("/api/lunar", params={"solar": "1990-01-31"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["display"] == "正月初五"
    # 农历对应的阳历日期
    assert data["next_solar"] == "2027-02-10"
    # 阳历生日的下一次 —— 不同
    assert data["next_solar_birthday"] == "2027-01-31"


def test_recipient_view_includes_lunar_next_solar():
    from datetime import datetime

    from src.web.domain import build_timeline

    raw = [{"name": "张三", "solar_birthday": "1990-01-20", "reminder_days": 3}]
    view = build_timeline(raw, 3, today=datetime(2026, 10, 6))[0]

    assert view.lunar_text == "腊月廿四"
    assert view.lunar_next_solar is not None
    assert view.lunar_next_solar.isoformat() == "2027-01-31"


def test_next_solar_for_lunar_matches_brute_force():
    """对拍：快速反查与逐日扫描结果必须一致（含跨年边界）。"""
    import datetime as dt

    from lunar_python import Solar

    from src.web.lunar import next_solar_for_lunar

    def brute_force(month: int, day: int, start: dt.date):
        cur = start
        for _ in range(400):
            lunar = Solar.fromYmd(cur.year, cur.month, cur.day).getLunar()
            if lunar.getMonth() == month and lunar.getDay() == day:
                return cur
            cur += dt.timedelta(days=1)
        return None

    starts = [
        dt.date(2026, 10, 6),
        dt.date(2026, 1, 3),
        dt.date(2026, 12, 28),
        dt.date(2027, 2, 20),
    ]
    pairs = [(12, 24), (2, 15), (1, 1), (11, 8), (6, 1), (12, 1)]

    checked = 0
    for start in starts:
        for month, day in pairs:
            stored = f"1990-{month:02d}-{day:02d}"
            fast = next_solar_for_lunar(stored, start)
            slow = brute_force(month, day, start)
            assert fast == slow, f"农历{month:02d}-{day:02d} start={start}: 快={fast} 慢={slow}"
            checked += 1
    assert checked == len(starts) * len(pairs)


def test_build_timeline_is_fast():
    """性能回归：时间轴不得再做逐日农历扫描。

    早期实现用 checker 扫 366 天，2 个人要 1.9 秒。这里给出宽松上限，
    只要没有回退到逐日扫描就远低于它。
    """
    import time
    from datetime import datetime

    from src.web.domain import build_timeline

    raw = [
        {"name": f"人{i}", "solar_birthday": "1990-01-20", "reminder_days": 3}
        for i in range(10)
    ]
    # 首次调用包含缓存预热，单独计时
    build_timeline(raw, 3, today=datetime(2026, 10, 6))

    t0 = time.perf_counter()
    build_timeline(raw, 3, today=datetime(2026, 10, 6))
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.2, f"10 人构建耗时 {elapsed * 1000:.0f} ms，疑似回退到逐日扫描"
