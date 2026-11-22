# agy-pi-browser 插件路由与边界规则

本插件为 `agy` 提供专职 Browser Use 执行后端（基于本地常驻 Pi RPC Agent + `openai-codex/gpt-6.1-terra` + Playwright MCP `--extension`）。

1. **优先分工原则（与 chrome-devtools 共存）**：
   - 当用户要求在浏览器中完成页面操作、表单填写、发邮件、登录态网页交互等一般 Browser Use 任务时，默认优先激活 `agy-pi-browser` 技能并使用 `pi_browser_run` / `pi_browser_resume` 派发执行。
   - 保留现有 `chrome-devtools`（`chrome-devtools-plugin`）不动，仅在用户明确点名使用 DevTools 或需要前端开发者调试（控制台日志、网络请求抓包、CSS 样式、性能 Trace、Lighthouse、内存快照）时直接调用 `chrome-devtools`。

2. **先对齐需求，再派单执行**：
   - 调用 `pi_browser_run` 前，若目标网址、操作步骤、关键输入内容（如收件人、主题、正文）或不可逆动作边界尚不明确，必须先在 `agy` 中向用户核实对齐，再一次性下发清晰完整的任务单。
   - 若用户已在当前消息中给出全部关键要素，可直接调用 `pi_browser_run` 执行，不要多余反问。
   - 复杂多页面任务应拆成目标清晰的小轮次派发，避免单轮堆积数十步触发超时或上下文膨胀。

3. **人机协同与保活续跑（NEEDS_HUMAN / 拖入标签组）**：
   - 会话默认通过 Pi `--mode rpc` 保活（空闲 600 秒自动释放）。`NEEDS_HUMAN` 暂停期间浏览器连接不断开，原标签页会保留在 Chrome 的彩色 `Playwright` 标签组中。
   - 若用户希望 AI 直接接管**此前手动打开的现成页面**，且本机处于 Token 自动连接模式：启动会话后由用户把该标签页拖进 Chrome 顶部彩色的 `Playwright` 标签组，再调用 `pi_browser_resume` 无缝接管。
   - 用户回复“继续 / 好了 / 已拖入 / 已登录”后，必须调用 `pi_browser_resume` 传入原 `session_id` 复用保活会话继续执行，不得另起新会话重复执行前置动作。
   - 如任务已全部结束且用户希望立刻解除 Chrome 标签组接管，可调用 `pi_browser_stop`。

4. **严禁虚假成功与盲目重试**：
   - 严格如实向用户汇报工具返回的 `STATUS`（含 `TIMEOUT` 回收的中间进展）、连接模式、模型名、工具调用事实、观察到的页面证据和日志路径；未验证的动作不得声称已完成。
