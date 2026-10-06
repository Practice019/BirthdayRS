#!/bin/bash
# deploy.sh — 快速部署本地改动
#
# 为什么需要它：Dockerfile 把 src/ 与 templates/ COPY 进了镜像，于是每次改
# 一行代码都要重新构建一层（1-2 分钟）。但 Python 代码与模板**不需要烘进镜像**
# —— 直接挂载进去即可，改完重启容器就生效（几秒）。
#
# 两种模式：
#   ./deploy.sh         挂载模式（默认）：代码从宿主机挂入，秒级生效。
#                       适合日常改代码。
#   ./deploy.sh --build 重建镜像：把代码 COPY 进镜像。只有在改动了
#                       Dockerfile / pyproject.toml / uv.lock（依赖变化）
#                       时才需要，否则不必。
#
# 注意：挂载模式下**镜像里的代码被宿主机覆盖**，所以它不验证"构建出来的镜像
# 能不能跑"。提交前或改依赖后要跑一次 --build 确认。
set -e

cd "$(dirname "$0")"

NAME=birthdayrs-web
IMAGE=birthdayrs:web
TOKEN="${BIRTHDAYRS_TOKEN:-RXaEvhLy-Yh8Ic2gWUef45xKrs8J9zJr8BqBzyhfuqw}"
PORT="${PORT:-8000}"
DATA="$(pwd)/data"

if [ "$1" = "--build" ]; then
  echo "==> 重建镜像（依赖或 Dockerfile 有变动时才需要）"
  DOCKER_CONFIG=/root/project/.docker-config \
    docker build --build-arg INSTALL_WEB=1 -t "$IMAGE" . 2>&1 | tail -3
  MOUNTS=""
  echo "==> 用镜像内的代码启动"
else
  echo "==> 挂载模式：代码从宿主机挂入，改完即生效"
  MOUNTS="-v $(pwd)/src:/app/src -v $(pwd)/templates:/app/templates"
  echo "    （不重建镜像。改动 Dockerfile / 依赖时才加 --build）"
fi

docker rm -f "$NAME" >/dev/null 2>&1 || true

# shellcheck disable=SC2086
docker run -d --name "$NAME" --restart unless-stopped \
  -e TZ=Asia/Shanghai \
  -e BIRTHDAYRS_TOKEN="$TOKEN" \
  -w /app/data \
  -v "$DATA:/app/data" \
  $MOUNTS \
  -p "$PORT:8000" \
  "$IMAGE" web --config /app/data/config.yml --host 0.0.0.0 --port 8000 >/dev/null

# 等它真的起来，而不是盲等固定秒数
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/?token=$TOKEN" || true)
  if [ "$code" = "200" ] || [ "$code" = "303" ]; then
    echo "==> 就绪 (HTTP $code)  http://127.0.0.1:$PORT/?token=$TOKEN"
    exit 0
  fi
  sleep 0.5
done

echo "!! 启动超时，看日志：docker logs $NAME"
docker logs --tail 20 "$NAME" 2>&1
exit 1
