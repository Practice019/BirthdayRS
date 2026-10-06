"""WxPusher 推送发送器（https://wxpusher.zjiecode.com）。

**按「受众」分流是这个渠道的核心职责。**

使用者记着两类人的生日：

- **团体成员**（``audience=group``）：大家互相认识，谁的生日到了应该让所有人都知道。
  用自己创建的 WxPusher 应用广播给**所有关注者**。
- **私人朋友**（``audience=self``，默认）：与团体无关，只有使用者自己需要被提醒。
  定向发给使用者自己的 UID —— 这条**绝不能**广播，否则私人信息就泄露给团体了。

两种模式（详见 ``WxPusherConfig``）：

- **标准模式**（``app_token``）：``POST /api/send/message``，用 ``uids`` 指定收件人。
  群发要它；定向也要它（配 ``self_uid``）。
- **极简模式**（``spt``）：``POST /api/send/message/simple-push``，只发给 SPT 持有者。
  没有 ``self_uid`` 时，这是"只发给我"的兜底路径。

**限制**（官方 OpenAPI 与文档，实测一致）：

- ``uids`` 单次最多 2000 个；``content`` ≤ 40000 字符且 UTF-8 ≤ 65535 字节
- ``summary`` ≤ 100 字符（各端展示还会更短，按 20 字左右准备）
- 接口限流约 2 QPS
- 成功判定：业务码 ``code == 1000``；HTTP 200 也可能是业务失败
- 查询关注者：``GET /api/fun/wxuser/v2``，``pageSize`` 上限 100，分页
"""

from __future__ import annotations

import logging
import re
import time
from typing import Dict, List, Optional

import httpx

from src.core.checker import Recipient
from src.core.config import WxPusherConfig
from src.notification.notification_base import NotificationBase
from src.notification.sender_email import retry_on_failure
from src.notification.sender_serverchan import render_plain_text

logger = logging.getLogger(__name__)

#: 标准推送：用 appToken + uids 指定收件人，支持群发。
STANDARD_ENDPOINT = "https://wxpusher.zjiecode.com/api/send/message"
#: 极简推送：只发给 SPT 持有者。
SIMPLE_ENDPOINT = "https://wxpusher.zjiecode.com/api/send/message/simple-push"
#: 查询关注该应用的用户。
USER_LIST_ENDPOINT = "https://wxpusher.zjiecode.com/api/fun/wxuser/v2"

#: 成功业务码。
SUCCESS_CODE = 1000

#: 单次请求最多能带的 UID 数（官方 OpenAPI 的 maxItems）。
MAX_UIDS_PER_REQUEST = 2000

#: 查询关注者时的分页大小（官方上限 100）。
_PAGE_SIZE = 100

#: 关注者列表的缓存时长（秒）。
#:
#: 缓存是必要的：一次 ``run`` 里可能有多条 group 记录命中，
#: 每条都去拉一次列表既慢又白耗接口配额（限流约 2 QPS）。
#:
#: 取 60 秒而不是更长：新朋友关注后，广播应该很快就能带上他。
#: 曾经设 600 秒，于是"有人刚关注、测试发送却还是只发到旧的那几个人" ——
#: 这类问题看起来像 bug，其实是缓存还没过期，很难联想到。
#: 60 秒足以覆盖"一次 run 内多条记录"的复用，代价是偶尔多几次请求。
_FOLLOWERS_TTL = 60

#: 进程内的关注者缓存：``{app_token: (过期时间戳, UID 列表)}``。
#: 放模块级而不是实例级 —— web 界面每次请求都会新建一个 sender 实例。
_followers_cache: Dict[str, tuple] = {}

#: 摘要在通知栏里能显示的长度，超出部分会被截掉。
_SUMMARY_DISPLAY = 20


class WxPusherSendError(RuntimeError):
    """WxPusher 返回了错误，消息带可操作的中文说明。"""


class WxPusherConfigError(WxPusherSendError):
    """配置不足以完成本次投递（缺 app_token / self_uid 之类）。"""


def _explain(code: int, message: str) -> str:
    """把 WxPusher 的业务错误码翻成可操作的中文提示。

    刻意**不回显**原始 message 里的敏感内容：错误信息可能回显令牌的一部分，
    而异常会进日志（``docker logs`` / ``birthday_reminder.log``）。
    """
    if code == 1001 and "SPT" in message.upper():
        return "SPT 无效或已被删除，请到设置页重新填写（扫码可获取新的 SPT）"
    if code == 1001 and "appToken" in message:
        return "appToken 无效或已被重置，请到设置页重新填写"
    if code == 1001:
        return f"请求被拒绝：{_redact(message)}"
    if code == 1002:
        return "令牌已过期或被停用，请到 WxPusher 后台确认后重新获取"
    if code == 1005:
        return "发送过于频繁（接口限流约 2 次/秒），请稍后重试"
    return f"WxPusher 返回错误（code={code}）：{_redact(message)}"


def _redact(text: str) -> str:
    """把可能出现的令牌打码，避免密钥进日志。"""
    if not text:
        return ""
    text = re.sub(r"SPT_\w+", "SPT_••••", text)
    return re.sub(r"AT_\w+", "AT_••••", text)


def clear_followers_cache() -> None:
    """清空关注者缓存。改过 app_token 或刚刚有人新关注时调用。"""
    _followers_cache.clear()


class WxPusherSender(NotificationBase):
    """通过 WxPusher 发送手机通知，按记录上的 ``audience`` 决定收件人。"""

    def __init__(self, wxpusher_config: WxPusherConfig):
        self.config = wxpusher_config

    # ---------- 内容 ----------

    def render_content(self, name: str, template_file: str, extra_info: Dict) -> str:
        """渲染推送正文。

        WxPusher 支持 Markdown，但这里仍用纯文本：与 ServerChan 保持同一套文案
        （``render_plain_text``），换渠道时内容一致，使用者不用适应两种格式。

        文案只有一套：**两种受众看到的内容相同**。团体广播本来就是要让大家都知道
        "谁快过生日了"，改成"亲爱的你"反而没意义 —— 收件人不是寿星本人。
        """
        return render_plain_text(name, extra_info)

    # ---------- 收件人解析 ----------

    async def fetch_followers(self) -> List[str]:
        """拉取所有关注该应用的用户 UID（分页），带进程内缓存。

        跳过 ``reject`` 为真的用户（被拉黑的人收不到消息，带上他们只是浪费配额）。
        """
        app_token = (self.config.app_token or "").strip()
        if not app_token:
            raise WxPusherConfigError(
                "没有配置 appToken，无法广播给团体：请在设置页填入应用密钥"
            )

        cached = _followers_cache.get(app_token)
        now = time.time()
        if cached and cached[0] > now:
            return cached[1]

        uids: List[str] = []
        page = 1
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                resp = await client.get(
                    USER_LIST_ENDPOINT,
                    params={"appToken": app_token, "page": page, "pageSize": _PAGE_SIZE},
                )
                if resp.status_code >= 400:
                    raise WxPusherSendError(
                        f"拉取关注者失败（HTTP {resp.status_code}），请稍后重试"
                    )
                try:
                    data = resp.json()
                except Exception as exc:
                    raise WxPusherSendError(f"关注者列表无法解析：{exc}") from exc

                if data.get("code") != SUCCESS_CODE:
                    raise WxPusherSendError(
                        _explain(int(data.get("code") or -1), str(data.get("msg") or ""))
                    )

                payload = data.get("data") or {}
                records = payload.get("records") or []
                for record in records:
                    if record.get("reject"):
                        continue
                    uid = (record.get("uid") or "").strip()
                    # type=0 是"关注应用"，1 是"关注主题"。只有前者能被应用消息触达。
                    if uid and record.get("type", 0) == 0:
                        uids.append(uid)

                total = int(payload.get("total") or 0)
                if not records or page * _PAGE_SIZE >= total:
                    break
                page += 1

        # 去重但保序：同一用户可能有多条记录（关注应用 + 关注主题）
        uids = list(dict.fromkeys(uids))
        _followers_cache[app_token] = (now + _FOLLOWERS_TTL, uids)
        return uids

    def _self_uids(self) -> List[str]:
        """「只发给我」的收件人 UID。"""
        uid = (self.config.self_uid or "").strip()
        return [uid] if uid else []

    async def resolve_targets(self, recipient: Recipient) -> tuple:
        """决定这条提醒发给谁，返回 ``(mode, uids, 人话描述)``。

        ``mode`` 为 ``"standard"`` 或 ``"simple"``。这个拆分是刻意的：
        **受众判断集中在一处**，路由的正确性可以直接单测，不必去跑 HTTP。
        """
        audience = getattr(recipient, "audience", "self") or "self"

        if audience == "group":
            if not self.config.has_standard:
                raise WxPusherConfigError(
                    f"{recipient.name} 标记为发给团体，但没有配置 appToken，"
                    "无法广播。请在设置页填入 WxPusher 应用的 appToken。"
                )
            uids = await self.fetch_followers()
            if not uids:
                raise WxPusherConfigError(
                    "应用下还没有任何人关注，团体提醒发不出去。"
                    "请先把应用的关注二维码发给大家。"
                )
            return "standard", uids, f"团体所有人（{len(uids)} 人）"

        # audience == "self"：私人提醒，只能定向，绝不能走广播
        self_uids = self._self_uids()
        if self_uids:
            return "standard", self_uids, "你自己"

        if self.config.has_spt:
            # 没有 self_uid 时退回极简推送 —— SPT 天然只发给持有者自己
            return "simple", [], "你自己（经极简推送）"

        raise WxPusherConfigError(
            f"{recipient.name} 标记为只发给你自己，但既没有填 self_uid、"
            "也没有配 SPT，无法定向发送。请在设置页补上其中之一。"
        )

    # ---------- 发送 ----------

    async def send(self, recipient: Recipient, content: str, days_until: int, age: int) -> None:
        mode, uids, target_desc = await self.resolve_targets(recipient)
        title = f"生日提醒 - {recipient.name} - {age}岁"

        if mode == "simple":
            await self._send_simple(content, title)
        else:
            await self._send_standard(uids, content, title)

        logger.info(
            "WxPusher 推送成功: %s → %s（%s）",
            recipient.name,
            target_desc,
            "群发" if getattr(recipient, "audience", "self") == "group" else "定向",
        )

    @retry_on_failure()
    async def _send_simple(self, content: str, title: str) -> None:
        """极简推送：只发给 SPT 持有者。"""
        spt = (self.config.spt or "").strip()
        if not spt:
            raise WxPusherConfigError("没有配置 SPT，无法走极简推送")

        payload = {
            "spt": spt,
            "content": content,
            "summary": title[:_SUMMARY_DISPLAY],
        }
        await self._post(SIMPLE_ENDPOINT, payload)

    @retry_on_failure()
    async def _send_standard(self, uids: List[str], content: str, title: str) -> None:
        """标准推送：按 UID 定向/群发。超过单次上限时自动分批。"""
        app_token = (self.config.app_token or "").strip()
        if not app_token:
            raise WxPusherConfigError("没有配置 appToken，无法走标准推送")

        for batch in _chunk(uids, MAX_UIDS_PER_REQUEST):
            payload = {
                "appToken": app_token,
                "content": content,
                "summary": title[:_SUMMARY_DISPLAY],
                "contentType": 1,
                "uids": batch,
            }
            await self._post(STANDARD_ENDPOINT, payload)

    async def _post(self, url: str, payload: Dict) -> None:
        """发一次请求并校验业务码。"""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)

        if resp.status_code >= 400:
            # 不把响应体原样抛出：它可能回显令牌。
            raise WxPusherSendError(
                f"WxPusher 返回 HTTP {resp.status_code}，请稍后重试"
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise WxPusherSendError(f"WxPusher 返回了无法解析的内容：{exc}") from exc

        code = data.get("code")
        if code != SUCCESS_CODE:
            detail = _explain(int(code or -1), str(data.get("msg") or ""))
            logger.error("WxPusher 推送失败: %s", detail)
            raise WxPusherSendError(detail)


def _chunk(items: List[str], size: int) -> List[List[str]]:
    """把列表切成不超过 ``size`` 的若干批。"""
    return [items[i:i + size] for i in range(0, len(items), size)]


def describe_config(config: Optional[WxPusherConfig]) -> Dict[str, object]:
    """给界面用的一句话状态描述：这个渠道现在能发哪几种受众。

    界面上不该让使用者去猜"我这么配，团体提醒到底发不发得出去"。
    """
    if config is None:
        return {"configured": False, "group_ready": False, "self_ready": False, "note": "未配置"}

    group_ready = config.group_broadcast_ready
    self_ready = config.self_only_ready

    notes = []
    if group_ready:
        notes.append("可广播给团体所有人")
    if self_ready:
        notes.append("可只发给你自己")
    if group_ready and not self_ready:
        notes.append("但没填 self_uid，私人提醒还发不出去")
    if not group_ready and self_ready:
        notes.append("但没有 appToken，团体提醒发不出去")

    return {
        "configured": config.has_standard or config.has_spt,
        "group_ready": group_ready,
        "self_ready": self_ready,
        "note": "；".join(notes) if notes else "配置不完整",
    }
