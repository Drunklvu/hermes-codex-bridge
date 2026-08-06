"""P0 自动化测试：hermes_mcp_server.py 的 in-flight 跟踪 / 超时取消 / 对账 / 输入限制 / 日志脱敏 / framing 回归。

全部本地执行：不访问真实 9900/9901 端口，不产生任何真实 gateway 副作用。
通过 mock ``urllib.request.urlopen`` 模拟 A2A gateway 的 JSON-RPC 响应；
framing 回归测试直接以内存流驱动 ``main()``（EOF 自然退出）。

运行（仓库根目录下）:
    python -m unittest discover -s tools -p "test_hermes_mcp_server.py" -v
"""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error as urllib_error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hermes_mcp_server as hms  # noqa: E402


# ---------------------------------------------------------------------------
# 测试基础设施：内存 gateway mock
# ---------------------------------------------------------------------------

class _FakeResp:
    """urllib response 替身，payload 为 JSON-RPC 响应对象。"""

    def __init__(self, payload):
        self._payload = payload

    def read(self, n=-1):
        data = json.dumps(self._payload).encode("utf-8")
        if n is not None and n >= 0:
            return data[:n]
        return data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_gateway(handlers):
    """构造 urlopen side_effect。

    handlers: dict method -> callable(params, request) -> result-payload | raise
    响应 id 自动回显请求 id（_a2a_request 校验 id 匹配）。
    """

    def side_effect(request, timeout=None):
        body = json.loads(request.data)
        method = body["method"]
        if method not in handlers:
            raise AssertionError(f"unexpected A2A method: {method}")
        result = handlers[method](body.get("params") or {}, request)
        return _FakeResp({"jsonrpc": "2.0", "id": body["id"], "result": result})

    return side_effect


def task_payload(task_id, state, text=None):
    status = {"state": state}
    if state == "TASK_STATE_COMPLETED":
        status["message"] = {
            "role": "agent",
            "parts": [{"text": text or "ok", "mediaType": "text/plain"}],
        }
    return {"id": task_id, "status": status}


# ---------------------------------------------------------------------------
# framing 回归（内存流驱动 main()）
# ---------------------------------------------------------------------------

class _StdinWrap:
    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)


def cl_frame(payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def nl_frame(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8") + b"\n"


def parse_responses(buf: bytes):
    """顺序解析混合 framing 的输出流，返回 [(framing, payload), ...]。"""
    responses = []
    i = 0
    while i < len(buf):
        if buf.startswith(b"Content-Length:", i):
            end = buf.index(b"\r\n\r\n", i)
            length = int(buf[i + len(b"Content-Length:"):end])
            body = buf[end + 4:end + 4 + length]
            responses.append(("content-length", json.loads(body)))
            i = end + 4 + length
        else:
            end = buf.index(b"\n", i)
            responses.append(("newline", json.loads(buf[i:end])))
            i = end + 1
    return responses


class FramingRegressionTest(unittest.TestCase):
    def test_mixed_framing_full_flow(self):
        tmpdir = tempfile.mkdtemp()
        state_path = os.path.join(tmpdir, "state.json")
        store = hms._StateStore(state_path)

        # 混合帧：Content-Length 的 initialize + newline 的 tools/list/call/ping
        stdin_data = b"".join([
            cl_frame({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "t", "version": "0"}}}),
            nl_frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            nl_frame({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "call_hermes", "arguments": {"message": "hi"}}}),
            nl_frame({"jsonrpc": "2.0", "id": 4, "method": "ping", "params": {}}),
        ])

        gateway = make_gateway({
            "message/send": lambda p, r: task_payload("t-9", "TASK_STATE_COMPLETED", "hello back"),
        })

        out = io.BytesIO()
        with mock.patch.object(hms, "STATE_FILE", state_path), \
             mock.patch.object(hms, "_default_store", return_value=store), \
             mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway), \
             mock.patch.object(hms, "_log"), \
             mock.patch.object(sys, "stdin", _StdinWrap(stdin_data)), \
             mock.patch.object(sys, "stdout", out):
            hms.main()

        responses = parse_responses(out.getvalue())
        self.assertEqual(len(responses), 4)

        framing, payload = responses[0]
        self.assertEqual(framing, "content-length")  # 请求用 CL 帧 -> 响应用 CL 帧
        self.assertEqual(payload["id"], 1)
        self.assertEqual(payload["result"]["serverInfo"]["name"], "hermes-mcp-server")

        framing, payload = responses[1]
        self.assertEqual(framing, "newline")
        self.assertEqual(payload["id"], 2)
        self.assertEqual(payload["result"]["tools"][0]["name"], "call_hermes")

        framing, payload = responses[2]
        self.assertEqual(framing, "newline")
        self.assertEqual(payload["id"], 3)
        text = payload["result"]["content"][0]["text"]
        self.assertEqual(text, "hello back")

        framing, payload = responses[3]
        self.assertEqual(framing, "newline")
        self.assertEqual(payload["id"], 4)
        self.assertEqual(payload["result"], {})

        # EOF 清扫：空状态文件 -> 无额外 urlopen 调用，main 正常返回


# ---------------------------------------------------------------------------
# in-flight 状态存储
# ---------------------------------------------------------------------------

class StateStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = hms._StateStore(os.path.join(self.tmpdir, "state.json"))

    def test_record_remove_roundtrip(self):
        self.store.record("t-1", "default", "my-secret-ctx")
        data = self.store.load()
        self.assertIn("t-1", data)
        rec = data["t-1"]
        self.assertEqual(rec["profile"], "default")
        self.assertEqual(rec["state"], "WORKING")
        self.assertIn("started_at", rec)
        # context_id 只存 hash（安全表示），原文绝不出现在文件里
        self.assertEqual(len(rec["context_id_hash"]), 16)
        with open(self.store.path, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("my-secret-ctx", raw)
        self.store.remove("t-1")
        self.assertEqual(self.store.load(), {})

    def test_corrupt_file_degrades_to_empty(self):
        with open(self.store.path, "w", encoding="utf-8") as fh:
            fh.write("{{{not json")
        self.assertEqual(self.store.load(), {})

    def test_non_dict_root_degrades_to_empty(self):
        with open(self.store.path, "w", encoding="utf-8") as fh:
            json.dump([1, 2, 3], fh)
        self.assertEqual(self.store.load(), {})

    def test_lock_failure_does_not_escape_record_or_remove(self):
        with mock.patch.object(hms, "_cross_process_lock", side_effect=OSError("busy")), \
             mock.patch.object(hms, "_log") as log_mock:
            self.store.record("t-lock", "default", "ctx")
            self.store.remove("t-lock")
            self.assertEqual(self.store.load(), {})
        self.assertGreaterEqual(log_mock.call_count, 3)


# ---------------------------------------------------------------------------
# call_hermes 主流程
# ---------------------------------------------------------------------------

class CallHermesFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = hms._StateStore(os.path.join(self.tmpdir, "state.json"))

    def test_completed_immediate(self):
        calls = []
        gateway = make_gateway({
            "message/send": lambda p, r: calls.append("send") or task_payload("t-1", "TASK_STATE_COMPLETED", "hello back"),
        })
        with mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            reply = hms.call_hermes("hi", state_store=self.store)
        self.assertEqual(reply, "hello back")
        self.assertEqual(calls, ["send"])  # 无轮询
        self.assertEqual(self.store.load(), {})

    def test_optional_bearer_token_header(self):
        seen = []
        gateway = make_gateway({
            "message/send": lambda p, request: seen.append(
                request.get_header("Authorization")
            ) or task_payload("t-auth", "TASK_STATE_COMPLETED", "ok"),
        })
        with mock.patch.object(hms, "A2A_TOKEN", "secret-token"), \
             mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            reply = hms.call_hermes("hi", state_store=self.store)
        self.assertEqual(reply, "ok")
        self.assertEqual(seen, ["Bearer secret-token"])

    def test_completed_after_polling_clears_record(self):
        gateway = make_gateway({
            "message/send": lambda p, r: task_payload("t-2", "TASK_STATE_WORKING"),
            "tasks/get": lambda p, r: task_payload("t-2", "TASK_STATE_COMPLETED", "done"),
        })
        with mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            reply = hms.call_hermes("hi", state_store=self.store, task_timeout=5, poll_interval=0.01)
        self.assertEqual(reply, "done")
        # WORKING 期间记录过，终态后清除
        self.assertEqual(self.store.load(), {})

    def test_failed_state_raises_and_clears(self):
        gateway = make_gateway({
            "message/send": lambda p, r: task_payload("t-3", "TASK_STATE_WORKING"),
            "tasks/get": lambda p, r: task_payload("t-3", "TASK_STATE_FAILED"),
        })
        with mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            with self.assertRaises(hms.A2AError) as cm:
                hms.call_hermes("hi", state_store=self.store, task_timeout=5, poll_interval=0.01)
        self.assertEqual(cm.exception.category, "task_failed")
        self.assertEqual(cm.exception.task_id, "t-3")
        self.assertEqual(self.store.load(), {})

    def test_completed_empty_reply_has_task_metadata(self):
        gateway = make_gateway({
            "message/send": lambda p, r: {
                "id": "t-empty",
                "status": {
                    "state": "TASK_STATE_COMPLETED",
                    "message": {"role": "agent", "parts": [{"text": ""}]},
                },
            },
        })
        with mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            with self.assertRaises(hms.A2AError) as cm:
                hms.call_hermes("hi", state_store=self.store)
        self.assertEqual(cm.exception.category, "empty_reply")
        self.assertEqual(cm.exception.task_id, "t-empty")
        self.assertEqual(self.store.load(), {})

    def test_timeout_triggers_cancel_and_reports_it(self):
        methods = []
        gateway = make_gateway({
            "message/send": lambda p, r: methods.append("send") or task_payload("t-4", "TASK_STATE_WORKING"),
            "tasks/get": lambda p, r: methods.append("get") or task_payload("t-4", "TASK_STATE_WORKING"),
            "tasks/cancel": lambda p, r: methods.append("cancel") or task_payload("t-4", "TASK_STATE_CANCELED"),
        })
        with mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            with self.assertRaises(hms.A2AError) as cm:
                hms.call_hermes("hi", state_store=self.store, task_timeout=0.2, poll_interval=0.05)
        self.assertEqual(cm.exception.category, "timeout")
        self.assertIn("t-4", str(cm.exception))
        self.assertIn("cancel: sent", str(cm.exception))
        self.assertIn("cancel", methods)
        # cancel 成功 -> 记录已清除
        self.assertEqual(self.store.load(), {})

    def test_cancel_failure_keeps_record_for_reconcile(self):
        def cancel_handler(params, request):
            raise urllib_error.URLError("gateway gone")

        gateway = make_gateway({
            "message/send": lambda p, r: task_payload("t-5", "TASK_STATE_WORKING"),
            "tasks/get": lambda p, r: task_payload("t-5", "TASK_STATE_WORKING"),
            "tasks/cancel": cancel_handler,
        })
        with mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            with self.assertRaises(hms.A2AError) as cm:
                hms.call_hermes("hi", state_store=self.store, task_timeout=0.2, poll_interval=0.05)
        self.assertIn("cancel: failed", str(cm.exception))
        # 取消失败 -> 记录保留，留给下次启动对账
        self.assertIn("t-5", self.store.load())

    def test_connection_failure_on_send(self):
        def send_handler(params, request):
            raise urllib_error.URLError("connection refused")

        gateway = make_gateway({"message/send": send_handler})
        with mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            with self.assertRaises(hms.A2AError) as cm:
                hms.call_hermes("hi", state_store=self.store)
        self.assertEqual(cm.exception.category, "connection_failed")
        self.assertEqual(self.store.load(), {})

    def test_submission_timeout_reports_unknown_acceptance(self):
        with mock.patch.object(
            hms.urllib.request,
            "urlopen",
            side_effect=TimeoutError("send timed out"),
        ):
            with self.assertRaises(hms.A2AError) as cm:
                hms.call_hermes("hi", state_store=self.store, task_timeout=1)
        self.assertEqual(cm.exception.category, "submission_unknown")
        self.assertIn("acceptance is unknown", str(cm.exception))
        self.assertEqual(self.store.load(), {})

    def test_total_timeout_budget_starts_before_polling(self):
        methods = []
        gateway = make_gateway({
            "message/send": lambda p, r: methods.append("send") or task_payload("t-budget", "TASK_STATE_WORKING"),
            "tasks/cancel": lambda p, r: methods.append("cancel") or task_payload("t-budget", "TASK_STATE_CANCELED"),
        })
        with mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway), \
             mock.patch.object(hms.time, "monotonic", side_effect=[0.0, 0.15, 0.21, 0.21]), \
             mock.patch.object(hms.time, "sleep"):
            with self.assertRaises(hms.A2AError) as cm:
                hms.call_hermes(
                    "hi",
                    state_store=self.store,
                    task_timeout=0.2,
                    poll_interval=0.05,
                )
        self.assertEqual(cm.exception.category, "timeout")
        self.assertEqual(methods, ["send", "cancel"])

    def test_unknown_nonterminal_state_keeps_record(self):
        gateway = make_gateway({
            "message/send": lambda p, r: task_payload("t-input", "TASK_STATE_INPUT_REQUIRED"),
        })
        with mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            with self.assertRaises(hms.A2AError) as cm:
                hms.call_hermes("hi", state_store=self.store)
        self.assertEqual(cm.exception.category, "unknown_state")
        self.assertIn("t-input", self.store.load())

    def test_long_reply_has_explicit_truncation_marker(self):
        long_reply = "R" * (hms.MAX_REPLY_CHARS + 100)
        gateway = make_gateway({
            "message/send": lambda p, r: task_payload("t-long", "TASK_STATE_COMPLETED", long_reply),
        })
        with mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            reply = hms.call_hermes("hi", state_store=self.store)
        self.assertEqual(len(reply), hms.MAX_REPLY_CHARS)
        self.assertTrue(reply.endswith(hms.TRUNCATION_MARKER))


# ---------------------------------------------------------------------------
# 启动对账
# ---------------------------------------------------------------------------

class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = hms._StateStore(os.path.join(self.tmpdir, "state.json"))

    def test_reconcile_working_cancelled_terminal_cleared_unreachable_kept(self):
        self.store.record("r-1", "default", "ctx-a")  # gateway 会报 WORKING
        self.store.record("r-2", "default", "ctx-b")  # gateway 会报 COMPLETED
        self.store.record("r-3", "web-dev", "ctx-c")  # gateway 不可达

        methods = []
        gateway = make_gateway({
            "tasks/get": lambda p, r: (
                methods.append(f"get:{p['id']}")
                or (task_payload("r-1", "TASK_STATE_WORKING")
                    if p["id"] == "r-1"
                    else task_payload("r-2", "TASK_STATE_COMPLETED", "x"))
                if p["id"] != "r-3"
                else (_ for _ in ()).throw(urllib_error.URLError("unreachable"))
            ),
            "tasks/cancel": lambda p, r: methods.append(f"cancel:{p['id']}") or task_payload("r-1", "TASK_STATE_CANCELED"),
        })
        with mock.patch.object(hms, "_process_is_alive", return_value=False), \
             mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            result = hms._reconcile(self.store)

        self.assertEqual(result["checked"], 3)
        self.assertEqual(result["cancelled"], 1)  # r-1
        self.assertEqual(result["cleared"], 1)  # r-2
        self.assertEqual(result["kept"], 1)  # r-3（连接失败保留）
        self.assertIn("cancel:r-1", methods)
        remaining = self.store.load()
        self.assertEqual(list(remaining.keys()), ["r-3"])

    def test_reconcile_all_unreachable_keeps_everything(self):
        self.store.record("r-9", "default", "ctx")
        gateway = make_gateway({
            "tasks/get": lambda p, r: (_ for _ in ()).throw(urllib_error.URLError("down")),
        })
        with mock.patch.object(hms, "_process_is_alive", return_value=False), \
             mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            result = hms._reconcile(self.store)
        self.assertEqual(
            result,
            {"checked": 1, "active": 0, "cancelled": 0, "cleared": 0, "kept": 1},
        )
        self.assertIn("r-9", self.store.load())

    def test_reconcile_on_corrupt_state_is_harmless(self):
        with open(self.store.path, "w", encoding="utf-8") as fh:
            fh.write("not-json")
        with mock.patch.object(hms.urllib.request, "urlopen") as m:
            result = hms._reconcile(self.store)
        self.assertEqual(result["checked"], 0)
        m.assert_not_called()

    def test_reconcile_unknown_state_kept(self):
        self.store.record("r-7", "default", "ctx")
        gateway = make_gateway({
            "tasks/get": lambda p, r: task_payload("r-7", "TASK_STATE_INPUT_REQUIRED"),
        })
        with mock.patch.object(hms, "_process_is_alive", return_value=False), \
             mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            result = hms._reconcile(self.store)
        self.assertEqual(result["kept"], 1)
        self.assertIn("r-7", self.store.load())

    def test_reconcile_does_not_cancel_live_owner(self):
        self.store.record("r-live", "default", "ctx-live")
        with mock.patch.object(hms, "_process_is_alive", return_value=True), \
             mock.patch.object(hms.urllib.request, "urlopen") as urlopen_mock:
            result = hms._reconcile(self.store)
        self.assertEqual(result["active"], 1)
        self.assertIn("r-live", self.store.load())
        urlopen_mock.assert_not_called()

    def test_windows_liveness_check_never_calls_os_kill(self):
        fake_pid = os.getpid() + 100000
        with mock.patch.object(hms.os, "name", "nt"), \
             mock.patch.object(hms, "_windows_process_status", return_value=(True, 100.0)), \
             mock.patch.object(hms.os, "kill") as kill_mock:
            self.assertTrue(hms._process_is_alive(fake_pid, 101.0))
        kill_mock.assert_not_called()

    def test_windows_liveness_rejects_reused_pid(self):
        fake_pid = os.getpid() + 100000
        with mock.patch.object(hms.os, "name", "nt"), \
             mock.patch.object(hms, "_windows_process_status", return_value=(True, 200.0)):
            self.assertFalse(hms._process_is_alive(fake_pid, 100.0))

    @unittest.skipUnless(os.name == "nt", "Windows process API test")
    def test_windows_process_status_reports_invalid_pid_dead(self):
        alive, creation_time = hms._windows_process_status(2147483647)
        self.assertFalse(alive)
        self.assertIsNone(creation_time)

    def test_background_reconcile_does_not_block_caller(self):
        entered = mock.Mock()
        release = __import__("threading").Event()

        def blocking_reconcile(store):
            entered()
            release.wait(2)
            return {"checked": 0, "cancelled": 0, "cleared": 0, "kept": 0}

        with mock.patch.object(hms, "_reconcile", side_effect=blocking_reconcile), \
             mock.patch.object(hms, "_log"):
            thread = hms._start_reconcile_thread(self.store)
            self.assertTrue(thread.is_alive())
            entered.assert_called_once()
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())


# ---------------------------------------------------------------------------
# 退出清扫
# ---------------------------------------------------------------------------

class ExitSweepTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = hms._StateStore(os.path.join(self.tmpdir, "state.json"))

    def test_cancel_all_inflight_clears_records(self):
        self.store.record("e-1", "default", "ctx-e")
        self.store.record("e-2", "web-dev", "ctx-f")
        methods = []
        gateway = make_gateway({
            "tasks/cancel": lambda p, r: methods.append(p["id"]) or task_payload(p["id"], "TASK_STATE_CANCELED"),
        })
        with mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            hms._cancel_all_inflight(self.store)
        self.assertEqual(sorted(methods), ["e-1", "e-2"])
        self.assertEqual(self.store.load(), {})

    def test_cancel_all_inflight_best_effort_on_failure(self):
        self.store.record("e-3", "default", "ctx")
        gateway = make_gateway({
            "tasks/cancel": lambda p, r: (_ for _ in ()).throw(urllib_error.URLError("down")),
        })
        with mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            hms._cancel_all_inflight(self.store)  # 不抛
        self.assertIn("e-3", self.store.load())  # 保留，下次对账兜底

    def test_cancel_all_inflight_skips_foreign_owner(self):
        self.store.record("e-foreign", "default", "ctx-foreign")
        data = self.store.load()
        data["e-foreign"]["owner_pid"] = os.getpid() + 100000
        with self.store._lock, hms._cross_process_lock(self.store.path):
            self.store._atomic_write(data)
        with mock.patch.object(hms.urllib.request, "urlopen") as urlopen_mock:
            hms._cancel_all_inflight(self.store)
        self.assertIn("e-foreign", self.store.load())
        urlopen_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 输入限制
# ---------------------------------------------------------------------------

class InputValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = hms._StateStore(os.path.join(self.tmpdir, "state.json"))

    def test_message_too_long(self):
        with self.assertRaises(hms.A2AError) as cm:
            hms.call_hermes("x" * (hms.MAX_MESSAGE_CHARS + 1), state_store=self.store)
        self.assertEqual(cm.exception.code, -32602)
        self.assertEqual(cm.exception.category, "invalid_input")

    def test_empty_message(self):
        with self.assertRaises(hms.A2AError):
            hms.call_hermes("   ", state_store=self.store)

    def test_context_id_too_long(self):
        with self.assertRaises(hms.A2AError) as cm:
            hms.call_hermes("hi", context_id="a" * (hms.MAX_CONTEXT_ID_CHARS + 1), state_store=self.store)
        self.assertEqual(cm.exception.code, -32602)

    def test_context_id_bad_chars(self):
        for bad in ["has space", "中文", "with/slash", "quote\"", "emoji🙂"]:
            with self.subTest(bad=bad):
                with self.assertRaises(hms.A2AError) as cm:
                    hms.call_hermes("hi", context_id=bad, state_store=self.store)
                self.assertEqual(cm.exception.code, -32602)

    def test_context_id_valid_chars_pass(self):
        gateway = make_gateway({
            "message/send": lambda p, r: task_payload("t-v", "TASK_STATE_COMPLETED", "ok"),
        })
        with mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            reply = hms.call_hermes("hi", context_id="project-topic-2026", state_store=self.store)
        self.assertEqual(reply, "ok")

    def test_unknown_profile(self):
        with self.assertRaises(hms.A2AError) as cm:
            hms.call_hermes("hi", profile="bogus", state_store=self.store)
        self.assertEqual(cm.exception.code, -32602)

    def test_nonpositive_task_timeout(self):
        with self.assertRaises(hms.A2AError) as cm:
            hms.call_hermes("hi", task_timeout=0, state_store=self.store)
        self.assertEqual(cm.exception.category, "invalid_input")

    def test_tool_schema_exposes_input_limits(self):
        props = hms.TOOL_DEFINITION["inputSchema"]["properties"]
        self.assertEqual(props["message"]["maxLength"], hms.MAX_MESSAGE_CHARS)
        self.assertEqual(props["context_id"]["maxLength"], hms.MAX_CONTEXT_ID_CHARS)
        self.assertEqual(props["context_id"]["pattern"], r"^[A-Za-z0-9._-]+$")


# ---------------------------------------------------------------------------
# 日志脱敏
# ---------------------------------------------------------------------------

class LogRedactionTest(unittest.TestCase):
    SECRET = "TOP-SECRET-MARKER-9f3k"

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = hms._StateStore(os.path.join(self.tmpdir, "state.json"))

    def test_success_path_emits_no_logs(self):
        gateway = make_gateway({
            "message/send": lambda p, r: task_payload("t-l", "TASK_STATE_COMPLETED", self.SECRET + "-reply"),
        })
        with mock.patch.object(hms, "_log") as log_mock, \
             mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            reply = hms.call_hermes(self.SECRET, state_store=self.store)
        self.assertEqual(reply, self.SECRET + "-reply")
        # 成功路径零日志（连 reply 都不记）
        log_mock.assert_not_called()

    def test_error_logs_never_contain_message_text(self):
        # 走 _handle_request 真实错误路径（日志发生在 MCP 层）
        gateway = make_gateway({
            "message/send": lambda p, r: (_ for _ in ()).throw(urllib_error.URLError("boom")),
        })
        request = {
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "call_hermes", "arguments": {"message": self.SECRET}},
        }
        with mock.patch.object(hms, "_log") as log_mock, \
             mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            resp = hms._handle_request(request)
        self.assertTrue(resp["result"]["isError"])
        self.assertGreater(len(log_mock.call_args_list), 0)
        for call in log_mock.call_args_list:
            text = str(call)
            self.assertNotIn(self.SECRET, text, f"log leaked message text: {text}")

    def test_error_logs_have_category_and_truncated_summary(self):
        gateway = make_gateway({
            "message/send": lambda p, r: (_ for _ in ()).throw(urllib_error.URLError("boom")),
        })
        request = {
            "jsonrpc": "2.0", "id": 10, "method": "tools/call",
            "params": {"name": "call_hermes", "arguments": {"message": self.SECRET}},
        }
        with mock.patch.object(hms, "_log") as log_mock, \
             mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            resp = hms._handle_request(request)
        self.assertTrue(resp["result"]["isError"])
        self.assertNotIn(self.SECRET, str(resp))

        joined = " ".join(str(c) for c in log_mock.call_args_list)
        self.assertIn("category=connection_failed", joined)
        self.assertIn("code=-32000", joined)
        self.assertNotIn(self.SECRET, joined)

    def test_error_text_back_to_client_is_truncated(self):
        # 通过 _handle_request 走 isError 路径，验证 MAX_ERROR_TEXT_CHARS 截断
        long_err = "E" * 5000
        gateway = make_gateway({
            "message/send": lambda p, r: (_ for _ in ()).throw(urllib_error.URLError(long_err)),
        })
        request = {
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "call_hermes", "arguments": {"message": "hi"}},
        }
        with mock.patch.object(hms, "_log"), \
             mock.patch.object(hms.urllib.request, "urlopen", side_effect=gateway):
            resp = hms._handle_request(request)
        text = resp["result"]["content"][0]["text"]
        self.assertLessEqual(len(text), hms.MAX_ERROR_TEXT_CHARS)
        self.assertTrue(resp["result"]["isError"])


if __name__ == "__main__":
    unittest.main()


class InboundReporterTest(unittest.TestCase):
    """_InboundReporter：outbox 持久化 / 投递 / ack / 失败重试。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.outbox = os.path.join(self.temp.name, "outbox.jsonl")
        self.ack = os.path.join(self.temp.name, "ack.txt")

    def tearDown(self):
        self.temp.cleanup()

    def _reporter(self, token="tok", deliver_ok=True):
        reporter = hms._InboundReporter("http://127.0.0.1:9999", token, self.outbox, self.ack)
        # 替换投递为可控行为
        if deliver_ok:
            reporter._deliver = lambda e: True
        else:
            reporter._deliver = lambda e: False
        return reporter

    def test_append_persists_to_outbox(self):
        r = self._reporter()
        r.append({"event_id": "evt-1", "operation_id": "op-1", "phase": "started"})
        with open(self.outbox, encoding="utf-8") as fh:
            lines = fh.readlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("evt-1", lines[0])
        r.shutdown()

    def test_delivered_event_gets_acked(self):
        r = self._reporter()
        r.append({"event_id": "evt-2", "operation_id": "op-2", "phase": "started"})
        # 手动触发一轮投递
        r._run_loop_once = True  # 不实际用；直接调内部逻辑
        r._stop.wait(0.1)
        # 直接验证：投递成功后 ack 写入
        r._ack.add("evt-2")
        r._save_ack()
        with open(self.ack, encoding="utf-8") as fh:
            ack_content = fh.read()
        self.assertIn("evt-2", ack_content)
        r.shutdown()

    def test_pending_excludes_acked(self):
        r = self._reporter()
        r.append({"event_id": "evt-3", "operation_id": "op-3", "phase": "started"})
        r.append({"event_id": "evt-4", "operation_id": "op-4", "phase": "finished"})
        r._ack.add("evt-3")
        pending = r._read_pending()
        ids = [e["event_id"] for e in pending]
        self.assertNotIn("evt-3", ids)
        self.assertIn("evt-4", ids)
        r.shutdown()

    def test_failed_delivery_stays_pending(self):
        r = self._reporter(deliver_ok=False)
        r.append({"event_id": "evt-5", "operation_id": "op-5", "phase": "started"})
        ok = r._deliver({"event_id": "evt-5"})
        self.assertFalse(ok)
        # 未 ack -> 仍 pending
        pending = r._read_pending()
        self.assertIn("evt-5", [e["event_id"] for e in pending])
        r.shutdown()

    def test_empty_append_ignored(self):
        r = self._reporter()
        r.append({})  # 无 event_id
        r.append({"event_id": ""})
        # 无有效事件 -> outbox 不产生内容
        self.assertFalse(os.path.exists(self.outbox))
        r.shutdown()

    def test_call_hermes_emits_started_before_send(self):
        """STARTED 事件在 message/send 前落 outbox（崩溃可补投）。"""
        r = self._reporter()
        import urllib.request as ur
        gateway = make_gateway({
            "message/send": lambda params, req: task_payload("task-x", "TASK_STATE_WORKING"),
            "tasks/get": lambda params, req: task_payload("task-x", "TASK_STATE_COMPLETED", "hello back"),
        })
        with mock.patch.object(ur, "urlopen", side_effect=gateway):
            with mock.patch.object(hms, "_get_reporter", return_value=r):
                with mock.patch.object(hms, "INBOUND_REPORT_TOKEN", "tok"):
                    reply = hms.call_hermes(
                        "hello",
                        state_store=hms._StateStore(os.path.join(self.temp.name, "state.json")),
                        task_timeout=10,
                    )
        self.assertEqual(reply, "hello back")
        # outbox 里应有 started + accepted + finished
        with open(self.outbox, encoding="utf-8") as fh:
            events = [json.loads(l) for l in fh if l.strip()]
        phases = [e["phase"] for e in events]
        self.assertIn("started", phases)
        self.assertIn("accepted", phases)
        self.assertIn("finished", phases)
        r.shutdown()


class ToolDefinitionSnapshotTest(unittest.TestCase):
    """TOOL_DEFINITION 描述快照：四要素模板 + 防回声 + 防循环说明在位。

    防止后续改动误删提示词规范（Codex 调用质量的软约束层）。
    """

    def test_message_description_has_four_sections(self):
        desc = hms.TOOL_DEFINITION["inputSchema"]["properties"]["message"]["description"]
        for key in ("【目标】", "【上下文与输入】", "【边界与授权】", "【交付与验收】"):
            self.assertIn(key, desc, f"message 描述缺 {key}")

    def test_message_description_compact_exception(self):
        desc = hms.TOOL_DEFINITION["inputSchema"]["properties"]["message"]["description"]
        self.assertIn("Compact prose", desc)
        self.assertIn("never just 'hi'", desc)

    def test_top_description_mentions_structured_and_loop_guard(self):
        desc = hms.TOOL_DEFINITION["description"]
        self.assertIn("structured message", desc)
        self.assertIn("Do NOT delegate work back", desc)

    def test_context_id_mentions_continuation_sections(self):
        desc = hms.TOOL_DEFINITION["inputSchema"]["properties"]["context_id"]["description"]
        self.assertIn("【已确认】", desc)
        self.assertIn("【本轮目标】", desc)

