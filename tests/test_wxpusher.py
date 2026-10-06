"""WxPusher 推送发送器测试。

全部用假的 httpx 客户端，绝不发起真实网络请求。
真实端到端验证由命令行手动执行（见 DEPLOY.md）。

用**假**令牌当测试数据。绝不要把真实 SPT / appToken / UID 写进测试文件 ——
测试文件会进版本库，令牌就随之泄露。

本文件重点覆盖两件事：
1. **受众路由**：团体广播 vs 私人定向。搞反了会把私人信息广播出去，
   这是本渠道最严重的事故，必须逐条钉住。
2. 错误处理不泄露令牌、失败可重试。
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config import Recipient, WxPusherConfig
from src.notification.sender_wxpusher import (
    SIMPLE_ENDPOINT,
    STANDARD_ENDPOINT,
    USER_LIST_ENDPOINT,
    WxPusherConfigError,
    WxPusherSendError,
    WxPusherSender,
    clear_followers_cache,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = str(REPO_ROOT / "templates")

FAKE_SPT = "SPT_faketokenforunittests0000"
FAKE_APP_TOKEN = "AT_fakeapptokenforunittests0"
FAKE_SELF_UID = "UID_fakeselfuid"
FAKE_FRIEND_UID = "UID_fakefrienduid"


@pytest.fixture(autouse=True)
def _clear_cache():
    """每个测试都从干净的关注者缓存开始，避免互相干扰。"""
    clear_followers_cache()
    yield
    clear_followers_cache()


def std_config(**overrides) -> WxPusherConfig:
    """标准模式：能广播、也能定向。"""
    base = {
        "spt": None,
        "app_token": FAKE_APP_TOKEN,
        "self_uid": FAKE_SELF_UID,
        "default_reminder_days": 3,
    }
    base.update(overrides)
    return WxPusherConfig(**base)


def make_recipient(**overrides) -> Recipient:
    base = {"name": "张三", "solar_birthday": "1990-01-20", "reminder_days": 3}
    base.update(overrides)
    return Recipient(**base)


def ok_response(payload=None):
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(
        return_value=payload
        if payload is not None
        else {"code": 1000, "msg": "处理成功", "success": True, "data": []}
    )
    return resp


def followers_response(uids, total=None, reject_uids=()):
    """构造关注者列表接口的响应。"""
    records = [
        {"uid": u, "type": 0, "reject": u in reject_uids}
        for u in uids
    ]
    return ok_response(
        {
            "code": 1000,
            "msg": "处理成功",
            "data": {"total": total if total is not None else len(uids), "records": records},
            "success": True,
        }
    )


EXTRA = {
    "days_until": 3,
    "age": 36,
    "solar_match": False,
    "lunar_match": True,
    "zodiac": "蛇",
    "constellation": "摩羯",
    "solar_term": "",
    "lunar_festival": "",
    "solar_festival": "",
}


def captured_posts(client):
    """取出所有 POST 的 (url, json)。"""
    return [(c.args[0] if c.args else c.kwargs["url"], c.kwargs["json"]) for c in client.post.call_args_list]


# ============ 受众路由：这是本渠道的核心 ============


@pytest.mark.asyncio
async def test_private_recipient_goes_to_self_uid_only():
    """私人提醒**只能**发给 self_uid。

    这是最重要的一条：如果它走了广播，使用者的私人朋友名单就泄露给团体了。
    """
    sender = WxPusherSender(std_config())
    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=ok_response())
        client_cls.return_value.__aenter__.return_value = client

        await sender.send(make_recipient(audience="self"), "正文", 3, 36)

    posts = captured_posts(client)
    assert len(posts) == 1
    url, payload = posts[0]
    assert url == STANDARD_ENDPOINT
    assert payload["uids"] == [FAKE_SELF_UID]
    # 绝不能带上别人的 UID
    assert FAKE_FRIEND_UID not in payload["uids"]


@pytest.mark.asyncio
async def test_group_recipient_broadcasts_to_all_followers():
    """团体提醒广播给所有关注者。"""
    sender = WxPusherSender(std_config())
    followers = ["UID_a", "UID_b", "UID_c"]

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=followers_response(followers))
        client.post = AsyncMock(return_value=ok_response())
        client_cls.return_value.__aenter__.return_value = client

        await sender.send(make_recipient(audience="group"), "正文", 3, 36)

    posts = captured_posts(client)
    assert len(posts) == 1
    url, payload = posts[0]
    assert url == STANDARD_ENDPOINT
    assert payload["uids"] == followers


@pytest.mark.asyncio
async def test_group_recipient_never_uses_simple_push():
    """广播必须走标准接口。

    极简推送只发给 SPT 持有者（使用者自己），用它做广播等于消息根本没发出去 ——
    而接口会返回成功，属于静默失败。
    """
    sender = WxPusherSender(std_config(spt=FAKE_SPT))

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=followers_response(["UID_a"]))
        client.post = AsyncMock(return_value=ok_response())
        client_cls.return_value.__aenter__.return_value = client

        await sender.send(make_recipient(audience="group"), "正文", 3, 36)

    urls = [u for u, _ in captured_posts(client)]
    assert SIMPLE_ENDPOINT not in urls


@pytest.mark.asyncio
async def test_private_without_uid_falls_back_to_spt():
    """没有 self_uid 时，私人提醒退回极简推送（只发给 SPT 持有者）。"""
    sender = WxPusherSender(std_config(self_uid=None, spt=FAKE_SPT))

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=ok_response())
        client_cls.return_value.__aenter__.return_value = client

        await sender.send(make_recipient(audience="self"), "正文", 3, 36)

    posts = captured_posts(client)
    assert len(posts) == 1
    url, payload = posts[0]
    assert url == SIMPLE_ENDPOINT
    assert payload["spt"] == FAKE_SPT


@pytest.mark.asyncio
async def test_group_without_app_token_fails_loudly():
    """只有 SPT 却想广播 → 明确报错，而不是悄悄降级成只发给自己。"""
    sender = WxPusherSender(
        WxPusherConfig(spt=FAKE_SPT, app_token=None, self_uid=None)
    )

    with pytest.raises(WxPusherConfigError) as exc:
        await sender.send(make_recipient(audience="group"), "正文", 3, 36)

    assert "appToken" in str(exc.value)


@pytest.mark.asyncio
async def test_private_without_any_route_fails_loudly():
    """既没 self_uid 也没 SPT → 报错，不能静默丢弃。"""
    sender = WxPusherSender(WxPusherConfig(app_token=FAKE_APP_TOKEN))

    with pytest.raises(WxPusherConfigError) as exc:
        await sender.send(make_recipient(audience="self"), "正文", 3, 36)

    assert "self_uid" in str(exc.value) or "SPT" in str(exc.value)


@pytest.mark.asyncio
async def test_group_without_followers_fails_loudly():
    """应用下没人关注 → 报错说明白，而不是发一条没人收的消息。"""
    sender = WxPusherSender(std_config())

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=followers_response([]))
        client_cls.return_value.__aenter__.return_value = client

        with pytest.raises(WxPusherConfigError) as exc:
            await sender.send(make_recipient(audience="group"), "正文", 3, 36)

    assert "关注" in str(exc.value)


@pytest.mark.asyncio
async def test_missing_audience_defaults_to_private():
    """没有 audience 字段的记录（旧配置）必须按"只发给我"处理。

    默认成广播是危险的方向 —— 泄露私人信息不可撤销。
    """
    sender = WxPusherSender(std_config())
    old_style = make_recipient()  # 不传 audience
    object.__delattr__(old_style, "audience")

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=ok_response())
        client_cls.return_value.__aenter__.return_value = client

        await sender.send(old_style, "正文", 3, 36)

    url, payload = captured_posts(client)[0]
    assert payload["uids"] == [FAKE_SELF_UID], "旧记录不该被广播"


# ============ 关注者列表 ============


@pytest.mark.asyncio
async def test_followers_skips_rejected_users():
    """被拉黑的用户收不到消息，不该占用配额。"""
    sender = WxPusherSender(std_config())

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=followers_response(["UID_ok", "UID_blocked"], reject_uids={"UID_blocked"})
        )
        client_cls.return_value.__aenter__.return_value = client

        uids = await sender.fetch_followers()

    assert uids == ["UID_ok"]


@pytest.mark.asyncio
async def test_followers_deduplicates():
    """同一用户可能同时关注应用和主题，UID 要先去重再发。"""
    sender = WxPusherSender(std_config())

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=followers_response(["UID_a", "UID_a", "UID_b"]))
        client_cls.return_value.__aenter__.return_value = client

        uids = await sender.fetch_followers()

    assert uids == ["UID_a", "UID_b"]


@pytest.mark.asyncio
async def test_followers_are_cached():
    """缓存是必要的：一次 run 里多条 group 记录不该反复拉列表（限流约 2 QPS）。"""
    sender = WxPusherSender(std_config())

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=followers_response(["UID_a"]))
        client_cls.return_value.__aenter__.return_value = client

        await sender.fetch_followers()
        await sender.fetch_followers()

    assert client.get.await_count == 1, "第二次应命中缓存"


@pytest.mark.asyncio
async def test_followers_error_is_explained():
    sender = WxPusherSender(std_config())

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=ok_response({"code": 1001, "msg": "appToken错误"})
        )
        client_cls.return_value.__aenter__.return_value = client

        with pytest.raises(WxPusherSendError) as exc:
            await sender.fetch_followers()

    assert "appToken" in str(exc.value)


# ============ 请求形状 ============


@pytest.mark.asyncio
async def test_standard_payload_shape():
    sender = WxPusherSender(std_config())
    content = sender.render_content("张三", "birthday.html", EXTRA)

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=ok_response())
        client_cls.return_value.__aenter__.return_value = client

        await sender.send(make_recipient(audience="self"), content, days_until=3, age=36)

    payload = captured_posts(client)[0][1]
    assert payload["appToken"] == FAKE_APP_TOKEN
    assert payload["content"] == content
    assert payload["contentType"] == 1
    assert payload["summary"]
    assert len(payload["summary"]) <= 20


@pytest.mark.asyncio
async def test_large_group_is_batched():
    """超过单次 2000 UID 上限时自动分批，否则接口会拒。"""
    sender = WxPusherSender(std_config())
    many = [f"UID_{i:05d}" for i in range(2500)]

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=followers_response(many, total=len(many)))
        client.post = AsyncMock(return_value=ok_response())
        client_cls.return_value.__aenter__.return_value = client

        await sender.send(make_recipient(audience="group"), "正文", 3, 36)

    posts = captured_posts(client)
    assert len(posts) == 2, "2500 个 UID 应拆成 2 批"
    assert len(posts[0][1]["uids"]) == 2000
    assert len(posts[1][1]["uids"]) == 500
    # 不能漏人
    assert len(set(posts[0][1]["uids"]) | set(posts[1][1]["uids"])) == 2500


# ============ 错误处理 ============


@pytest.mark.asyncio
async def test_business_code_non_1000_is_failure():
    """HTTP 200 但业务码不是 1000，必须当成失败。"""
    sender = WxPusherSender(std_config())

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=ok_response({"code": 1002, "msg": "令牌不存在"}))
        client_cls.return_value.__aenter__.return_value = client

        with pytest.raises(WxPusherSendError):
            await sender.send(make_recipient(), "正文", 3, 36)


@pytest.mark.asyncio
async def test_http_error_does_not_leak_token():
    """失败信息不能回显响应体 —— 它可能含令牌，而异常会进日志。"""
    sender = WxPusherSender(std_config())

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        resp = MagicMock()
        resp.status_code = 500
        resp.text = f"internal error for {FAKE_APP_TOKEN}"
        client.post = AsyncMock(return_value=resp)
        client_cls.return_value.__aenter__.return_value = client

        with pytest.raises(WxPusherSendError) as exc:
            await sender.send(make_recipient(), "正文", 3, 36)

    assert FAKE_APP_TOKEN not in str(exc.value)


@pytest.mark.asyncio
async def test_business_error_message_redacts_tokens():
    """业务错误的 message 里若回显令牌，也要脱敏后再抛出。"""
    sender = WxPusherSender(std_config())

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post = AsyncMock(
            return_value=ok_response({"code": 9999, "msg": f"参数错误：{FAKE_APP_TOKEN} 不合法"})
        )
        client_cls.return_value.__aenter__.return_value = client

        with pytest.raises(WxPusherSendError) as exc:
            await sender.send(make_recipient(), "正文", 3, 36)

    assert FAKE_APP_TOKEN not in str(exc.value)
    assert "AT_••••" in str(exc.value)


@pytest.mark.asyncio
async def test_retry_repeats_on_failure():
    """失败应重试 3 次（复用 EmailSender 的指数退避装饰器）。"""
    sender = WxPusherSender(std_config())

    with patch("httpx.AsyncClient") as client_cls, patch(
        "asyncio.sleep", new=AsyncMock()
    ):
        client = AsyncMock()
        client.post = AsyncMock(return_value=ok_response({"code": 1002, "msg": "bad"}))
        client_cls.return_value.__aenter__.return_value = client

        with pytest.raises(WxPusherSendError):
            await sender.send(make_recipient(), "正文", 3, 36)

    assert client.post.await_count == 3, "应尝试 3 次"


# ============ 内容与配置 ============


def test_render_content_matches_serverchan_wording():
    """推送文案与 ServerChan 共用一份，换渠道时内容不变。"""
    from src.notification.sender_serverchan import render_plain_text

    sender = WxPusherSender(std_config())
    assert sender.render_content("李四", "birthday.html", EXTRA) == render_plain_text(
        "李四", EXTRA
    )


def test_config_capabilities():
    """能否广播 / 能否定向，判断要准确 —— 界面靠它提示缺口。"""
    both = std_config()
    assert both.group_broadcast_ready is True
    assert both.self_only_ready is True

    # 只有 appToken：能广播，但私人提醒没处发
    only_app = WxPusherConfig(app_token=FAKE_APP_TOKEN)
    assert only_app.group_broadcast_ready is True
    assert only_app.self_only_ready is False

    # 只有 SPT：私人能发，广播不行
    only_spt = WxPusherConfig(spt=FAKE_SPT)
    assert only_spt.group_broadcast_ready is False
    assert only_spt.self_only_ready is True

    # 都有 appToken 和 self_uid：不用 SPT 也能定向
    app_and_uid = WxPusherConfig(app_token=FAKE_APP_TOKEN, self_uid=FAKE_SELF_UID)
    assert app_and_uid.self_only_ready is True


def test_describe_config_reports_gaps():
    """状态描述要说清缺什么，不能只说"未配置"。"""
    from src.notification.sender_wxpusher import describe_config

    only_app = describe_config(WxPusherConfig(app_token=FAKE_APP_TOKEN))
    assert only_app["group_ready"] is True
    assert only_app["self_ready"] is False
    assert "self_uid" in only_app["note"]

    none = describe_config(None)
    assert none["configured"] is False
