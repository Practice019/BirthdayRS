# BirthdayRS · 生日提醒系统

[![CI](https://github.com/Practice019/BirthdayRS/actions/workflows/ci.yml/badge.svg)](https://github.com/Practice019/BirthdayRS/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Ruff](https://img.shields.io/badge/code%20style-flake8-orange.svg)](.flake8)

支持**农历与阳历**双历法的生日提醒系统。只需填身份证上的出生年月日，农历生日自动换算，
到时间通过邮件或微信推送提醒你。

> 提醒是发给**你自己**的，不是发给过生日的人。这是一个"别忘了别人生日"的工具。

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [功能](#功能)
- [快速开始](#快速开始)
- [三种使用方式](#三种使用方式)
- [配置说明](#配置说明)
- [通知渠道](#通知渠道)
- [命令行](#命令行)
- [开发](#开发)
- [部署](#部署)
- [常见问题](#常见问题)
- [许可证](#许可证)

---

## 它解决什么问题

农历生日每年对应的阳历日期都不同（2026 年的正月初五是 2 月 17 日，2027 年是 2 月 10 日），
靠脑子记必然出错，靠日历 App 又大多只支持阳历。

这个项目做的事很简单：**你只填一次身份证上的出生年月日，之后每年自动算、自动提醒。**

## 功能

| 功能 | 说明 |
|---|---|
| 双历法 | 阳历与农历同时支持，取两者中**更近**的那次生日提醒 |
| 自动换算 | 只填阳历生日，农历由程序推导。不需要查万年历 |
| 农历闰月 | 闰月按绝对月份记录，保证这些人每年都有提醒 |
| 传统信息 | 生肖、干支纪年/月/日/时、节气、节日、星座、星期 |
| 三种通知 | Resend（HTTP API）、SMTP 邮件、[ServerChan](https://sct.ftqq.com/) 微信推送 |
| 桌面应用 | 双击启动，原生窗口，**不占用任何端口** |
| 网页管理台 | 时间轴总览、增删改、提醒预览、测试发送 |
| 容器部署 | 多阶段构建，镜像里只装运行必需的依赖 |

## 快速开始

需要 **Python 3.10+**。推荐用 [uv](https://docs.astral.sh/uv/)（也支持 pip）。

```bash
git clone https://github.com/Practice019/BirthdayRS.git
cd BirthdayRS

# 安装依赖
uv sync --all-extras        # 或：pip install -e ".[desktop,web]"

# 生成自己的配置
cp config.example.yml config.yml
# 编辑 config.yml，至少填上你的邮箱和 API Key

# 试跑一次，确认能发出提醒
uv run python -m src.main run --config config.yml
```

`config.yml` 里只需要改三处：通知渠道的密钥、接收提醒的邮箱、收件人名单。

## 三种使用方式

### 1. 桌面应用（推荐日常使用）

```bash
uv run python -m src.main app --config config.yml
```

Windows 上直接双击项目根目录的 **`BirthdayRS.bat`**。

会打开一个原生窗口。**它不监听任何端口** —— 界面直接加载进窗口，前后端在进程内通信，
网络上没有任何可访问的东西。

| 页面 | 作用 |
|---|---|
| 时间轴 | 每个人的下一次生日、倒计时、是否在提醒窗口内 |
| 添加收件人 | 姓名 + 身份证出生年月日 + 提醒天数 + 备注，农历自动换算 |
| 设置 | 接收邮箱、发件人名字、API Key、默认提前天数 |
| 预览 / 测试发送 | 看到实际会发出的内容，或立刻真发一封 |

> Windows 需要 **WebView2 运行时**（Windows 11 与较新的 Windows 10 已预装）。
> 日志在 `~/.birthdayrs/app.log`。

### 2. 网页管理台

```bash
uv run python -m src.main web --config config.yml   # http://127.0.0.1:8000
```

同一个界面的浏览器版本，**会占用端口**，适合放在局域网或服务器上。

> ⚠️ **它没有身份验证**。默认只绑定 `127.0.0.1`。若要对局域网开放，
> 请自己加一层访问控制（反向代理 + Basic Auth 之类）。

### 3. 只跑定时任务（服务器场景）

```bash
uv run python -m src.main run --config config.yml
```

跑一次、检查、发送、退出。交给 cron / 任务计划程序 / 容器编排来定时调用。

## 配置说明

```yaml
notification:
  # 只需配一种渠道，三者可共存（逗号分隔）
  resend:
    api_key: re_your_api_key_here
    from_email: onboarding@resend.dev
    from_name: 生日提醒                # 收件人邮箱里显示的发件人名字
    default_receive_email: you@example.com
    default_reminder_days: 3

  smtp:
    host: smtp.example.com
    port: 587
    username: your_email@example.com
    password: your_password           # QQ 邮箱要用"授权码"，不是登录密码
    default_receive_email: you@example.com
    default_reminder_days: 3

  serverchan:
    default_sckey: your_serverchan_sckey
    default_reminder_days: 3

  start_notification: resend          # resend / email / serverchan

recipients:
  - name: 张三
    solar_birthday: 1990-01-20        # 身份证上的出生日期
    lunar_birthday: 1989-12-24        # 由上一行自动换算，人工无需维护
    reminder_days: 3                  # 提前几天，0 = 当天
    note: 大学同学                     # 只给自己看的备注
```

### 两个关键约定

**`solar_birthday` 是唯一的数据来源。** 网页与桌面界面都从它推导农历。
手写的 `lunar_birthday` 与推导结果不一致时，界面会标出来并提示。

**`lunar_birthday` 存的是农历年月日，不是阳历日期。** 例如 `1989-12-24` 表示
**农历腊月廿四**。程序拿它直接与农历月/日比较。这一点极易误解 —— 早期版本的
示例注释写成"对应的阳历日期"，照那个填必然填错，现已更正。

**闰月按绝对月份记录**，所以闰月出生的人每年都会收到提醒（而不是只在有闰月的那年）。
这是有意的取舍，详见 [DESIGN.md](DESIGN.md)。

## 通知渠道

### Resend（推荐）

最省事的方式，注册后拿一个 API Key 即可，不用配 SMTP。

1. 在 [resend.com/api-keys](https://resend.com/api-keys) 创建 Key
2. 填进 `resend.api_key`

**限制**：没有验证自有域名时，`from` 只能是 `onboarding@resend.dev`，
且**只能发到 Key 持有者自己的邮箱**。要发到任意地址，需先在
[resend.com/domains](https://resend.com/domains) 验证域名。

发件人可以自定义显示名（`from_name`），这样收件人看到的是「生日提醒」而不是
「onboarding」。

### SMTP

用你自己的邮箱服务商。QQ 邮箱需要在设置里开启 SMTP 并生成**授权码**，
不能直接用登录密码。

### ServerChan

[ServerChan](https://sct.ftqq.com/) 微信扫码登录拿到 SENDKEY，推送直接到微信。
不需要域名，适合只想在手机上收提醒的场景。

## 命令行

```bash
python -m src.main <命令> [选项]

  app        启动桌面应用（原生窗口，无端口）
  web        启动网页管理台（占用端口）
  run        运行一次检查并发送
  validate   校验配置文件
  info       显示当前配置概览
  preview    预览邮件内容（生成 HTML 文件到 previews/）
```

所有命令都接受 `-c/--config` 指定配置文件路径，默认 `config.yml`。

## 开发

```bash
uv sync --all-extras        # 全部依赖

uv run pytest -q            # 测试
uv run flake8 src tests     # 代码风格

# 端到端验证（会真的开窗口，检查渲染与"不监听端口"）
uv run --with psutil python tools/verify_desktop.py
```

依赖按用途分组（见 `pyproject.toml`）：

| extra | 内容 | 用途 |
|---|---|---|
| （核心） | jinja2, lunar_python, httpx, aiosmtplib, ruamel.yaml, click | CLI 与容器 |
| `desktop` | pywebview | 桌面应用 |
| `web` | fastapi, uvicorn, python-multipart | 网页管理台 |
| `dev` | pytest, flake8, psutil | 测试 |
| `docs` | sphinx, furo | 文档 |

> `pywebview` 特意**不放在核心依赖**：它依赖 `pythonnet`（Windows 专属），
> 放进核心会让 Linux 容器和 CI 装不上。核心依赖里只有 PyYAML 含原生扩展。

## 部署

### Docker（网页界面）

容器里没有显示器和 WebView2，桌面窗口跑不起来，所以容器部署 = 网页界面。

```bash
docker compose build
docker compose up -d birthdayrs-web     # 打开 http://localhost:8000
```

只要定时发送、不需要网页（镜像更小）：

```bash
docker build -t birthdayrs .
docker run --rm -v $PWD/config.yml:/app/config.yml \
  -e TZ=Asia/Shanghai birthdayrs run --config /app/config.yml
```

**`config.yml` 必须用挂载注入，不要打进镜像** —— 它含 API Key 与授权码。

> 不要写 `VOLUME ["/app/config.yml"]`：Docker 的 `VOLUME` 把路径当**目录**，
> 对文件路径会创建同名目录，程序读配置会抛 `IsADirectoryError`。

### 定时执行

任选一种：

- **服务器 cron**：`docker run --rm ... birthdayrs run --config /app/config.yml`
- **Windows 任务计划程序**：调用 `BirthdayRS.bat` 或 `python -m src.main run`
- **systemd timer**：写个 oneshot service + timer

> 时区很重要：农历与"今天"的判断依赖本地日期。容器默认 UTC，
> 会让北京时间凌晨 0–8 点的判断偏一天，所以 compose 里设了 `TZ=Asia/Shanghai`。

## 常见问题

**农历生日填错了会怎样？**
界面上会标出"配置里的农历值与推算不符，已按阳历计算"。提醒仍按阳历走，不会漏。

**为什么提醒发给我自己，不是发给过生日的人？**
设计如此。这是给自己用的备忘工具。要送给本人，把 `default_receive_email`
改成对方的邮箱即可。

**`run` 为什么什么都不发？**
`run` 只在提醒窗口内发送 —— 即"距离生日 ≤ `reminder_days` 天"。
如果最近的人生日还有 100 天，那它什么都不做是正常的。
想立刻验证，用桌面/网页界面里的**测试发送**按钮。

**网页界面安全吗？**
没有身份验证，默认只绑本机。对外暴露前请自行加访问控制。

**容器里能跑桌面应用吗？**
不能。需要宿主机的 WebView2/WKWebView 和显示器。容器只用网页界面。

## 许可证

[MIT](LICENSE) © 2025 cszhang

本项目基于 [wllzhang/BirthdayRS](https://github.com/wllzhang/BirthdayRS) 二次开发，
在此致谢。MIT 许可证要求保留原始版权声明，请勿删除 `LICENSE` 中的署名。
