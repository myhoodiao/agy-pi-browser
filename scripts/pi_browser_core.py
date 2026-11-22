"""
Agy Pi 浏览器执行器（agy-pi-browser）核心调度模块（v2.0）。

仅使用 Python 3.10+ 标准库，兼容 Windows 与 Linux，负责：
- 通过 Pi Agent 原生 `--mode rpc` 维持同会话保活子进程（默认 openai-codex/gpt-6.1-terra）
- 保持 Playwright MCP `--extension` 连接跨轮不断连，支持 NEEDS_HUMAN 暂停续跑与拖入标签组人机协同
- 严格按 LF 二进制帧解析 Pi RPC JSONL 事件流、校验实际模型与工具调用证据
- 超时/中止时自动从事件流回收已执行的中间进展（最后 URL、步骤、错误与片段），避免白干
- 管理活动运行锁（active_run.json）、保活状态（keepalive_worker.json）与跨平台进程树清理
"""

from __future__ import annotations

import atexit
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from typing import Any
import uuid

DEFAULT_PROVIDER = "openai-codex"
DEFAULT_MODEL = "gpt-6.1-terra"
DEFAULT_FULL_MODEL = f"{DEFAULT_PROVIDER}/{DEFAULT_MODEL}"
DEFAULT_THINKING = "medium"
DEFAULT_TIMEOUT_SECONDS = 300
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 900
DEFAULT_IDLE_TIMEOUT_SECONDS = 600
STARTUP_HANDSHAKE_TIMEOUT_SECONDS = 30
ABORT_GRACE_SECONDS = 5
SHUTDOWN_GRACE_SECONDS = 3

VALID_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
EXCLUDED_TOOLS = "mcp__windows__*,mcp__web_search__*,bash,edit,write"
VALID_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
VALID_WORKER_STATUSES = {"COMPLETED", "NEEDS_HUMAN", "BLOCKED", "TIMEOUT"}
UI_DIALOG_METHODS = {"select", "confirm", "input", "editor"}

_EOF_SENTINEL = object()


class PiBrowserError(Exception):
    """可控错误：参数校验失败、并发锁冲突、子进程启动/退出异常或模型不匹配。"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_pi_agent_dir() -> Path:
    env_dir = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return (Path.home() / ".pi" / "agent").resolve()


def resolve_project_root(project_root: Path | None = None) -> Path:
    if project_root is not None:
        return Path(project_root).expanduser().resolve()
    env_root = os.environ.get("AGY_PI_BROWSER_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def resolve_state_dir(project_root: Path | None = None, state_dir: Path | None = None) -> Path:
    if state_dir is not None:
        base = Path(state_dir).expanduser().resolve()
    else:
        env_state = os.environ.get("AGY_PI_BROWSER_STATE_DIR", "").strip()
        if env_state:
            base = Path(env_state).expanduser().resolve()
        else:
            base = resolve_project_root(project_root) / ".state"
    for sub in ("sessions", "pi_sessions", "logs", "workdir"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    return base


def resolve_worker_prompt(project_root: Path | None = None) -> Path:
    root = resolve_project_root(project_root)
    prompt_path = root / "prompts" / "browser_worker.md"
    if not prompt_path.is_file():
        raise PiBrowserError(f"未找到执行器提示词文件：{prompt_path}")
    return prompt_path


def resolve_idle_timeout_seconds(override: int | None = None) -> int:
    if override is not None:
        return max(0, int(override))
    raw = os.environ.get("AGY_PI_BROWSER_IDLE_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_IDLE_TIMEOUT_SECONDS
    try:
        val = int(raw)
        return max(0, val)
    except ValueError:
        return DEFAULT_IDLE_TIMEOUT_SECONDS


def resolve_pi_invocation(pi_cmd_prefix: list[str] | None = None) -> list[str]:
    if pi_cmd_prefix:
        return [str(part) for part in pi_cmd_prefix]

    env_cmd = os.environ.get("AGY_PI_LAUNCHER_CMD", "").strip()
    if env_cmd:
        if env_cmd.startswith("["):
            parsed = json.loads(env_cmd)
            if not isinstance(parsed, list) or not parsed or not all(isinstance(x, str) and x for x in parsed):
                raise PiBrowserError("AGY_PI_LAUNCHER_CMD JSON 格式无效，应为非空字符串数组。")
            return parsed
        parts = shlex.split(env_cmd, posix=(os.name != "nt"))
        cleaned = [p.strip('"') for p in parts if p.strip('"')]
        if not cleaned:
            raise PiBrowserError("AGY_PI_LAUNCHER_CMD 为空。")
        return cleaned

    launcher_js = get_pi_agent_dir() / "bin" / "pi-launcher.js"
    node_exe = shutil.which("node")
    if not node_exe:
        fallback_nodes = (
            [Path(r"C:\Program Files\nodejs\node.exe")]
            if os.name == "nt"
            else [Path("/usr/local/bin/node"), Path("/usr/bin/node")]
        )
        for cand in fallback_nodes:
            if cand.is_file():
                node_exe = str(cand)
                break

    if launcher_js.is_file() and node_exe:
        return [node_exe, str(launcher_js)]

    candidates = ("pi.cmd", "pi.CMD", "pi") if os.name == "nt" else ("pi",)
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return [found]

    bin_dir = get_pi_agent_dir() / "bin"
    for fallback_name in ("pi.cmd", "pi") if os.name == "nt" else ("pi",):
        fallback_cmd = bin_dir / fallback_name
        if fallback_cmd.is_file():
            return [str(fallback_cmd)]

    raise PiBrowserError("未找到 Node/pi-launcher.js 或 pi 可执行文件，请确认 Pi Agent 已安装。")


def validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not VALID_SESSION_ID_RE.match(session_id):
        raise PiBrowserError(
            "session_id 格式非法：仅允许字母、数字、'-'、'_'、'.'，且首尾必须为字母或数字。"
        )
    return session_id


def parse_full_model(model_spec: str | None) -> tuple[str, str]:
    raw = (model_spec or "").strip()
    if not raw:
        return DEFAULT_PROVIDER, DEFAULT_MODEL
    if "/" not in raw:
        raise PiBrowserError(
            f"模型参数格式无效：'{raw}'，必须为 'provider/model'（例如 '{DEFAULT_FULL_MODEL}'）。"
        )
    provider, model_id = raw.split("/", 1)
    provider = provider.strip()
    model_id = model_id.strip()
    if not provider or not model_id:
        raise PiBrowserError(f"模型参数格式无效：'{raw}'，provider 和 model 均不能为空。")
    return provider, model_id


def validate_thinking(thinking: str | None) -> str:
    val = (thinking or DEFAULT_THINKING).strip().lower()
    if val not in VALID_THINKING_LEVELS:
        raise PiBrowserError(
            f"thinking 等级无效：'{val}'。可选值：{', '.join(VALID_THINKING_LEVELS)}。"
        )
    return val


def validate_timeout(timeout_seconds: int | None) -> int:
    if timeout_seconds is None:
        return DEFAULT_TIMEOUT_SECONDS
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
        raise PiBrowserError("timeout_seconds 必须为整数。")
    if timeout_seconds < MIN_TIMEOUT_SECONDS or timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise PiBrowserError(
            f"timeout_seconds 必须在 {MIN_TIMEOUT_SECONDS}~{MAX_TIMEOUT_SECONDS} 秒之间，当前为 {timeout_seconds}。"
        )
    return timeout_seconds


def build_pi_command(
    *,
    pi_cmd_prefix: list[str],
    pi_session_dir: Path,
    session_id: str,
    provider: str,
    model_id: str,
    thinking: str,
    worker_prompt_path: Path,
    mode: str = "rpc",
) -> list[str]:
    cmd = [
        *pi_cmd_prefix,
        "--mode",
        mode,
    ]
    if mode != "rpc":
        cmd.append("--print")
    cmd.extend(
        [
            "--session-dir",
            str(pi_session_dir),
            "--session-id",
            session_id,
            "--no-extensions",
            "-e",
            "builtin:mcp",
            "-e",
            "builtin:tool-search",
            "--no-skills",
            "--no-prompt-templates",
            "--exclude-tools",
            EXCLUDED_TOOLS,
            "--provider",
            provider,
            "--model",
            model_id,
            "--thinking",
            thinking,
            "--append-system-prompt",
            str(worker_prompt_path),
        ]
    )
    return cmd


def is_pid_running(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            if not ok:
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def kill_process_tree(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        res = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        return res.returncode == 0 or not is_pid_running(pid)
    # POSIX: 优先向独立进程组发信号，确保同步清理子进程（如 playwright-mcp）
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except OSError:
            return False

    time.sleep(0.5)
    if is_pid_running(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return not is_pid_running(pid)


def _active_run_path(state_dir: Path) -> Path:
    return state_dir / "active_run.json"


def _keepalive_file_path(state_dir: Path) -> Path:
    return state_dir / "keepalive_worker.json"


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_json_dict(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _clear_pid_file(path: Path, expected_pid: int | None = None) -> None:
    if not path.is_file():
        return
    if expected_pid is not None:
        current = _read_json_dict(path)
        if current and current.get("pid") != expected_pid:
            return
    try:
        path.unlink()
    except OSError:
        pass


def _read_active_run(state_dir: Path) -> dict[str, Any] | None:
    return _read_json_dict(_active_run_path(state_dir))


def _write_active_run(state_dir: Path, payload: dict[str, Any]) -> None:
    _write_json_dict(_active_run_path(state_dir), payload)


def _clear_active_run(state_dir: Path, expected_pid: int | None = None) -> None:
    _clear_pid_file(_active_run_path(state_dir), expected_pid=expected_pid)


def _read_keepalive_file(state_dir: Path) -> dict[str, Any] | None:
    return _read_json_dict(_keepalive_file_path(state_dir))


def _write_keepalive_file(state_dir: Path, payload: dict[str, Any]) -> None:
    _write_json_dict(_keepalive_file_path(state_dir), payload)


def _clear_keepalive_file(state_dir: Path, expected_pid: int | None = None) -> None:
    _clear_pid_file(_keepalive_file_path(state_dir), expected_pid=expected_pid)


def _ensure_no_live_active_run(state_dir: Path) -> None:
    current = _read_active_run(state_dir)
    if not current:
        return
    pid = current.get("pid")
    if isinstance(pid, int) and is_pid_running(pid):
        sid = current.get("session_id", "unknown")
        raise PiBrowserError(
            f"已有浏览器任务正在运行中（PID={pid}, session_id={sid}）。"
            "为避免并发操控同一 Chrome 冲突，请等待其完成或先调用 pi_browser_stop。"
        )
    _clear_active_run(state_dir)


def _session_file_path(state_dir: Path, session_id: str) -> Path:
    validate_session_id(session_id)
    return state_dir / "sessions" / f"{session_id}.json"


def _load_session_meta(state_dir: Path, session_id: str) -> dict[str, Any]:
    path = _session_file_path(state_dir, session_id)
    if not path.is_file():
        raise PiBrowserError(f"未找到会话记录：{session_id}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PiBrowserError(f"读取会话记录失败（{session_id}）：{exc}") from exc
    if not isinstance(data, dict):
        raise PiBrowserError(f"会话记录格式损坏：{session_id}")
    return data


def _save_session_meta(state_dir: Path, meta: dict[str, Any]) -> None:
    path = _session_file_path(state_dir, meta["session_id"])
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _list_sessions(state_dir: Path, limit: int = 10) -> list[dict[str, Any]]:
    sessions_dir = state_dir / "sessions"
    items: list[dict[str, Any]] = []
    for path in sessions_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and data.get("session_id"):
            items.append(data)
    items.sort(key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""), reverse=True)
    return items[:limit]


def parse_pi_json_events(
    stdout_text: str,
    *,
    expected_provider: str,
    expected_model: str,
) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    non_json_lines: list[str] = []
    for raw_line in (stdout_text or "").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            non_json_lines.append(stripped[:200])
            continue
        if isinstance(ev, dict):
            events.append(ev)

    assistant_messages: list[dict[str, Any]] = []
    tool_calls_by_id: dict[str, str] = {}
    ordered_tools: list[str] = []

    def _record_tool(tool_name: str, call_id: str | None = None) -> None:
        if not tool_name:
            return
        if call_id and call_id in tool_calls_by_id:
            return
        if call_id:
            tool_calls_by_id[call_id] = tool_name
        ordered_tools.append(tool_name)

    for ev in events:
        ev_type = ev.get("type")
        if ev_type == "tool_execution_start":
            tname = ev.get("toolName")
            tcall_id = ev.get("toolCallId")
            if isinstance(tname, str):
                _record_tool(tname, tcall_id if isinstance(tcall_id, str) else None)
        elif ev_type == "message_end":
            msg = ev.get("message")
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                assistant_messages.append(msg)
                for block in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
                    if isinstance(block, dict) and block.get("type") == "toolCall":
                        bname = block.get("name")
                        bid = block.get("id")
                        if isinstance(bname, str):
                            _record_tool(bname, bid if isinstance(bid, str) else None)

    if not assistant_messages:
        extra = f"；非 JSON 输出示例：{non_json_lines[0]}" if non_json_lines else ""
        raise PiBrowserError(f"Pi 未产生任何 assistant message_end 事件{extra}")

    last_msg = assistant_messages[-1]
    last_stop = last_msg.get("stopReason")
    last_err = last_msg.get("errorMessage")
    if last_stop in ("error", "aborted") or last_err:
        raise PiBrowserError(
            f"Pi 执行异常终止：stopReason={last_stop!r}, error={last_err!r}"
        )
    if last_stop == "length":
        raise PiBrowserError("Pi 输出因达到最大输出长度被截断（stopReason='length'）。")

    actual_provider = last_msg.get("provider")
    actual_model = last_msg.get("model")
    if actual_provider and actual_provider != expected_provider:
        raise PiBrowserError(
            f"模型提供商不匹配：要求 '{expected_provider}'，实际返回 '{actual_provider}'。"
        )
    if actual_model and actual_model != expected_model:
        raise PiBrowserError(
            f"模型不匹配：要求 '{expected_model}'，实际返回 '{actual_model}'。"
        )

    text_parts: list[str] = []
    for part in last_msg.get("content", []) if isinstance(last_msg.get("content"), list) else []:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            text_parts.append(part["text"])

    final_text = "\n".join(text_parts).strip()
    if not final_text:
        raise PiBrowserError("Pi 最终回复为空文本。")

    total_tokens = 0
    for msg in assistant_messages:
        usage = msg.get("usage")
        if isinstance(usage, dict):
            total_tokens += int(usage.get("totalTokens") or 0)

    playwright_tools = [t for t in ordered_tools if t.startswith("mcp__playwright__") or "playwright" in t.lower()]
    unique_playwright = list(dict.fromkeys(playwright_tools))

    return {
        "text": final_text,
        "provider": actual_provider or expected_provider,
        "model": actual_model or expected_model,
        "full_model": f"{actual_provider or expected_provider}/{actual_model or expected_model}",
        "turns_in_invocation": len(assistant_messages),
        "total_tokens": total_tokens,
        "tools_called": ordered_tools,
        "playwright_tools_called": playwright_tools,
        "unique_playwright_tools": unique_playwright,
        "non_json_warnings": non_json_lines,
    }


def extract_partial_progress(stdout_text: str) -> dict[str, Any]:
    """从超时或中止时的不完整 JSONL 事件流中捞回已执行进度，避免长任务白干。"""
    assistant_messages: list[dict[str, Any]] = []
    ordered_tools: list[str] = []
    tool_calls_by_id: dict[str, str] = {}
    recent_actions: list[str] = []
    tool_errors: list[str] = []
    last_url = "N/A"
    last_assistant_text = ""

    page_url_re = re.compile(r"Page URL:\s*(\S+)")

    def _record_tool(tool_name: str, call_id: str | None = None) -> None:
        if not tool_name:
            return
        if call_id and call_id in tool_calls_by_id:
            return
        if call_id:
            tool_calls_by_id[call_id] = tool_name
        ordered_tools.append(tool_name)

    for raw_line in (stdout_text or "").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue

        ev_type = ev.get("type")
        if ev_type == "tool_execution_start":
            tname = ev.get("toolName")
            tcall_id = ev.get("toolCallId")
            args = ev.get("args")
            if isinstance(tname, str):
                _record_tool(tname, tcall_id if isinstance(tcall_id, str) else None)
                if isinstance(args, dict):
                    url_arg = args.get("url")
                    if isinstance(url_arg, str) and url_arg.startswith(("http://", "https://")):
                        last_url = url_arg
                    arg_preview = json.dumps(args, ensure_ascii=False)
                    if len(arg_preview) > 100:
                        arg_preview = arg_preview[:100] + "..."
                    recent_actions.append(f"{tname}({arg_preview})")
                else:
                    recent_actions.append(tname)
        elif ev_type == "tool_execution_end":
            tname = ev.get("toolName") or ""
            res = ev.get("result")
            txt_parts: list[str] = []
            if isinstance(res, dict):
                for block in res.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                        txt_parts.append(block["text"])
            joined = "\n".join(txt_parts)
            m = page_url_re.search(joined)
            if m:
                last_url = m.group(1).strip()
            if ev.get("isError"):
                err_line = " ".join(joined.strip().splitlines())[:160]
                if err_line:
                    tool_errors.append(f"{tname}: {err_line}")
        elif ev_type == "message_end":
            msg = ev.get("message")
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                assistant_messages.append(msg)
                texts: list[str] = []
                for block in msg.get("content") or []:
                    if isinstance(block, dict):
                        if block.get("type") == "toolCall" and isinstance(block.get("name"), str):
                            _record_tool(block["name"], block.get("id") if isinstance(block.get("id"), str) else None)
                        elif block.get("type") == "text" and isinstance(block.get("text"), str):
                            texts.append(block["text"])
                candidate = "\n".join(texts).strip()
                if candidate:
                    last_assistant_text = candidate[:400]

    total_tokens = 0
    for msg in assistant_messages:
        usage = msg.get("usage")
        if isinstance(usage, dict):
            total_tokens += int(usage.get("totalTokens") or 0)

    playwright_tools = [t for t in ordered_tools if t.startswith("mcp__playwright__") or "playwright" in t.lower()]
    unique_playwright = list(dict.fromkeys(playwright_tools))

    return {
        "turns_in_invocation": len(assistant_messages),
        "total_tokens": total_tokens,
        "tools_called": ordered_tools,
        "playwright_tools_called": playwright_tools,
        "unique_playwright_tools": unique_playwright,
        "last_url": last_url,
        "recent_actions": recent_actions[-6:],
        "tool_errors": tool_errors[-4:],
        "last_assistant_text": last_assistant_text,
    }


def parse_worker_report(text: str) -> dict[str, Any]:
    """解析执行器按模板输出的结构化字段，允许温和格式偏差。"""
    status = "COMPLETED"
    current_url = "N/A"
    summary_lines: list[str] = []
    human_action_lines: list[str] = []
    evidence_lines: list[str] = []
    active_section: str | None = None

    field_re = re.compile(
        r"^[`*\s]*(STATUS|CURRENT_URL|SUMMARY|HUMAN_ACTION_REQUIRED|EVIDENCE)[`*\s]*:\s*(.*)$",
        re.IGNORECASE,
    )

    for raw_line in (text or "").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            continue
        match = field_re.match(stripped)
        if match:
            key = match.group(1).upper()
            val = match.group(2).strip()
            active_section = key
            if key == "STATUS":
                token = val.split()[0].upper().strip("`*_.,:") if val else "COMPLETED"
                if token in VALID_WORKER_STATUSES:
                    status = token
            elif key == "CURRENT_URL":
                if val:
                    current_url = val
            elif key == "SUMMARY":
                if val:
                    summary_lines.append(val)
            elif key == "HUMAN_ACTION_REQUIRED":
                if val:
                    human_action_lines.append(val)
            elif key == "EVIDENCE":
                if val:
                    evidence_lines.append(val)
            continue

        if active_section == "SUMMARY" and stripped:
            summary_lines.append(stripped)
        elif active_section == "HUMAN_ACTION_REQUIRED" and stripped:
            human_action_lines.append(stripped)
        elif active_section == "EVIDENCE" and stripped:
            evidence_lines.append(stripped)

    summary = " ".join(summary_lines).strip() or (text or "").strip()
    human_action = " ".join(human_action_lines).strip() or "无"
    return {
        "status": status,
        "current_url": current_url,
        "summary": summary,
        "human_action_required": human_action,
        "evidence": evidence_lines,
        "raw_text": (text or "").strip(),
    }


class RpcWorker:
    """长驻 Pi `--mode rpc` 子进程封装，保持 Playwright MCP 浏览器会话跨轮在线。"""

    def __init__(
        self,
        *,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        session_id: str,
        provider: str,
        model_id: str,
        thinking: str,
        state_dir: Path,
    ) -> None:
        self.session_id = session_id
        self.provider = provider
        self.model_id = model_id
        self.thinking = thinking
        self.state_dir = state_dir
        self.created_at_mono = time.monotonic()
        self.last_active_mono = self.created_at_mono
        self.turns_executed = 0
        self.busy = False

        self._write_lock = threading.Lock()
        self._turn_lock = threading.Lock()
        self._recording = False
        self._turn_raw_lines: list[str] = []
        self._turn_stderr_lines: list[str] = []
        self._recent_stderr: list[str] = []
        self._event_queue: queue.Queue[Any] = queue.Queue()
        self._closed = False
        self._req_seq = 0

        popen_kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": env,
            "shell": False,
        }
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True

        try:
            self.proc = subprocess.Popen(cmd, **popen_kwargs)
        except OSError as exc:
            raise PiBrowserError(f"无法启动 Pi RPC 子进程：{exc}") from exc

        self.pid = self.proc.pid
        self._stdout_thread = threading.Thread(target=self._stdout_loop, daemon=True)
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def is_alive(self) -> bool:
        return not self._closed and self.proc.poll() is None

    def _next_req_id(self, prefix: str = "req") -> str:
        self._req_seq += 1
        return f"{prefix}-{self._req_seq}"

    def _write_json_line(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        with self._write_lock:
            if self.proc.stdin is None or self.proc.stdin.closed:
                raise PiBrowserError("Pi RPC stdin 已关闭，无法发送命令。")
            try:
                self.proc.stdin.write(raw)
                self.proc.stdin.flush()
            except OSError as exc:
                raise PiBrowserError(f"向 Pi RPC 写入命令失败：{exc}") from exc

    def _stdout_loop(self) -> None:
        assert self.proc.stdout is not None
        try:
            while True:
                raw_bytes = self.proc.stdout.readline()
                if not raw_bytes:
                    break
                line = raw_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue
                with self._turn_lock:
                    if self._recording:
                        self._turn_raw_lines.append(line)
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(parsed, dict):
                    continue
                if parsed.get("type") == "extension_ui_request":
                    method = parsed.get("method")
                    req_id = parsed.get("id")
                    if method in UI_DIALOG_METHODS and isinstance(req_id, str):
                        try:
                            self._write_json_line(
                                {"type": "extension_ui_response", "id": req_id, "cancelled": True}
                            )
                        except PiBrowserError:
                            pass
                    continue
                self._event_queue.put(parsed)
        finally:
            self._closed = True
            self._event_queue.put(_EOF_SENTINEL)

    def _stderr_loop(self) -> None:
        assert self.proc.stderr is not None
        try:
            while True:
                raw_bytes = self.proc.stderr.readline()
                if not raw_bytes:
                    break
                line = raw_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                with self._turn_lock:
                    self._recent_stderr.append(line)
                    if len(self._recent_stderr) > 80:
                        self._recent_stderr = self._recent_stderr[-80:]
                    if self._recording:
                        self._turn_stderr_lines.append(line)
        except Exception:
            pass

    def _drain_queue(self) -> None:
        while True:
            try:
                item = self._event_queue.get_nowait()
                if item is _EOF_SENTINEL:
                    self._event_queue.put(_EOF_SENTINEL)
                    return
            except queue.Empty:
                return

    def send_command(self, payload: dict[str, Any], *, timeout: float = 15.0) -> dict[str, Any]:
        req_id = str(payload.get("id") or self._next_req_id(str(payload.get("type") or "cmd")))
        body = {**payload, "id": req_id}
        self._write_json_line(body)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PiBrowserError(f"Pi RPC 命令超时（{body.get('type')}，{timeout:.0f}s）。")
            try:
                item = self._event_queue.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
            if item is _EOF_SENTINEL:
                rc = self.proc.poll()
                err_tail = "\n".join(self._recent_stderr[-10:]).strip()[:400]
                raise PiBrowserError(
                    f"Pi RPC 子进程在执行 {body.get('type')} 时提前退出（code={rc}）。{err_tail}"
                )
            if item.get("type") == "response" and item.get("id") == req_id:
                if not item.get("success", False):
                    raise PiBrowserError(
                        f"Pi RPC 命令 {body.get('type')} 失败：{item.get('error') or 'unknown error'}"
                    )
                return item

    def handshake(self, timeout: float = STARTUP_HANDSHAKE_TIMEOUT_SECONDS) -> dict[str, Any]:
        resp = self.send_command({"type": "get_state"}, timeout=timeout)
        self.last_active_mono = time.monotonic()
        return resp.get("data") if isinstance(resp.get("data"), dict) else {}

    def set_thinking_level(self, level: str) -> None:
        if level == self.thinking:
            return
        self.send_command({"type": "set_thinking_level", "level": level}, timeout=10.0)
        self.thinking = level

    def run_prompt(
        self,
        *,
        prompt_text: str,
        thinking: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        if not self.is_alive():
            raise PiBrowserError("Pi RPC 工作进程未运行。")

        self.set_thinking_level(thinking)
        self._drain_queue()

        with self._turn_lock:
            self._turn_raw_lines = []
            self._turn_stderr_lines = []
            self._recording = True

        req_id = self._next_req_id("prompt")
        prompt_accepted = False
        settled = False
        timed_out = False
        eof_hit = False
        aborted_cleanly = False
        reject_error: str | None = None

        t0 = time.monotonic()
        deadline = t0 + timeout_seconds
        try:
            self._write_json_line({"id": req_id, "type": "prompt", "message": prompt_text})
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    item = self._event_queue.get(timeout=min(0.25, remaining))
                except queue.Empty:
                    continue
                if item is _EOF_SENTINEL:
                    eof_hit = True
                    break
                ev_type = item.get("type")
                if ev_type == "response" and item.get("id") == req_id:
                    if not item.get("success", False):
                        reject_error = str(item.get("error") or "prompt rejected")
                        break
                    prompt_accepted = True
                    disposition = (item.get("data") or {}).get("disposition")
                    if disposition == "handled":
                        settled = True
                        break
                elif ev_type == "agent_settled":
                    settled = True
                    break

            if timed_out and self.is_alive():
                abort_id = self._next_req_id("abort")
                try:
                    self._write_json_line({"id": abort_id, "type": "abort"})
                    abort_deadline = time.monotonic() + ABORT_GRACE_SECONDS
                    while time.monotonic() < abort_deadline:
                        try:
                            item = self._event_queue.get(timeout=0.2)
                        except queue.Empty:
                            continue
                        if item is _EOF_SENTINEL:
                            break
                        if item.get("type") == "agent_settled":
                            aborted_cleanly = True
                            break
                        if item.get("type") == "response" and item.get("id") == abort_id:
                            aborted_cleanly = True
                            break
                except PiBrowserError:
                    pass
        finally:
            elapsed = round(time.monotonic() - t0, 2)
            with self._turn_lock:
                self._recording = False
                stdout_text = "\n".join(self._turn_raw_lines)
                stderr_text = "\n".join(self._turn_stderr_lines)
            self.last_active_mono = time.monotonic()

        self.turns_executed += 1
        return {
            "stdout_text": stdout_text,
            "stderr_text": stderr_text,
            "elapsed_seconds": elapsed,
            "prompt_accepted": prompt_accepted,
            "settled": settled,
            "timed_out": timed_out,
            "aborted_cleanly": aborted_cleanly,
            "eof_hit": eof_hit,
            "reject_error": reject_error,
            "returncode": self.proc.poll(),
            "recent_stderr": "\n".join(self._recent_stderr[-15:]),
        }

    def _close_pipes(self) -> None:
        with self._write_lock:
            if self.proc.stdin is not None and not self.proc.stdin.closed:
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
        for th in (self._stdout_thread, self._stderr_thread):
            if th.is_alive() and th is not threading.current_thread():
                th.join(timeout=1.0)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass

    def close(self, *, force: bool = False) -> bool:
        self._closed = True
        was_running = self.proc.poll() is None
        try:
            if not was_running:
                return False
            if not force:
                with self._write_lock:
                    try:
                        if self.proc.stdin is not None and not self.proc.stdin.closed:
                            self.proc.stdin.close()
                    except OSError:
                        pass
                try:
                    self.proc.wait(timeout=SHUTDOWN_GRACE_SECONDS)
                    return True
                except subprocess.TimeoutExpired:
                    pass
            kill_process_tree(self.pid)
            try:
                self.proc.wait(timeout=3)
            except Exception:
                pass
            return True
        finally:
            self._close_pipes()


class RpcWorkerManager:
    """进程内单例：管理长驻 Pi RPC Worker 与空闲自动回收。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._worker: RpcWorker | None = None
        self._idle_timer: threading.Timer | None = None
        self._idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS

    def _cancel_timer_locked(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _schedule_idle_timer_locked(self, idle_timeout_seconds: int) -> None:
        self._cancel_timer_locked()
        self._idle_timeout_seconds = idle_timeout_seconds
        if self._worker is None or not self._worker.is_alive():
            return
        if idle_timeout_seconds <= 0:
            self._close_worker_locked(force=False)
            return

        expected_worker = self._worker

        def _on_idle_expired() -> None:
            with self._lock:
                if self._worker is expected_worker and not expected_worker.busy:
                    self._close_worker_locked(force=False)

        timer = threading.Timer(idle_timeout_seconds, _on_idle_expired)
        timer.daemon = True
        timer.start()
        self._idle_timer = timer

    def _close_worker_locked(self, *, force: bool = False) -> dict[str, Any]:
        self._cancel_timer_locked()
        worker = self._worker
        self._worker = None
        if worker is None:
            return {"closed": False, "pid": None, "session_id": None}
        pid = worker.pid
        sid = worker.session_id
        state_dir = worker.state_dir
        was_running = worker.close(force=force)
        _clear_keepalive_file(state_dir, expected_pid=pid)
        return {"closed": True, "was_running": was_running, "pid": pid, "session_id": sid}

    def acquire_for_turn(
        self,
        *,
        session_id: str,
        provider: str,
        model_id: str,
        thinking: str,
        workdir: Path,
        pi_session_dir: Path,
        state_dir: Path,
        worker_prompt_path: Path,
        pi_cmd_prefix: list[str],
    ) -> tuple[RpcWorker, str]:
        with self._lock:
            self._cancel_timer_locked()
            existing = self._worker
            if existing is not None and existing.is_alive():
                if existing.busy:
                    raise PiBrowserError(
                        f"已有浏览器任务正在运行中（PID={existing.pid}, session_id={existing.session_id}）。"
                    )
                if (
                    existing.session_id == session_id
                    and existing.provider == provider
                    and existing.model_id == model_id
                    and existing.state_dir == state_dir
                ):
                    existing.busy = True
                    _write_keepalive_file(
                        state_dir,
                        {
                            "pid": existing.pid,
                            "session_id": existing.session_id,
                            "busy": True,
                            "updated_at": utc_now_iso(),
                            "idle_timeout_seconds": resolve_idle_timeout_seconds(),
                        },
                    )
                    return existing, "rpc_warm"
                self._close_worker_locked(force=False)
            elif existing is not None:
                self._close_worker_locked(force=False)

            # 清理跨进程遗留的旧保活进程（若有）
            stale_ka = _read_keepalive_file(state_dir)
            if stale_ka:
                stale_pid = stale_ka.get("pid")
                if isinstance(stale_pid, int) and stale_pid != os.getpid() and is_pid_running(stale_pid):
                    kill_process_tree(stale_pid)
                _clear_keepalive_file(state_dir)

            cmd = build_pi_command(
                pi_cmd_prefix=pi_cmd_prefix,
                pi_session_dir=pi_session_dir,
                session_id=session_id,
                provider=provider,
                model_id=model_id,
                thinking=thinking,
                worker_prompt_path=worker_prompt_path,
                mode="rpc",
            )
            child_env = os.environ.copy()
            child_env["ANTIGRAVITY_NO_EXTRA_TOOLS"] = "1"
            child_env["PYTHONIOENCODING"] = "utf-8"

            worker = RpcWorker(
                cmd=cmd,
                cwd=workdir,
                env=child_env,
                session_id=session_id,
                provider=provider,
                model_id=model_id,
                thinking=thinking,
                state_dir=state_dir,
            )
            try:
                worker.handshake()
            except Exception:
                worker.close(force=True)
                raise

            worker.busy = True
            self._worker = worker
            _write_keepalive_file(
                state_dir,
                {
                    "pid": worker.pid,
                    "session_id": worker.session_id,
                    "busy": True,
                    "started_at": utc_now_iso(),
                    "updated_at": utc_now_iso(),
                    "idle_timeout_seconds": resolve_idle_timeout_seconds(),
                },
            )
            return worker, "rpc_cold"

    def release_after_turn(
        self,
        worker: RpcWorker,
        *,
        keep_alive: bool = True,
        force_close: bool = False,
        idle_timeout_seconds: int | None = None,
    ) -> None:
        with self._lock:
            worker.busy = False
            if self._worker is not worker:
                if force_close or not keep_alive:
                    worker.close(force=force_close)
                return
            resolved_idle = resolve_idle_timeout_seconds(idle_timeout_seconds)
            if force_close or not keep_alive or resolved_idle <= 0 or not worker.is_alive():
                self._close_worker_locked(force=force_close)
                return
            _write_keepalive_file(
                worker.state_dir,
                {
                    "pid": worker.pid,
                    "session_id": worker.session_id,
                    "busy": False,
                    "updated_at": utc_now_iso(),
                    "idle_timeout_seconds": resolved_idle,
                    "turns_on_worker": worker.turns_executed,
                },
            )
            self._schedule_idle_timer_locked(resolved_idle)

    def get_keepalive_info(self, state_dir: Path) -> dict[str, Any] | None:
        with self._lock:
            worker = self._worker
            if worker is not None and worker.is_alive():
                return {
                    "alive": True,
                    "in_process": True,
                    "pid": worker.pid,
                    "session_id": worker.session_id,
                    "full_model": f"{worker.provider}/{worker.model_id}",
                    "thinking": worker.thinking,
                    "busy": worker.busy,
                    "turns_on_worker": worker.turns_executed,
                    "idle_seconds": round(max(0.0, time.monotonic() - worker.last_active_mono), 1),
                    "idle_timeout_seconds": self._idle_timeout_seconds,
                }
        disk = _read_keepalive_file(state_dir)
        if not disk:
            return None
        pid = disk.get("pid")
        alive = isinstance(pid, int) and is_pid_running(pid)
        if not alive:
            _clear_keepalive_file(state_dir)
            return None
        return {**disk, "alive": True, "in_process": False}

    def close_all(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            return self._close_worker_locked(force=force)


_WORKER_MANAGER = RpcWorkerManager()


def shutdown_keepalive_worker(*, force: bool = False) -> dict[str, Any]:
    return _WORKER_MANAGER.close_all(force=force)


atexit.register(lambda: shutdown_keepalive_worker(force=False))


def _wrap_initial_prompt(task: str) -> str:
    return (
        "以下是用户在 agy 中已对齐确认的 Browser Use 任务，请严格按照浏览器执行规范完成并核对证据：\n\n"
        f"{task.strip()}"
    )


def _wrap_resume_prompt(instruction: str, *, connection_mode: str) -> str:
    if connection_mode == "rpc_warm":
        prefix = (
            "当前处于同一保活浏览器会话（Playwright 与 Chrome 标签页连接保持在线，无需重开空白页）。"
            "请先用 browser_tabs / browser_snapshot 核对当前标签页与用户刚处理/拖入的页面状态，"
            "再继续执行以下指令（不要重复已完成且不可逆的操作）：\n\n"
        )
    else:
        prefix = (
            "浏览器连接因空闲超时或服务重启已重新建立（对话记忆已延续，但原有标签页可能未保持连接）。"
            "请先用 browser_tabs 确认当前标签页状态，必要时再导航回目标页面继续执行以下指令（不要重复已完成且不可逆的操作）：\n\n"
        )
    return f"{prefix}{instruction.strip()}"


def _execute_turn(
    *,
    session_meta: dict[str, Any],
    prompt_text: str,
    turn_index: int,
    thinking: str,
    timeout_seconds: int,
    state_dir: Path,
    worker_prompt_path: Path,
    pi_cmd_prefix: list[str],
    is_resume: bool = False,
) -> dict[str, Any]:
    _ensure_no_live_active_run(state_dir)

    session_id = session_meta["session_id"]
    provider = session_meta["provider"]
    model_id = session_meta["model"]
    workdir = Path(session_meta["cwd"])
    pi_session_dir = Path(session_meta["pi_session_dir"])
    workdir.mkdir(parents=True, exist_ok=True)
    pi_session_dir.mkdir(parents=True, exist_ok=True)

    logs_dir = state_dir / "logs"
    events_path = logs_dir / f"{session_id}_turn{turn_index:02d}_events.jsonl"
    stderr_path = logs_dir / f"{session_id}_turn{turn_index:02d}_stderr.txt"

    worker, connection_mode = _WORKER_MANAGER.acquire_for_turn(
        session_id=session_id,
        provider=provider,
        model_id=model_id,
        thinking=thinking,
        workdir=workdir,
        pi_session_dir=pi_session_dir,
        state_dir=state_dir,
        worker_prompt_path=worker_prompt_path,
        pi_cmd_prefix=pi_cmd_prefix,
    )

    final_prompt = (
        _wrap_resume_prompt(prompt_text, connection_mode=connection_mode)
        if is_resume
        else _wrap_initial_prompt(prompt_text)
    )

    _write_active_run(
        state_dir,
        {
            "pid": worker.pid,
            "session_id": session_id,
            "turn": turn_index,
            "started_at": utc_now_iso(),
            "full_model": f"{provider}/{model_id}",
            "connection_mode": connection_mode,
        },
    )

    keep_alive_after = True
    force_close_after = False
    try:
        run_out = worker.run_prompt(
            prompt_text=final_prompt,
            thinking=thinking,
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        keep_alive_after = False
        force_close_after = True
        raise
    finally:
        _clear_active_run(state_dir, expected_pid=worker.pid)

    stdout_data = run_out["stdout_text"]
    stderr_data = run_out["stderr_text"]
    elapsed = run_out["elapsed_seconds"]
    events_path.write_text(stdout_data or "", encoding="utf-8")
    stderr_path.write_text(stderr_data or "", encoding="utf-8")

    if run_out["reject_error"]:
        _WORKER_MANAGER.release_after_turn(worker, keep_alive=False, force_close=True)
        session_meta["status"] = "error"
        session_meta["updated_at"] = utc_now_iso()
        _save_session_meta(state_dir, session_meta)
        raise PiBrowserError(f"Pi RPC 拒绝执行提示词：{run_out['reject_error']}")

    if run_out["timed_out"]:
        # 超时后强制释放可能卡死的子进程，但完整捞回本轮已执行的中间进度返回给 agy
        _WORKER_MANAGER.release_after_turn(worker, keep_alive=False, force_close=not run_out["aborted_cleanly"])
        partial = extract_partial_progress(stdout_data)
        evidence: list[str] = [
            f"本轮已运行 {elapsed}s（达到 {timeout_seconds}s 上限），已中止并回收中间进展。",
            f"累计子轮次：{partial['turns_in_invocation']} · Playwright 工具调用：{len(partial['playwright_tools_called'])} 次 · Token：{partial['total_tokens']}",
        ]
        if partial["last_url"] != "N/A":
            evidence.append(f"最后停留页面：{partial['last_url']}")
        if partial["recent_actions"]:
            evidence.append("最近操作：" + " -> ".join(partial["recent_actions"]))
        if partial["tool_errors"]:
            for err in partial["tool_errors"]:
                evidence.append(f"工具报错：{err}")
        if partial["last_assistant_text"]:
            evidence.append(f"超时前最后输出片段：{partial['last_assistant_text']}")

        summary = (
            f"任务达到单轮超时上限（{timeout_seconds}s）已中止，已回收 {len(partial['playwright_tools_called'])} 次浏览器操作进度。"
            "可基于已收集证据拆分剩余步骤后继续。"
        )
        turn_record = {
            "turn": turn_index,
            "timestamp": utc_now_iso(),
            "thinking": thinking,
            "connection_mode": connection_mode,
            "status": "TIMEOUT",
            "current_url": partial["last_url"],
            "summary": summary,
            "elapsed_seconds": elapsed,
            "turns_in_invocation": partial["turns_in_invocation"],
            "total_tokens": partial["total_tokens"],
            "playwright_tools_count": len(partial["playwright_tools_called"]),
            "unique_playwright_tools": partial["unique_playwright_tools"],
            "events_log": str(events_path),
            "stderr_log": str(stderr_path),
        }
        session_meta["status"] = "timeout"
        session_meta["current_url"] = partial["last_url"]
        session_meta["updated_at"] = turn_record["timestamp"]
        session_meta["turns"].append(turn_record)
        _save_session_meta(state_dir, session_meta)

        return {
            "session_id": session_id,
            "turn": turn_index,
            "status": "TIMEOUT",
            "connection_mode": connection_mode,
            "keepalive_active": False,
            "idle_timeout_seconds": resolve_idle_timeout_seconds(),
            "current_url": partial["last_url"],
            "summary": summary,
            "human_action_required": "无",
            "evidence": evidence,
            "raw_text": partial["last_assistant_text"],
            "provider": provider,
            "model": model_id,
            "full_model": f"{provider}/{model_id}",
            "thinking": thinking,
            "elapsed_seconds": elapsed,
            "turns_in_invocation": partial["turns_in_invocation"],
            "total_tokens": partial["total_tokens"],
            "tools_called": partial["tools_called"],
            "playwright_tools_called": partial["playwright_tools_called"],
            "unique_playwright_tools": partial["unique_playwright_tools"],
            "events_log": str(events_path),
            "stderr_log": str(stderr_path),
        }

    if run_out["eof_hit"] and not run_out["settled"]:
        _WORKER_MANAGER.release_after_turn(worker, keep_alive=False, force_close=True)
        err_preview = (stderr_data or run_out["recent_stderr"] or stdout_data or "").strip()[:500]
        session_meta["status"] = "error"
        session_meta["updated_at"] = utc_now_iso()
        _save_session_meta(state_dir, session_meta)
        raise PiBrowserError(
            f"Pi RPC 子进程意外退出（code={run_out['returncode']}）。详情：{err_preview}；日志：{stderr_path}"
        )

    try:
        parsed = parse_pi_json_events(
            stdout_data,
            expected_provider=provider,
            expected_model=model_id,
        )
    except PiBrowserError:
        _WORKER_MANAGER.release_after_turn(worker, keep_alive=False, force_close=True)
        session_meta["status"] = "error"
        session_meta["updated_at"] = utc_now_iso()
        _save_session_meta(state_dir, session_meta)
        raise

    _WORKER_MANAGER.release_after_turn(
        worker,
        keep_alive=keep_alive_after,
        force_close=force_close_after,
    )

    report = parse_worker_report(parsed["text"])
    status_lower = report["status"].lower()
    idle_sec = resolve_idle_timeout_seconds()
    keepalive_active = idle_sec > 0 and worker.is_alive()

    turn_record = {
        "turn": turn_index,
        "timestamp": utc_now_iso(),
        "thinking": thinking,
        "connection_mode": connection_mode,
        "status": report["status"],
        "current_url": report["current_url"],
        "summary": report["summary"],
        "elapsed_seconds": elapsed,
        "turns_in_invocation": parsed["turns_in_invocation"],
        "total_tokens": parsed["total_tokens"],
        "playwright_tools_count": len(parsed["playwright_tools_called"]),
        "unique_playwright_tools": parsed["unique_playwright_tools"],
        "events_log": str(events_path),
        "stderr_log": str(stderr_path),
    }

    session_meta["status"] = status_lower
    session_meta["current_url"] = report["current_url"]
    session_meta["updated_at"] = turn_record["timestamp"]
    session_meta["turns"].append(turn_record)
    _save_session_meta(state_dir, session_meta)

    return {
        "session_id": session_id,
        "turn": turn_index,
        "status": report["status"],
        "connection_mode": connection_mode,
        "keepalive_active": keepalive_active,
        "idle_timeout_seconds": idle_sec,
        "current_url": report["current_url"],
        "summary": report["summary"],
        "human_action_required": report["human_action_required"],
        "evidence": report["evidence"],
        "raw_text": report["raw_text"],
        "provider": parsed["provider"],
        "model": parsed["model"],
        "full_model": parsed["full_model"],
        "thinking": thinking,
        "elapsed_seconds": elapsed,
        "turns_in_invocation": parsed["turns_in_invocation"],
        "total_tokens": parsed["total_tokens"],
        "tools_called": parsed["tools_called"],
        "playwright_tools_called": parsed["playwright_tools_called"],
        "unique_playwright_tools": parsed["unique_playwright_tools"],
        "events_log": str(events_path),
        "stderr_log": str(stderr_path),
    }


def run_task(
    *,
    task: str,
    thinking: str | None = None,
    timeout_seconds: int | None = None,
    model: str | None = None,
    project_root: Path | None = None,
    state_dir: Path | None = None,
    pi_cmd_prefix: list[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(task, str) or not task.strip():
        raise PiBrowserError("task 不能为空。")

    root = resolve_project_root(project_root)
    resolved_state = resolve_state_dir(root, state_dir)
    worker_prompt_path = resolve_worker_prompt(root)
    provider, model_id = parse_full_model(model)
    resolved_thinking = validate_thinking(thinking)
    resolved_timeout = validate_timeout(timeout_seconds)
    cmd_prefix = resolve_pi_invocation(pi_cmd_prefix)

    session_id = str(uuid.uuid4())
    now = utc_now_iso()
    session_meta: dict[str, Any] = {
        "session_id": session_id,
        "provider": provider,
        "model": model_id,
        "full_model": f"{provider}/{model_id}",
        "thinking": resolved_thinking,
        "cwd": str(resolved_state / "workdir"),
        "pi_session_dir": str(resolved_state / "pi_sessions"),
        "status": "running",
        "current_url": "N/A",
        "initial_task": task.strip(),
        "created_at": now,
        "updated_at": now,
        "turns": [],
    }
    _save_session_meta(resolved_state, session_meta)

    return _execute_turn(
        session_meta=session_meta,
        prompt_text=task,
        turn_index=1,
        thinking=resolved_thinking,
        timeout_seconds=resolved_timeout,
        state_dir=resolved_state,
        worker_prompt_path=worker_prompt_path,
        pi_cmd_prefix=cmd_prefix,
        is_resume=False,
    )


def _select_resume_session_id(state_dir: Path) -> str:
    sessions = _list_sessions(state_dir, limit=50)
    if not sessions:
        raise PiBrowserError("当前没有任何可续跑的浏览器会话，请先调用 pi_browser_run。")
    for item in sessions:
        if item.get("status") == "needs_human":
            return str(item["session_id"])
    return str(sessions[0]["session_id"])


def resume_task(
    *,
    instruction: str,
    session_id: str | None = None,
    thinking: str | None = None,
    timeout_seconds: int | None = None,
    project_root: Path | None = None,
    state_dir: Path | None = None,
    pi_cmd_prefix: list[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(instruction, str) or not instruction.strip():
        raise PiBrowserError("instruction 不能为空。")

    root = resolve_project_root(project_root)
    resolved_state = resolve_state_dir(root, state_dir)
    worker_prompt_path = resolve_worker_prompt(root)
    resolved_timeout = validate_timeout(timeout_seconds)
    cmd_prefix = resolve_pi_invocation(pi_cmd_prefix)

    target_sid = (
        session_id.strip()
        if isinstance(session_id, str) and session_id.strip()
        else _select_resume_session_id(resolved_state)
    )
    session_meta = _load_session_meta(resolved_state, target_sid)
    resolved_thinking = validate_thinking(thinking or session_meta.get("thinking"))
    turn_index = len(session_meta.get("turns", [])) + 1

    return _execute_turn(
        session_meta=session_meta,
        prompt_text=instruction,
        turn_index=turn_index,
        thinking=resolved_thinking,
        timeout_seconds=resolved_timeout,
        state_dir=resolved_state,
        worker_prompt_path=worker_prompt_path,
        pi_cmd_prefix=cmd_prefix,
        is_resume=True,
    )


def _find_chrome_user_data_dirs() -> list[Path]:
    home = Path.home()
    if os.name == "nt":
        local_app = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
        return [
            local_app / "Google" / "Chrome" / "User Data",
            local_app / "Microsoft" / "Edge" / "User Data",
        ]
    return [
        home / ".config" / "google-chrome",
        home / ".config" / "chromium",
        home / ".config" / "microsoft-edge",
    ]


def _check_playwright_extension_in_profile(profile_name: str | None) -> bool | None:
    target_profile = profile_name or "Default"
    for base in _find_chrome_user_data_dirs():
        ext_root = base / target_profile / "Extensions"
        if not ext_root.is_dir():
            continue
        for manifest in ext_root.glob("*/*/manifest.json"):
            try:
                text = manifest.read_text(encoding="utf-8", errors="ignore")
                if "Playwright" in text:
                    return True
            except OSError:
                continue
        return False
    return None


def _is_chrome_running() -> bool | None:
    if os.name == "nt":
        try:
            res = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            return "chrome.exe" in (res.stdout or "").lower()
        except Exception:
            return None
    pgrep = shutil.which("pgrep")
    if pgrep:
        try:
            res = subprocess.run(
                [pgrep, "-f", "chrome|chromium"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return res.returncode == 0
        except Exception:
            return None
    proc_dir = Path("/proc")
    if proc_dir.is_dir():
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                comm = (entry / "comm").read_text(encoding="utf-8", errors="ignore").lower()
                if "chrome" in comm or "chromium" in comm:
                    return True
            except OSError:
                continue
        return False
    return None


def _check_environment_health(project_root: Path) -> dict[str, Any]:
    agent_dir = get_pi_agent_dir()
    health: dict[str, Any] = {
        "worker_prompt_present": (project_root / "prompts" / "browser_worker.md").is_file(),
        "pi_auth_file_present": (agent_dir / "auth.json").is_file(),
        "pi_version": None,
        "pi_invocation_ready": False,
        "pi_invocation_error": None,
        "pi_mcp_playwright_enabled": False,
        "pi_mcp_playwright_exposure": None,
        "pi_mcp_playwright_uses_extension": None,
        "pi_mcp_playwright_profile": None,
        "pi_mcp_playwright_has_token": None,
        "pi_mcp_playwright_connect_mode": None,
        "chrome_profile_extension_installed": None,
        "chrome_running": None,
    }

    version_file = agent_dir / "install" / "current-version"
    if version_file.is_file():
        try:
            health["pi_version"] = version_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    try:
        resolve_pi_invocation()
        health["pi_invocation_ready"] = True
    except PiBrowserError as exc:
        health["pi_invocation_error"] = str(exc)

    mcp_file = agent_dir / "mcp.json"
    if mcp_file.is_file():
        try:
            cfg = json.loads(mcp_file.read_text(encoding="utf-8"))
            pw = (cfg.get("mcpServers") or {}).get("playwright")
            if isinstance(pw, dict):
                health["pi_mcp_playwright_enabled"] = pw.get("enabled", True) is not False
                health["pi_mcp_playwright_exposure"] = pw.get("exposure")
                args = [str(a) for a in (pw.get("args") or [])]
                env = pw.get("env") if isinstance(pw.get("env"), dict) else {}
                uses_ext = "--extension" in args
                profile_name: str | None = None
                if "--profile-dir-name" in args:
                    idx = args.index("--profile-dir-name")
                    if idx + 1 < len(args):
                        profile_name = args[idx + 1]
                elif isinstance(env.get("PLAYWRIGHT_MCP_PROFILE_DIR_NAME"), str):
                    profile_name = env["PLAYWRIGHT_MCP_PROFILE_DIR_NAME"].strip() or None
                has_token = bool(str(env.get("PLAYWRIGHT_MCP_EXTENSION_TOKEN") or "").strip())

                health["pi_mcp_playwright_uses_extension"] = uses_ext
                health["pi_mcp_playwright_profile"] = profile_name
                health["pi_mcp_playwright_has_token"] = has_token
                if uses_ext:
                    health["pi_mcp_playwright_connect_mode"] = (
                        "extension_token_auto" if has_token else "extension_tab_picker"
                    )
                    health["chrome_profile_extension_installed"] = _check_playwright_extension_in_profile(profile_name)
                elif "--cdp-endpoint" in args:
                    health["pi_mcp_playwright_connect_mode"] = "cdp_endpoint"
                else:
                    health["pi_mcp_playwright_connect_mode"] = "standalone"
        except Exception:
            pass

    health["chrome_running"] = _is_chrome_running()
    return health


def get_status(
    *,
    session_id: str | None = None,
    include_health: bool = True,
    project_root: Path | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    root = resolve_project_root(project_root)
    resolved_state = resolve_state_dir(root, state_dir)

    active = _read_active_run(resolved_state)
    active_info: dict[str, Any] | None = None
    if active:
        pid = active.get("pid")
        active_info = {
            **active,
            "alive": is_pid_running(pid) if isinstance(pid, int) else False,
        }

    result: dict[str, Any] = {
        "state_dir": str(resolved_state),
        "active_run": active_info,
        "keepalive_worker": _WORKER_MANAGER.get_keepalive_info(resolved_state),
    }

    if isinstance(session_id, str) and session_id.strip():
        result["session"] = _load_session_meta(resolved_state, session_id.strip())
    else:
        recent = _list_sessions(resolved_state, limit=10)
        result["recent_sessions"] = [
            {
                "session_id": s.get("session_id"),
                "status": s.get("status"),
                "full_model": s.get("full_model"),
                "current_url": s.get("current_url"),
                "turns_count": len(s.get("turns", [])),
                "updated_at": s.get("updated_at"),
                "initial_task": (s.get("initial_task") or "")[:80],
            }
            for s in recent
        ]

    if include_health:
        result["health"] = _check_environment_health(root)

    return result


def stop_active(
    *,
    session_id: str | None = None,
    project_root: Path | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    root = resolve_project_root(project_root)
    resolved_state = resolve_state_dir(root, state_dir)
    expected_sid = session_id.strip() if isinstance(session_id, str) and session_id.strip() else None

    active = _read_active_run(resolved_state)
    ka_info = _WORKER_MANAGER.get_keepalive_info(resolved_state)

    if not active and not ka_info:
        return {"stopped": False, "message": "当前没有运行中或保活中的 Pi 浏览器会话。"}

    target_sid = (active or {}).get("session_id") or (ka_info or {}).get("session_id")
    if expected_sid and target_sid and target_sid != expected_sid:
        raise PiBrowserError(
            f"当前活动/保活会话属于 session_id={target_sid}，与传入的 {expected_sid} 不匹配。"
        )

    stopped_pids: list[int] = []
    was_running = False

    ka_close = _WORKER_MANAGER.close_all(force=bool(active))
    if ka_close.get("closed") and isinstance(ka_close.get("pid"), int):
        stopped_pids.append(ka_close["pid"])
        was_running = was_running or bool(ka_close.get("was_running"))

    for lock_data, clear_fn in (
        (active, _clear_active_run),
        (_read_keepalive_file(resolved_state), _clear_keepalive_file),
    ):
        if not lock_data:
            continue
        pid = lock_data.get("pid")
        if isinstance(pid, int) and pid not in stopped_pids and is_pid_running(pid):
            kill_process_tree(pid)
            stopped_pids.append(pid)
            was_running = True
        clear_fn(resolved_state)

    if isinstance(target_sid, str) and _session_file_path(resolved_state, target_sid).is_file():
        meta = _load_session_meta(resolved_state, target_sid)
        if active:
            meta["status"] = "stopped"
            meta["updated_at"] = utc_now_iso()
            _save_session_meta(resolved_state, meta)

    return {
        "stopped": True,
        "session_id": target_sid,
        "pids": stopped_pids,
        "was_running": was_running,
        "message": (
            "已终止活动任务并断开保活浏览器连接（Chrome 标签组已释放）。"
            if active
            else "已关闭保活浏览器会话并释放 Chrome 标签组连接。"
        ),
    }


def format_turn_result_markdown(result: dict[str, Any]) -> str:
    status = result["status"]
    badge = {
        "COMPLETED": "✅ COMPLETED",
        "NEEDS_HUMAN": "⏸️ NEEDS_HUMAN",
        "BLOCKED": "⚠️ BLOCKED",
        "TIMEOUT": "⏱️ TIMEOUT",
    }.get(status, status)

    pw_tools = result.get("unique_playwright_tools") or []
    pw_summary = (
        f"{len(result.get('playwright_tools_called') or [])} 次（{', '.join(pw_tools)}）"
        if pw_tools
        else "0 次（未检测到 mcp__playwright 工具调用）"
    )

    conn_mode = result.get("connection_mode", "rpc_cold")
    idle_sec = result.get("idle_timeout_seconds", DEFAULT_IDLE_TIMEOUT_SECONDS)
    if conn_mode == "rpc_warm":
        conn_desc = (
            f"RPC 保活复用（原标签页上下文已保持；空闲 {idle_sec}s 自动释放）"
            if result.get("keepalive_active")
            else "RPC 保活复用（本轮结束后已释放）"
        )
    else:
        conn_desc = (
            f"RPC 新建连接（保活已开启，空闲 {idle_sec}s 自动释放）"
            if result.get("keepalive_active")
            else "RPC 新建连接（本轮结束后已释放）"
        )

    lines = [
        f"### {badge}",
        "",
        f"- **session_id**: `{result['session_id']}`（第 {result['turn']} 轮）",
        f"- **连接模式**: {conn_desc}",
        f"- **实际模型**: `{result['full_model']}`（thinking: `{result['thinking']}`）",
        f"- **执行耗时**: {result['elapsed_seconds']}s · 子轮次: {result['turns_in_invocation']} · Token: {result['total_tokens']}",
        f"- **Playwright 调用**: {pw_summary}",
        f"- **CURRENT_URL**: {result['current_url']}",
        f"- **SUMMARY**: {result['summary']}",
    ]

    if status == "NEEDS_HUMAN" or result.get("human_action_required") not in (None, "", "无"):
        lines.append(f"- **HUMAN_ACTION_REQUIRED**: {result['human_action_required']}")

    evidence = result.get("evidence") or []
    if evidence:
        lines.append("- **EVIDENCE**:")
        for item in evidence:
            lines.append(f"  {item if item.startswith('-') else '- ' + item}")
    else:
        lines.append("")
        lines.append("**执行器原文输出**：")
        lines.append(result.get("raw_text") or "")

    lines.append(f"- **事件日志**: `{result['events_log']}`")
    return "\n".join(lines)
