"""桌面界面包。

与 ``src/web`` 的关系：业务逻辑共用（``src/web/domain.py`` 等），
只有传输层与表现层不同 —— 这里用 pywebview 的原生窗口 + js_api，
web 版用 FastAPI + HTTP。
"""

from src.desktop.app import run

__all__ = ["run"]
