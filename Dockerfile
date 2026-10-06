# =============================================================================
# BirthdayRS — Docker 部署（网页界面）
#
# 容器里**只能**用网页界面：桌面窗口需要宿主机的 WebView2/WKWebView 与显示器，
# 容器里两者都没有。所以容器部署 = 起一个 HTTP 服务，用浏览器访问。
#
# 因此默认装上 web extra（fastapi/uvicorn）；desktop extra 永远不装 ——
# pywebview 依赖 pythonnet（Windows 专属），在 Linux 容器里装不上，也用不到。
# =============================================================================

FROM python:3.12-slim

# 时区很重要：农历/干支与"今天"的计算都依赖本地日期，
# 容器默认 UTC 会让北京时间凌晨 0-8 点的判断偏一天。
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# 默认装 Web 界面依赖。若只想做"每天定时发一次"、不需要网页，
# 可用 --build-arg INSTALL_WEB=0 把镜像做小。
ARG INSTALL_WEB=1

WORKDIR /app

# uv：用它的 lock 保证依赖可复现
RUN pip install --no-cache-dir uv

# tzdata 让 TZ 真正生效（slim 镜像默认不带时区库）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 先只复制依赖描述文件，让这层能被缓存
COPY pyproject.toml uv.lock ./

# --no-dev：不装 pytest/flake8/sphinx
# 不装 desktop extra：容器里用不到（见文件头说明）
RUN if [ "$INSTALL_WEB" = "1" ]; then \
        uv sync --no-dev --extra web --no-install-project; \
    else \
        uv sync --no-dev --no-install-project; \
    fi

# 再复制源码与模板
COPY src/ ./src/
COPY templates/ ./templates/

# 装项目本身（容器里直接跑源码，不需要 editable）
RUN if [ "$INSTALL_WEB" = "1" ]; then \
        uv sync --no-dev --frozen --extra web; \
    else \
        uv sync --no-dev --frozen; \
    fi

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app

# config.yml 由使用者在运行时挂载进来，不打进镜像：
# 它含 SMTP 授权码与 Resend API Key，烘进镜像会随镜像分发泄露。
#
# 注意：这里**不能**写 `VOLUME ["/app/config.yml"]`。
# Docker 的 VOLUME 指令把路径当**目录**处理，对文件路径会创建一个同名目录；
# 于是不挂载直接 `docker run` 时，/app/config.yml 是个目录，
# 程序读配置会抛 IsADirectoryError。用挂载（-v）注入文件才是对的。
EXPOSE 8000

# 默认起 Web 管理台，浏览器访问 http://<主机>:8000
# 若只想要"跑一次检查就退出"（交给 cron），覆盖命令即可：
#   docker run --rm -v ...:/app/config.yml <image> run --config /app/config.yml
ENTRYPOINT ["/app/.venv/bin/python", "-m", "src.main"]
CMD ["web", "--config", "/app/config.yml", "--host", "0.0.0.0", "--port", "8000"]
