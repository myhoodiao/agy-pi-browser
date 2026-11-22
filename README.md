# agy-pi-browser

将 **Antigravity (`agy`)** 作为需求对齐与总控工作台，通过纯 Python 标准库 stdio MCP 把 **Browser Use** 任务派发给常驻 **Pi RPC Agent（默认 `openai-codex/gpt-6.1-terra` + `@playwright/mcp --extension`）**，直接复用本机日常已登录 Chrome，并支持**人机同会话保活协同**。

---

## 核心实现思路

```text
用户 ──▶ agy（需求对齐 + 总控）
            │  MCP (stdio JSON-RPC, 长驻进程)
            ▼
       pi_browser_mcp.py（缓存核心实例 + 退出清理）
            │
            ▼
       pi_browser_core.py（RpcWorkerManager 保活调度）
            │  stdin / stdout 二进制 LF-framed JSONL (Pi --mode rpc)
            ▼
       Pi Agent（常驻子进程，默认空闲 600s 自动回收）
         ├─ 模型：openai-codex/gpt-6.1-terra（默认 thinking: medium）
         ├─ 工具：builtin:mcp + builtin:tool-search + read/grep（排除 bash/edit/write）
         └─ MCP ：@playwright/mcp --extension ──▶ 日常已登录 Chrome（单主 Profile）
```

### 为什么改成 `--mode rpc` 保活（相比单轮 `--mode json --print`）

旧版每轮启一个 `pi --print` 子进程，轮次结束进程退出，导致 Playwright MCP 随之断开连接、Chrome 里的受控标签组变成孤儿：
- 遇到 `NEEDS_HUMAN` 暂停让人过验证码或登录，下一轮 `resume` 重新连接只会拿到一个全新 `about:blank`，原页面现场丢失；
- 人类事先打开好的页面无法交给 AI；
- 每轮都要重新冷启动、`tool_search` 并重新打开网址。

v2.0 改为在 MCP Server 进程内托管常驻 **`pi --mode rpc`** 子进程（`RpcWorkerManager`）：
1. **跨轮不断连**：单轮返回 `NEEDS_HUMAN` / `COMPLETED` 后保持进程与 Playwright 扩展连接在线（默认空闲 `600s` 自动关闭，也可由 `pi_browser_stop` 随时手动释放）；同 `session_id` 续跑直接命中热连接（`rpc_warm`），零冷启动、原标签页不丢。
2. **人机页面协同（两种方式）**：
   - **暂停接力**：AI 停在登录/验证码/高风险页返回 `NEEDS_HUMAN` -> 人在原标签页处理 -> 回复“继续” -> `pi_browser_resume` 直接留在同一页面继续。
   - **接管人类已打开的现成页面**：在 Token 免弹窗模式下，Playwright 扩展会在 Chrome 顶部创建彩色 `Playwright` 标签组；只需把你已打开的目标标签页**直接拖进该彩色标签组**，扩展会立即移交控制权（零弹窗），随后 `pi_browser_resume` 即可接管操作。若未配置 Token，扩展会在首次连接时弹出原生选页界面勾选现成标签页。
3. **单主 Profile + 同 Profile 内多 Google 账号**：不做复杂的多 Chrome Profile 切换调度；默认盯住装有 Playwright 扩展的单个主 Profile，Google 系多账号直接在网页内通过账号切换器 / `authuser=N` 切换。
4. **超时不白干（中间进展回收）**：单轮触及超时时先发 RPC `abort`、再清理进程树，并自动从已落盘的 JSONL 事件流中提取最后 URL、已执行步骤、工具报错与最后输出片段，以结构化 `STATUS: TIMEOUT` 返回给 `agy`。
5. **跨平台零依赖**：`scripts/` 仅依赖 Python 3.10+ 标准库，同时支持 Windows（`taskkill /T /F`）与 Linux（独立进程组 `os.killpg`）。

---

## 前置依赖与 Pi 侧关键配置

目标设备（Windows / Linux）需提前具备：
- Python **3.10+**、Node.js（建议 20+）
- 已安装并登录好模型的 **Pi Agent**（默认调用 `openai-codex/gpt-6.1-terra`）
- Chrome 已安装官方 **Playwright Extension**，并在 Pi 的 `~/.pi/agent/mcp.json` 中配置好 `playwright` MCP。

`~/.pi/agent/mcp.json` 中 `playwright` 节点参考（脱敏示例，路径与 Token 按目标设备实际填写；`--profile-dir-name` 为可选，仅在多 Chrome Profile 冲突时按需指定）：

```json
{
  "mcpServers": {
    "playwright": {
      "command": "node",
      "args": [
        "<path-to-playwright-mcp>/node_modules/@playwright/mcp/cli.js",
        "--extension"
      ],
      "env": {
        "PLAYWRIGHT_MCP_EXTENSION_TOKEN": "<your-extension-token>"
      },
      "enabled": true,
      "exposure": "deferred",
      "timeout": 120
    }
  }
}
```

> 提示：
> - 若不设置 `PLAYWRIGHT_MCP_EXTENSION_TOKEN`，首次连接时会弹出扩展选页页面供手动勾选已有标签页。
> - Windows 下若使用系统代理且遇到本地 `[::1]` 回环拦截，可在 `env` 中追加 `"NODE_OPTIONS": "--dns-result-order=ipv4first"`。

---

## 安装与自检

### Windows（PowerShell）

```powershell
git clone https://github.com/myhoodiao/agy-pi-browser.git
cd agy-pi-browser
.\install.ps1
```

### Linux（Bash）

```bash
git clone https://github.com/myhoodiao/agy-pi-browser.git
cd agy-pi-browser
chmod +x install.sh
./install.sh
```

安装脚本会自动完成：部署插件到 `~/.gemini/config/plugins/agy-pi-browser` -> 按本机实际路径生成 `mcp_config.json` -> 运行单元测试 -> 执行 MCP JSON-RPC 握手校验 -> 调用 `agy plugin validate --strict`（若 `agy` 在 PATH 中）。新开或重启 `agy` 会话即可生效。

---

## 提供给 `agy` 的 MCP 工具

| 工具 | 说明 |
|---|---|
| `pi_browser_run` | 启动新 Pi RPC 保活会话并执行对齐后的浏览器任务 |
| `pi_browser_resume` | 沿原 `session_id` 复用保活连接继续执行（人工处理或拖入标签组后无缝接力） |
| `pi_browser_status` | 只读检查运行锁、保活状态、历史会话与环境健康度（含扩展模式/Profile/Chrome 状态，不输出任何令牌明文） |
| `pi_browser_stop` | 终止运行中任务，或主动断开空闲保活连接并释放 Chrome 的 `Playwright` 标签组 |

## 常用环境变量

- `AGY_PI_BROWSER_IDLE_TIMEOUT`：空闲保活超时秒数，默认 `600`（设为 `0` 可关闭跨轮保活，单轮结束立即释放连接）。
- `AGY_PI_LAUNCHER_CMD`：可选覆盖 Pi 启动命令（支持 JSON 数组字符串或命令行字符串）。
- `AGY_PI_BROWSER_ROOT` / `AGY_PI_BROWSER_STATE_DIR`：覆盖插件根目录与状态日志目录（安装脚本会自动写入 `mcp_config.json`）。

## 目录结构

```text
agy-pi-browser/
├─ scripts/
│  ├─ pi_browser_core.py      # Pi RPC 保活调度、事件流解析、超时进展回收、跨平台进程树清理
│  └─ pi_browser_mcp.py       # 纯标准库 stdio MCP Server（模块缓存 + 退出清理）
├─ prompts/
│  └─ browser_worker.md       # 执行器行为约束（人机协同、多 Google 账号切换、快照控量）
├─ plugin/                    # agy 插件清单、规则（AGENTS.md）、技能（SKILL.md）、图标
├─ tests/
│  └─ test_pi_browser.py      # 离线单元测试套件
├─ install.ps1                # Windows 安装与自检脚本
└─ install.sh                 # Linux 安装与自检脚本
```
