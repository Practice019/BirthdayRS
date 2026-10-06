# BirthdayRS 部署与改动记录（Docker）

## 当前状态

| 项 | 值 |
|---|---|
| 代码 | `/root/project/BirthdayRS`（clone 自 github.com/Practice019/BirthdayRS） |
| 镜像 | `birthdayrs:web`（254 MB，构建自本仓库 Dockerfile，`INSTALL_WEB=1`） |
| 容器 | `birthdayrs-web`，`--restart unless-stopped` |
| 端口 | **8000**（宿主机 8000 空闲；8089/8090/8091/9000 被其他容器占用） |
| 访问 | `http://<主机>:8000/?token=<令牌>` |
| 令牌 | 经 `BIRTHDAYRS_TOKEN` 固定（**实际值不入库**，见下方说明） |
| 数据目录 | `/root/project/BirthdayRS/data/` → 容器内 `/app/data/` |
| 时区 | `TZ=Asia/Shanghai`（农历/干支/"今天"的判断依赖它） |
| 通知渠道 | **邮件（Resend）+ 手机推送（WxPusher）**，两者都会发 |

## 关于令牌与密钥

**本文件不记录任何真实凭据。** 下面命令里的令牌与各渠道密钥都是占位符，
实际值只存在于服务器上的 `data/config.yml`（已被 `.gitignore` 排除）与容器环境变量里。

启动时如果没给 `BIRTHDAYRS_TOKEN`，程序会随机生成一个并打印在日志里，
用 `docker logs <容器名> | grep token` 取。

## 启动命令

```bash
cd /root/project/BirthdayRS
docker run -d --name birthdayrs-web --restart unless-stopped \
  -e TZ=Asia/Shanghai \
  -e BIRTHDAYRS_TOKEN=<你启动时生成的令牌> \
  -w /app/data \
  -v /root/project/BirthdayRS/data:/app/data \
  -p 8000:8000 \
  birthdayrs:web web --config /app/data/config.yml --host 0.0.0.0 --port 8000
```

取访问地址（不设 `BIRTHDAYRS_TOKEN` 时令牌每次启动随机生成）：

```bash
docker logs birthdayrs-web | grep token
```

重新构建镜像：

```bash
cd /root/project/BirthdayRS
DOCKER_CONFIG=/root/project/.docker-config \
  docker build --build-arg INSTALL_WEB=1 -t birthdayrs:web .
```

> `DOCKER_CONFIG` 必须指向可写目录：容器内的 `/root/.docker` 是只读的，
> 默认 buildx 会因 `read-only file system` 直接失败。

跑测试 / lint（宿主机只有 Python 3.6，项目要求 3.10+，所以走容器）：

```bash
/root/project/run-tests.sh pytest -q               # 173 passed
/root/project/run-tests.sh flake8 src tests tools  # 干净
docker run --rm -v /root/project/BirthdayRS:/app -w /app python:3.12-slim \
  /app/.venv/bin/python tools/verify_docker.py
```

## 本次改动

### 1. 新增：访问令牌鉴权（`src/web/auth.py`）

管理台能改配置（里面有 Resend API Key）、能触发真实发信，之前**完全没有鉴权**，
绑 `0.0.0.0` 时任何能连到端口的人都能操作。现在默认开启，行为对齐 dsh 的 web UI：

- 启动时生成随机令牌（`--token` / `BIRTHDAYRS_TOKEN` 可固定），打印完整访问 URL。
- 首次访问 `?token=...` → 校验通过 → 种 30 天 `HttpOnly` cookie → **303 跳到去掉
  token 的干净地址**（不让令牌留在地址栏与浏览器历史里）。
- 之后凭 cookie 直接访问。无令牌/令牌错误 → 401 提示页。
- `/static/*` 放行：只是 CSS/JS，且拒绝页要靠它渲染。
- 比较用 `secrets.compare_digest`，避免时序侧信道。

桌面端与测试走 `create_app(config)`（不传 token）→ 不鉴权，既有用法不受影响；
只有命令行 `web` 命令会总是传令牌。

### 2. 修复：默认提前天数三处口径不一致（真实 bug）

**症状**：设置页填了 1 天，时间轴仍显示 3 天，而且 `run` 真按 3 天发。

**根因**：`get_notification_summary()` 取值写死 `smtp` 优先，而设置页读写的都是
`resend`；`Config.from_yaml()`（`run` 用的路径）同样写死 smtp 优先。配置里
`config.example.yml` 自带 `smtp.default_reminder_days: 3`，于是永远盖住 resend 的 1。

**修法**：新增 `effective_default_reminder_days()`，按 **`start_notification` 的
渠道顺序**取值，三处（列表页 / 设置页 / `run`）统一走它。

### 3. 新增：收件人的"提前几天提醒"留空 = 继承全局默认

原来新增收件人时表单会预填全局默认值并写进配置，等于把当时的默认值**固化**到这个人
身上 —— 之后改全局默认对他再也不生效。现在：

- 表单默认留空，只把全局值放在 `placeholder` 与提示文案里。
- 留空提交 → 配置里**不写** `reminder_days` 键 → 运行时继承全局默认。
- 显式填 `0` → 写 `reminder_days: 0`（"只在当天提醒"，与"不设置"是两回事）。
- 时间轴上继承来的显示 `1 天` + `默认` 标记，与单独设置的区分开。

桌面端（`body.html` / `app.js` / `api.py`）同步了同样的口径。

### 4. 修复：compose/Dockerfile 的单文件挂载缺陷

仓库的 compose 把**单个文件**挂进容器：

```yaml
- ./config.yml:/app/config.yml
```

而 `ConfigRepository._save()` 用「临时文件 + `os.replace()`」做原子替换
（`src/web/repository.py`），bind mount 的单文件无法被 rename 覆盖：

```
OSError: [Errno 16] Device or resource busy:
'/app/.config-2c_mnty6.yml.tmp' -> '/app/config.yml'
```

表现为管理台里**增删改收件人一律 500**。已改为挂载整个 `data/` 目录
（配置放 `data/config.yml`），并用 `working_dir: /app/data` 让日志一起持久化。
`tools/verify_docker.py` 增加了断言，防止这个坑回归。

### 5. 新增：手机推送渠道（WxPusher）

在原有的邮件渠道之外，接入 [WxPusher](https://wxpusher.zjiecode.com) 极简推送（SPT），
与邮件**同时发送**。新增 `src/notification/sender_wxpusher.py`，实现 `NotificationBase`，
复用 `render_plain_text()`（与 ServerChan 共用一份文案，换渠道内容不变）。

**为什么选 WxPusher 的 App 通道，而不是它的微信通道**：WxPusher 的微信侧走
ClawBot，每次激活只有 24 小时有效、每 10 条需手动重新激活；生日提醒一个月可能才
触发一次，会**静默失效**（接口返回成功，人收不到）。App 通道没有这个限制。

**设置页现在按「让项目能正常发提醒所必需」来组织**，分四个分组：
用哪些方式提醒我（渠道勾选）、手机推送、邮件、提醒时机。
密钥类字段一律只显示打码值，留空 = 不修改，要清空须显式勾选。

**随之修掉的两个真问题**：

1. **「测试发送」被邮箱检查拦死**（`src/web/app.py`、`src/desktop/api.py`）：
   原来无论启用什么渠道，都先要求 `recipient.email` 存在，否则直接返回
   "没有可用的收件邮箱" —— 而推送渠道根本没有"收件邮箱"这个概念，
   于是永远走不到发送那一步。现在改成**只在实际要用邮件渠道时才要求邮箱**。
   设置页的测试也从"只走邮件渠道"改成**走全部已启用渠道**：配置页刚改完，
   要验证的是"这套配置整体能不能送到我手上"。

2. **配置缓存导致「改了设置不生效」**：`app.state.config` 在启动时缓存了一份快照，
   而保存设置后只有设置页那几个路由会刷新它 —— 于是收件人页的测试发送仍按旧渠道发。
   实测表现为：界面显示已改成"只开手机推送"，日志里却还在发邮件。
   现在配置不再缓存到 `app.state`，统一经 `ConfigManager.reload()` 取，
   失效逻辑也收口到这一个公开方法（以前各处直接改私有 `_config`）。

3. **空配置段会让配置加载崩溃**（`src/core/config.py`）：清空 API Key 后
   `resend: {}` 会因缺必填字段抛 `TypeError`，整个程序起不来。
   现在空段按"未配置"处理 —— 少一个渠道只是不发那种通知，不该让程序挂掉。

4. **ServerChan 顺手加固**：新增 `sctp` 前缀识别（Server酱³ 端点）、加
   `@retry_on_failure()`、失败信息不再回显原始响应体（含 SendKey，会进日志）。

## 验证结果

鉴权：

```
GET  /                          401   无令牌
GET  /?token=wrong              401   令牌错误
GET  /?token=<正确>             303   Location 已去掉 token；Set-Cookie HttpOnly Max-Age=2592000
GET  / (带 cookie)              200
POST /recipients                401   写操作同样被拦
GET  /static/style.css          200   静态放行
```

提醒天数口径（`start_notification: resend`，resend=1 / smtp=3）：

```
设置页 value="1" · 时间轴「默认提前 1 天」· 表单提示「当前 1 天」
Config.from_yaml 给收件人套的默认值也是 1
```

留空 vs 显式 0：

```
留空提交 → 条目不写 reminder_days      → 时间轴显示 "1 天 默认"
填 0     → 条目写 reminder_days: 0     → 时间轴显示 "0 天"
```

手机推送（真实发送，容器内）：

```
POST /settings/test       → 已通过 2 个渠道发出测试提醒
  Resend:   HTTP 200, id=01a1111c-922e-75f5-b47f-5b00775ace30
  WxPusher: HTTP 200, WxPusher 推送成功: 测试提醒

POST /recipients/0/test-send → 已通过邮件、手机推送把 XXX 的提醒发到 <邮箱> 和 手机
```

渠道切换（回归那个「改了设置不生效」的问题）：

```
只勾选"手机推送" → start_notification: wxpusher
  → 收件人页测试发送：日志里只有 Created wxpusher sender，没有 Resend 请求
改回两个都勾 → start_notification: resend,wxpusher，两个渠道都发出
```

密钥不泄露：设置页只渲染 `SPT_aB...dU40`，完整 SPT 在页面里搜不到。

测试：`193 passed`；flake8 干净；`tools/verify_docker.py` 全 PASS
（唯一 FAIL 是"容器内无 docker 命令"，是该工具在容器里运行的预期结果）。

## 待办：加收件人

两个渠道都已配好并验证过真实发送。**`recipients` 目前为空**，
到 `http(s)://<主机>:8000/recipients/new` 添加即可。

添加时「提前几天提醒」**留空**就是用设置里的默认值（当前 1 天），
只有想给某个人单独设才填。

## 注意事项

- 令牌保护的是"能不能进这个界面"。要在公网暴露，仍建议在反代层再加一道。
  cookie 未加 `Secure`（内网 http 加了就发不出去），挂 https 反代时请在反代层收口。
- 常驻 Web 管理台**不会自动发提醒**。定时发送要另配 cron 跑一次性任务：
  ```bash
  docker run --rm -v /root/project/BirthdayRS/data:/app/data -w /app/data \
    -e TZ=Asia/Shanghai birthdayrs:web run --config /app/data/config.yml
  ```
- 想固定令牌就设 `BIRTHDAYRS_TOKEN`；容器重启后书签与 cookie 才不会失效。
