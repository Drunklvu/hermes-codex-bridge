"""TaskService 窄接口——SDK 接入的接缝（阶段 1）。

设计原则（Codex 共创方案）：
- 现有 codex_a2a_bridge.py 仍是唯一执行者和状态权威
- TaskService 只是「窄接口」，供未来 SDK sidecar（:10000）调用
- 不改变现有 Codex 协作行为（:9998 私有协议完全不动）
- 用 typing.Protocol 定义，现有 CodexBridge 天然满足（鸭子类型），
  不需要强改现有类——SDK sidecar 依赖此接口而非具体类

行为契约（不可回归，见 docs/test-inventory-20260807.md ⭐ 标注）：
- submit 幂等：同一 idempotency_key 不重复启动 Codex 进程
- cancel 竞态：与 completed 原子转换，终态不可逆
- events 单调 cursor：断线重连不丢终态、不重复执行
- inbound 侧：噪音判定/幂等合并/tombstone 由核心管理，sidecar 只读
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# 内部数据模型（DTO）——不让 SDK/Pydantic 类型渗入核心
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TaskView:
    """任务快照（SDK 侧可消费的最小视图）。"""
    id: str
    state: str                      # TASK_STATE_*（核心的规范化状态）
    direction: str                  # outbound / inbound
    context_id: str
    created_at: str
    finished_at: str | None = None
    summary: str = ""
    message_summary: str = ""
    reply_summary: str = ""
    gateway_state: str | None = None
    source: str | None = None       # none / memory / file / inbound-report
    noise: bool = False
    extra: dict[str, Any] = field(default_factory=dict)  # SDK 私有字段扩展位

    @classmethod
    def from_task(cls, t: dict[str, Any]) -> "TaskView":
        status = t.get("status") or {}
        # 兼容两种格式：内部存 status.state（TASK_STATE_*），API 层扁平化 state（COMPLETED）
        raw_state = status.get("state") or t.get("state") or ""
        state = raw_state if raw_state.startswith("TASK_STATE_") else (
            "TASK_STATE_" + raw_state if raw_state else "")
        return cls(
            id=t.get("id", ""),
            state=state,
            direction=t.get("direction", ""),
            context_id=t.get("contextId", ""),
            created_at=t.get("created_at", ""),
            finished_at=t.get("finished_at"),
            summary=t.get("summary", ""),
            message_summary=t.get("message_summary", ""),
            reply_summary=t.get("reply_summary", ""),
            gateway_state=t.get("gateway_state"),
            source=t.get("source"),
            noise=bool(t.get("noise")),
            extra={k: v for k, v in t.items()
                   if k not in {"id", "status", "state", "direction", "contextId",
                                "created_at", "finished_at", "summary",
                                "message_summary", "reply_summary",
                                "gateway_state", "source", "noise"}},
        )


@dataclass(frozen=True)
class EventView:
    """实时事件（单调 cursor，供轮询/流式）。"""
    seq: int
    type: str                       # message / tool / item / inbound / status
    role: str                       # assistant / user / tool / system
    ts: str
    text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_event(cls, e: dict[str, Any]) -> "EventView":
        return cls(
            seq=e.get("seq", 0),
            type=e.get("type", ""),
            role=e.get("role", ""),
            ts=e.get("ts", ""),
            text=e.get("text", ""),
            extra={k: v for k, v in e.items()
                   if k not in {"seq", "type", "role", "ts", "text"}},
        )


@dataclass(frozen=True)
class SubmitRequest:
    """提交任务请求。"""
    prompt: str
    context_id: str = ""
    idempotency_key: str = ""       # 幂等键：同一键不重复执行
    principal: str = "local"        # 身份命名空间（远程部署时用于隔离）
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SubmitResult:
    task_id: str
    created: bool                   # False = 幂等命中已存在任务
    task: TaskView | None = None


# ---------------------------------------------------------------------------
# 窄接口定义
# ---------------------------------------------------------------------------

@runtime_checkable
class TaskService(Protocol):
    """SDK sidecar 依赖的最小接口（现有 CodexBridge 天然满足）。

    方法语义与现有行为严格一致（不新增、不改变）：
    - submit     -> start_task（+ 幂等键支持，未实现幂等时由调用方保证）
    - get        -> 按 id 取单任务
    - cancel     -> 取消（现有 cancel 语义：终态不可逆）
    - events     -> 事件流（单调 seq cursor）
    - list       -> 查询任务列表
    """

    def submit(self, req: SubmitRequest) -> SubmitResult:
        """提交任务。幂等键命中时返回已存在任务（created=False）。"""
        ...

    def get(self, task_id: str) -> TaskView | None:
        """按任务 id 取快照；不存在返回 None。"""
        ...

    def cancel(self, task_id: str) -> bool:
        """取消任务（WORKING 才可取消，终态返回 False）。"""
        ...

    def events(self, task_id: str, after: int = -1, limit: int = 500) -> tuple[list[EventView], int]:
        """返回 seq > after 的事件 + 最新 seq（单调 cursor）。"""
        ...

    def list(self, states: list[str] | None = None, limit: int = 100) -> list[TaskView]:
        """按状态过滤查询任务列表。"""
        ...


class LegacyBridgeAdapter:
    """把现有 CodexBridge 适配成 TaskService（鸭子类型直通，零行为改变）。

    不做任何转换逻辑——只是把现有方法包装成 Protocol 签名，
    保证 SDK sidecar 依赖 TaskService 而不依赖具体类。
    """

    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge

    def submit(self, req: SubmitRequest) -> SubmitResult:
        task_id = self._bridge.start_task(req.prompt, req.context_id)
        return SubmitResult(task_id=task_id, created=True)

    def get(self, task_id: str) -> TaskView | None:
        task = self._bridge.store.get(task_id)
        return TaskView.from_task(task) if task else None

    def cancel(self, task_id: str) -> bool:
        ok, _msg, _task = self._bridge.delete_task(task_id)
        return ok

    def events(self, task_id: str, after: int = -1, limit: int = 500) -> tuple[list[EventView], int]:
        events, latest = self._bridge.store.events_since(task_id, after=after, limit=limit)
        return [EventView.from_event(e) for e in events], latest

    def list(self, states: list[str] | None = None, limit: int = 100) -> list[TaskView]:
        tasks = self._bridge.query_tasks(states=states)
        return [TaskView.from_task(t) for t in tasks][:limit]
