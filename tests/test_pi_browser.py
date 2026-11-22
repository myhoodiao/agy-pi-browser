"""agy-pi-browser v2.0 离线单元测试套件（覆盖 Pi RPC 保活、热续跑、超时进展回收、空闲回收与 MCP 协议）。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import pi_browser_core as core  # noqa: E402
import pi_browser_mcp as mcp_srv  # noqa: E402


FAKE_PI_RPC_SCRIPT = r"""
import json
import os
from pathlib import Path
import sys
import time

args = sys.argv[1:]

def flag_value(name):
    if name in args:
        idx = args.index(name)
        if idx + 1 < len(args):
            return args[idx + 1]
    return None

session_dir = Path(flag_value("--session-dir"))
session_id = flag_value("--session-id")
provider = flag_value("--provider")
model = flag_value("--model")
thinking = flag_value("--thinking") or "medium"
mode = os.environ.get("FAKE_PI_MODE", "success")

marker = session_dir / f"{session_id}.state.json"

def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()

def load_state():
    if marker.exists():
        return json.loads(marker.read_text(encoding="utf-8"))
    return {"turn": 0, "pids": [], "thinkings": [], "cwd": os.getcwd()}

for raw_line in sys.stdin:
    line = raw_line.strip()
    if not line:
        continue
    cmd = json.loads(line)
    ctype = cmd.get("type")
    req_id = cmd.get("id")

    if ctype == "get_state":
        emit({
            "id": req_id,
            "type": "response",
            "command": "get_state",
            "success": True,
            "data": {
                "sessionId": session_id,
                "thinkingLevel": thinking,
                "isStreaming": False,
            },
        })
        continue

    if ctype == "set_thinking_level":
        thinking = cmd.get("level") or thinking
        # 模拟真实 Pi：先发异步事件再发 command response
        emit({"type": "thinking_level_changed", "level": thinking})
        emit({"id": req_id, "type": "response", "command": "set_thinking_level", "success": True})
        continue

    if ctype == "abort":
        emit({"id": req_id, "type": "response", "command": "abort", "success": True})
        emit({"type": "agent_settled"})
        continue

    if ctype == "prompt":
        prompt = cmd.get("message", "")
        if mode == "reject_prompt":
            emit({"id": req_id, "type": "response", "command": "prompt", "success": False, "error": "busy"})
            continue

        emit({
            "id": req_id,
            "type": "response",
            "command": "prompt",
            "success": True,
            "data": {"disposition": "started"},
        })

        if mode == "crash":
            sys.stderr.write("simulated rpc crash\n")
            sys.stderr.flush()
            sys.exit(2)

        if mode == "timeout_with_progress":
            emit({"type": "tool_execution_start", "toolCallId": "t1", "toolName": "mcp__playwright__browser_navigate", "args": {"url": "https://console.cloud.google.com/billing"}})
            emit({"type": "tool_execution_end", "toolCallId": "t1", "toolName": "mcp__playwright__browser_navigate", "isError": False, "result": {"content": [{"type": "text", "text": "### Page\n- Page URL: https://console.cloud.google.com/billing/012345\n"}]}})
            emit({"type": "tool_execution_start", "toolCallId": "t2", "toolName": "mcp__playwright__browser_click", "args": {"target": "e99"}})
            emit({"type": "tool_execution_end", "toolCallId": "t2", "toolName": "mcp__playwright__browser_click", "isError": True, "result": {"content": [{"type": "text", "text": "### Error\nTimeoutError: Timeout 5000ms exceeded."}]}})
            emit({
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "provider": provider,
                    "model": model,
                    "stopReason": "toolUse",
                    "usage": {"totalTokens": 1234},
                    "content": [{"type": "text", "text": "已核对账单首页，正在展开赠金明细"}],
                },
            })
            time.sleep(10)
            continue

        if mode == "error_stop":
            emit({
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "provider": provider,
                    "model": model,
                    "stopReason": "error",
                    "errorMessage": "quota exhausted",
                    "content": [],
                },
            })
            emit({"type": "agent_settled"})
            continue

        actual_model = "gpt-6-astra" if mode == "wrong_model" else model

        state = load_state()
        turn = int(state["turn"]) + 1
        state["turn"] = turn
        state["pids"].append(os.getpid())
        state["thinkings"].append(thinking)
        state["last_prompt"] = prompt
        state["cwd"] = os.getcwd()
        marker.write_text(json.dumps(state), encoding="utf-8")

        # 测试 extension_ui_request 自动取消不会卡死
        if mode == "ui_dialog":
            emit({"type": "extension_ui_request", "id": "ui-1", "method": "confirm", "title": "Confirm"})

        if mode == "needs_human_then_complete" and turn == 1:
            text = (
                "STATUS: NEEDS_HUMAN\n"
                "CURRENT_URL: about:blank\n"
                "SUMMARY: 未发现目标结账页，等待用户把标签页拖入彩色 Playwright 标签组。\n"
                "HUMAN_ACTION_REQUIRED: 请把目标标签页拖进 Chrome 顶部彩色 Playwright 标签组后回复继续。\n"
                "EVIDENCE:\n"
                "- 当前仅见空白标签页"
            )
        elif mode == "needs_human_then_complete" and turn >= 2:
            text = (
                "STATUS: COMPLETED\n"
                "CURRENT_URL: https://example.com/checkout\n"
                "SUMMARY: 已复用保活连接接管用户拖入的页面并完成操作。\n"
                "HUMAN_ACTION_REQUIRED: 无\n"
                "EVIDENCE:\n"
                "- 已在原标签页确认提交成功"
            )
        else:
            text = (
                "STATUS: COMPLETED\n"
                "CURRENT_URL: https://mail.google.com/mail/u/0/#inbox\n"
                "SUMMARY: 测试任务执行成功。\n"
                "HUMAN_ACTION_REQUIRED: 无\n"
                "EVIDENCE:\n"
                "- 页面已出现 Message sent"
            )

        emit({"type": "tool_execution_start", "toolCallId": f"tc-{turn}", "toolName": "mcp__playwright__browser_snapshot", "args": {}})
        emit({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "provider": provider,
                "model": actual_model,
                "stopReason": "stop",
                "usage": {"totalTokens": 321},
                "content": [
                    {"type": "toolCall", "id": f"tc-{turn}", "name": "mcp__playwright__browser_snapshot", "arguments": {}},
                    {"type": "text", "text": text},
                ],
            },
        })
        emit({"type": "agent_settled"})
"""


class PiBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        core.shutdown_keepalive_worker(force=True)
        mcp_srv._CACHED_CORE = None
        mcp_srv._CACHED_CORE_PATH = None
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.fake_pi = self.root / "fake_pi_rpc.py"
        self.fake_pi.write_text(FAKE_PI_RPC_SCRIPT, encoding="utf-8")
        self.pi_cmd = [sys.executable, "-u", str(self.fake_pi)]
        self.old_env = os.environ.copy()

    def tearDown(self) -> None:
        core.shutdown_keepalive_worker(force=True)
        if mcp_srv._CACHED_CORE is not None:
            mcp_srv._CACHED_CORE.shutdown_keepalive_worker(force=True)
        mcp_srv._CACHED_CORE = None
        mcp_srv._CACHED_CORE_PATH = None
        os.environ.clear()
        os.environ.update(self.old_env)
        self.tmp.cleanup()

    def test_build_pi_command_rpc_flags(self) -> None:
        cmd = core.build_pi_command(
            pi_cmd_prefix=self.pi_cmd,
            pi_session_dir=self.state_dir / "pi_sessions",
            session_id="abc-123",
            provider="openai-codex",
            model_id="gpt-6.1-terra",
            thinking="high",
            worker_prompt_path=REPO_ROOT / "prompts" / "browser_worker.md",
        )
        self.assertEqual(cmd[cmd.index("--mode") + 1], "rpc")
        self.assertNotIn("--print", cmd)
        self.assertIn("--no-extensions", cmd)
        self.assertIn("builtin:mcp", cmd)
        self.assertIn("builtin:tool-search", cmd)
        self.assertEqual(cmd[cmd.index("--exclude-tools") + 1], core.EXCLUDED_TOOLS)
        self.assertEqual(cmd[cmd.index("--provider") + 1], "openai-codex")
        self.assertEqual(cmd[cmd.index("--model") + 1], "gpt-6.1-terra")
        self.assertEqual(cmd[cmd.index("--thinking") + 1], "high")

    def test_run_task_success_keepalive_and_stop(self) -> None:
        self.assertEqual(core.DEFAULT_THINKING, "medium")
        os.environ["FAKE_PI_MODE"] = "success"
        result = core.run_task(
            task="打开 Gmail 并发送测试邮件",
            project_root=REPO_ROOT,
            state_dir=self.state_dir,
            pi_cmd_prefix=self.pi_cmd,
        )
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["connection_mode"], "rpc_cold")
        self.assertTrue(result["keepalive_active"])
        self.assertEqual(result["full_model"], "openai-codex/gpt-6.1-terra")
        self.assertEqual(result["unique_playwright_tools"], ["mcp__playwright__browser_snapshot"])
        self.assertTrue(Path(result["events_log"]).is_file())
        self.assertFalse((self.state_dir / "active_run.json").exists())

        status = core.get_status(include_health=False, project_root=REPO_ROOT, state_dir=self.state_dir)
        self.assertIsNotNone(status["keepalive_worker"])
        self.assertTrue(status["keepalive_worker"]["alive"])
        self.assertEqual(status["keepalive_worker"]["session_id"], result["session_id"])

        stop_res = core.stop_active(project_root=REPO_ROOT, state_dir=self.state_dir)
        self.assertTrue(stop_res["stopped"])
        status_after = core.get_status(include_health=False, project_root=REPO_ROOT, state_dir=self.state_dir)
        self.assertIsNone(status_after["keepalive_worker"])

    def test_needs_human_then_warm_resume_reuses_same_rpc_pid(self) -> None:
        os.environ["FAKE_PI_MODE"] = "needs_human_then_complete"
        t1 = core.run_task(
            task="接管用户打开的结账页",
            thinking="medium",
            project_root=REPO_ROOT,
            state_dir=self.state_dir,
            pi_cmd_prefix=self.pi_cmd,
        )
        self.assertEqual(t1["status"], "NEEDS_HUMAN")
        self.assertEqual(t1["connection_mode"], "rpc_cold")
        sid = t1["session_id"]

        # 省略 session_id 时自动挑 needs_human 会话，并动态切换 thinking 等级，同时复用同一 RPC 进程
        t2 = core.resume_task(
            instruction="已把目标页拖进 Playwright 标签组，请继续。",
            thinking="high",
            project_root=REPO_ROOT,
            state_dir=self.state_dir,
            pi_cmd_prefix=self.pi_cmd,
        )
        self.assertEqual(t2["session_id"], sid)
        self.assertEqual(t2["turn"], 2)
        self.assertEqual(t2["status"], "COMPLETED")
        self.assertEqual(t2["connection_mode"], "rpc_warm")
        self.assertEqual(t2["thinking"], "high")

        marker = self.state_dir / "pi_sessions" / f"{sid}.state.json"
        recorded = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(recorded["turn"], 2)
        self.assertEqual(len(recorded["pids"]), 2)
        self.assertEqual(recorded["pids"][0], recorded["pids"][1], "热续跑必须复用同一个常驻 Pi RPC PID")
        self.assertEqual(recorded["thinkings"], ["medium", "high"])

        # 关闭保活后再续跑应自动回退为 rpc_cold（新 PID）
        core.shutdown_keepalive_worker(force=False)
        t3 = core.resume_task(
            instruction="再次复核页面结果",
            session_id=sid,
            project_root=REPO_ROOT,
            state_dir=self.state_dir,
            pi_cmd_prefix=self.pi_cmd,
        )
        self.assertEqual(t3["connection_mode"], "rpc_cold")
        recorded_after = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(recorded_after["turn"], 3)
        self.assertNotEqual(recorded_after["pids"][1], recorded_after["pids"][2])

    def test_idle_timeout_auto_closes_worker(self) -> None:
        os.environ["FAKE_PI_MODE"] = "success"
        os.environ["AGY_PI_BROWSER_IDLE_TIMEOUT"] = "1"
        core.run_task(
            task="测试空闲自动回收",
            project_root=REPO_ROOT,
            state_dir=self.state_dir,
            pi_cmd_prefix=self.pi_cmd,
        )
        time.sleep(1.8)
        status = core.get_status(include_health=False, project_root=REPO_ROOT, state_dir=self.state_dir)
        self.assertIsNone(status["keepalive_worker"])

    def test_timeout_recovers_partial_progress_and_cleans_lock(self) -> None:
        os.environ["FAKE_PI_MODE"] = "timeout_with_progress"
        orig_min = core.MIN_TIMEOUT_SECONDS
        core.MIN_TIMEOUT_SECONDS = 1
        try:
            res = core.run_task(
                task="测试超时进展回收",
                timeout_seconds=1,
                project_root=REPO_ROOT,
                state_dir=self.state_dir,
                pi_cmd_prefix=self.pi_cmd,
            )
            self.assertEqual(res["status"], "TIMEOUT")
            self.assertEqual(res["current_url"], "https://console.cloud.google.com/billing/012345")
            self.assertEqual(len(res["playwright_tools_called"]), 2)
            self.assertEqual(res["total_tokens"], 1234)
            md = core.format_turn_result_markdown(res)
            self.assertIn("TIMEOUT", md)
            self.assertIn("Timeout 5000ms exceeded", md)
            self.assertIn("已核对账单首页", md)
            self.assertFalse((self.state_dir / "active_run.json").exists())
            status = core.get_status(include_health=False, project_root=REPO_ROOT, state_dir=self.state_dir)
            self.assertIsNone(status["keepalive_worker"])
        finally:
            core.MIN_TIMEOUT_SECONDS = orig_min

    def test_error_paths_model_mismatch_crash_and_error_stop(self) -> None:
        for mode in ("wrong_model", "crash", "error_stop", "reject_prompt"):
            os.environ["FAKE_PI_MODE"] = mode
            with self.assertRaises(core.PiBrowserError, msg=f"mode={mode} 应抛出 PiBrowserError"):
                core.run_task(
                    task=f"测试异常路径 {mode}",
                    project_root=REPO_ROOT,
                    state_dir=self.state_dir,
                    pi_cmd_prefix=self.pi_cmd,
                )
            self.assertFalse((self.state_dir / "active_run.json").exists())

    def test_extension_ui_request_auto_cancelled_without_hang(self) -> None:
        os.environ["FAKE_PI_MODE"] = "ui_dialog"
        res = core.run_task(
            task="测试扩展 UI 弹窗自动取消",
            project_root=REPO_ROOT,
            state_dir=self.state_dir,
            pi_cmd_prefix=self.pi_cmd,
        )
        self.assertEqual(res["status"], "COMPLETED")

    def test_concurrent_lock_blocks_overlapping_run(self) -> None:
        resolved = core.resolve_state_dir(REPO_ROOT, self.state_dir)
        (resolved / "active_run.json").write_text(
            json.dumps({"pid": os.getpid(), "session_id": "busy-1"}),
            encoding="utf-8",
        )
        with self.assertRaises(core.PiBrowserError) as ctx:
            core.run_task(
                task="并发抢占测试",
                project_root=REPO_ROOT,
                state_dir=self.state_dir,
                pi_cmd_prefix=self.pi_cmd,
            )
        self.assertIn("busy-1", str(ctx.exception))

    def test_health_diagnostics_never_leak_extension_token(self) -> None:
        fake_agent_dir = self.root / "fake_pi_agent"
        fake_agent_dir.mkdir(parents=True, exist_ok=True)
        secret_token = "SUPER_SECRET_TOKEN_VALUE_123456"
        (fake_agent_dir / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "playwright": {
                            "command": "node",
                            "args": ["cli.js", "--extension", "--profile-dir-name", "Default"],
                            "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": secret_token},
                            "enabled": True,
                            "exposure": "deferred",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        os.environ["PI_CODING_AGENT_DIR"] = str(fake_agent_dir)
        health = core._check_environment_health(REPO_ROOT)
        self.assertTrue(health["pi_mcp_playwright_enabled"])
        self.assertTrue(health["pi_mcp_playwright_uses_extension"])
        self.assertTrue(health["pi_mcp_playwright_has_token"])
        self.assertEqual(health["pi_mcp_playwright_profile"], "Default")
        self.assertEqual(health["pi_mcp_playwright_connect_mode"], "extension_token_auto")
        self.assertNotIn(secret_token, json.dumps(health))

    def test_mcp_jsonrpc_protocol_and_cached_warm_resume(self) -> None:
        os.environ["AGY_PI_BROWSER_ROOT"] = str(REPO_ROOT)
        os.environ["AGY_PI_BROWSER_STATE_DIR"] = str(self.state_dir)
        os.environ["AGY_PI_LAUNCHER_CMD"] = json.dumps(self.pi_cmd)
        os.environ["FAKE_PI_MODE"] = "needs_human_then_complete"

        init_resp = mcp_srv.handle_jsonrpc_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}
        )
        self.assertEqual(init_resp["result"]["serverInfo"]["name"], "agy-pi-browser-mcp")
        self.assertEqual(init_resp["result"]["serverInfo"]["version"], "2.0.0")

        list_resp = mcp_srv.handle_jsonrpc_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool_names = [t["name"] for t in list_resp["result"]["tools"]]
        self.assertEqual(tool_names, ["pi_browser_run", "pi_browser_resume", "pi_browser_status", "pi_browser_stop"])

        run_resp = mcp_srv.handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "pi_browser_run", "arguments": {"task": "打开测试页面"}},
            }
        )
        self.assertFalse(run_resp["result"]["isError"])
        self.assertIn("NEEDS_HUMAN", run_resp["result"]["content"][0]["text"])

        resume_resp = mcp_srv.handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "pi_browser_resume", "arguments": {"instruction": "继续执行"}},
            }
        )
        self.assertFalse(resume_resp["result"]["isError"])
        resume_text = resume_resp["result"]["content"][0]["text"]
        self.assertIn("COMPLETED", resume_text)
        self.assertIn("RPC 保活复用", resume_text)

        stop_resp = mcp_srv.handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "pi_browser_stop", "arguments": {}},
            }
        )
        self.assertFalse(stop_resp["result"]["isError"])

        unknown_resp = mcp_srv.handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "nonexistent_tool", "arguments": {}},
            }
        )
        self.assertEqual(unknown_resp["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
