import importlib.util
import json
import uuid
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen


MODULE_PATH = Path(__file__).resolve().parent.parent / "codex_a2a_bridge.py"
SPEC = importlib.util.spec_from_file_location("codex_a2a_bridge", MODULE_PATH)
bridge_module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(bridge_module)


class FakeBridge:
    sync_wait = 2

    def __init__(self, state_dir: Path, hide_orphan_tasks: bool = False):
        self.store = bridge_module.TaskStore(state_dir / "tasks.json")
        self.sessions = bridge_module.SessionStore(state_dir / "sessions.json")
        self.token = None
        self.hide_orphan_tasks = hide_orphan_tasks

    def card(self, base_url):
        return bridge_module.CodexBridge.card(self, base_url)

    def delete_task(self, task_id):
        # 复用真实删除逻辑（找 codex 可执行文件部分会被跳过：无 session 文件时安全）
        return bridge_module.CodexBridge.delete_task(self, task_id)

    def handle_inbound_event(self, event):
        return bridge_module.CodexBridge.handle_inbound_event(self, event)

    def start_task(self, prompt, context_id):
        task_id = "task-test"
        task = {
            "id": task_id,
            "contextId": context_id,
            "status": {
                "state": "TASK_STATE_COMPLETED",
                "timestamp": bridge_module.utc_timestamp(),
                "message": bridge_module.agent_message(f"echo:{prompt}", context_id),
            },
            "artifacts": [{"artifactId": "artifact-test", "parts": [bridge_module.text_part(f"echo:{prompt}")]}],
        }
        self.store.add(task).set()
        return task_id


class BridgeProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        fake = FakeBridge(Path(self.temp.name))
        self.server = bridge_module.BridgeServer(("127.0.0.1", 0), fake)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def rpc(self, method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": "test", "method": method, "params": params}).encode()
        req = Request(self.url, data=body, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=5) as response:
            return json.load(response)

    def test_agent_card(self):
        with urlopen(self.url + "/.well-known/agent-card.json", timeout=5) as response:
            card = json.load(response)
        self.assertEqual(card["name"], "Codex CLI")
        self.assertEqual(card["supportedInterfaces"][0]["protocolVersion"], "1.0")
        self.assertEqual(card["skills"][0]["name"], "coding")

    def test_pascal_case_send_message(self):
        result = self.rpc("SendMessage", {"message": {"role": "ROLE_USER", "parts": [{"text": "hello"}]}})
        self.assertEqual(result["result"]["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(result["result"]["artifacts"][0]["parts"][0]["text"], "echo:hello")

    def test_legacy_send_and_get(self):
        sent = self.rpc("message/send", {"message": {"role": "user", "parts": [{"text": "legacy"}]}})
        task_id = sent["result"]["id"]
        fetched = self.rpc("tasks/get", {"id": task_id})
        self.assertEqual(fetched["result"]["artifacts"][0]["parts"][0]["text"], "echo:legacy")


if __name__ == "__main__":
    unittest.main()


class TaskEventLogTests(unittest.TestCase):
    """实时监控事件流：append_event / events_since 环形缓冲与增量游标。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = bridge_module.TaskStore(Path(self.temp.name) / "tasks.json")
        self.store.add({
            "id": "t-events",
            "contextId": "ctx-events",
            "status": {"state": "TASK_STATE_WORKING", "timestamp": bridge_module.utc_timestamp()},
        })

    def tearDown(self):
        self.temp.cleanup()

    def test_append_and_incremental_poll(self):
        self.store.append_event("t-events", {"type": "session", "ts": "t1", "text": "会话已创建"})
        self.store.append_event("t-events", {"type": "message", "ts": "t2", "text": "思考中"})
        self.store.append_event("t-events", {"type": "tool", "ts": "t3", "text": "调用工具 read_file"})

        # 全量拉取
        events, latest = self.store.events_since("t-events", after=-1)
        self.assertEqual(latest, 2)
        self.assertEqual([e["seq"] for e in events], [0, 1, 2])
        self.assertEqual(events[0]["type"], "session")
        self.assertEqual(events[2]["type"], "tool")

        # 增量拉取：after=1 只返回 2 之后的
        events, latest = self.store.events_since("t-events", after=1)
        self.assertEqual([e["seq"] for e in events], [2])

        # after=latest 返回空
        events, latest = self.store.events_since("t-events", after=2)
        self.assertEqual(events, [])
        self.assertEqual(latest, 2)

    def test_ring_buffer_caps_at_limit(self):
        for i in range(bridge_module.EVENT_LOG_LINES + 50):
            self.store.append_event("t-events", {"type": "item", "ts": f"t{i}", "text": f"e{i}"})
        events, latest = self.store.events_since("t-events", after=-1)
        self.assertEqual(len(events), bridge_module.EVENT_LOG_LINES)
        # 最早的 seq 被挤掉了
        self.assertEqual(events[0]["seq"], 50)
        self.assertEqual(latest, bridge_module.EVENT_LOG_LINES + 49)

    def test_unknown_task_returns_empty(self):
        events, latest = self.store.events_since("t-nope", after=-1)
        self.assertEqual(events, [])
        self.assertEqual(latest, -1)


class MonitorEndpointTests(unittest.TestCase):
    """GET /ui、/tasks、/tasks/<id>/events 监控端点。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        fake = FakeBridge(Path(self.temp.name))
        self.server = bridge_module.BridgeServer(("127.0.0.1", 0), fake)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_ui_page_served_without_auth(self):
        with urlopen(self.url + "/ui", timeout=5) as response:
            body = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("Codex A2A 桥", body)
        self.assertIn("refreshTasks", body)
        self.assertIn("confirmDlg", body)

    def test_task_list_endpoint(self):
        fake_id = self.rpc_send()
        with urlopen(self.url + "/tasks", timeout=5) as response:
            payload = json.load(response)
        tasks = payload["tasks"]
        self.assertTrue(any(t["id"] == fake_id for t in tasks))
        listed = next(t for t in tasks if t["id"] == fake_id)
        self.assertEqual(listed["state"], "COMPLETED")

    def test_task_events_endpoint_incremental(self):
        task_id = self.rpc_send()
        # 初始空
        with urlopen(self.url + f"/tasks/{task_id}/events?after=-1", timeout=5) as response:
            first = json.load(response)
        self.assertEqual(first["events"], [])
        self.assertEqual(first["latest"], -1)

        # 手动追加事件后增量拉取
        self.server.bridge.store.append_event(task_id, {"type": "message", "ts": "t1", "text": "你好"})
        self.server.bridge.store.append_event(task_id, {"type": "tool", "ts": "t2", "text": "调用工具"})
        with urlopen(self.url + f"/tasks/{task_id}/events?after=-1", timeout=5) as response:
            full = json.load(response)
        self.assertEqual([e["seq"] for e in full["events"]], [0, 1])
        with urlopen(self.url + f"/tasks/{task_id}/events?after=0", timeout=5) as response:
            incr = json.load(response)
        self.assertEqual([e["seq"] for e in incr["events"]], [1])

    def test_unknown_task_events_returns_empty_list(self):
        # events 对不存在任务返回空（HTTP 200 + 空列表，不是错误）
        with urlopen(self.url + "/tasks/nope/events?after=-1", timeout=5) as response:
            payload = json.load(response)
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["events"], [])

    def rpc_send(self):
        body = json.dumps({"jsonrpc": "2.0", "id": "t", "method": "SendMessage",
                           "params": {"message": {"role": "ROLE_USER", "parts": [{"text": "hi"}]}}}).encode()
        req = Request(self.url, data=body, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=5) as response:
            result = json.load(response)
        return result["result"]["id"]


class ReviewFixTests(unittest.TestCase):
    """二轮复审修复的回归测试：Host 校验、query_brief、append_event 防护、工具事件提取。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = bridge_module.TaskStore(Path(self.temp.name) / "tasks.json")
        self.store.add({
            "id": "t-review",
            "contextId": "ctx-review",
            "status": {"state": "TASK_STATE_WORKING", "timestamp": bridge_module.utc_timestamp()},
        })

    def tearDown(self):
        self.temp.cleanup()

    def test_append_event_rejects_unknown_task(self):
        # 未知/已淘汰任务不得创建孤儿事件日志
        self.store.append_event("t-ghost", {"type": "message", "text": "x"})
        events, latest = self.store.events_since("t-ghost", after=-1)
        self.assertEqual(events, [])
        self.assertEqual(latest, -1)
        self.assertNotIn("t-ghost", self.store.event_logs)

    def test_append_event_ignores_caller_seq(self):
        # 调用方提供的 seq 不得覆盖内部序号
        self.store.append_event("t-review", {"type": "message", "seq": 999, "text": "a"})
        self.store.append_event("t-review", {"type": "message", "seq": 0, "text": "b"})
        events, latest = self.store.events_since("t-review", after=-1)
        self.assertEqual([e["seq"] for e in events], [0, 1])
        self.assertEqual(latest, 1)

    def test_query_brief_omits_sensitive_and_results(self):
        # query_brief 只返回元数据：无 session_id、无结果文本
        self.store.add({
            "id": "t-full",
            "contextId": "ctx-full",
            "session_id": "sess-secret",
            "status": {"state": "TASK_STATE_COMPLETED", "timestamp": bridge_module.utc_timestamp()},
            "artifacts": [{"parts": [{"text": "SECRET-RESULT"}]}],
        })
        brief = self.store.query_brief()
        self.assertTrue(any(t["id"] == "t-full" for t in brief))
        full = next(t for t in brief if t["id"] == "t-full")
        self.assertNotIn("session_id", full)
        self.assertNotIn("SECRET-RESULT", json.dumps(brief, ensure_ascii=False))

    def test_progress_event_extracts_command_and_mcp_fields(self):
        # command_execution 用 command 字段、mcp_tool_call 用 server.tool
        bridge = bridge_module.CodexBridge(
            codex=None, workspace=Path(self.temp.name), state_dir=Path(self.temp.name),
            model="", sync_wait=1, codex_timeout=5, max_concurrent=1,
        )
        bridge.store = self.store
        bridge._record_progress_event("t-review", {"type": "command_execution", "command": "ls -la"})
        bridge._record_progress_event("t-review", {"type": "mcp_tool_call", "server": "playwright", "tool": "browser_navigate"})
        bridge._record_progress_event("t-review", {"type": "function_call", "name": "read_file", "arguments": "path"})
        events, _ = self.store.events_since("t-review", after=-1)
        texts = [e["text"] for e in events if e["type"] == "tool"]
        self.assertTrue(any("ls -la" in t for t in texts), texts)
        self.assertTrue(any("playwright.browser_navigate" in t for t in texts), texts)
        self.assertTrue(any("read_file" in t for t in texts), texts)


    def test_update_preserves_created_at(self):
        # 终态更新只带 status/artifacts 时，不得丢失原任务的 created_at
        self.store.add({
            "id": "t-keep",
            "contextId": "ctx-keep",
            "status": {"state": "TASK_STATE_WORKING", "timestamp": bridge_module.utc_timestamp()},
            "created_at": "2026-08-05T00:00:00.000Z",
        })
        self.store.update(
            "t-keep",
            {
                "id": "t-keep",
                "status": {"state": "TASK_STATE_COMPLETED", "timestamp": bridge_module.utc_timestamp()},
            },
            terminal=True,
        )
        task = self.store.get("t-keep")
        self.assertEqual(task["created_at"], "2026-08-05T00:00:00.000Z")

    def test_query_brief_sorts_newest_first(self):
        # created_at 存在时，列表按创建时间倒序（最新在前）
        self.store.add({
            "id": "t-old", "contextId": "ctx-old",
            "status": {"state": "TASK_STATE_COMPLETED", "timestamp": bridge_module.utc_timestamp()},
            "created_at": "2026-08-05T00:00:00.000Z",
        })
        self.store.add({
            "id": "t-new", "contextId": "ctx-new",
            "status": {"state": "TASK_STATE_COMPLETED", "timestamp": bridge_module.utc_timestamp()},
            "created_at": "2026-08-05T12:00:00.000Z",
        })
        brief = self.store.query_brief()
        ids = [t["id"] for t in brief]
        self.assertLess(ids.index("t-new"), ids.index("t-old"))


class MonitorHostAuthTests(unittest.TestCase):
    """监控端点的 Host 校验与认证分流。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        fake = FakeBridge(Path(self.temp.name))
        self.server = bridge_module.BridgeServer(("127.0.0.1", 0), fake)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _get(self, path, host=None):
        import urllib.error
        req = Request(self.url + path)
        if host:
            req.add_header("Host", host)
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_monitor_rejects_non_loopback_host(self):
        # 监控端点拒绝非回环 Host（防 DNS-rebinding）
        status, body = self._get("/tasks", host="evil.com")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad host")

    def test_monitor_accepts_loopback_host(self):
        status, _ = self._get("/tasks", host="localhost")
        self.assertEqual(status, 200)

    def test_protected_get_requires_token(self):
        # 受保护 GET（agent-card）在启用 token 时返回 401
        import urllib.error
        fake = FakeBridge(Path(self.temp.name))
        fake.token = "secret-token"
        server = bridge_module.BridgeServer(("127.0.0.1", 0), fake)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            req = Request(url + "/.well-known/agent-card.json")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class ConversationAndDeleteTests(unittest.TestCase):
    """对话详情（role/解析）+ 删除端点（token/WORKING/共享 context）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = bridge_module.TaskStore(Path(self.temp.name) / "tasks.json")
        self.store.add({
            "id": "t-conv", "contextId": "ctx-conv",
            "status": {"state": "TASK_STATE_COMPLETED", "timestamp": bridge_module.utc_timestamp()},
            "created_at": "2026-08-06T00:00:00.000Z",
            "summary": "测试对话简介",
        })

    def tearDown(self):
        self.temp.cleanup()

    def test_events_carry_roles(self):
        # user 首条 + assistant/tool 后续，role 正确
        self.store.append_event("t-conv", {"type": "message", "role": "user", "ts": "t0", "text": "我的任务"})
        self.store.append_event("t-conv", {"type": "message", "role": "assistant", "ts": "t1", "text": "好的"})
        self.store.append_event("t-conv", {"type": "tool", "role": "tool", "ts": "t2", "text": "调用 read_file"})
        events, _ = self.store.events_since("t-conv", after=-1)
        self.assertEqual([e["role"] for e in events], ["user", "assistant", "tool"])

    def test_extract_summary(self):
        # 带桥前缀的 prompt 提取实际内容
        p = "You are Codex receiving a task.\n\nHermes task:\n帮我找五张示例图片放在工作目录"
        s = bridge_module.CodexBridge._extract_summary(p)
        self.assertTrue(s.startswith("帮我找五张"))
        self.assertNotIn("Hermes task", s)
        # 截断
        long_p = "x" * 100
        self.assertEqual(len(bridge_module.CodexBridge._extract_summary(long_p, 40)), 40)

    def test_summary_preserved_through_terminal_update(self):
        # 终态 update 不丢 summary（漏洞 3 回归）
        self.store.update("t-conv", {
            "id": "t-conv",
            "status": {"state": "TASK_STATE_COMPLETED", "timestamp": bridge_module.utc_timestamp()},
        }, terminal=True)
        task = self.store.get("t-conv")
        self.assertEqual(task["summary"], "测试对话简介")

    def test_find_codex_session_file(self):
        # 找不到时返回 None（不炸）
        self.assertIsNone(bridge_module.find_codex_session_file(""))
        self.assertIsNone(bridge_module.find_codex_session_file("019f-not-exist-0000"))

    def test_parse_codex_conversation_missing(self):
        self.assertEqual(bridge_module.parse_codex_conversation("nope-nope"), [])

    def test_taskstore_remove(self):
        removed = self.store.remove("t-conv")
        self.assertEqual(removed["id"], "t-conv")
        self.assertIsNone(self.store.get("t-conv"))
        self.assertIsNone(self.store.remove("t-conv"))


class DeleteEndpointTests(unittest.TestCase):
    """DELETE /tasks/<id> 的认证、状态校验与行为。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fake = FakeBridge(Path(self.temp.name))
        self.server = bridge_module.BridgeServer(("127.0.0.1", 0), self.fake)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _delete(self, task_id, token=None):
        import urllib.error
        req = Request(self.url + "/tasks/" + task_id, method="DELETE")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_delete_requires_token(self):
        self.fake.token = "secret-token"
        self.fake.start_task("x", "ctx-del")
        status, _ = self._delete("task-test")
        self.assertEqual(status, 401)
        # 带 token 通过
        status, body = self._delete("task-test", token="secret-token")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_delete_missing_task_404(self):
        status, body = self._delete("task-nope")
        self.assertEqual(status, 404)

    def test_delete_working_task_rejected(self):
        # FakeBridge 返回 COMPLETED；构造一个 WORKING 任务直接测 delete_task
        ok, msg, _ = self.fake.store and self.server.bridge.delete_task("task-nope")
        # 直接测 bridge 逻辑
        self.server.bridge.store.add({
            "id": "task-work", "contextId": "ctx-w",
            "status": {"state": "TASK_STATE_WORKING", "timestamp": bridge_module.utc_timestamp()},
        })
        ok, msg, _ = self.server.bridge.delete_task("task-work")
        self.assertFalse(ok)
        self.assertIn("WORKING", msg)

    def test_delete_completed_ok(self):
        self.fake.start_task("hello", "ctx-del2")
        status, body = self._delete("task-test")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIsNone(self.server.bridge.store.get("task-test"))

    def test_delete_keeps_session_when_siblings_exist(self):
        # 同一 context 两个任务：删一个应保留 Codex 会话文件（有兄弟）
        self.server.bridge.sessions.set("ctx-shared", "019f-shared-0000")
        self.server.bridge.store.add({
            "id": "task-a", "contextId": "ctx-shared",
            "status": {"state": "TASK_STATE_COMPLETED", "timestamp": bridge_module.utc_timestamp()},
        })
        self.server.bridge.store.add({
            "id": "task-b", "contextId": "ctx-shared",
            "status": {"state": "TASK_STATE_COMPLETED", "timestamp": bridge_module.utc_timestamp()},
        })
        ok, msg, _ = self.server.bridge.delete_task("task-a")
        self.assertTrue(ok)
        # 会话映射保留（兄弟任务还用）
        self.assertEqual(self.server.bridge.sessions.get("ctx-shared"), "019f-shared-0000")
        # 删最后一个才清映射
        ok, _, _ = self.server.bridge.delete_task("task-b")
        self.assertTrue(ok)
        self.assertIsNone(self.server.bridge.sessions.get("ctx-shared"))


class ReviewFixRound2Tests(unittest.TestCase):
    """二轮复审修复回归：结果文件删除、工具事件解析、会话文件校验。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = bridge_module.TaskStore(Path(self.temp.name) / "tasks.json")
        self.store.add({
            "id": "t-r2", "contextId": "ctx-r2",
            "status": {"state": "TASK_STATE_COMPLETED", "timestamp": bridge_module.utc_timestamp()},
        })

    def tearDown(self):
        self.temp.cleanup()

    def test_remove_deletes_result_file(self):
        # remove 同步清理 results/<task_id>.txt
        result_file = self.store.results_dir / "t-r2.txt"
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_file.write_text("result content", encoding="utf-8")
        self.store.remove("t-r2")
        self.assertFalse(result_file.exists())

    def test_session_id_strict_validation(self):
        # 非完整 UUID 直接拒绝，不触发扫描
        self.assertIsNone(bridge_module.find_codex_session_file("019f"))
        self.assertIsNone(bridge_module.find_codex_session_file("not-a-uuid"))
        self.assertIsNone(bridge_module.find_codex_session_file(""))

    def test_parse_tool_events_from_payload_types(self):
        # 工具事件（function_call / custom_tool_call / command_execution）应解析为 tool
        sess_dir = bridge_module._codex_sessions_root()
        if not sess_dir.exists():
            self.skipTest("no codex sessions dir")
        # 找一个含 function_call 的真实文件验证解析器不炸
        found = False
        for p in sess_dir.rglob("rollout-*.jsonl"):
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if "function_call" in line or "custom_tool_call" in line:
                            found = True
                            break
            except OSError:
                continue
            if found:
                break
        if not found:
            self.skipTest("no tool-event session found")
        # 解析器不炸即通过（格式健壮性）
        msgs = bridge_module.parse_codex_conversation(p.stem.split("-")[-1])
        self.assertIsInstance(msgs, list)


class OrphanFilterTests(unittest.TestCase):
    """hide_orphan_tasks：无会话文件的任务从列表隐藏。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fake = FakeBridge(Path(self.temp.name), hide_orphan_tasks=True)
        self.server = bridge_module.BridgeServer(("127.0.0.1", 0), self.fake)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _add_task(self, task_id, context_id, state="COMPLETED"):
        self.fake.store.add({
            "id": task_id, "contextId": context_id,
            "status": {"state": state, "timestamp": bridge_module.utc_timestamp()},
        })

    def _list_ids(self):
        with urlopen(self.url + "/tasks", timeout=5) as response:
            payload = json.load(response)
        return [t["id"] for t in payload["tasks"]]

    def test_orphan_completed_task_hidden(self):
        # 无会话映射的终态任务 -> 隐藏
        self._add_task("task-orphan", "ctx-orphan")
        self.assertNotIn("task-orphan", self._list_ids())

    def test_working_task_always_visible(self):
        # WORKING 任务即使无会话也显示（进行中）
        self._add_task("task-working", "ctx-working", state="TASK_STATE_WORKING")
        self.assertIn("task-working", self._list_ids())

    def test_task_with_session_mapping_visible_if_file_exists(self):
        # 有会话映射 + 真实文件存在 -> 显示
        sess_dir = bridge_module._codex_sessions_root()
        if not sess_dir.exists():
            self.skipTest("no codex sessions dir")
        # 找一个真实存在的会话文件做映射
        index = bridge_module.session_index()
        if not index:
            self.skipTest("no codex session files")
        sid = next(iter(index))
        self.fake.sessions.set("ctx-real", sid)
        self._add_task("task-real", "ctx-real")
        self.assertIn("task-real", self._list_ids())


class InboundEventTests(unittest.TestCase):
    """POST /inbound/events：token 校验、幂等合并、状态机。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fake = FakeBridge(Path(self.temp.name))
        self.fake.inbound_token = "inb-secret"
        self.server = bridge_module.BridgeServer(("127.0.0.1", 0), self.fake)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _post(self, event, token="inb-secret"):
        import urllib.error
        req = Request(
            self.url + "/",
            data=json.dumps({"jsonrpc": "2.0", "id": "t1", "method": "inbound/events", "params": event}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def _evt(self, op, phase, eid=None, **kw):
        e = {
            "schema_version": 1,
            "event_id": eid or f"evt-{uuid.uuid4().hex[:16]}",
            "operation_id": op,
            "phase": phase,
            "direction": "inbound",
            "source": "hermes_mcp",
            "profile": "default",
            "context_id": "ctx-proj",
            "observed_at": "2026-08-06T12:00:00.000Z",
        }
        e.update(kw)
        return e

    def test_requires_inbound_token(self):
        status, _ = self._post(self._evt("op-a", "started"), token="wrong")
        self.assertEqual(status, 401)
        status, body = self._post(self._evt("op-a", "started"))
        self.assertEqual(status, 202)
        self.assertTrue(body["result"]["ok"])

    def test_invalid_phase_rejected(self):
        status, _ = self._post(self._evt("op-b", "bogus"))
        self.assertEqual(status, 400)

    def test_duplicate_event_idempotent(self):
        evt = self._evt("op-c", "started")
        s1, b1 = self._post(evt)
        s2, b2 = self._post(evt)
        self.assertEqual(s1, 202)
        self.assertEqual(s2, 200)
        self.assertTrue(b2["result"]["duplicate"])
        # 只有一条任务
        tasks = self.fake.store.query_brief()
        self.assertEqual(len([t for t in tasks if t["id"] == b1["result"]["task_id"]]), 1)

    def test_state_machine_merges_operation(self):
        s, b = self._post(self._evt("op-d", "started", message_summary="帮我看看"))
        task_id = b["result"]["task_id"]
        self._post(self._evt("op-d", "accepted", gateway_task_id="task-gw-1", gateway_state="TASK_STATE_WORKING", state="WORKING"))
        self._post(self._evt("op-d", "finished", gateway_task_id="task-gw-1", gateway_state="TASK_STATE_COMPLETED", state="COMPLETED", reply_summary="看完了"))
        tasks = self.fake.store.query_brief()
        t = next(t for t in tasks if t["id"] == task_id)
        self.assertEqual(t["state"], "COMPLETED")
        self.assertEqual(t["direction"], "inbound")
        self.assertEqual(t["gateway_task_id"], "task-gw-1")
        self.assertEqual(t["gateway_state"], "TASK_STATE_COMPLETED")

    def test_inbound_not_hidden_by_orphan_filter(self):
        self.fake.hide_orphan_tasks = True
        s, b = self._post(self._evt("op-e", "finished", state="COMPLETED"))
        with urlopen(self.url + "/tasks", timeout=5) as response:
            payload = json.load(response)
        ids = [t["id"] for t in payload["tasks"]]
        self.assertIn(b["result"]["task_id"], ids)

    def test_conversation_returns_summary(self):
        s, b = self._post(self._evt("op-f", "started", message_summary="你好 Hermes"))
        task_id = b["result"]["task_id"]
        self._post(self._evt("op-f", "finished", state="COMPLETED", reply_summary="你好 Codex"))
        with urlopen(self.url + f"/tasks/{task_id}/conversation", timeout=5) as response:
            conv = json.load(response)
        self.assertEqual(conv["source"], "inbound-report")
        roles = [m["role"] for m in conv["messages"]]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)


class InboundReviewFixTests(unittest.TestCase):
    """复审修复回归：tombstone、token 缺失、终态不回退。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fake = FakeBridge(Path(self.temp.name))
        self.fake.inbound_token = "inb-secret"
        self.server = bridge_module.BridgeServer(("127.0.0.1", 0), self.fake)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _post(self, event, token="inb-secret"):
        import urllib.error
        req = Request(
            self.url + "/",
            data=json.dumps({"jsonrpc": "2.0", "id": "t1", "method": "inbound/events", "params": event}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def _evt(self, op, phase, **kw):
        e = {
            "schema_version": 1,
            "event_id": f"evt-{uuid.uuid4().hex[:16]}",
            "operation_id": op,
            "phase": phase,
            "direction": "inbound",
            "source": "hermes_mcp",
            "profile": "default",
            "context_id": "ctx-proj",
            "observed_at": "2026-08-06T12:00:00.000Z",
        }
        e.update(kw)
        return e

    def test_terminal_state_not_reverted_by_working(self):
        # 终态后 WORKING 事件不能回退（状态映射修复）
        s, b = self._post(self._evt("op-rv1", "started"))
        task_id = b["result"]["task_id"]
        self._post(self._evt("op-rv1", "finished", state="COMPLETED", reply_summary="done"))
        self._post(self._evt("op-rv1", "state", state="WORKING"))
        tasks = self.fake.store.query_brief()
        t = next(t for t in tasks if t["id"] == task_id)
        self.assertEqual(t["state"], "COMPLETED")

    def test_deleted_inbound_does_not_resurrect(self):
        # tombstone：终态任务删除后，同 operation 的后续事件不重建
        s, b = self._post(self._evt("op-rv2", "started"))
        task_id = b["result"]["task_id"]
        self._post(self._evt("op-rv2", "finished", state="COMPLETED", reply_summary="done"))
        self.fake.delete_task(task_id)
        # 同 operation 再来一个事件（outbox 重投场景）
        s2, b2 = self._post(self._evt("op-rv2", "state", state="COMPLETED"))
        tasks = self.fake.store.query_brief()
        self.assertNotIn(task_id, [t["id"] for t in tasks])

    def test_no_token_configured_endpoint_rejected(self):
        # 未配置 inbound_token -> 端点 403 拒绝（防匿名写入）
        fake2 = FakeBridge(Path(self.temp.name))
        fake2.inbound_token = None
        server2 = bridge_module.BridgeServer(("127.0.0.1", 0), fake2)
        t2 = threading.Thread(target=server2.serve_forever, daemon=True)
        t2.start()
        url2 = f"http://127.0.0.1:{server2.server_address[1]}"
        import urllib.error
        req = Request(
            url2 + "/",
            data=json.dumps({"jsonrpc": "2.0", "id": "t1", "method": "inbound/events", "params": self._evt("op-rv3", "started")}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=5) as response:
                self.fail("should be rejected")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 403)
        server2.shutdown()
        server2.server_close()
        t2.join(timeout=2)


class WorkstreamTests(unittest.TestCase):
    """SessionStore 工作线注册表：resolve/touch/close/兼容。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = bridge_module.SessionStore(Path(self.temp.name) / "sessions.json")

    def tearDown(self):
        self.temp.cleanup()

    def test_resolve_creates_gen01(self):
        ctx = self.store.resolve_workstream("demo-project", profile="default", workspace="D:/x")
        self.assertEqual(ctx, "ctx-demo-project#01")
        meta = self.store.workstreams["demo-project"]
        self.assertEqual(meta["generation"], 1)
        self.assertEqual(meta["status"], "active")

    def test_resolve_reuses_active(self):
        ctx1 = self.store.resolve_workstream("demo-project")
        ctx2 = self.store.resolve_workstream("demo-project")
        self.assertEqual(ctx1, ctx2)  # 复用当前代次

    def test_resolve_after_close_new_generation(self):
        ctx1 = self.store.resolve_workstream("demo-project")
        self.store.close_workstream("demo-project", "done")
        ctx2 = self.store.resolve_workstream("demo-project")
        self.assertEqual(ctx2, "ctx-demo-project#02")  # 新代次
        self.assertNotEqual(ctx1, ctx2)

    def test_touch_updates_metadata(self):
        ctx = self.store.resolve_workstream("demo-project")
        self.store.touch_workstream("demo-project", session_id="019f-abc", message_count=42, estimated_tokens=5000, file_size=1024)
        meta = self.store.workstreams["demo-project"]
        self.assertEqual(meta["session_id"], "019f-abc")
        self.assertEqual(meta["message_count"], 42)
        # sessions 映射也更新
        self.assertEqual(self.store.get(ctx), "019f-abc")

    def test_old_format_compat(self):
        # 旧格式只写 sessions 不写 workstreams，加载兼容
        path = Path(self.temp.name) / "sessions.json"
        path.write_text(json.dumps({"sessions": [{"context_id": "ctx-old", "session_id": "019f-old"}]}), encoding="utf-8")
        s2 = bridge_module.SessionStore(path)
        self.assertEqual(s2.get("ctx-old"), "019f-old")
        self.assertEqual(s2.workstreams, {})

    def test_ws_lock_serializes(self):
        l1 = self.store.ws_lock("demo-project")
        l2 = self.store.ws_lock("demo-project")
        self.assertIs(l1, l2)  # 同一工作线同一把锁
        l3 = self.store.ws_lock("other")
        self.assertIsNot(l1, l3)  # 不同工作线不同锁

    def test_estimate_session_stats_missing(self):
        stats = bridge_module.estimate_session_stats("019f-nope-0000")
        self.assertEqual(stats["exists"], False)


class WorkstreamHealthTests(unittest.TestCase):
    """健康检查/rotate/归档/自动兜底。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = bridge_module.SessionStore(Path(self.temp.name) / "sessions.json")

    def tearDown(self):
        self.temp.cleanup()

    def _seed(self, name, tokens=0, msgs=0, size=0, last="2026-08-06T12:00:00.000Z"):
        ctx = self.store.resolve_workstream(name)
        self.store.touch_workstream(name, session_id="019f-x", message_count=msgs, estimated_tokens=tokens, file_size=size)
        # 直接改 last_used_at 控制空闲
        with self.store.lock:
            self.store.workstreams[name]["last_used_at"] = last
        return ctx

    def test_warning_when_tokens_high(self):
        self._seed("ws1", tokens=450_000)  # > 400k warn
        self.assertEqual(self.store.check_health("ws1"), "warning")

    def test_active_when_healthy(self):
        self._seed("ws2", tokens=100_000, msgs=10)
        self.assertEqual(self.store.check_health("ws2"), "active")

    def test_should_rotate_over_hard_threshold(self):
        self._seed("ws3", tokens=600_000)  # > 500k rotate
        reason = self.store.should_rotate("ws3")
        self.assertIsNotNone(reason)
        self.assertIn("ROTATE", reason)

    def test_should_rotate_none_under_threshold(self):
        self._seed("ws4", tokens=200_000)
        self.assertIsNone(self.store.should_rotate("ws4"))

    def test_archive_and_cleanup(self):
        self._seed("ws5")
        self.store.close_workstream("ws5", "done")
        self.store.set_workstream_status("ws5", "archived", "cleanup")
        # 把 closed_at 改成过期
        import datetime
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        with self.store.lock:
            self.store.workstreams["ws5"]["closed_at"] = old
        removed = self.store.cleanup_archived(max_age_days=30)
        self.assertEqual(removed, 1)
        self.assertNotIn("ws5", self.store.workstreams)

    def test_ephemeral_disabled_by_default(self):
        self.assertEqual(self.store.resolve_ephemeral(workspace="D:/x", profile="default"), "")

    def test_ephemeral_enabled_reuses_window(self):
        import importlib
        orig = bridge_module.WS_AUTO_EPHEMERAL
        bridge_module.WS_AUTO_EPHEMERAL = True
        try:
            ctx1 = self.store.resolve_ephemeral(workspace="D:/x", profile="default")
            ctx2 = self.store.resolve_ephemeral(workspace="D:/x", profile="default")
            self.assertEqual(ctx1, ctx2)  # 窗口内复用
            self.assertIn("auto:D:/x:default", self.store.workstreams)
        finally:
            bridge_module.WS_AUTO_EPHEMERAL = orig


class InboundStuckAndQueueTests(unittest.TestCase):
    """僵尸 inbound 不占队列 + stuck 超时清理。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = bridge_module.TaskStore(Path(self.temp.name) / "tasks.json")

    def tearDown(self):
        self.temp.cleanup()

    def _add_inbound_working(self, task_id, observed):
        task = {
            "id": task_id,
            "direction": "inbound",
            "contextId": "ctx-stuck",
            "status": {"state": "TASK_STATE_WORKING", "timestamp": observed},
            "created_at": observed,
            "last_observed_at": observed,
        }
        self.store.tasks[task_id] = task

    def test_active_count_excludes_inbound(self):
        # inbound WORKING 不占 Codex 槽位
        self._add_inbound_working("inb-stuck-1", "2026-08-06T08:00:00.000Z")
        self.assertEqual(self.store.active_count(), 0)

    def test_active_count_counts_outbound(self):
        self.store.tasks["task-out-1"] = {
            "id": "task-out-1",
            "status": {"state": "TASK_STATE_WORKING", "timestamp": "2026-08-06T08:00:00.000Z"},
        }
        self.assertEqual(self.store.active_count(), 1)

    def test_cleanup_stuck_inbound_marks_failed(self):
        import datetime
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self._add_inbound_working("inb-stuck-old", old)
        fresh = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self._add_inbound_working("inb-stuck-fresh", fresh)
        affected = self.store.cleanup_stuck_inbound(max_age=300)
        self.assertIn("inb-stuck-old", affected)
        self.assertNotIn("inb-stuck-fresh", affected)
        self.assertEqual(self.store.tasks["inb-stuck-old"]["status"]["state"], "TASK_STATE_FAILED")
        self.assertEqual(self.store.tasks["inb-stuck-fresh"]["status"]["state"], "TASK_STATE_WORKING")

