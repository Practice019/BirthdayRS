# -*- coding: utf-8 -*-
"""Docker 部署的本地可验证项。

本机没装 Docker 时，仍然可以验证镜像**会不会构建成功、能不能跑起来**的关键前提：

1. 容器路径不依赖 GUI 库（pywebview/pythonnet 在 Linux 装不上）
2. Web 界面在缺少 GUI 库时能正常渲染（容器里只有网页界面）
3. Dockerfile 引用的文件都真的存在且在 .dockerignore 里没被排除
4. compose 与 Dockerfile 的默认命令一致

真正的镜像构建需要 Docker 环境，这里最后会明确提示未验证的部分。

用法：
    uv run python tools/verify_docker.py
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS: list = []


def check(label: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((label, passed, detail))


# ---------------------------------------------------------------- 1. 依赖隔离

def check_gui_isolation() -> None:
    """容器会加载的模块不得依赖 GUI 库。"""
    gui_roots = {"webview", "pythonnet", "clr", "clr_loader"}
    offenders = []

    for path in (ROOT / "src").rglob("*.py"):
        if "desktop" in path.parts:
            continue                      # 桌面模块不会进容器
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in gui_roots:
                        offenders.append(f"{path.relative_to(ROOT)}: {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = (node.module or "").split(".")[0]
                if mod in gui_roots:
                    offenders.append(f"{path.relative_to(ROOT)}: {node.module}")

    check("容器路径不依赖 GUI 库", not offenders, str(offenders) if offenders else "")


def check_extras_split() -> None:
    """pywebview 必须在 desktop extra 里，不能出现在核心依赖。"""
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    core = " ".join(cfg["project"]["dependencies"]).lower()
    desktop = " ".join(cfg["project"].get("optional-dependencies", {}).get("desktop", [])).lower()
    web = " ".join(cfg["project"].get("optional-dependencies", {}).get("web", [])).lower()

    check("pywebview 不在核心依赖", "pywebview" not in core,
          f"核心: {cfg['project']['dependencies']}")
    check("pywebview 在 desktop extra", "pywebview" in desktop)
    check("fastapi 在 web extra", "fastapi" in web)
    check("fastapi 不在核心依赖", "fastapi" not in core)


# ---------------------------------------------------------------- 2. Web 界面

def check_web_ui_without_gui() -> None:
    """容器里只有网页界面，验证它在缺 GUI 库时能渲染。"""
    import builtins

    blocked = {"webview", "pythonnet", "clr", "clr_loader"}
    real_import = builtins.__import__

    def guard(name, *args, **kwargs):
        if name.split(".")[0] in blocked:
            raise ImportError(f"blocked for simulation: {name}")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = guard
    try:
        from fastapi.testclient import TestClient

        from src.web.app import create_app

        app = create_app(str(ROOT / "config.yml"))
        client = TestClient(app)

        for path, needle in [
            ("/", "时间轴"),
            ("/settings", "设置"),
            ("/recipients/new", "身份证出生年月日"),
        ]:
            resp = client.get(path)
            ok = resp.status_code == 200 and needle in resp.text
            check(f"Web {path} 可用（无 GUI 库）", ok, f"HTTP {resp.status_code}")

        resp = client.get("/static/style.css")
        check("Web 静态资源可用", resp.status_code == 200 and len(resp.text) > 1000,
              f"HTTP {resp.status_code}, {len(resp.text)} bytes")
    finally:
        builtins.__import__ = real_import


# ---------------------------------------------------------------- 3. 构建上下文

def _dockerignore_patterns() -> list:
    path = ROOT / ".dockerignore"
    if not path.exists():
        return []
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def check_build_context_files() -> None:
    """Dockerfile 需要的文件必须存在，且不能被 .dockerignore 排除。"""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copied = re.findall(r"^COPY\s+(.+?)\s+\.?/", dockerfile, re.M)

    patterns = _dockerignore_patterns()

    for entry in copied:
        for item in entry.split():
            if item in (".", "./"):
                continue
            target = ROOT / item.rstrip("/")
            exists = target.exists()
            check(f"COPY 源存在: {item}", exists)

            if exists:
                # 被 .dockerignore 排除会让构建时缺文件
                name = item.rstrip("/").split("/")[-1]
                excluded = any(
                    pat.rstrip("/") == item.rstrip("/") or pat == name
                    for pat in patterns
                )
                check(f"COPY 源未被 .dockerignore 排除: {item}", not excluded,
                      "" if not excluded else f"被这些规则命中: {patterns}")


def check_web_templates_included() -> None:
    """网页界面依赖 templates/web —— 它绝不能被排除，否则容器里页面直接崩。"""
    patterns = _dockerignore_patterns()
    excluded = any("templates/web" in p for p in patterns)
    check("templates/web 未被 .dockerignore 排除", not excluded,
          "" if not excluded else "网页界面会因缺模板而崩溃")

    files = list((ROOT / "templates" / "web").glob("*.html"))
    check("templates/web 里有模板文件", len(files) > 0, f"{len(files)} 个")


# ---------------------------------------------------------------- 4. 配置一致性

def check_default_command() -> None:
    """Dockerfile 默认应是 web（容器里只能走网页），且端口/主机与 EXPOSE 一致。"""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    m = re.search(r'^CMD\s+\[(.+?)\]', dockerfile, re.M)
    check("Dockerfile 有 CMD", bool(m))
    if m:
        cmd = m.group(1)
        check("默认命令是 web（容器里用网页界面）", '"web"' in cmd, cmd)
        check("默认监听 0.0.0.0（容器外可访问）", "0.0.0.0" in cmd, cmd)
        check("EXPOSE 与命令端口一致",
              "EXPOSE 8000" in dockerfile and "8000" in cmd)


def check_compose_matches_dockerfile() -> None:
    """compose 里 web 服务的端口要与 EXPOSE 一致。"""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    check("compose 有 web profile", "profiles:" in compose and "web" in compose)
    check("compose 映射 8000 端口", "8000:8000" in compose)
    check("compose 用挂载注入 config.yml（不打进镜像）",
          "./config.yml:/app/config.yml" in compose)
    check("compose 设置时区", "TZ" in compose)


def check_install_web_arg() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    check("Dockerfile 有 INSTALL_WEB 构建参数", "ARG INSTALL_WEB" in dockerfile)
    check("默认装 web extra", "ARG INSTALL_WEB=1" in dockerfile)
    check("永不安装 desktop extra",
          "desktop" not in re.sub(r"#.*", "", dockerfile))


def check_docker_available() -> None:
    """本机是否有 Docker —— 决定能不能真正构建。"""
    import shutil

    docker = shutil.which("docker")
    RESULTS.append(("__docker__", bool(docker), docker or "未安装"))
    check("本机已安装 Docker（否则无法实际构建镜像）", bool(docker),
          docker or "未安装：需在有 Docker 的机器上执行 docker compose build")


# ---------------------------------------------------------------- 入口

def main() -> int:
    check_gui_isolation()
    check_extras_split()
    check_web_ui_without_gui()
    check_build_context_files()
    check_web_templates_included()
    check_default_command()
    check_compose_matches_dockerfile()
    check_install_web_arg()
    check_docker_available()

    print()
    print("=" * 66)
    print("Docker 部署 · 本地可验证项")
    print("=" * 66)

    failed = 0
    docker_ok = True
    for label, passed, detail in RESULTS:
        if label == "__docker__":
            docker_ok = passed
            continue
        mark = "PASS" if passed else "FAIL"
        if not passed:
            failed += 1
        suffix = f"  ({detail})" if detail else ""
        print(f"  [{mark}] {label}{suffix}")

    print("-" * 66)
    print(f"结果: {'全部通过' if failed == 0 else f'{failed} 项失败'}")
    if not docker_ok:
        print()
        print("未验证的部分（本机没有 Docker）：")
        print("  · 镜像能否真正构建")
        print("  · 容器能否启动并对外提供网页")
        print("  在有 Docker 的机器上执行：")
        print("    docker compose build")
        print("    docker compose up -d birthdayrs-web")
        print("    浏览器打开 http://localhost:8000")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
