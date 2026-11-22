# agy-pi-browser 项目约束

1. **语言与实证**：默认使用简体中文。严格区分“已完成”与“已验证”；未实跑通过的内容不得写成已验证。
2. **零额外依赖与边界隔离**：
   - `scripts/` 仅使用 Python 3.10+ 标准库，兼容 Windows 与 Linux，不引入第三方 PyPI 包。
   - 不主动篡改用户 `~/.pi/agent/mcp.json`、`~/.pi/agent/settings.json` 或 `~/.gemini/config/mcp_config.json`，与 `chrome-devtools` 保持共存互不干扰。
   - 日志、状态与健康检查结果严禁输出 `~/.pi/agent/auth.json`、`~/.pi/agent/mcp.json` 中的任何令牌或凭据明文。
3. **跨设备与开源脱敏规范**：仓库内不得提交本机绝对路径硬编码、个人邮箱、令牌、密钥或 `.state/` 运行日志。
4. **安全与人工接管**：严禁绕过登录、验证码、2FA 或扩展授权；遇到阻塞必须返回 `NEEDS_HUMAN` 并保持会话在线，待用户手动处理后通过 `pi_browser_resume` 续跑。
