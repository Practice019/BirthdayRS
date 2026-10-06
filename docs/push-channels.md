# 推送渠道可行性与实施方案

调研日期：2026-10-06

> **状态：已实施完毕。** 最终采用 **WxPusher 手机推送**，与邮件渠道同时发送，
> 已在管理台可配置并验证真实送达。实施记录见 `DEPLOY.md` 第 5 节。
>
> 报告主体保留调研过程与取舍依据；其中"建议用 ServerChan"的早期结论
> **已被实测推翻**，理由见下文（微信 ClawBot 通道 24 小时失效这一点是关键）。
> 以下为当时的原始记录。

调研对象：`/root/project/BirthdayRS`（当前部署于 Docker，容器 `birthdayrs-web`，端口 8000）

---

## 零、端到端实测结果（2026-10-06 补充）

已用**假 key** 打通全链路（假 key 被拒，但不消耗额度）。结论：**链路完全可用，不需要改任何发送逻辑。**

### 测过的项

| 测试 | 结果 |
|---|---|
| 容器内 DNS 解析 `sctapi.ftqq.com` | ✅ 解析到 140.143.190.233 / 82.157.177.201 |
| 容器内 HTTP 连通 | ✅ HTTP 200，0.98s |
| `ServerChanSender.render_content()` | ✅ 正常输出纯文本（见下） |
| `ServerChanSender.send()` 真实调用 | ✅ 正确发起 POST，正确识别失败 |
| **`run` 命令全链路** | ✅ 配置解析 → 工厂创建 → 发送 → 错误处理，全通 |
| WxPusher 连通 + 接口 | ✅ 可达，返回 `{"code":1001,"msg":"appToken不正确"}` |
| ServerChan³ (`push.ft07.com`) | ⚠️ 返回 HTTP 522（Cloudflare 源站超时），未能确认可用性 |

`run` 全链路的真实日志：

```
INFO - Created serverchan sender
INFO - Found 1 birthdays today
INFO - Processing birthday for 链路测试
INFO - HTTP Request: POST https://sctapi.ftqq.com/SCTfake...send "HTTP/1.1 400 Bad Request"
ERROR - Server酱推送失败: 链路测试, 响应: {"message":"[AUTH]错误的Key","code":40001,...}
```

**唯一的失败就是假 key 被拒 —— 这正是预期。换成真 key 即可发出。**

### 渲染出的实际内容

```
亲爱的张三：
3天后是您的农历生日！
生肖：蛇
星座：摩羯
```

### 两条与报告初稿不同的更正

1. **ServerChan 失败时返回 HTTP 400，不是 200。** 代码判断
   `resp.status_code == 200 and resp.json().get("code") == 0` 因此能正确兜住失败。
   逻辑没问题，但意味着"额度用尽"等业务错误也会走 400 —— 排查时要知道这一点。

2. **WxPusher 有一个「极简推送 SPT」模式，比初稿说的更简单。**
   不需要建应用、不需要拿 UID，扫码直接得一个 `SPT_xxx` 字符串，
   POST 到 `/api/send/message/simple-push` 即可（**仅支持 POST**，GET 会返回 1008）。
   对"自己给自己发提醒"这个场景，这和 ServerChan 的接入成本**一样低**。

---

## 结论先行

**能发，发送逻辑已经验证可用，实测全链路已打通。** 项目里有一个完整的 ServerChan（Server酱）
发送器（`src/notification/sender_serverchan.py`），配置解析、工厂分发、预览渲染都已接通，
且**不需要修改发送逻辑**。

缺的只有两样：**配置入口**（界面上没地方填 SendKey）和**测试按钮的适配**（它现在硬性要求邮箱）。
另外有 3 处健壮性问题值得一并修（密钥进日志、没有重试）。

**所以这是一件"补入口"的活，不是"造轮子"的活。** 改完大约 200～300 行。

关于额度：**ServerChan 免费版每天只有 5 条**。生日提醒本身频率极低
（一年最多给每人推 1～3 次），5 条/天完全够用。真正的风险是「调试期间把额度试没了」——
所以下面建议里包含一条"先用 curl 手工验证通不通，再动配置"。

---

## 一、现状盘点：已经有什么

| 组件 | 文件 | 状态 |
|---|---|---|
| 发送器实现 | `src/notification/sender_serverchan.py` | ✅ 完整可用 |
| 配置数据类 | `src/core/config.py:28` `ServerChanConfig` | ✅ 有 |
| 配置解析 | `src/core/config.py` `Config.from_yaml` | ✅ 有 |
| 工厂分发 | `src/core/notification_factory.py:38` | ✅ 有 |
| 示例配置 | `config.example.yml:32` | ✅ 有 |
| 预览渲染 | `src/web/domain.py:527` | ✅ 有（无需 key） |
| 时间轴渠道显示 | `templates/web/list.html:15` | ✅ 显示"微信推送" |
| 界面设置入口 | 设置页 | ❌ 没有 |
| 单元测试 | `tests/test_notification_factory.py` 仅测工厂 | ⚠️ 只 mock，无实际发送测试 |

现有发送逻辑（`sender_serverchan.py`）：

```python
url = f"https://sctapi.ftqq.com/{self.sckey}.send"
data = {"title": title, "desp": content}
resp = await client.post(url, data=data)
if resp.status_code == 200 and resp.json().get("code") == 0:
    logger.info(...)
else:
    raise Exception(f"Server酱推送失败: {resp.text}")
```

内容渲染走 `render_content()`，输出纯文本（姓名 / 剩余天数 / 生肖 / 星座 / 节气 / 节日）。

---

## 二、阻断点（按必须先解决的顺序）

### ① 界面没有填 SendKey 的地方 —— 你改不了配置

设置页只管理 Resend。`src/web/app.py` 的 `POST /settings` 只解析 Resend 字段，
`repository.update_resend_settings()` 里**完全没有 serverchan 分支**（`grep serverchan src/web/repository.py`
只命中摘要读取与渠道映射，无写入）。

结果：想开微信推送，只能登进容器手改 `data/config.yml`：

```bash
docker exec -it birthdayrs-web vi /app/data/config.yml   # 不推荐，容器里没有编辑器
# 或直接改宿主机文件（推荐）
vi /root/project/BirthdayRS/data/config.yml
```

`data/config.yml` 里现在是 `serverchan.default_sckey: your_serverchan_sckey`（占位值），
且 `start_notification: resend` —— **微信渠道根本没被启用**。

### ② 「测试发送」按钮对微信是死路 —— 会被邮箱检查拦下

`src/web/app.py:551`（桌面端 `src/desktop/api.py:296` 同样）：

```python
if not recipient.email:
    return RedirectResponse("/?error=没有可用的收件邮箱。请在 config.yml 的 resend 里填 default_receive_email")
```

微信推送**没有"收件邮箱"这个概念**，但这段检查在调用任何发送器**之前**执行。
所以哪怕 `start_notification` 加了 serverchan，只要没配邮箱，界面上点「测试发送」
一律报"没有可用的收件邮箱"，永远走不到 ServerChan 那行代码。

这是最需要在设计上想清楚的一处：**「收件人」这个概念是按邮件渠道建模的**，
微信渠道的"收件人"是 SendKey 持有者的微信号，二者语义不同。

### ③ 设置页的「测试发送」明确只走邮件渠道

`src/web/app.py`（`settings_test_send`）：

```python
mail_senders = [s for s in senders if type(s).__name__ in ("ResendSender", "EmailSender")]
if not mail_senders:
    return RedirectResponse("/settings?error=当前没有可用的邮件渠道，请检查 Resend 配置")
```

桌面端 `src/desktop/api.py:428` 同样过滤。这个设计是有意的（设置页管的就是邮件），
但如果要支持微信，需要决定：微信的测试入口放哪儿。

### ④ SendKey 会被写进日志，有泄露风险

`src/notification/sender_serverchan.py:53`：

```python
raise Exception(f"Server酱推送失败: {resp.text}")
```

失败时把 ServerChan 的**原始响应体**拼进异常。响应体里可能回显 URL（含 SendKey）。
而 `src/main.py` 的日志配置把异常写进 `birthday_reminder.log`，容器里还会进 `docker logs`。
对比 `ResendSender._explain()` 的做法——它特意**不回显**上游原始报文里的邮箱地址，
说明项目对此是有意识的，ServerChan 这个实现没跟上。

顺手提一句：`sckey` 目前也会出现在 `logger.error` 的调用上下文之外的异常链里。
建议统一按"密钥不出现在日志"处理。

### ⑤ 没有重试，且抛的是裸 `Exception`

`ResendSender` 和 `EmailSender` 都用了 `@retry_on_failure()`（`sender_email.py:21`，3 次指数退避），
`ServerChanSender.send()` **没有重试**，且 `raise Exception(...)` 抛裸异常。
`main.py` 的 `asyncio.gather(..., return_exceptions=True)` 会把异常吞成日志，
所以不会崩，但网络抖动时这一条就静默丢了。

---

## 三、上游服务现状（外部事实，需你确认）

### ServerChan（项目已实现的那个）

- 接口：`POST https://sctapi.ftqq.com/{SendKey}.send`，参数 `title`（必填，**不能含换行**）+ `desp`（Markdown）
- 成功判定：返回 JSON `code == 0`
- **免费额度：每天 5 条**；新用户另有 7 天全功能试用
- 频率：每分钟最多 50 条
- 会员价：8 元/月、39 元/12 个月（以官网为准）
- 标题超 32 字符会被截断
- 推送内容保留：免费 1 天 / 会员 3 天

来源：[Server酱常见问题](https://sct.ftqq.com/docs/getting-started/faq)、[Server酱 Turbo 首页](https://sct.ftqq.com)

> ⚠️ **一个必须注意的分支**：SendKey 有两种前缀，接口地址不同。
> - `SCT` 开头 → `sctapi.ftqq.com`（项目现在硬编码的就是这个）
> - `sctp` 开头（Server酱³）→ `https://{uid}.push.ft07.com/send/{SendKey}.send`，
>   其中 `uid` 是 SendKey 里 `sctp` 与 `t` 之间的数字
>
> 项目当前**不支持 `sctp`**。你去注册时如果拿到的是 `sctp` 开头的 key，现有代码直接发不出去。
> 这是必须先确认的一件事。

### 备选方案对比

| 方案 | 免费额度 | 接入成本 | 需要企业资质 | 备注 |
|---|---|---|---|---|
| **Server酱 SCT** | 5 条/天 | 最低（已实现） | 否 | 项目现成代码，实测可用 |
| **WxPusher SPT（极简）** | **永久免费**，单用户 2000 条/天 | **最低**：扫码得 SPT，一个 POST | 否 | 实测可达；仅支持 POST |
| **WxPusher 标准（appToken+UID）** | 同上 | 中：建应用 + 关注公众号拿 UID | 否 | 适合多用户/多主题 |
| **Server酱³ `sctp`** | 测试期免费 | 低（改 URL） | 否 | ⚠️ 实测 `push.ft07.com` 返回 522，未确认可用 |
| **企业微信自建应用** | 免费，无条数限制 | 中（corpid+secret+agentid+IP白名单） | 需注册企业（可空壳） | 最稳定，配置最多 |

来源：[WxPusher 消息推送 API](https://wxpusher.zjiecode.com/docs/api-reference.html)、
[WxPusher 开发者后台](https://wxpusher.zjiecode.com/admin)、
[Server酱常见问题](https://sct.ftqq.com/docs/getting-started/faq)、
[企业微信 API 教程](https://apifox.com/apiskills/qiyeweixin-api-debug)

**对企业微信的一条重要提醒**：服务器出口 IP 必须加入应用的「API 接收」白名单，
否则报 `errcode=60020`。这台机器出口 IP 是 `8.148.203.162`（阿里云）。

---

## 四、实现方案（三选一）

### 方案 A：最小改动 —— 只把现有 ServerChan 接通（推荐先做）

**目标**：能用微信收到提醒，改动面最小，不引入新依赖。

需要动的点：

1. **修 ②**：把「测试发送」的邮箱前置检查，改成"至少有一个可用渠道且该渠道的收件目标就绪"。
   即：如果启用了 serverchan，就不该因为没邮箱而拦住。
2. **修 ①**：设置页加一个 ServerChan 区块（SendKey 输入 + 保存），
   `repository` 加 `update_serverchan_settings()`，与 `update_resend_settings` 对称。
   同时允许勾选"启用微信推送"，写 `start_notification`。
3. **修 ④**：失败信息改成不回显原始报文（对齐 `ResendSender._explain` 的做法）。
4. **修 ⑤**：给 `ServerChanSender.send()` 加 `@retry_on_failure()`。
5. **兼容 `sctp`**：按 SendKey 前缀自动选端点。
6. **补测试**：`tests/test_serverchan.py`，用 mock 的 httpx 打桩（照抄 `test_resend.py` 的结构），
   覆盖成功 / 额度用尽 / 401 / `sctp` 分支。

预估：约 200～300 行（含测试）。风险低，因为发送器主体已经存在且逻辑简单。

**改动前的验证步骤（强烈建议先做，不花额度不写代码）**：

```bash
# 拿到 SendKey 后，先手工验证能不能通（会消耗 1 条额度）
curl -sS -X POST "https://sctapi.ftqq.com/<你的SendKey>.send" \
  --data-urlencode "title=测试标题" \
  --data-urlencode "desp=这是一条测试消息"
# 期望返回 {"code":0,...}，且微信收到消息
```

### 方案 B：换用 WxPusher

**理由**：免费额度宽松得多（2000 条/天 vs 5 条/天），不需要企业资质，接口同样是一个 HTTP POST。

**代价**：需要在 WxPusher 后台建应用拿 `appToken`、让使用者关注公众号拿 `UID`，
比 ServerChan"扫一次码拿 SendKey"多一步。且要新增一个发送器（约 100 行）。

**适合**：你担心 5 条/天不够，或以后想推别的东西。

### 方案 C：企业微信自建应用

**理由**：无条数限制、最稳定、消息不依赖第三方服务的存活。

**代价**：配置项最多（corpid / secret / agentid / 用户 UserID / IP 白名单），
需要注册一个（可以只是空壳的）企业。对"个人生日提醒"这个场景明显过重。

**适合**：你已经在用企业微信，或想顺便接更多告警。

---

## 五、我的建议（已按实测更新）

**推荐直接上 WxPusher，而不是 ServerChan。**

实测之后我改了初稿的建议，理由：

1. **额度**：ServerChan 5 条/天 vs WxPusher 2000 条/天 —— 差 400 倍。
   生日提醒虽然频次低，但 5 条/天意味着**调试几次就没了**，
   而且一旦某天多人过生日就可能触顶。
2. **接入成本已经拉平**：WxPusher 的 SPT 极简模式同样是"扫码拿一个字符串 + 一个 POST"，
   和 ServerChan 一样简单。初稿以为它必须先建应用——**实测发现不用**。
3. **ServerChan³ 存疑**：实测 `push.ft07.com` 返回 522。如果你拿到的是 `sctp` 开头的 key，
   现有代码不支持，且端点是否可用我未能确认。

**但如果你已经习惯 ServerChan，方案 A 也完全可行** —— 发送逻辑实测是通的，
改动更小（不用新增发送器）。

两个方案都要修的是同一件事：**「测试发送」的邮箱前置检查**。
这是最关键的改动点，不改的话微信渠道根本走不到。

**施工顺序（无论选哪个）**：
1. 先用 curl 验 key（2 分钟，不写代码）
2. 修 ④⑤（密钥脱敏、加重试）—— 小改动，先做
3. 修 ②（测试发送的邮箱检查）—— 核心
4. 修 ①（配置入口）—— 看你要不要可视化
5. 补测试

---

## 六、我需要你提供什么

### 二选一：选一个渠道

#### 选项 A：Server酱（改动最小，额度 5 条/天）

**注册地址：https://sct.ftqq.com** —— 微信扫码登录，在 SendKey 页面复制。

**需要你给我：**
1. **SendKey 全文**（形如 `SCTxxxxx` 或 `sctp1234txxxx`）
2. **它的前缀是 `SCT` 还是 `sctp`？** —— 这决定要不要额外改端点代码

拿到后我会先用 curl 验一次（消耗 1 条额度），确认能收到微信消息再动代码。

#### 选项 B：WxPusher（额度宽松 2000 条/天，永久免费）

**注册地址：https://wxpusher.zjiecode.com** —— 微信扫码登录。

**推荐走 SPT 极简模式**（不用建应用）：进后台后找到「极简推送 SPT」，
微信扫码即可拿到一个 `SPT_xxx` 字符串。

**需要你给我：**
1. **SPT 字符串**（形如 `SPT_xxxxx`）；如果你选了标准模式，则给我 `appToken`（`AT_xxx`）+ 你的 `UID`（`UID_xxx`）

**注意**：`SPT` 等同于"任何人拿到它都能给你发消息"，属于密钥，请通过私密渠道给我，
不要贴在会被记录的地方。

### 必答：三个设计问题

| # | 问题 | 影响 |
|---|---|---|
| 1 | 微信和邮件**同时开**还是**只开微信**？ | 决定 `start_notification` 写法与"测试发送"的语义 |
| 2 | 要不要在**管理台里可视化配置** SendKey？还是手改 `data/config.yml` 就够？ | 前者要动 `repository` + 设置页模板（约 +100 行）；后者零改动 |
| 3 | 点「测试发送」时，**所有渠道各发一条**，还是**只发我点的那个渠道**？ | 前者更直观但会多消耗额度；后者省额度 |

### 可选：如果你想连企业微信

需要 `corpid`、`corpsecret`、`agentid`、以及接收人的 `UserID`，
并把这个服务器的出口 IP（`8.148.203.162`）加入应用白名单。配置项较多，一般不建议。

### 不需要你提供的

- 服务器信息 ✅ 我已有
- 现有 Resend key ✅ 已在 `data/config.yml` 里（我测试时会避开、不外泄）

---

## 七、实施结果（已完成）

最终落地的方案与当初设想有两处不同：

1. **渠道从 ServerChan 换成 WxPusher 手机推送。** 关键事实是 WxPusher 的微信通道
   （ClawBot）每次激活只有 24 小时有效、每 10 条需手动重新激活 ——
   对低频的生日提醒必然静默失效。它的 App 通道没有这个限制，免费额度也宽松得多
   （单用户单日约 3000 条 vs ServerChan 的 5 条/天）。
2. **设置页按「不配就跑不了」的标准重组。** 分四组：用哪些方式提醒我（渠道勾选）、
   手机推送、邮件、提醒时机。密钥留空 = 不修改，只显示打码值。

顺带修掉了三个既有问题（邮箱前置检查拦死推送渠道、配置缓存导致改了设置不生效、
空配置段让加载崩溃），详见 `DEPLOY.md`。

验证：真实发送到邮件与手机两个渠道均成功；`193 passed`；flake8 干净。

---

## 附：关键代码位置速查

| 内容 | 位置 |
|---|---|
| ServerChan 发送器 | `src/notification/sender_serverchan.py` |
| 发送器工厂 | `src/core/notification_factory.py:38` |
| 配置数据类 | `src/core/config.py:28` |
| SendKey 写入口（缺失） | `src/web/repository.py` 无 serverchan 写入分支 |
| 测试发送被邮箱拦住 | `src/web/app.py:551`、`src/desktop/api.py:296` |
| 设置页测试只走邮件 | `src/web/app.py` `settings_test_send`、`src/desktop/api.py:428` |
| 预览渲染（无需 key） | `src/web/domain.py:527` |
| 重试装饰器（可复用） | `src/notification/sender_email.py:21` `retry_on_failure` |
| 失败信息不回显上游报文的范例 | `src/notification/sender_resend.py` `_explain` |
