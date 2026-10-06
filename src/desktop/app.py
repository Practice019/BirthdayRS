"""桌面应用入口：开一个原生窗口，不起任何网络服务。

**零端口**是这个模块的核心约束：

- ``webview.start(http_server=False)`` —— 不起 pywebview 的内置 HTTP 服务
- 页面用 ``html=`` 直接塞进窗口，不用 ``url=``（后者会触发内置服务）
- 前后端通过 ``js_api`` 在进程内直接调用

实测：进程 ``net_connections`` 为空，即不监听任何 socket。

界面由 ``src/desktop/ui.py`` 生成（单文件 HTML，内联 CSS/JS），
不放静态文件、不依赖外部 CDN —— 分发时整个应用只需 Python 解释器。
"""

from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

WINDOW_TITLE = "生日提醒"
WINDOW_MIN_SIZE = (900, 620)


def _setup_logging() -> Optional[Path]:
    """把日志写到用户目录，出错时便于排查（窗口里看不到控制台）。"""
    log_dir = Path.home() / ".birthdayrs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "app.log"
    except OSError:
        return None

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8")],
    )
    return log_file


def _show_fatal(title: str, message: str) -> None:
    """启动失败时给出可见的提示。

    桌面应用没有控制台，异常如果只打到日志，使用者只会看到窗口一闪而过。
    """
    try:
        import webview

        webview.create_window(
            title,
            html=(
                "<html><head><meta charset='utf-8'></head>"
                "<body style=\"font-family:'Microsoft YaHei',sans-serif;"
                "padding:32px;line-height:1.8;color:#1f2328\">"
                f"<h2 style='color:#cf222e;margin-top:0'>{title}</h2>"
                f"<pre style='white-space:pre-wrap;background:#f6f8fa;"
                f"padding:16px;border-radius:6px;font-size:13px'>{message}</pre>"
                "<p style='color:#59636e;font-size:13px'>"
                "详细日志：~/.birthdayrs/app.log</p>"
                "</body></html>"
            ),
            width=680,
            height=420,
        )
        webview.start(http_server=False)
    except Exception:
        # 连报错窗口都开不起来，只能打到 stderr
        print(f"{title}\n{message}", file=sys.stderr)


def run(config_path: Optional[str] = None) -> int:
    """启动桌面应用。返回进程退出码。"""
    log_file = _setup_logging()

    try:
        import webview
    except ImportError:
        _show_fatal(
            "缺少依赖",
            "桌面应用需要 pywebview。\n\n请运行：uv sync\n"
            "或：pip install pywebview",
        )
        return 1

    try:
        from src.desktop.api import AppApi
        from src.desktop.ui import build_html

        api = AppApi(config_path)

        # 首屏数据在窗口创建前就算好：pywebview 注入 js_api 要等 2~3 秒，
        # 若等前端来拉，使用者会先看到一段空白。直接把结果带进页面，
        # 前端 boot() 时立即用它渲染。
        try:
            bootstrap = api.get_timeline()
        except Exception as exc:
            logger.exception("预取时间轴失败")
            bootstrap = {"ok": False, "error": f"读取数据失败：{exc}"}

        html = build_html(bootstrap)
    except Exception as exc:
        logger.exception("初始化失败")
        _show_fatal("启动失败", f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}")
        return 1

    # 窗口由 pywebview 自己持有，这里不需要保留引用
    webview.create_window(
        WINDOW_TITLE,
        html=html,
        js_api=api,
        width=1180,
        height=780,
        min_size=WINDOW_MIN_SIZE,
        text_select=True,
    )

    if log_file:
        logger.info("桌面应用启动，日志：%s", log_file)

    # http_server=False：不起内置 HTTP 服务，进程不监听任何端口。
    webview.start(http_server=False, debug=False)
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
