"""管理台的访问令牌（token）鉴权。

**为什么需要它**：管理台能改 ``config.yml``（里面是 Resend API Key）、能触发
真实发信，而 ``web`` 命令通常要 ``--host 0.0.0.0`` 才能从别的机器访问。
没有鉴权时，任何能连到端口的人都能做这些事。

**为什么不做账号密码**：这是单人自用的内部工具。多一套密码就多一份需要保管、
轮换、找回的凭据，而使用者真正的需求只是"别让外人点进来"。所以采用
「进程启动时生成一个随机 token」的方案 —— 与 dsh 的 web UI 一致：

1. 启动时生成 token（或由 ``--token`` / ``BIRTHDAYRS_TOKEN`` 指定），
   把带 token 的完整 URL 打印到终端与日志。
2. 浏览器首次访问该 URL（``?token=...``）→ 校验通过 → 种一个 30 天的 cookie，
   然后 303 跳到**去掉 token 的干净地址**。
3. 之后直接访问域名即可，cookie 生效。

**边界与取舍**：

- token 校验用 ``secrets.compare_digest``，避免按字符比较被时序侧信道猜出。
- token 出现在 URL 里，会被浏览器历史、以及可能存在的反代访问日志记录。
  这是 URL 传凭据的固有代价；用 cookie 换掉它正是为了把暴露窗口缩到一次。
- cookie 是 ``HttpOnly`` 的（前端 JS 拿不到），但**没有加 ``Secure``**：
  本项目的部署姿势通常是内网 http，加了 Secure 会让 cookie 根本发不出去。
  若你挂在 https 反代后面，建议自行在反代层收口。
- 这里不做登录失败次数限制：token 是 32 字节随机串，暴力枚举不现实。
"""

from __future__ import annotations

import logging
import secrets
import urllib.parse
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)

#: cookie 名。带项目前缀，避免同域下与其他服务冲突。
COOKIE_NAME = "birthdayrs_token"

#: cookie 有效期（秒）。30 天 —— 与 dsh 的 web UI 一致。
COOKIE_MAX_AGE = 30 * 24 * 3600

#: 无需鉴权即可访问的路径前缀。
#: 只放静态资源：它们是 CSS/JS，不含任何秘密；而拒绝页本身要靠它们才能正常显示。
_PUBLIC_PREFIXES = ("/static/",)

#: 环境变量名：用它可以在容器重启后沿用同一个 token。
ENV_TOKEN = "BIRTHDAYRS_TOKEN"


def generate_token() -> str:
    """生成一个新的访问令牌。32 字节熵，URL 安全。"""
    return secrets.token_urlsafe(32)


def resolve_token(explicit: Optional[str] = None) -> str:
    """决定本次启动用哪个 token。

    优先级：命令行 ``--token`` > 环境变量 ``BIRTHDAYRS_TOKEN`` > 随机生成。
    显式指定便于容器重启（``--restart unless-stopped``）后书签不失效。
    """
    if explicit and explicit.strip():
        return explicit.strip()

    import os

    from_env = (os.environ.get(ENV_TOKEN) or "").strip()
    if from_env:
        return from_env

    return generate_token()


def access_url(host: str, port: int, token: str) -> str:
    """拼出带 token 的访问地址。

    ``0.0.0.0`` 是「监听所有网卡」，不是能访问的地址，展示成 ``127.0.0.1``
    才符合使用者"本机点开看看"的直觉；远程访问时把主机名换掉即可。
    """
    display_host = host
    if host in ("0.0.0.0", "::", ""):
        display_host = "127.0.0.1"
    return f"http://{display_host}:{port}/?token={urllib.parse.quote(token)}"


def _is_public(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def _constant_time_equals(a: str, b: str) -> bool:
    """恒定时间比较。长度不同直接返回 False（长度本身会泄露，但 token 定长）。"""
    if not a or not b:
        return False
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _denied_page(reason: str) -> HTMLResponse:
    """未通过鉴权时的提示页。

    刻意不透露正确 token，也不透露它有多少位。给出"去哪儿找"就够了。
    """
    body = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>需要访问令牌 · 生日提醒</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <main class="page">
    <div class="pagehead">
      <h1>需要访问令牌</h1>
    </div>
    <p class="flash flash--err" role="alert">{reason}</p>
    <section class="panel">
      <h2 class="panel__title">怎么进来</h2>
      <div class="panel__body">
        <p>管理台只能通过启动时打印的那个地址访问，它长这样：</p>
        <p class="mono">http://&lt;主机&gt;:&lt;端口&gt;/?token=&lt;令牌&gt;</p>
        <p class="muted">
          令牌在服务启动时打印在终端和日志里。用容器部署时可以这样取：
        </p>
        <p class="mono">docker logs &lt;容器名&gt; | grep token</p>
        <p class="muted">
          打开一次之后浏览器会记住，之后直接访问域名即可。
        </p>
      </div>
    </section>
  </main>
</body>
</html>
"""
    return HTMLResponse(body, status_code=401)


def _strip_token(url: str) -> str:
    """去掉 URL 里的 token 参数，保留其余查询参数与路径。"""
    parsed = urllib.parse.urlsplit(url)
    kept = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if k != "token"
    ]
    query = urllib.parse.urlencode(kept)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment)
    )


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """令牌鉴权中间件。

    放在最外层：任何路由（含将来新增的）都自动受保护，不需要逐个加依赖。
    漏加一处就漏一个后门，中间件比装饰器更难漏。
    """

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next):
        if _is_public(request.url.path):
            return await call_next(request)

        # 1) 已换过 cookie：放行
        if _constant_time_equals(request.cookies.get(COOKIE_NAME, ""), self.token):
            return await call_next(request)

        # 2) URL 里带了 token：校验通过就换 cookie，并跳走去掉 URL 里的 token
        supplied = request.query_params.get("token", "")
        if supplied:
            if _constant_time_equals(supplied, self.token):
                response: Response = RedirectResponse(
                    _strip_token(str(request.url)), status_code=303
                )
                response.set_cookie(
                    COOKIE_NAME,
                    self.token,
                    max_age=COOKIE_MAX_AGE,
                    httponly=True,
                    samesite="lax",
                    path="/",
                )
                return response

            logger.warning(
                "令牌不正确，拒绝访问 path=%s client=%s",
                request.url.path,
                request.client.host if request.client else "?",
            )
            return _denied_page("访问令牌不正确。")

        # 3) 都没有：拒绝
        logger.info(
            "未携带令牌，拒绝访问 path=%s client=%s",
            request.url.path,
            request.client.host if request.client else "?",
        )
        return _denied_page("这个地址需要访问令牌才能打开。")
