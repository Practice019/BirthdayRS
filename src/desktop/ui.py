"""把界面资源拼成一份自包含的 HTML。

为什么不直接 ``url="index.html"``：那会触发 pywebview 的内置 HTTP 服务
（进程会监听一个随机端口）。用 ``html=`` 直载字符串才是零端口。

所有资源内联（CSS/JS/HTML），因此：

- 不依赖外部 CDN，离线机器也能正常显示（分发场景常见）
- 不用处理相对路径，打包成 exe 后不会因工作目录变化而找不到文件
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

_STATIC = Path(__file__).resolve().parent / "static"

#: 窗口标题里用的产品名，同时用于页面 <title>
APP_TITLE = "生日提醒"


def _read(name: str) -> str:
    path = _STATIC / name
    if not path.exists():
        raise FileNotFoundError(f"界面资源缺失：{path}")
    return path.read_text(encoding="utf-8")


def _json_for_script(value: Any) -> str:
    """把 Python 对象序列化进 <script> 的安全形式。

    ``</script>`` 出现在字符串里会提前闭合脚本块，必须转义 ——
    收件人姓名、备注都是使用者输入，可能包含任意字符。
    """
    text = json.dumps(value, ensure_ascii=False)
    return text.replace("</", "<\\/").replace("<!--", "<\\!--")


@lru_cache(maxsize=1)
def _assets() -> tuple:
    return _read("style.css"), _read("body.html"), _read("app.js")


def build_html(bootstrap: Optional[Dict[str, Any]] = None) -> str:
    """生成完整页面。

    ``bootstrap`` 是窗口创建前就算好的首屏数据。pywebview 注入 ``js_api``
    需要 2~3 秒，若等前端来拉，使用者会先看到一段空白；把结果直接嵌进页面，
    ``boot()`` 时立即渲染。
    """
    css, body, js = _assets()
    payload = _json_for_script(bootstrap if bootstrap is not None else None)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{APP_TITLE}</title>
<style>
{css}
</style>
</head>
<body>
{body}
<script>
/* 首屏数据：由 Python 在窗口创建前算好并嵌入。
   前端 boot() 会立即用它渲染，不必等 js_api 注入完成。 */
window.__BOOTSTRAP__ = {payload};
</script>
<script>
{js}
</script>
</body>
</html>
"""
