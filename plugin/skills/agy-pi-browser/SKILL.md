---
name: agy-pi-browser
description: >-
  Default Browser Use execution workflow in agy: aligns requirements first, then delegates web
  automation tasks to a persistent Pi RPC Agent (openai-codex/gpt-6.1-terra + Playwright MCP
  --extension) reusing the user's signed-in Chrome, with live keepalive across NEEDS_HUMAN pauses,
  drag-into-tab-group handoff for human-opened pages, and partial progress recovery on timeout.
---

# Agy Pi 浏览器执行器（Terra + Playwright）

本技能通过 `agy-pi-browser` MCP 服务将 `agy` 中对齐好的网页操作任务派发给本机常驻 **Pi RPC Agent**（默认 `openai-codex/gpt-6.1-terra` + `@playwright/mcp --extension`），直接复用日常 Chrome 登录态执行，并保持同会话浏览器连接在线以支持人机无缝接力。

## 工具清单

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `pi_browser_run` | 启动新保活会话并执行首轮任务 | `task`（必填）, `thinking`（默认 `medium`）, `timeout_seconds`（默认 `300`）, `model`（默认 `openai-codex/gpt-6.1-terra`） |
| `pi_browser_resume` | 沿原 `session_id` 复用保活连接继续执行 | `instruction`（必填）, `session_id`（建议显式传入，省略时优先选 `needs_human` 会话）, `thinking`, `timeout_seconds` |
| `pi_browser_status` | 查看环境健康度（含扩展连接模式与 Profile）、运行锁、保活状态与历史会话 | `session_id`（可选）, `include_health`（默认 `true`） |
| `pi_browser_stop` | 终止运行中任务或主动关闭空闲保活连接，释放 Chrome 标签组 | `session_id`（可选） |

---

## 标准工作流

### 1. 需求对齐（在 `agy` 内完成）
派发前检查关键要素是否齐备：
- 目标网站 URL，或说明需要接管用户已经手动打开的现成页面；
- 若涉及同 Chrome Profile 下的多个 Google 账号，明确目标邮箱（执行器会通过网页内账号切换或 `authuser` 处理，无需切换 Chrome Profile）；
- 具体操作步骤、输入内容与成功判据；
- 是否明确授权提交、发送、支付或删除等不可逆动作。

复杂任务建议按阶段拆成聚焦的短轮次（例如“先只读核查 -> 再执行修改 -> 最后复核”），避免单轮塞入过多跨站步骤。

### 2. 人机页面协同的两种方式
- **方式 A（AI 暂停让人处理，再接回去）**：
  1. `pi_browser_run` 遇到登录、扫码、2FA、CAPTCHA 或高风险确认时返回 `STATUS: NEEDS_HUMAN`，此时 **Pi RPC 与 Chrome 连接保持在线**，原标签页不会丢失。
  2. 转述 `HUMAN_ACTION_REQUIRED` 请用户在原标签页完成处理，等待用户回复“继续”。
  3. 调用 `pi_browser_resume(session_id=..., instruction=...)` 命中 `RPC 保活复用`，直接从当前页面继续。
- **方式 B（接管用户已经手动打开的现成页面）**：
  - 若环境为 Token 免弹窗连接模式（`extension_token_auto`），新会话初始只包含扩展创建的彩色 `Playwright` 标签组。
  - 首轮启动后若返回 `NEEDS_HUMAN` 提示未看到目标页，只需让用户**把现成标签页拖进 Chrome 顶部彩色的 `Playwright` 标签组**，再调用 `pi_browser_resume` 即可无缝接管。
  - 若环境未配置 Token（`extension_tab_picker`），扩展会弹出选页界面供用户直接勾选现成标签页。

### 3. 处理返回状态
- **`STATUS: COMPLETED`**：核对 `EVIDENCE` 与工具调用统计后如实汇报；需要追加核查时直接调用 `pi_browser_resume` 复用当前保活页面；全部完成后若想立刻释放 Chrome 标签组可调用 `pi_browser_stop`。
- **`STATUS: NEEDS_HUMAN`**：记录 `session_id`，向用户转述所需手动动作，等待回复后沿原 `session_id` 调用 `pi_browser_resume`。
- **`STATUS: TIMEOUT`**：单轮达到耗时上限时，后端会自动回收并汇报超时前停留的 URL、最近操作与报错摘要；请先据此向用户汇报已有进展，再决定拆分剩余步骤继续或调整任务范围。
- **`STATUS: BLOCKED` 或工具报错**：调用 `pi_browser_status` 检查 `health`（扩展模式、Profile、Chrome 进程）与 `keepalive_worker` / `active_run` 状态；如实汇报阻塞原因与日志路径，禁止绕过安全边界。

## 与 `chrome-devtools` 的协作边界
- **普通网页操作 / 表单 / 登录态交互 / 人机接力流程**：优先走 `agy-pi-browser`。
- **前端开发者调试（Console、Network、CSS、Performance Trace、Lighthouse、Heap）**：使用原有 `chrome-devtools` MCP，两者互不干扰。
