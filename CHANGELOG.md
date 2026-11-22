# 更新记录

## 2.0.0（2026-11-22）

- **架构升级为 Pi `--mode rpc` 常驻保活会话**：
  - 将单轮冷启动（`--mode json --print`）改造为进程内 `RpcWorkerManager` 托管的常驻 Pi RPC 子进程，支持空闲超时自动回收（默认 600s，`AGY_PI_BROWSER_IDLE_TIMEOUT` 可配）与 MCP 退出自动清理。
  - 修复 `pi_browser_mcp.py` 每次工具调用重载核心模块导致内存态丢失的问题，确保 `pi_browser_resume` 命中同进程热连接（`rpc_warm`）。
- **打通人机页面协同**：
  - `NEEDS_HUMAN` 暂停期间不再断开 Playwright 扩展连接，解决多轮续跑丢失原标签页与表单/结果页现场的问题。
  - 更新执行器提示词、Skill 与规则，明确支持在 Token 免弹窗模式下通过“拖入 Chrome 彩色 `Playwright` 标签组”接管人类已打开的现成页面，以及单主 Profile 内多 Google 账号（`authuser`）切换。
- **超时进展回收与诊断增强**：
  - 单轮超时时新增 `extract_partial_progress`，自动回收最后 URL、操作轨迹、工具报错与最后输出片段，以结构化 `STATUS: TIMEOUT` 返回。
  - 增强 `pi_browser_status` 健康检查：只读汇报 Playwright 扩展连接模式、Profile 名、扩展安装状态、保活进程状态与跨平台 Chrome 检测，绝不输出任何令牌明文。
  - `pi_browser_stop` 支持在空闲时主动断开保活连接并释放 Chrome 标签组。
- **跨平台支持与开源整理**：
  - 适配 Windows + Linux 路径检测与 POSIX 进程组清理（`start_new_session` + `os.killpg`），新增 `install.sh`。
  - 精简重写 `README.md`，脱敏本机路径与隐私信息，准备公开至 GitHub 仓库。

## 1.0.0（2026-11-20）

- 初始落地 `agy-pi-browser` 插件：纯标准库 stdio MCP Server、无头 Pi Agent（`--mode json --print`，`openai-codex/gpt-6.1-terra`）与 `@playwright/mcp --extension` 集成，保留与 `chrome-devtools` 共存。
- 默认思考等级调整为 `medium`（支持单次覆盖）。
