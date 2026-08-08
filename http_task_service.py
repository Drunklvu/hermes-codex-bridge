"""HttpTaskService——TaskService 的 HTTP 实现（阶段 3）。

sidecar 通过它调桥 :9998 的 internal/* 端点：
- POST internal/submit   -> 提交任务
- POST internal/cancel   -> 取消任务
- GET  /internal/tasks/<id> -> 单任务查询
- GET  /tasks/<id>/events -> 事件流（桥已有端点）

实现 TaskService Protocol（task_service.py），SDK 侧零感知。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from task_service import EventView, SubmitRequest, SubmitResult, TaskView

logger = logging.getLogger("http-task-service")


class HttpTaskService:
    """HTTP 版 TaskService：调桥 internal 端点（Bearer 鉴权）。"""

    def __init__(self, bridge_url: str = "http://127.0.0.1:9998", token: str = "") -> None:
        self._base = bridge_url.rstrip("/")
        self._token = token

    # ---- 内部 HTTP helper ----

    def _headers(self, json_body: bool = False) -> dict[str, str]:
        h = {}
        if self._token:
            h["Authorization"] = "Bearer " + self._token
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def _post(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self._base + "/", data=body, headers=self._headers(json_body=True), method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def _get(self, path: str) -> dict[str, Any]:
        req = urllib.request.Request(self._base + path, headers=self._headers(), method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    # ---- TaskService Protocol ----

    def submit(self, req: SubmitRequest) -> SubmitResult:
        try:
            r = self._post("internal/submit", {"prompt": req.prompt, "context_id": req.context_id})
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"submit failed: {e.code} {e.read().decode()[:200]}") from e
        if not r.get("ok"):
            raise RuntimeError(f"submit rejected: {r}")
        return SubmitResult(task_id=r["task_id"], created=True)

    def get(self, task_id: str) -> TaskView | None:
        try:
            r = self._get("/internal/tasks/" + urllib.parse.quote(task_id))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise RuntimeError(f"get failed: {e.code}") from e
        return TaskView(
            id=r.get("id", ""),
            state=r.get("state", ""),
            direction=r.get("direction") or "",
            context_id=r.get("contextId") or "",
            created_at=r.get("created_at") or "",
            finished_at=r.get("finished_at"),
            summary=r.get("summary", ""),
        )

    def cancel(self, task_id: str) -> bool:
        try:
            r = self._post("internal/cancel", {"task_id": task_id})
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"cancel failed: {e.code}") from e
        return bool(r.get("ok"))

    def events(self, task_id: str, after: int = -1, limit: int = 500) -> tuple[list[EventView], int]:
        try:
            r = self._get(f"/tasks/{urllib.parse.quote(task_id)}/events?after={after}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return [], -1
            raise RuntimeError(f"events failed: {e.code}") from e
        events = [EventView(seq=e.get("seq", 0), type=e.get("type", ""), role=e.get("role", ""),
                            ts=e.get("ts", ""), text=e.get("text", ""))
                  for e in r.get("events", [])]
        return events[-limit:], r.get("latest", -1)

    def list(self, states: list[str] | None = None, limit: int = 100) -> list[TaskView]:
        # 桥 /tasks 已支持 state 过滤参数
        q = "&".join(f"state={urllib.parse.quote(s)}" for s in (states or []))
        url = "/tasks" + (("?" + q) if q else "")
        r = self._get(url)
        tasks = [TaskView(
            id=t.get("id", ""),
            state=t.get("state", ""),
            direction=t.get("direction", ""),
            context_id=t.get("contextId", ""),
            created_at=t.get("created_at", ""),
            finished_at=t.get("finished_at"),
            summary=t.get("summary", ""),
        ) for t in r.get("tasks", [])]
        return tasks[:limit]
