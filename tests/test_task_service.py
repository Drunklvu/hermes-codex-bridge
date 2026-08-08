"""TaskService 窄接口测试（阶段 1 交付验证）。

验证目标：
1. LegacyBridgeAdapter 直通现有桥（行为不变）
2. TaskView/EventView 转换正确（字段映射无丢失）
3. 接口语义与现有行为一致（submit/get/cancel/events/list）
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

# 发布版：根目录模块用 importlib 加载（tests/ 子目录无法直接 import）
_MODULE_DIR = Path(__file__).resolve().parent.parent
import sys as _sys

_TS_SPEC = importlib.util.spec_from_file_location("task_service", _MODULE_DIR / "task_service.py")
task_service = importlib.util.module_from_spec(_TS_SPEC)
assert _TS_SPEC.loader is not None
_sys.modules["task_service"] = task_service  # dataclass 处理需要模块在 sys.modules
_TS_SPEC.loader.exec_module(task_service)

_BRIDGE_SPEC = importlib.util.spec_from_file_location("codex_a2a_bridge", _MODULE_DIR / "codex_a2a_bridge.py")
bridge_module = importlib.util.module_from_spec(_BRIDGE_SPEC)
assert _BRIDGE_SPEC.loader is not None
_sys.modules["codex_a2a_bridge"] = bridge_module
_BRIDGE_SPEC.loader.exec_module(bridge_module)


class TaskServiceAdapterTests(unittest.TestCase):
    """LegacyBridgeAdapter 直通现有桥的验证。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = bridge_module.TaskStore(Path(self.temp.name) / "tasks.json")
        self.bridge = bridge_module.CodexBridge(
            codex=None, workspace=Path(self.temp.name), state_dir=Path(self.temp.name),
            model="", sync_wait=1, codex_timeout=5, max_concurrent=1,
        )
        self.bridge.store = self.store
        self.adapter = task_service.LegacyBridgeAdapter(self.bridge)
        # 预置一个任务
        self.store.add({
            "id": "task-abc123",
            "direction": "outbound",
            "contextId": "ctx-test",
            "status": {"state": "TASK_STATE_COMPLETED", "timestamp": bridge_module.utc_timestamp()},
            "summary": "测试任务",
            "created_at": bridge_module.utc_timestamp(),
        })

    def tearDown(self):
        self.temp.cleanup()

    def test_adapter_is_task_service_protocol(self):
        """适配器应满足 TaskService Protocol（runnable_checkable）。"""
        self.assertIsInstance(self.adapter, task_service.TaskService)

    def test_get_returns_task_view(self):
        """get 返回 TaskView，字段映射正确。"""
        tv = self.adapter.get("task-abc123")
        self.assertIsNotNone(tv)
        assert tv is not None
        self.assertEqual(tv.id, "task-abc123")
        self.assertEqual(tv.state, "TASK_STATE_COMPLETED")
        self.assertEqual(tv.direction, "outbound")
        self.assertEqual(tv.context_id, "ctx-test")
        self.assertEqual(tv.summary, "测试任务")

    def test_get_missing_returns_none(self):
        """不存在的任务返回 None。"""
        self.assertIsNone(self.adapter.get("task-nonexistent"))

    def test_events_returns_event_views(self):
        """events 返回 EventView 列表 + 单调 seq。"""
        self.store.append_event("task-abc123", {
            "type": "message", "role": "assistant", "ts": "t1", "text": "hello",
        })
        events, latest = self.adapter.events("task-abc123", after=-1)
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], task_service.EventView)
        self.assertEqual(events[0].text, "hello")
        self.assertEqual(events[0].seq, 0)
        self.assertEqual(latest, 0)

    def test_list_returns_task_views(self):
        """list 返回 TaskView 列表。"""
        tasks = self.adapter.list()
        self.assertEqual(len(tasks), 1)
        self.assertIsInstance(tasks[0], task_service.TaskView)
        self.assertEqual(tasks[0].id, "task-abc123")

    def test_list_filters_by_state(self):
        """list 按状态过滤（透传 query_tasks）。"""
        # 加一个 WORKING 任务
        self.store.add({
            "id": "task-working",
            "direction": "outbound",
            "contextId": "ctx-w",
            "status": {"state": "TASK_STATE_WORKING", "timestamp": bridge_module.utc_timestamp()},
            "created_at": bridge_module.utc_timestamp(),
        })
        working = self.adapter.list(states=["TASK_STATE_WORKING"])
        self.assertEqual(len(working), 1)
        self.assertEqual(working[0].id, "task-working")

    def test_cancel_deletes_task(self):
        """cancel 透传 delete_task：删除终态任务。"""
        ok = self.adapter.cancel("task-abc123")
        self.assertTrue(ok)
        self.assertIsNone(self.adapter.get("task-abc123"))

    def test_cancel_working_rejected(self):
        """WORKING 任务不可取消（delete_task 拒绝）。"""
        self.store.add({
            "id": "task-busy",
            "direction": "outbound",
            "contextId": "ctx-b",
            "status": {"state": "TASK_STATE_WORKING", "timestamp": bridge_module.utc_timestamp()},
            "created_at": bridge_module.utc_timestamp(),
        })
        ok = self.adapter.cancel("task-busy")
        self.assertFalse(ok)
        self.assertIsNotNone(self.adapter.get("task-busy"))

    def test_taskview_from_task_keeps_extra_fields(self):
        """TaskView 转换保留私有扩展字段（SDK 语义扩展位）。"""
        self.store.add({
            "id": "task-extra",
            "direction": "inbound",
            "contextId": "ctx-e",
            "status": {"state": "TASK_STATE_COMPLETED", "timestamp": bridge_module.utc_timestamp()},
            "created_at": bridge_module.utc_timestamp(),
            "operation_id": "op-123",
            "source": "hermes_mcp",
        })
        tv = self.adapter.get("task-extra")
        assert tv is not None
        self.assertEqual(tv.extra.get("operation_id"), "op-123")
        self.assertEqual(tv.source, "hermes_mcp")


class TaskStateMachineTests(unittest.TestCase):
    """TaskStore._can_transition 状态流转规则测试（显式规则收口后的单测）。"""

    @classmethod
    def setUpClass(cls):
        # TaskStore 需要 state_file，用临时目录构造
        cls._tmp = tempfile.mkdtemp()
        from pathlib import Path
        cls.store = bridge_module.TaskStore(Path(cls._tmp) / "tasks.json")

    def test_new_task_any_state(self):
        """无当前状态 → 任何新状态都合法。"""
        self.assertTrue(self.store._can_transition(None, "TASK_STATE_WORKING"))
        self.assertTrue(self.store._can_transition(None, "TASK_STATE_COMPLETED"))

    def test_terminal_to_working_rejected(self):
        """终态 → WORKING 拒绝（任务不允许复活）。"""
        for terminal in ["TASK_STATE_COMPLETED", "TASK_STATE_FAILED",
                         "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"]:
            self.assertFalse(self.store._can_transition(terminal, "TASK_STATE_WORKING"),
                             f"{terminal} -> WORKING 应拒绝")

    def test_working_updates_allowed(self):
        """WORKING → 任何状态都允许（运行中任务可完成/失败/取消）。"""
        self.assertTrue(self.store._can_transition("TASK_STATE_WORKING", "TASK_STATE_COMPLETED"))
        self.assertTrue(self.store._can_transition("TASK_STATE_WORKING", "TASK_STATE_FAILED"))
        self.assertTrue(self.store._can_transition("TASK_STATE_WORKING", "TASK_STATE_CANCELED"))
        self.assertTrue(self.store._can_transition("TASK_STATE_WORKING", "TASK_STATE_REJECTED"))

    def test_terminal_to_terminal_allowed(self):
        """终态之间互转保留（历史行为：REJECTED→COMPLETED 等场景存在）。"""
        self.assertTrue(self.store._can_transition("TASK_STATE_COMPLETED", "TASK_STATE_REJECTED"))
        self.assertTrue(self.store._can_transition("TASK_STATE_CANCELED", "TASK_STATE_FAILED"))


if __name__ == "__main__":
    unittest.main()
