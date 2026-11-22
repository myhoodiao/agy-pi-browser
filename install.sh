#!/usr/bin/env bash
set -euo pipefail

PYTHON_EXE="${1:-}"
BACKEND_DIR="${2:-}"

echo "=== [1/5] 检查 Python 3.10+ 运行环境 ==="
resolve_python() {
  if [[ -n "$PYTHON_EXE" && -x "$PYTHON_EXE" ]]; then
    echo "$PYTHON_EXE"
    return 0
  fi
  for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        command -v "$cand"
        return 0
      fi
    fi
  done
  echo "错误：未找到可用的 Python 3.10+，请先安装或传入路径参数。" >&2
  return 1
}

RESOLVED_PYTHON="$(resolve_python)"
echo "  已定位 Python: $RESOLVED_PYTHON ($("$RESOLVED_PYTHON" --version))"

echo "=== [2/5] 解析插件部署路径 ==="
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="${HOME}/.gemini/config/plugins/agy-pi-browser"
if [[ -n "$BACKEND_DIR" ]]; then
  EFFECTIVE_ROOT="$(cd "$BACKEND_DIR" && pwd)"
else
  EFFECTIVE_ROOT="$REPO_ROOT"
fi

echo "  源码目录: $REPO_ROOT"
echo "  插件路径: $PLUGIN_ROOT"
echo "  引擎目录: $EFFECTIVE_ROOT"

echo "=== [3/5] 部署插件文件与生成 mcp_config.json ==="
mkdir -p \
  "$PLUGIN_ROOT/assets" \
  "$PLUGIN_ROOT/prompts" \
  "$PLUGIN_ROOT/rules" \
  "$PLUGIN_ROOT/scripts" \
  "$PLUGIN_ROOT/skills/agy-pi-browser"

cp -f "$REPO_ROOT/plugin/plugin.json" "$PLUGIN_ROOT/plugin.json"
cp -f "$REPO_ROOT/plugin/assets/logo.svg" "$PLUGIN_ROOT/assets/logo.svg"
cp -f "$REPO_ROOT/plugin/rules/AGENTS.md" "$PLUGIN_ROOT/rules/AGENTS.md"
cp -f "$REPO_ROOT/plugin/skills/agy-pi-browser/SKILL.md" "$PLUGIN_ROOT/skills/agy-pi-browser/SKILL.md"
cp -f "$REPO_ROOT/prompts/browser_worker.md" "$PLUGIN_ROOT/prompts/browser_worker.md"
cp -f "$REPO_ROOT/scripts/pi_browser_core.py" "$PLUGIN_ROOT/scripts/pi_browser_core.py"
cp -f "$REPO_ROOT/scripts/pi_browser_mcp.py" "$PLUGIN_ROOT/scripts/pi_browser_mcp.py"

MCP_SCRIPT_PATH="$EFFECTIVE_ROOT/scripts/pi_browser_mcp.py"
STATE_DIR_PATH="$EFFECTIVE_ROOT/.state"

"$RESOLVED_PYTHON" - "$RESOLVED_PYTHON" "$MCP_SCRIPT_PATH" "$EFFECTIVE_ROOT" "$STATE_DIR_PATH" "$PLUGIN_ROOT/mcp_config.json" <<'PY'
import json, sys
py_exe, mcp_script, root_dir, state_dir, out_path = sys.argv[1:]
cfg = {
    "mcpServers": {
        "pi-browser": {
            "command": py_exe,
            "args": ["-X", "utf8", mcp_script],
            "env": {
                "AGY_PI_BROWSER_ROOT": root_dir,
                "AGY_PI_BROWSER_STATE_DIR": state_dir,
                "PYTHONIOENCODING": "utf-8",
            },
        }
    }
}
with open(out_path, "w", encoding="utf-8", newline="\n") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
echo "  已部署插件并生成 mcp_config.json"

echo "=== [4/5] 运行离线单元测试 ==="
"$RESOLVED_PYTHON" -B -X utf8 -m unittest discover -s "$REPO_ROOT/tests" -v

echo "=== [5/5] 执行 MCP JSON-RPC 握手与插件严格校验 ==="
SMOKE_OUT="$(printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26"}}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n' | "$RESOLVED_PYTHON" -X utf8 "$MCP_SCRIPT_PATH")"
for tool in pi_browser_run pi_browser_resume pi_browser_status pi_browser_stop; do
  if [[ "$SMOKE_OUT" != *"$tool"* ]]; then
    echo "错误：MCP 握手自检缺失工具 $tool" >&2
    exit 1
  fi
done
echo "  MCP JSON-RPC 握手与 4 个工具校验通过"

if command -v agy >/dev/null 2>&1; then
  agy plugin validate "$PLUGIN_ROOT" --strict
else
  echo "  未检测到 agy 命令，跳过 agy plugin validate"
fi

echo ""
echo "✅ agy-pi-browser 安装与自检全部通过！新开或重启 agy 会话即可使用。"
