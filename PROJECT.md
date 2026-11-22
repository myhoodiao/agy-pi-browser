# 项目事实与验证状态

## 当前架构（v2.0.0）

- **执行引擎**：常驻无头 Pi Agent（`--mode rpc`），默认模型 `openai-codex/gpt-6.1-terra`（`thinking: medium`，支持单次覆盖），仅启用 `builtin:mcp` 与 `builtin:tool-search`，排除 `mcp__windows__*`、`mcp__web_search__*`、`bash`、`edit`、`write`。
- **浏览器后端**：复用用户 `~/.pi/agent/mcp.json` 中配置的 `@playwright/mcp --extension`，不修改 Chrome 默认启动方式，与 `agy` 原有 `chrome-devtools` 共存。
- **人机协同与保活**：
  - `pi_browser_mcp.py` 缓存核心模块，进程内由 `RpcWorkerManager` 维护单一长驻 Pi RPC 子进程（默认空闲 600s 自动释放，可由 `AGY_PI_BROWSER_IDLE_TIMEOUT` 调整，或由 `pi_browser_stop` 主动释放）。
  - `NEEDS_HUMAN` 暂停期间保持 Playwright 与 Chrome 的标签组连接在线；支持人工处理完原页后热续跑（`rpc_warm`），也支持在 Token 免弹窗模式下将人类已打开的现成标签页直接拖入 Chrome 顶部的彩色 `Playwright` 标签组移交控制权。
  - 单主 Profile + 同 Profile 内多 Google 账号（`authuser` / 网页账号切换器），不引入复杂的多 Chrome Profile 调度器。
- **超时回收**：单轮超时时尝试优雅 `abort` 并清理进程树，同时从 JSONL 事件流提取最后 URL、最近操作、工具报错与最后输出片段，以 `STATUS: TIMEOUT` 返回给 `agy`。
- **跨平台支持**：Python 3.10+ 纯标准库核心与 `install.ps1`（Windows）/ `install.sh`（Linux）双端安装自检脚本。

## 验证状态

- 离线单元测试（`python -B -X utf8 -m unittest discover -s tests -v`）：覆盖 Pi RPC 启动握手、热续跑复用同 PID、动态切换 `thinking`、冷回退、空闲自动回收、超时中间进展回收、扩展 UI 弹窗防挂起、健康检查防令牌泄露、MCP JSON-RPC 协议与并发锁。
- 真机 Pi RPC 握手验证：已验证本机 Pi `1.5.2` 在 `--mode rpc` 下 `get_state`、`set_thinking_level`、会话持久化与 stdin EOF 优雅退出。
- Linux 端安装与真机 Chrome 端到端实跑：需在目标 Linux 设备拉取仓库后独立验收（本机为 Windows，不将 Windows 实测结果代替 Linux 验收）。
