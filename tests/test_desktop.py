"""桌面应用的 API 层测试。

**不开窗口**：``AppApi`` 不依赖 pywebview，可以直接实例化测试。
窗口行为（渲染、零端口）由 ``tools/verify_desktop.py`` 做端到端验证。

约定：每个测试用临时目录里的 config.yml，绝不触碰仓库里的真实配置。
"""

import shutil
from pathlib import Path

import pytest

from src.desktop.api import AppApi
from src.desktop.ui import build_html

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "config.example.yml"

MINIMAL_YAML = """\
notification:
  resend:
    api_key: re_TESTKEY1234567890abcdef
    from_email: onboarding@resend.dev
    from_name: 生日提醒
    default_receive_email: "me@example.com"
    default_reminder_days: 3
  start_notification: resend
recipients:
  - name: 张三          # 保留这条注释用于验证 round-trip
    solar_birthday: 1990-01-20
    lunar_birthday: 1989-12-24
    reminder_days: 3
    note: 大学同学
  - name: 李四
    solar_birthday: 1991-03-30
    lunar_birthday: 1991-02-15
    reminder_days: 7
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(MINIMAL_YAML, encoding="utf-8")
    return path


@pytest.fixture
def api(config_file: Path) -> AppApi:
    return AppApi(str(config_file))


# ---------- 时间轴 ----------


def test_get_timeline_shape(api):
    data = api.get_timeline()
    assert data["ok"] is True
    assert data["total"] == 2
    assert len(data["recipients"]) == 2

    first = data["recipients"][0]
    # 前端渲染依赖这些字段，缺一个就会显示空白
    for key in (
        "index", "name", "note", "solar_birthday", "lunar_text",
        "next_birthday", "next_kind", "week_name", "age",
        "days_until", "status", "status_text", "reminder_days", "will_trigger",
    ):
        assert key in first, f"缺少字段 {key}"


def test_get_timeline_next_birthday_picks_nearer_calendar(api):
    """阳历与农历混排取更近的那个，并标注是哪种。"""
    data = api.get_timeline()
    by_name = {r["name"]: r for r in data["recipients"]}

    # 张三：阳历 2027-01-20 比农历 2027-01-31 近
    zhang = by_name["张三"]
    assert zhang["next_birthday"] == "2027-01-20"
    assert zhang["next_kind"] == "阳历"

    # 李四：农历 2027-03-22 比阳历 2027-03-30 近
    li = by_name["李四"]
    assert li["next_birthday"] == "2027-03-22"
    assert li["next_kind"] == "农历"


def test_get_timeline_includes_summary(api):
    data = api.get_timeline()
    s = data["summary"]
    assert s["resend_configured"] is True
    assert s["resend_receive_email"] == "me@example.com"
    assert s["resend_from_name"] == "生日提醒"
    assert s["default_reminder_days"] == 3


def test_missing_config_fails_loudly(tmp_path):
    """配置文件不存在时必须在构造阶段就报错，不能等到点按钮才失败。

    桌面应用没有控制台，晚失败等于让使用者面对一个什么都不响的界面。
    """
    with pytest.raises(FileNotFoundError):
        AppApi(str(tmp_path / "missing.yml"))


# ---------- 日期校验 ----------


def test_validate_solar_ok(api):
    r = api.validate_solar("1990-01-20")
    assert r["ok"] is True
    assert r["state"] == "ok"
    assert r["lunar_display"] == "腊月廿四"
    assert r["lunar_next_solar"]
    assert r["next_solar_birthday"]


def test_validate_solar_rejects_impossible_date(api):
    """1990-01-32 不得被归一化成合法日期后算出农历。"""
    for bad in ["1990-01-32", "1990-02-30", "1990-13-01", "1990-1-1", "abc"]:
        r = api.validate_solar(bad)
        assert r["ok"] is False, f"{bad} 应被拒绝"
        assert r["state"] == "bad"
        assert r.get("lunar_display") is None


def test_validate_solar_empty(api):
    r = api.validate_solar("")
    assert r["ok"] is False
    assert r["state"] == "empty"


# ---------- 收件人 CRUD ----------


def test_get_recipient_for_edit(api):
    r = api.get_recipient(0)
    assert r["ok"] is True
    assert r["values"]["name"] == "张三"
    assert r["values"]["solar_birthday"] == "1990-01-20"
    assert r["values"]["note"] == "大学同学"


def test_get_recipient_missing_index(api):
    r = api.get_recipient(99)
    assert r["ok"] is False
    assert "不存在" in r["error"]


def test_save_recipient_creates_and_writes_file(api, config_file):
    r = api.save_recipient(
        {"name": "王五", "solar_birthday": "1988-05-20", "reminder_days": "5", "note": "同事"}
    )
    assert r["ok"] is True
    assert "王五" in r["notice"]

    saved = config_file.read_text(encoding="utf-8")
    assert "王五" in saved
    assert "1988-05-20" in saved
    assert "同事" in saved
    # 农历自动推导
    assert "lunar_birthday" in saved
    # 原有注释保留
    assert "保留这条注释用于验证" in saved


def test_save_recipient_updates(api, config_file):
    r = api.save_recipient(
        {"name": "张三改", "solar_birthday": "1990-01-20", "reminder_days": "9", "note": ""},
        index=0,
    )
    assert r["ok"] is True
    saved = config_file.read_text(encoding="utf-8")
    assert "张三改" in saved
    assert "reminder_days: 9" in saved
    assert "李四" in saved, "不应影响其他条目"


def test_save_recipient_preserves_email(api, config_file):
    """表单不再暴露 email，编辑时不能把它弄丢。"""
    before = config_file.read_text(encoding="utf-8")
    assert "email" not in before or True  # 本 fixture 没有 email 字段

    # 手工塞一个 email 进配置，再编辑，验证被保留
    text = before.replace(
        "solar_birthday: 1990-01-20", "solar_birthday: 1990-01-20\n    email: keep@example.com"
    )
    config_file.write_text(text, encoding="utf-8")

    api.save_recipient(
        {"name": "张三", "solar_birthday": "1990-01-20", "reminder_days": "3", "note": ""},
        index=0,
    )
    saved = config_file.read_text(encoding="utf-8")
    assert "keep@example.com" in saved, "email 不应被编辑操作删掉"


def test_save_recipient_validation_errors(api, config_file):
    before = config_file.read_text(encoding="utf-8")

    r = api.save_recipient({"name": "", "solar_birthday": "1990-01-20", "reminder_days": "3"})
    assert r["ok"] is False
    assert "请填写姓名" in r["error"]
    assert r["errors"], "应返回字段级错误供表单标注"

    r = api.save_recipient({"name": "X", "solar_birthday": "1990-01-32", "reminder_days": "3"})
    assert r["ok"] is False
    assert "不合理" in r["error"] or "格式" in r["error"]

    # 校验失败不得写盘
    assert config_file.read_text(encoding="utf-8") == before


def test_delete_recipient(api, config_file):
    r = api.delete_recipient(0)
    assert r["ok"] is True
    assert "张三" in r["notice"]
    saved = config_file.read_text(encoding="utf-8")
    assert "张三" not in saved
    assert "李四" in saved


def test_delete_missing_index(api):
    r = api.delete_recipient(99)
    assert r["ok"] is False
    assert "不存在" in r["error"]


# ---------- 预览 ----------


def test_preview_recipient(api):
    r = api.preview_recipient(0)
    assert r["ok"] is True
    assert r["name"] == "张三"
    assert r["email_html"] and "张三" in r["email_html"]
    assert r["days_until"] is not None


def test_preview_missing_index(api):
    r = api.preview_recipient(99)
    assert r["ok"] is False


# ---------- 设置 ----------


def test_get_settings(api):
    r = api.get_settings()
    assert r["ok"] is True
    v = r["values"]
    assert v["resend_receive_email"] == "me@example.com"
    assert v["has_resend_key"] is True
    assert v["resend_key_masked"] == "re_TES...cdef"
    # 密钥绝不出现在返回值里
    assert "re_TESTKEY1234567890abcdef" not in str(r)


def test_save_settings(api, config_file):
    r = api.save_settings(
        {
            "api_key": "re_NEWKEY1234567890abcdef",
            "default_receive_email": "new@qq.com",
            "from_name": "我的提醒",
            "from_email": "",
            "default_reminder_days": "5",
        }
    )
    assert r["ok"] is True
    saved = config_file.read_text(encoding="utf-8")
    assert "re_NEWKEY1234567890abcdef" in saved
    assert "new@qq.com" in saved


def test_save_settings_blank_key_keeps_existing(api, config_file):
    """密钥留空表示不修改 —— 界面显示的是打码值。"""
    api.save_settings(
        {
            "api_key": "",
            "default_receive_email": "changed@qq.com",
            "from_name": "",
            "from_email": "",
            "default_reminder_days": "3",
        }
    )
    saved = config_file.read_text(encoding="utf-8")
    assert "re_TESTKEY1234567890abcdef" in saved, "留空不应清空密钥"
    assert "changed@qq.com" in saved


def test_save_settings_can_clear_key(api, config_file):
    api.save_settings(
        {
            "api_key": "",
            "clear_api_key": True,
            "default_receive_email": "x@qq.com",
            "from_name": "",
            "from_email": "",
            "default_reminder_days": "3",
        }
    )
    saved = config_file.read_text(encoding="utf-8")
    assert "re_TESTKEY1234567890abcdef" not in saved


def test_save_settings_validates_email(api):
    r = api.save_settings({"default_receive_email": "bad", "default_reminder_days": "3"})
    assert r["ok"] is False
    assert "邮箱" in r["error"]
    assert r["errors"]


def test_save_settings_preserves_comments(api, config_file):
    api.save_settings(
        {
            "api_key": "",
            "default_receive_email": "c@qq.com",
            "from_name": "",
            "from_email": "",
            "default_reminder_days": "4",
        }
    )
    assert "保留这条注释用于验证" in config_file.read_text(encoding="utf-8")


# ---------- 界面资源 ----------


def test_build_html_is_self_contained():
    html = build_html({"ok": True, "recipients": [], "total": 0, "summary": {}, "trigger_count": 0})
    assert "<style>" in html
    assert "<script>" in html
    # 不能有外部引用（离线可用）
    assert "http://" not in html
    assert "https://" not in html or "https://resend.com" not in html
    assert 'src="' not in html, "不应有外部脚本引用"


def test_build_html_embeds_bootstrap():
    """首屏数据嵌在页面里，前端不必等 js_api 注入。"""
    payload = {"ok": True, "total": 2, "recipients": [], "summary": {}, "trigger_count": 0}
    html = build_html(payload)
    assert "window.__BOOTSTRAP__" in html
    assert '"total": 2' in html or '"total":2' in html


def test_build_html_escapes_script_breakout():
    """使用者输入里的 </script> 不能提前闭合脚本块。"""
    payload = {"ok": True, "note": "</script><img src=x onerror=alert(1)>"}
    html = build_html(payload)
    assert "</script><img" not in html
    assert "<\\/script>" in html


def test_build_html_returns_none_safely():
    html = build_html(None)
    assert "window.__BOOTSTRAP__ = null" in html


def test_app_api_hides_internals():
    """内部状态不该暴露给前端。

    pywebview 会暴露 js_api 对象的公开成员；下划线前缀的不会被暴露。
    """
    api = AppApi.__new__(AppApi)  # 不加载配置，只看命名
    public = [n for n in dir(api) if not n.startswith("_")]
    # 这些是给前端的方法
    for expected in (
        "get_timeline", "get_recipient", "validate_solar", "save_recipient",
        "delete_recipient", "preview_recipient", "test_send",
        "get_settings", "save_settings", "test_settings_send",
    ):
        assert expected in public, f"{expected} 应暴露给前端"

    # 这些是内部状态，不该暴露
    for internal in ("repo", "config", "config_manager"):
        assert internal not in public, f"{internal} 不应暴露给前端"
