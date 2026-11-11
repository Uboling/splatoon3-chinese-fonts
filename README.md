# WorkBuddy 每日签到 · GitHub Actions 云端部署包

把签到从本机搬到云端：**不依赖 WorkBuddy 客户端开机**，由 GitHub Actions 每天定时调用官方接口完成幂等签到，并把日报推送到微信（WxPusher）。

## 原理

云端没有本机登录态（`workbuddy-desktop.info`），所以走积分助手的**通道 C（长效 `refresh_token` 自续期）**：

```
GitHub Secret (refresh_token)
   └─ POST copilot.tencent.com/v2/plugin/auth/token/refresh  → 短效 access_token
        ├─ POST www.codebuddy.cn/v2/billing/meter/checkin-activity-status  （只读）
        └─ POST www.codebuddy.cn/v2/billing/meter/daily-checkin            （当日未签时，幂等）

执行结束 → POST wxpusher.zjiecode.com/api/send/message  → 微信日报（可选）
```

签到链路只调这三个接口，不碰兑换 / 抽奖 / 旅行等其他写接口。签到当日重复执行返回 `code 10001`，不会重复发奖。

## 目录

| 文件 | 作用 |
|---|---|
| `scripts/cloud_checkin.py` | 纯标准库签到脚本 + WxPusher 日报推送；凭据只从环境变量读，绝不输出 token |
| `.github/workflows/daily-checkin.yml` | 定时 + 手动触发、Secret 注入、令牌轮换回写 |
| `.gitignore` | 防止凭据 / 临时文件误提交 |

---

## 一、一次性准备

### 1. 在本机导出长效令牌（只做一次）

在**你自己的终端**执行（客户端需处于已登录状态）：

```bash
python "C:/Users/徐航/.workbuddy/skills/totorosir-workbuddy-score/scripts/rt_auth.py" --export-rt
```

这条命令会把长效令牌明文打印到你的终端。**只在你本机可见**：不要贴进聊天、代码、issue 或日志。
若希望先落盘、稍后再取，可用 `--export-rt --save`（保存到 `~/.workbuddy/checkin-rt.json`，取其中 `rt` 字段）。

> 长效令牌等同账号钥匙。按积分助手的安全边界，我不会代你导出、也不会在对话里显示它。

### 2. 拿到 WxPusher 的接收者 UID（或主题 ID）

WxPusher 标准推送除 `appToken` 外**必须**指定接收者，二选一：

- `WXPUSHER_UID`：微信里关注你的应用 → 菜单「获取UID」；或管理后台用户列表查看（`GET https://wxpusher.zjiecode.com/api/fun/wxuser/v2?appToken=...&page=1&pageSize=100`）。形如 `UID_xxxx`。
- `WXPUSHER_TOPIC_ID`：在管理后台创建主题并关注，使用数字主题 ID（适合多人接收）。

多个值用逗号分隔。`appToken` 与 UID 都是凭据，只填进 GitHub Secrets，绝不写进代码或提交记录。

### 3. 准备私有仓库并推送

新建或复用一个**私有**仓库，然后把本目录推上去：

```bash
cd "C:/Users/徐航/WorkBuddy/2026-11-11-09-27-57/workbuddy-cloud-checkin"
git init -b main
git add .
git commit -m "feat: WorkBuddy cloud daily check-in"
git remote add origin https://github.com/<你的用户名>/<你的仓库名>.git
git push -u origin main
```

> 若是复用已有仓库（例如旧仓库改造），把旧文件删掉、保留本目录 4 个文件即可；工作流用 `${{ github.repository }}` 动态取仓库名，不依赖目录名。

### 4. 配置 Secrets

仓库 → **Settings → Secrets and variables → Actions → New repository secret**：

| Secret | 必填 | 说明 |
|---|---|---|
| `WORKBUDDY_REFRESH_TOKEN` | ✅ | 第 1 步导出的长效令牌 |
| `WXPUSHER_APP_TOKEN` | ✅（要日报时） | WxPusher 应用 `appToken`（`AT_...`） |
| `WXPUSHER_UID` | ✅ 二选一 | 接收者 UID；与 `WXPUSHER_TOPIC_ID` 填一个即可 |
| `WXPUSHER_TOPIC_ID` | ✅ 二选一 | 数字主题 ID |
| `WXPUSHER_ALWAYS` | ⬜ 可选 | 设为 `1` 时「今日已签」也推送（默认每天一条） |
| `SECRETS_ADMIN_TOKEN` | ⬜ 可选但推荐 | 用于自动回写轮换后的令牌，见下节 |

`SECRETS_ADMIN_TOKEN` 建议用 **fine-grained PAT**：只授权这一个仓库，Repository permissions 只勾 **Secrets: Read and write**，到期时间设最短可用周期。不配也能跑，但令牌轮换后可能需要手动更新 Secret。

### 5. 手动验证一次

**Actions → WorkBuddy 每日签到 → Run workflow**：

- 勾选 `check_only`：只验证链路、不签到，日志出现 `"status": "ok"`。
- 同时勾选 `force_push`：**强制推送一条日报**（忽略「今日已签」去重），专门用来验证 WxPusher 配置；日志应出现 `push.ok = true` 与 `"reason": "已推送：WorkBuddy 签到推送 11月11日 周三 成功"`。
- 两个都不勾：真实签到并按「每天一条」规则推送；日志显示 `clicked`（领取成功）或 `skip_already_signed`（今日已签），两者都算成功。
- 推送结果看 JSON 里的 `push` 字段：`{"enabled": true, "ok": true, "reason": "已推送：WorkBuddy 签到推送 11月11日 周三 成功"}`。

---

## 二、微信日报推送

- **主题（通知栏标题）**：`WorkBuddy 签到推送 11月11日 周三 成功` —— 固定前缀 `WorkBuddy 签到推送` + UTC+8 日期 + 空格 + 是否成功（`成功` / `失败`）；日期含月日与星期，不依赖 GitHub runner 的本地时区。正文抬头与主题一致，正文另带 UTC+8 完整时间戳。
- 各端通知栏通常只显示约 20 字，加前缀后日期 / 结果可能被截断；完整标题始终在消息正文抬头里。
- **内容**：执行结果、状态、本次积分、积分余额、连续签到天数、长效令牌是否轮换。全部不含任何令牌。
- **每天一条**：workflow 一天跑两次做幂等兜底，第二次「今日已签」默认不再重复推送；设 `WXPUSHER_ALWAYS=1` 可关闭去重，手动运行时也可勾选 `force_push` 强制推一条用于验证。
- **失败隔离**：未配置 / 缺 UID / 接口报错都只写进日志与 Step Summary，不影响签到结果与退出码。
- 使用 `contentType=2`（HTML）；`summary` 为通知栏标题（接口上限 100 字，各端约显示 20 字，故保持简短）。
- 成功判定：接口返回 `code=1000` 或 `success=true`（仅代表任务已受理）。

## 三、定时与令牌轮换

- **cron 用 UTC**：workflow 里两条 cron 分别是北京 **09:17** 和 **13:17**。避开整点可降低 GitHub 排队延迟；双跑是幂等兜底，不需要可删掉第二条。
- **60 天休眠**：GitHub 会停用连续 60 天无任何活动的仓库的定时 workflow。偶尔手动触发一次、或 push 一次即可恢复。
- **令牌轮换是关键坑**：刷新接口每次可能返回新的 `refresh_token`，而 Actions 容器没有持久盘。脚本在刷新成功后**立刻**把新令牌写到 `$RUNNER_TEMP/rotated_rt.txt`（0600），workflow 再用 `gh secret set` 从 stdin 回写到同名 Secret（值不进日志），最后删除临时文件。
  - 配了 `SECRETS_ADMIN_TOKEN` → 全自动续期，长期免维护。
  - 没配 → 若服务端真的作废旧令牌，第二天会报「令牌刷新失败」；此时重新执行第 1 步导出并更新 Secret 即可。脚本会在摘要里明确标出「长效令牌轮换：是」。
- 回写 step 用 `if: always()`：即便签到失败，只要刷新成功就必须回写，否则会丢失新令牌。

## 四、安全边界

- 仓库**建议设为私有**；令牌只存 GitHub Secrets，不写进任何文件 / 日志 / artifact。
- 脚本所有输出（文字、JSON、Step Summary、错误信息）都不含令牌明文，也不含脱敏片段。
- 签到链路只调用刷新、状态查询、签到三个接口；**唯一写操作**是 `daily-checkin`；推送只调用 WxPusher 发送消息接口。
- WxPusher `appToken` / UID 只经 Secret 注入、只作为请求体字段发送，不出现在日志、摘要、代码或 Step Summary。
- `appToken` 若曾在对话 / 聊天里明文出现过，建议在 WxPusher 后台「appToken」重置一次再更新 Secret（重置后旧值立即失效）。
- 临时令牌文件权限 0600，runner 销毁即消失；`gh secret set` 走 stdin，不出现在命令行。
- 撤销方式：WorkBuddy 客户端重新登录会使旧长效令牌失效；PAT 可随时在 GitHub 设置里 revoke。

## 五、已知限制与排错

| 现象 | 处理 |
|---|---|
| `need_setup` / 退出码 2 | Secret `WORKBUDDY_REFRESH_TOKEN` 未配置或为空 |
| 「令牌刷新失败」 | 长效令牌失效：重新导出并更新 Secret；必要时先在客户端重新登录 |
| `skip_already_signed` / `code 10001` | 正常幂等返回，不是失败 |
| 日报没收到 | 看 `push.enabled` / `push.reason`；最常见是缺 `WXPUSHER_UID`、UID 未关注应用、或 appToken 已重置 |
| `push.ok=false` | 见 `push.reason`；确认接口返回码，必要时用 `check_only` 手动跑一次复现 |
| 一天收到两条 | 默认已去重；检查是否误设了 `WXPUSHER_ALWAYS=1` |
| HTTP 超时 / 连接失败 | GitHub 托管 runner 在境外，访问 `copilot.tencent.com`、`www.codebuddy.cn`、`wxpusher.zjiecode.com` 属跨境链路，需实测；不稳时改用 **self-hosted runner（国内机器）** 或腾讯云函数 SCF |
| `skip_check_only` | 勾选了「只验证链路」，未执行签到，符合预期 |
| 回写报 `Resource not accessible` | `SECRETS_ADMIN_TOKEN` 权限不足，fine-grained PAT 需授予该仓库 Secrets: Read and write |
| 定时任务不再触发 | 60 天无活动被 GitHub 停用，手动 Run workflow 或 push 一次恢复 |

> 备选：若境外 runner 连通性不理想，把 workflow 的 `runs-on` 改成 `self-hosted`（国内常开机器），脚本与 Secrets 机制完全不用改；或告诉我换成腾讯云函数 SCF 版本。

---

技能依据：`WorkBuddy积分助手 3.1.7` 通道 C（`scripts/rt_auth.py`）与 `workbuddy_checkin.py` 的签到实现。本部署包未修改技能原文件。
