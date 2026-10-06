# -*- coding: utf-8 -*-
"""桌面应用端到端验证：真实开窗口、检查渲染、确认零端口。

用法：
    uv run --with psutil python tools/verify_desktop.py

为什么需要它：单元测试不开窗口，验证不了"窗口能不能起来、渲染对不对、
是否真的不监听端口"。这三件事恰恰是桌面应用的核心承诺。

窗口会自动开启并在验证后关闭，不需要人工操作。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

# 允许直接脚本运行（把项目根加入 sys.path）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402
import webview  # noqa: E402

from src.desktop.api import AppApi  # noqa: E402
from src.desktop.ui import build_html  # noqa: E402

CONFIG = sys.argv[1] if len(sys.argv) > 1 else "config.yml"

#: pywebview 注入 js_api 需要数秒；evaluate_js 在桥接就绪前会阻塞。
#: 这个等待是**探针的等待**，不代表应用渲染慢（首帧由页面自记耗时衡量）。
BRIDGE_WAIT = 6.0

RESULT: dict = {"checks": [], "ok": True}


def check(label: str, actual, expected=None, predicate=None) -> None:
    """记录一项检查结果。"""
    passed = predicate(actual) if predicate else (actual == expected)
    RESULT["checks"].append((label, actual, passed))
    if not passed:
        RESULT["ok"] = False


def own_listen_ports() -> list:
    """本进程正在监听的端口。桌面应用应当是空列表。"""
    me = psutil.Process()
    return sorted(
        c.laddr.port for c in me.net_connections(kind="inet") if c.status == "LISTEN"
    )


def drive(window) -> None:
    time.sleep(BRIDGE_WAIT)

    try:
        check("首帧耗时(页面自记) < 2000ms",
              window.evaluate_js("window.__FIRST_PAINT_MS ?? -1"),
              predicate=lambda v: isinstance(v, (int, float)) and 0 < v < 2000)

        check("页面标题", window.evaluate_js("document.title"), "生日提醒")

        navs = window.evaluate_js(
            "Array.from(document.querySelectorAll('.nav__link')).map(b=>b.textContent)"
        )
        check("导航项", navs, ["时间轴", "添加收件人", "设置"])

        rows = window.evaluate_js("document.querySelectorAll('#timeline-body tr').length")
        check("时间轴有数据行", rows, predicate=lambda v: v > 0)

        check("通知摘要已渲染",
              window.evaluate_js("document.querySelector('#meta').children.length"),
              predicate=lambda v: v > 0)

        # --- 设置页 ---
        window.evaluate_js("document.querySelector('[data-view=\"settings\"]').click()")
        time.sleep(1.2)
        check("设置页可见",
              window.evaluate_js("!document.querySelector('#view-settings').classList.contains('hidden')"),
              True)
        check("接收邮箱已回填",
              window.evaluate_js("document.querySelector('#s-receive').value"),
              predicate=lambda v: bool(v))
        check("密钥打码显示",
              window.evaluate_js("document.querySelector('#s-key-mask').textContent"),
              predicate=lambda v: "..." in v or v == "")

        # --- 日期校验 ---
        window.evaluate_js("document.querySelector('[data-view=\"form\"]').click()")
        time.sleep(0.8)

        window.evaluate_js(
            "var i=document.querySelector('#f-solar'); i.value='1990-01-20';"
            "i.dispatchEvent(new Event('input',{bubbles:true}));"
        )
        time.sleep(1.8)
        check("合法日期 -> 有效",
              window.evaluate_js("document.querySelector('#solar-status').getAttribute('data-state')"),
              "ok")
        check("合法日期显示农历",
              window.evaluate_js("document.querySelector('#lunar-note').innerText"),
              predicate=lambda v: "腊月" in v or "农历" in v)

        window.evaluate_js(
            "var i=document.querySelector('#f-solar'); i.value='1990-01-32';"
            "i.dispatchEvent(new Event('input',{bubbles:true}));"
        )
        time.sleep(1.8)
        check("非法日期 -> 不合理",
              window.evaluate_js("document.querySelector('#solar-status').getAttribute('data-state')"),
              "bad")
        check("非法日期不显示农历",
              window.evaluate_js("document.querySelector('#lunar-note').innerText"),
              predicate=lambda v: "不合理" in v)

        # --- JS 错误 ---
        errs = window.evaluate_js("window.__errs || []")
        check("无 JS 报错", errs, [])

        # --- 零端口（核心承诺）---
        ports = own_listen_ports()
        check("不监听任何端口", ports, [])

    except Exception as exc:  # 探针自身出错也要如实报告
        RESULT["ok"] = False
        RESULT["checks"].append(("探针异常", f"{type(exc).__name__}: {exc}", False))
    finally:
        time.sleep(0.3)
        try:
            window.destroy()
        except Exception:
            pass


def main() -> int:
    api = AppApi(CONFIG)
    html = build_html(api.get_timeline())

    window = webview.create_window(
        "生日提醒 · 验证",
        html=html,
        js_api=api,
        width=1180,
        height=780,
        min_size=(900, 620),
    )

    threading.Thread(target=drive, args=(window,), daemon=True).start()
    # 与生产一致：不起内置 HTTP 服务
    webview.start(http_server=False, debug=False)

    print()
    print("=" * 62)
    print("桌面应用端到端验证")
    print("=" * 62)
    for label, actual, passed in RESULT["checks"]:
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {label}: {actual!r}")
    print("-" * 62)
    print("结果:", "全部通过" if RESULT["ok"] else "有失败项")
    print("=" * 62)
    return 0 if RESULT["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
