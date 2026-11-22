"""
Agy Pi 浏览器执行器（agy-pi-browser）stdio MCP Server（v2.0）。

提供 4 个工具：
- pi_browser_run：启动新 Pi RPC 保活会话，调用 openai-codex/gpt-6.1-terra + Playwright MCP 执行浏览器任务。
- pi_browser_resume：沿原 Pi session_id 复用保活连接继续执行（人工登录/验证/拖入标签组后无缝接力，或同会话追加核对）。
- pi_browser_status：查看本机环境健康度（含扩展连接模式、Profile 状态）、活动运行锁、保活会话与历史记录。
- pi_browser_stop：终止运行中任务或主动关闭空闲保活会话，释放 Chrome Playwright 标签组连接。
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any

SUPPORTED_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
DEFAULT_PROTOCOL_VERSION = "2025-03-26"
DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parent.parent
THINKING_ENUM = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]

_CACHED_CORE: Any = None
_CACHED_CORE_PATH: Path | None = None


def resolve_project_root() -> Path:
    raw = os.environ.get("AGY_PI_BROWSER_ROOT", "").strip()
    root = Path(raw) if raw else DEFAULT_PROJECT_ROOT
    script_path = root / "scripts" / "pi_browser_core.py"
    if script_path.is_file():
        return root
    alt_script = Path(__file__).resolve().parent / "pi_browser_core.py"
    if alt_script.is_file():
        return alt_script.parent.parent
    raise FileNotFoundError(f"agy-pi-browser 核心后端缺失：{script_path}")


def load_backend_module(project_root: Path | None = None) -> Any:
    """缓存核心模块实例，确保同进程内的 Pi RPC 保活管理器跨工具调用持续生效。"""
    global _CACHED_CORE, _CACHED_CORE_PATH
    root = project_root or resolve_project_root()
    script_path = (root / "scripts" / "pi_browser_core.py").resolve()
    if _CACHED_CORE is not None and _CACHED_CORE_PATH == script_path:
        return _CACHED_CORE

    spec = importlib.util.spec_from_file_location("agy_pi_browser_core", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载核心后端：{script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["agy_pi_browser_core"] = module
    spec.loader.exec_module(module)
    _CACHED_CORE = module
    _CACHED_CORE_PATH = script_path
    return module


def handle_pi_browser_run(arguments: dict[str, Any]) -> dict[str, Any]:
    task = arguments.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("参数 'task' 必须为非空字符串。")
    thinking = arguments.get("thinking")
    timeout_seconds = arguments.get("timeout_seconds")
    model = arguments.get("model")

    core = load_backend_module()
    with contextlib.redirect_stdout(sys.stderr):
        result = core.run_task(
            task=task,
            thinking=thinking,
            timeout_seconds=timeout_seconds,
            model=model,
        )
        text = core.format_turn_result_markdown(result)
    return {
        "content": [{"type": "text", "text": text}],
        "isError": False,
    }


def handle_pi_browser_resume(arguments: dict[str, Any]) -> dict[str, Any]:
    instruction = arguments.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("参数 'instruction' 必须为非空字符串。")
    session_id = arguments.get("session_id")
    thinking = arguments.get("thinking")
    timeout_seconds = arguments.get("timeout_seconds")

    core = load_backend_module()
    with contextlib.redirect_stdout(sys.stderr):
        result = core.resume_task(
            instruction=instruction,
            session_id=session_id,
            thinking=thinking,
            timeout_seconds=timeout_seconds,
        )
        text = core.format_turn_result_markdown(result)
    return {
        "content": [{"type": "text", "text": text}],
        "isError": False,
    }


def handle_pi_browser_status(arguments: dict[str, Any]) -> dict[str, Any]:
    session_id = arguments.get("session_id")
    include_health = arguments.get("include_health", True)

    core = load_backend_module()
    with contextlib.redirect_stdout(sys.stderr):
        result = core.get_status(
            session_id=session_id,
            include_health=bool(include_health),
        )
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
        "isError": False,
    }


def handle_pi_browser_stop(arguments: dict[str, Any]) -> dict[str, Any]:
    session_id = arguments.get("session_id")

    core = load_backend_module()
    with contextlib.redirect_stdout(sys.stderr):
        result = core.stop_active(session_id=session_id)
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
        "isError": False,
    }


TOOLS_LIST = [
    {
        "name": "pi_browser_run",
        "description": (
            "派发一条已完成需求对齐的 Browser Use 任务给常驻 Pi RPC Agent（默认使用 "
            "openai-codex/gpt-6.1-terra + @playwright/mcp --extension，复用日常已登录 Chrome）。"
            "单轮结束后保持 Playwright 连接在线（默认空闲 600s 自动释放），支持 NEEDS_HUMAN 暂停续跑、"
            "用户拖入现成标签页到彩色 Playwright 标签组接管，以及超时自动回收中间进度。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "结构清晰、已对齐目标网址/步骤/输入内容/成功判据的浏览器任务说明",
                },
                "thinking": {
                    "type": "string",
                    "enum": THINKING_ENUM,
                    "description": "可选思考等级，默认 medium",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 30,
                    "maximum": 900,
                    "description": "可选单轮超时秒数（30~900），默认 300",
                },
                "model": {
                    "type": "string",
                    "description": "可选覆盖模型（格式 provider/model），默认 openai-codex/gpt-6.1-terra",
                },
            },
            "required": ["task"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pi_browser_resume",
        "description": (
            "沿用已有 Pi 浏览器会话（session_id）复用保活连接继续执行，适用于 NEEDS_HUMAN 人工处理后继续、"
            "用户把现成标签页拖入彩色 Playwright 标签组后接管，或在同一页面上下文中追加核对操作。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "下一步指令（例如：用户已把目标页拖入 Playwright 标签组/已完成登录，请继续执行）",
                },
                "session_id": {
                    "type": "string",
                    "description": "可选会话 ID；省略时优先选择最近处于 needs_human 的会话，否则选最近会话",
                },
                "thinking": {
                    "type": "string",
                    "enum": THINKING_ENUM,
                    "description": "可选覆盖本轮思考等级",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 30,
                    "maximum": 900,
                    "description": "可选单轮超时秒数（30~900），默认 300",
                },
            },
            "required": ["instruction"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pi_browser_status",
        "description": (
            "只读查询 Pi 浏览器执行器的运行锁（active_run）、保活会话状态（keepalive_worker）、"
            "历史会话列表/详情及本机环境健康度（Pi 启动器、Playwright 扩展连接模式、Profile 与 Chrome 进程）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "可选，查看指定会话详情",
                },
                "include_health": {
                    "type": "boolean",
                    "description": "是否执行只读本机环境健康检查（默认 true）",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "pi_browser_stop",
        "description": "终止当前运行中的 Pi 浏览器任务，或主动断开空闲保活会话并释放 Chrome 的 Playwright 标签组。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "可选，校验活动/保活会话是否属于该 session_id 后终止",
                },
            },
            "additionalProperties": False,
        },
    },
]


def handle_jsonrpc_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    req_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    if req_id is None:
        return None

    if method == "initialize":
        client_ver = params.get("protocolVersion")
        negotiated = (
            client_ver
            if isinstance(client_ver, str) and client_ver in SUPPORTED_PROTOCOL_VERSIONS
            else DEFAULT_PROTOCOL_VERSION
        )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": negotiated,
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": "agy-pi-browser-mcp",
                    "version": "2.0.0",
                },
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS_LIST}}

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": "执行失败: arguments 必须为 JSON 对象"}],
                    "isError": True,
                },
            }
        try:
            if tool_name == "pi_browser_run":
                result = handle_pi_browser_run(arguments)
            elif tool_name == "pi_browser_resume":
                result = handle_pi_browser_resume(arguments)
            elif tool_name == "pi_browser_status":
                result = handle_pi_browser_status(arguments)
            elif tool_name == "pi_browser_stop":
                result = handle_pi_browser_stop(arguments)
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"未知工具: {tool_name}"},
                }
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"执行失败: {exc}"}],
                    "isError": True,
                },
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def serve_stdio() -> None:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")

    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }
                sys.stdout.write(json.dumps(err_resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
                continue

            if not isinstance(msg, dict):
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Invalid Request: expected JSON object"},
                }
                sys.stdout.write(json.dumps(err_resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
                continue

            resp = handle_jsonrpc_message(msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
    finally:
        if _CACHED_CORE is not None:
            with contextlib.redirect_stdout(sys.stderr):
                try:
                    _CACHED_CORE.shutdown_keepalive_worker(force=False)
                except Exception:
                    pass


if __name__ == "__main__":
    serve_stdio()
