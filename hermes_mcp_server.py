"""Hermes MCP server: stdio MCP -> local Hermes A2A (JSON-RPC over HTTP).

A pure-stdlib stdio MCP server exposing one tool, ``call_hermes``. The tool
sends a user message to the local Hermes agent over A2A JSON-RPC
(POST http://127.0.0.1:9900), polls ``tasks/get`` while the task is WORKING,
and returns Hermes's reply text as an MCP text result.

MCP protocol methods implemented (JSON-RPC over stdio):
  initialize, notifications/initialized, tools/list, tools/call, ping

Stdio input accepts both legacy ``Content-Length`` frames and the newer
newline-delimited JSON framing. Each response uses the framing detected for
its corresponding request.

Only MCP frames are written to stdout; logs go to stderr.

Configuration via environment variables:
  HERMES_A2A_URL          A2A JSON-RPC endpoint (default http://127.0.0.1:9900)
  HERMES_DEFAULT_URL      default profile endpoint (defaults to HERMES_A2A_URL)
  HERMES_WEB_DEV_URL      web-dev profile endpoint (default http://127.0.0.1:9901)
  HERMES_A2A_TOKEN        optional Bearer token sent to Hermes A2A endpoints
  HERMES_TASK_TIMEOUT     max seconds to wait for a task (default 300)
  HERMES_POLL_INTERVAL    seconds between tasks/get polls (default 1.0)
  HERMES_HTTP_TIMEOUT     per-HTTP-request timeout in seconds (default 10.0)
  HERMES_STATE_FILE       path to the in-flight task state file (default
                          ~/.hermes/mcp_inflight.json)

In-flight task tracking: once message/send returns a task id, the task is
recorded in an atomically-written JSON state file. On poll timeout the
server best-effort calls tasks/cancel; on stdin EOF / SIGINT / SIGTERM it
best-effort cancels all recorded tasks; on the next startup it reconciles
the file (query each recorded task, cancel still-WORKING ones, drop
terminal ones, keep those whose gateway is unreachable). Reconciliation
failures never prevent startup. On Windows, SIGTERM delivered via
TerminateProcess does not run Python handlers — the state file and the
next-start reconciliation are the recovery backstop.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import re
import signal
import sys
import threading
import time
import datetime
import uuid
from pathlib import Path
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "hermes-mcp-server"
SERVER_VERSION = "1.0.0"

A2A_URL = os.environ.get("HERMES_A2A_URL", "http://127.0.0.1:9900")
PROFILE_PORTS = {
    "default": os.environ.get("HERMES_DEFAULT_URL", A2A_URL),
    "web-dev": os.environ.get("HERMES_WEB_DEV_URL", "http://127.0.0.1:9901"),
}
A2A_TOKEN = os.environ.get("HERMES_A2A_TOKEN", "").strip()
STATE_FILE = os.environ.get(
    "HERMES_STATE_FILE",
    os.path.join(os.path.expanduser("~"), ".hermes", "mcp_inflight.json"),
)
TASK_TIMEOUT = float(os.environ.get("HERMES_TASK_TIMEOUT", "300"))
POLL_INTERVAL = float(os.environ.get("HERMES_POLL_INTERVAL", "1.0"))
HTTP_TIMEOUT = float(os.environ.get("HERMES_HTTP_TIMEOUT", "10.0"))
MAX_BODY_BYTES = 2 ** 20  # 1 MiB per A2A HTTP response
MAX_REPLY_CHARS = 200_000
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024

# Input limits and error-text redaction budgets.
MAX_MESSAGE_CHARS = 50_000
MAX_CONTEXT_ID_CHARS = 64
CONTEXT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_ERROR_TEXT_CHARS = 2_000
LOG_SUMMARY_CHARS = 200
RECONCILE_TIMEOUT = 5.0
CANCEL_TIMEOUT = 5.0
TRUNCATION_MARKER = "\n\n[Hermes response truncated by MCP bridge]"
INBOUND_REPORT_URL = os.environ.get("INBOUND_REPORT_URL", "http://127.0.0.1:9998")


def _load_inbound_env() -> tuple[str, str]:
    """从环境变量或 state 目录的 inbound.env（start 脚本生成）读上报配置。

    优先级：环境变量 > inbound.env 文件 > 默认（URL 默认 9998，token 空=禁用）。
    """
    url = os.environ.get("INBOUND_REPORT_URL", "")
    token = os.environ.get("INBOUND_REPORT_TOKEN", "")
    if url and token:
        return url, token
    # 尝试从已知位置读 inbound.env（start_codex_a2a_bridge.ps1 写入）：
    # 1) 桥 state 目录（脚本父目录的 .codex-a2a 兄弟目录）
    # 2) 本进程 state 文件所在目录（兜底）
    candidates = []
    script_dir = Path(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(script_dir.parent / ".codex-a2a" / "inbound.env")
    candidates.append(Path(os.path.dirname(os.path.abspath(STATE_FILE))) / "inbound.env")
    for env_file in candidates:
        try:
            with open(env_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    value = value.strip()
                    if key == "INBOUND_REPORT_URL" and not url:
                        url = value
                    elif key == "INBOUND_REPORT_TOKEN" and not token:
                        token = value
        except OSError:
            continue
        if url and token:
            break
    return url or "http://127.0.0.1:9998", token


_INBOUND_REPORT_URL, _INBOUND_REPORT_TOKEN = _load_inbound_env()
INBOUND_REPORT_URL = _INBOUND_REPORT_URL
INBOUND_REPORT_TOKEN = _INBOUND_REPORT_TOKEN
INBOUND_OUTBOX_FILE = os.environ.get(
    "INBOUND_OUTBOX_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "inbound_outbox.jsonl"),
)
INBOUND_ACK_FILE = os.environ.get(
    "INBOUND_ACK_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "inbound_outbox.ack"),
)
INBOUND_MAX_OUTBOX = int(os.environ.get("INBOUND_MAX_OUTBOX", "500"))
INBOUND_RETRY_BACKOFF = 2.0  # 指数退避基数（秒）
INBOUND_REPORT_TIMEOUT = 5.0
INBOUND_SUMMARY_CHARS = 400  # message/reply 摘要长度上限（与桥一致）

_CONTENT_LENGTH_HEADER_NAMES = {b"content-length", b"content-type"}

TERMINAL_FAILED_STATES = {
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
}

TERMINAL_STATES = TERMINAL_FAILED_STATES | {"TASK_STATE_COMPLETED"}

TOOL_DEFINITION = {
    "name": "call_hermes",
    "description": (
        "Delegate a concrete task to the local Hermes agent and wait for its final reply. "
        "Use a structured message (see message format below) for tool-using, multi-step, "
        "state-changing, or context-dependent work so Hermes has the background, "
        "authorization boundary, and acceptance criteria it needs. Hermes may perform "
        "real actions within the stated authorization. Do NOT delegate work back to "
        "Codex with this tool (prevents Hermes<->Codex loops)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_MESSAGE_CHARS,
                "description": (
                    "Task text sent verbatim to Hermes. Default structured format:\n"
                    "'请帮我完成以下任务：\n【目标】one-line objective\n"
                    "【上下文与输入】background, file paths, inputs (or 无)\n"
                    "【边界与授权】scope + negative list, e.g. 只读；仅限某目录；"
                    "禁止修改文件/外网/发送\n【交付与验收】expected deliverable, "
                    "format, length, done-criteria'.\n"
                    "Each section may be one concise line. Compact prose is allowed "
                    "only for a trivial one-shot task with no side effects or "
                    "prior-context dependency; it must still state the concrete "
                    "objective and desired answer (never just 'hi')."
                ),
            },
            "profile": {
                "type": "string",
                "enum": ["default", "web-dev"],
                "default": "default",
                "description": (
                    "Which Hermes profile to run the task on. default = main profile (port 9900); "
                    "web-dev = second profile for web development (port 9901)."
                ),
            },
            "context_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_CONTEXT_ID_CHARS,
                "pattern": r"^[A-Za-z0-9._-]+$",
                "description": (
                    "Conversation/session id (logical workstream). REUSE the same id "
                    "for follow-up tasks in the same workstream so Hermes continues "
                    "with full context; omit for a fresh one-shot task. When "
                    "continuing, also add 【已确认】 (confirmed results so far) and "
                    "【本轮目标】 (this round's increment) to the message. Suggested "
                    "naming: '<project>-<workstream>' (e.g. 'demo-project'). Use a "
                    "'-review' suffix for independent reviews (isolation) and "
                    "'-parallel-a/b' for concurrent tasks on the same topic."
                ),
            },
        },
        "required": ["message"],
        "additionalProperties": False,
    },
}


class A2AError(Exception):
    """Raised for A2A transport, JSON-RPC, or terminal task failures.

    ``category`` labels the failure class for log redaction
    (timeout / connection_failed / rpc_error / task_failed / invalid_input ...).
    ``task_id`` is attached when known so logs and errors can identify the
    task that may still be running on the gateway.
    """

    def __init__(
        self,
        message: str,
        code: int = -32000,
        category: str = "generic",
        task_id: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.category = category
        self.task_id = task_id


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _short(text, limit: int = LOG_SUMMARY_CHARS) -> str:
    """Truncate text for logs; never raises."""
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


@contextmanager
def _cross_process_lock(path: str):
    """Serialize state-file updates across MCP server processes."""
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    with open(lock_path, "a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _windows_process_status(pid: int) -> tuple[bool, float | None]:
    """Return Windows process liveness and creation time without signalling it."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    wait_timeout = 258
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        process_query_limited_information | synchronize,
        False,
        pid,
    )
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no such process.
            return False, None
        # Access denied and unknown query failures are treated as alive.
        return True, None
    try:
        wait_result = kernel32.WaitForSingleObject(handle, 0)
        if wait_result != wait_timeout:
            return False, None

        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return True, None
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return True, ticks / 10_000_000 - 11_644_473_600
    finally:
        kernel32.CloseHandle(handle)


def _process_is_alive(pid, record_started_at: float | None = None) -> bool:
    """Check owner liveness and reject a PID reused after the task was recorded."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        alive, creation_time = _windows_process_status(pid)
        if not alive:
            return False
        if creation_time is not None and record_started_at is not None:
            return creation_time <= float(record_started_at) + 1.0
        return True
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class _StateStore:
    """Atomically persisted in-flight task records.

    One JSON file mapping task_id -> {profile, context_id_hash, started_at,
    state}. context_id is stored only as a SHA-256 prefix (safe
    representation). Writes go to a sibling temp file then ``os.replace``
    for atomicity on Windows. Any storage failure is logged and swallowed —
    the server must never die because its state file is unwritable or
    corrupt; the record is simply not durable and best-effort paths degrade
    gracefully.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()

    def _load_unlocked(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("state file root must be an object")
            return {k: v for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            _log(f"state file unreadable ({_short(exc)}); starting with empty state")
            return {}

    def load(self) -> dict:
        try:
            with self._lock, _cross_process_lock(self.path):
                return self._load_unlocked()
        except OSError as exc:
            _log(f"state lock failed (continuing): {_short(exc)}")
            return {}

    def _atomic_write(self, data: dict) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp_path, self.path)

    def record(self, task_id: str, profile: str, context_id: str | None) -> None:
        try:
            with self._lock, _cross_process_lock(self.path):
                data = self._load_unlocked()
                data[task_id] = {
                    "profile": profile,
                    "context_id_hash": hashlib.sha256((context_id or "").encode("utf-8")).hexdigest()[:16],
                    "owner_pid": os.getpid(),
                    "started_at": time.time(),
                    "state": "WORKING",
                }
                self._atomic_write(data)
        except OSError as exc:
            _log(f"state write failed (continuing): {_short(exc)}")

    def remove(self, task_id: str) -> None:
        try:
            with self._lock, _cross_process_lock(self.path):
                data = self._load_unlocked()
                if task_id not in data:
                    return
                del data[task_id]
                self._atomic_write(data)
        except OSError as exc:
            _log(f"state write failed (continuing): {_short(exc)}")


class _InboundReporter:
    """反向链路事件上报器：JSONL outbox + 后台发送线程 + 指数退避重试。

    call_hermes 同步 append 事件到 outbox（追加写，原子性靠单行 JSON），
    后台线程消费投递到桥的 POST /inbound/events。已确认的 event_id 记录
    在 ack 文件（避免积压重复发送）。上报失败绝不影响 call_hermes 结果。
    """

    def __init__(self, url: str, token: str, outbox_file: str, ack_file: str):
        self.url = url.rstrip("/")
        self.token = token
        self.outbox_file = outbox_file
        self.ack_file = ack_file
        self._lock = threading.RLock()
        self._ack: set[str] = set()
        self._load_ack()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="inbound-reporter", daemon=True)
        self._thread.start()

    def _load_ack(self) -> None:
        try:
            with open(self.ack_file, "r", encoding="utf-8") as fh:
                self._ack = {line.strip() for line in fh if line.strip()}
        except OSError:
            self._ack = set()

    def _save_ack(self) -> None:
        try:
            # 原子写：临时文件 + os.replace，多进程共享 ack 时避免半写/交错
            tmp = self.ack_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                for eid in sorted(self._ack):
                    fh.write(eid + "\n")
            os.replace(tmp, self.ack_file)
        except OSError:
            pass  # ack 丢失只导致重复投递，桥端幂等兜底

    def append(self, event: dict) -> None:
        """同步追加一个事件到 outbox（不阻塞、不抛出影响调用方）。"""
        event_id = event.get("event_id") or ""
        if not event_id:
            return
        try:
            with self._lock:
                with open(self.outbox_file, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as exc:
            _log(f"inbound outbox append failed: {_short(exc)}")

    def _run(self) -> None:
        """后台线程：读取未确认事件，指数退避投递，成功后记录 ack。"""
        backoff = INBOUND_RETRY_BACKOFF
        while not self._stop.is_set():
            try:
                pending = self._read_pending()
                if pending:
                    for event in pending:
                        if self._stop.is_set():
                            return
                        if self._deliver(event):
                            with self._lock:
                                self._ack.add(event["event_id"])
                                self._save_ack()
                            backoff = INBOUND_RETRY_BACKOFF
                        else:
                            backoff = min(backoff * 2, 60)
                            break  # 失败：等退避后重试同一批
                else:
                    backoff = INBOUND_RETRY_BACKOFF
            except Exception as exc:
                _log(f"inbound reporter error: {_short(exc)}")
            self._stop.wait(backoff)

    def _read_pending(self) -> list[dict]:
        try:
            with self._lock:
                with open(self.outbox_file, "r", encoding="utf-8") as fh:
                    lines = fh.readlines()
            pending = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                eid = event.get("event_id") or ""
                if eid and eid not in self._ack:
                    pending.append(event)
            return pending[:INBOUND_MAX_OUTBOX]
        except OSError:
            return []

    def _deliver(self, event: dict) -> bool:
        if not self.token:
            return True  # 未配置 token：视为投递成功（监控不可用时静默降级）
        try:
            req = urllib.request.Request(
                self.url + "/",
                data=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": event["event_id"],
                        "method": "inbound/events",
                        "params": event,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=INBOUND_REPORT_TIMEOUT) as resp:
                return 200 <= resp.status < 300
        except Exception as exc:
            _log(f"inbound report failed (will retry): {_short(exc)}")
            return False

    def flush(self, timeout: float = 5.0) -> None:
        """退出前尽力投递剩余事件（不保证成功）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                pending = self._read_pending()
                if not pending:
                    return
                if all(self._deliver(e) for e in pending):
                    return
            except Exception:
                pass
            time.sleep(0.5)

    def shutdown(self) -> None:
        self._stop.set()


_reporter: _InboundReporter | None = None


def _get_reporter() -> _InboundReporter:
    global _reporter
    if _reporter is None:
        _reporter = _InboundReporter(
            INBOUND_REPORT_URL, INBOUND_REPORT_TOKEN, INBOUND_OUTBOX_FILE, INBOUND_ACK_FILE
        )
    return _reporter


_request_counter = 0


def _next_request_id() -> int:
    global _request_counter
    _request_counter += 1
    return _request_counter


def _a2a_request(method: str, params: dict, base_url: str, timeout: float | None = None) -> dict:
    """Send one A2A JSON-RPC request and return the result object.

    ``timeout`` overrides HTTP_TIMEOUT — pass task_timeout for synchronous
    message/send (which blocks until the agent finishes), keep the short
    default for polling calls.
    """
    request_id = _next_request_id()
    body = json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if A2A_TOKEN:
        headers["Authorization"] = f"Bearer {A2A_TOKEN}"
    req = urllib.request.Request(
        base_url,
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout if timeout is not None else HTTP_TIMEOUT) as resp:
            raw = resp.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = b""
        try:
            detail = exc.read(MAX_BODY_BYTES + 1)
        except Exception:
            pass
        text = detail[:500].decode("utf-8", "replace").strip() or str(exc.reason)
        raise A2AError(f"A2A HTTP {exc.code}: {text}", category="http_error") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", None)
        category = (
            "connection_timeout"
            if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
            else "connection_failed"
        )
        raise A2AError(
            f"A2A connection failed ({base_url}): {exc}",
            category=category,
        ) from exc

    if len(raw) > MAX_BODY_BYTES:
        raise A2AError("A2A response exceeds 1 MiB size limit", category="response_too_large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise A2AError(f"Invalid A2A JSON response: {exc}", category="invalid_response") from exc
    if not isinstance(payload, dict):
        raise A2AError("A2A response must be a JSON object", category="invalid_response")
    if payload.get("error"):
        err = payload["error"]
        raise A2AError(
            f"A2A RPC error {err.get('code')}: {err.get('message', 'unknown')}",
            category="rpc_error",
        )
    if payload.get("id") != request_id:
        raise A2AError("A2A response id does not match request", category="invalid_response")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise A2AError("A2A result must be an object", category="invalid_response")
    return result


def _collect_part_text(parts) -> list:
    chunks = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str):
            chunks.append(part["text"])
        root = part.get("root")
        if isinstance(root, dict) and isinstance(root.get("text"), str):
            chunks.append(root["text"])
    return chunks


def _extract_reply(task: dict) -> str:
    status = task.get("status") or {}
    message = status.get("message") or {}
    chunks = _collect_part_text(message.get("parts"))
    if not chunks:
        for artifact in task.get("artifacts") or []:
            if isinstance(artifact, dict):
                chunks.extend(_collect_part_text(artifact.get("parts")))
    reply = "\n".join(chunk for chunk in chunks if chunk).strip()
    if not reply:
        raise A2AError("Hermes task completed but contained no text reply")
    if len(reply) <= MAX_REPLY_CHARS:
        return reply
    keep = max(0, MAX_REPLY_CHARS - len(TRUNCATION_MARKER))
    return reply[:keep] + TRUNCATION_MARKER


def utc_timestamp() -> str:
    """ISO-8601 UTC 时间戳（与桥的格式一致）。"""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _norm_state(gateway_state: str) -> str:
    """Hermes gateway 原始状态 -> 监控规范状态。"""
    mapping = {
        "TASK_STATE_WORKING": "WORKING",
        "TASK_STATE_COMPLETED": "COMPLETED",
        "TASK_STATE_FAILED": "FAILED",
        "TASK_STATE_REJECTED": "FAILED",
        "TASK_STATE_CANCELED": "CANCELED",
    }
    return mapping.get(str(gateway_state).upper(), "UNKNOWN")


_default_store_instance = None


def _default_store() -> _StateStore:
    global _default_store_instance
    if _default_store_instance is None:
        _default_store_instance = _StateStore(STATE_FILE)
    return _default_store_instance


def _best_effort_cancel(
    base_url: str,
    task_id: str | None,
    store: _StateStore,
    timeout: float = CANCEL_TIMEOUT,
) -> tuple[str, bool]:
    """Try to cancel a task; return (result_description, was_removed).

    Best-effort by design: a failed cancel keeps the record in the state
    file so the next startup reconciliation can retry it.
    """
    if not task_id:
        return "no_task_id", False
    try:
        _a2a_request("tasks/cancel", {"id": task_id}, base_url, timeout=timeout)
        store.remove(task_id)
        return "sent", True
    except Exception as exc:
        return f"failed({_short(exc, 120)})", False


def _cancel_all_inflight(store: _StateStore, timeout: float = CANCEL_TIMEOUT) -> None:
    """Best-effort cancel of tasks owned by the exiting process."""
    for task_id, rec in list(store.load().items()):
        if rec.get("owner_pid") != os.getpid():
            continue
        base_url = PROFILE_PORTS.get(rec.get("profile", "default"), A2A_URL)
        try:
            _a2a_request("tasks/cancel", {"id": task_id}, base_url, timeout=timeout)
            store.remove(task_id)
        except Exception as exc:
            _log(f"exit cancel failed task={task_id}: {_short(exc)}")


def _reconcile(store: _StateStore, timeout: float = RECONCILE_TIMEOUT) -> dict:
    """Reconcile persisted in-flight records at startup.

    - WORKING tasks -> best-effort cancel (they belong to a dead process)
    - terminal states -> drop the record
    - unreachable gateway / INPUT_REQUIRED / unknown states -> keep the record
    Never raises; a failed reconciliation must not block startup.
    """
    result = {"checked": 0, "active": 0, "cancelled": 0, "cleared": 0, "kept": 0}
    for task_id, rec in list(store.load().items()):
        result["checked"] += 1
        owner_pid = rec.get("owner_pid")
        if owner_pid is not None and _process_is_alive(owner_pid, rec.get("started_at")):
            result["active"] += 1
            continue
        base_url = PROFILE_PORTS.get(rec.get("profile", "default"), A2A_URL)
        try:
            task = _a2a_request("tasks/get", {"id": task_id}, base_url, timeout=timeout)
        except Exception as exc:
            _log(f"reconcile: task {task_id} unreachable ({_short(exc, 120)}); keeping record")
            result["kept"] += 1
            continue
        state = str((task.get("status") or {}).get("state", "")).upper()
        if state in TERMINAL_STATES:
            store.remove(task_id)
            result["cleared"] += 1
        elif state == "TASK_STATE_WORKING":
            _, removed = _best_effort_cancel(base_url, task_id, store, timeout=timeout)
            result["cancelled" if removed else "kept"] += 1
        else:
            _log(f"reconcile: task {task_id} in non-terminal state {state}; keeping record")
            result["kept"] += 1
    return result


def _start_reconcile_thread(store: _StateStore) -> threading.Thread:
    """Reconcile stale tasks without delaying the MCP initialize handshake."""

    def _worker() -> None:
        try:
            result = _reconcile(store)
            _log(f"startup reconcile: {result}")
        except Exception as exc:
            _log(f"startup reconcile failed (continuing): {_short(exc)}")

    thread = threading.Thread(
        target=_worker,
        name="hermes-mcp-reconcile",
        daemon=True,
    )
    thread.start()
    return thread


def _install_signal_handlers(store: _StateStore) -> None:
    """Best-effort in-flight cancel on SIGINT/SIGTERM/SIGBREAK.

    On Windows, SIGTERM delivered via TerminateProcess never runs Python
    handlers — the state file plus next-start reconciliation is the
    recovery backstop.
    """

    def _handler(signum, _frame):
        try:
            _log(f"signal {signum}: best-effort cancelling in-flight tasks")
            _cancel_all_inflight(store)
        finally:
            os._exit(128 + signum)

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass  # not on the main thread / unsupported


def call_hermes(
    message: str,
    base_url: str | None = None,
    profile: str = "default",
    context_id: str | None = None,
    task_timeout: float = TASK_TIMEOUT,
    poll_interval: float = POLL_INTERVAL,
    state_store: _StateStore | None = None,
) -> str:
    """Send ``message`` to Hermes, poll until completion, return reply text.

    ``profile`` selects the Hermes profile: ``default`` (9900) or
    ``web-dev`` (9901). ``base_url`` overrides profile routing entirely.
    ``context_id`` reuses a conversation (continue with prior context);
    omit for a fresh one-shot task. ``state_store`` overrides the default
    in-flight state file (used by tests and embedded callers).

    In-flight tasks are recorded so a timeout, stdin EOF, or process death
    can best-effort cancel them (or reconcile on the next startup).
    """
    if not isinstance(message, str) or not message.strip():
        raise A2AError("message must be a non-empty string", code=-32602, category="invalid_input")
    if len(message) > MAX_MESSAGE_CHARS:
        raise A2AError(f"message exceeds {MAX_MESSAGE_CHARS} chars", code=-32602, category="invalid_input")
    if context_id is not None:
        if not isinstance(context_id, str) or not context_id.strip():
            raise A2AError("context_id must be a non-empty string", code=-32602, category="invalid_input")
        if len(context_id) > MAX_CONTEXT_ID_CHARS:
            raise A2AError(
                f"context_id exceeds {MAX_CONTEXT_ID_CHARS} chars",
                code=-32602,
                category="invalid_input",
            )
        if not CONTEXT_ID_PATTERN.match(context_id):
            raise A2AError(
                "context_id may only contain letters, digits, '.', '_', '-'",
                code=-32602,
                category="invalid_input",
            )
    if task_timeout <= 0:
        raise A2AError("task_timeout must be positive", code=-32602, category="invalid_input")

    if base_url is None:
        base_url = PROFILE_PORTS.get(profile)
        if base_url is None:
            raise A2AError(
                f"unknown profile '{profile}' (expected: {', '.join(PROFILE_PORTS)})",
                code=-32602,
                category="invalid_input",
            )

    store = state_store if state_store is not None else _default_store()
    context_id = context_id or f"ctx-{uuid.uuid4().hex}"
    operation_id = f"op-{uuid.uuid4().hex}"
    reporter = _get_reporter()

    def _emit(phase: str, **extra) -> None:
        """发一个 inbound 事件到 outbox（尽力而为，绝不抛出）。"""
        try:
            event = {
                "schema_version": 1,
                "event_id": f"evt-{uuid.uuid4().hex[:16]}",
                "operation_id": operation_id,
                "phase": phase,
                "direction": "inbound",
                "source": "hermes_mcp",
                "profile": profile,
                "context_id": context_id,
                "observed_at": utc_timestamp(),
            }
            event.update(extra)
            reporter.append(event)
        except Exception:
            pass

    # STARTED 必须在 message/send 之前落 outbox（崩溃也能补投）
    _emit("started", message_summary=message[:INBOUND_SUMMARY_CHARS])
    send_params = {
        "message": {
            "role": "user",
            "parts": [{"text": message, "mediaType": "text/plain"}],
            "contextId": context_id,
        }
    }
    deadline = time.monotonic() + task_timeout
    try:
        task = _a2a_request("message/send", send_params, base_url, timeout=task_timeout)
    except A2AError as exc:
        if exc.category == "connection_timeout":
            raise A2AError(
                "Hermes message/send timed out before a task id was returned; "
                "task acceptance is unknown and cancellation is not possible",
                category="submission_unknown",
            ) from exc
        raise
    task_id = task.get("id")
    state = str((task.get("status") or {}).get("state", "")).upper()

    if task_id:
        _emit(
            "accepted",
            gateway_task_id=task_id,
            gateway_state=state,
            state=_norm_state(state),
        )
    else:
        # 无 task id：提交结果未知，不能假报 FAILED
        _emit("state", state="UNKNOWN", error_category="submission_unknown")
    if task_id and state not in TERMINAL_STATES:
        store.record(task_id, profile, context_id)

    poll_interval = max(0.05, poll_interval)
    while state == "TASK_STATE_WORKING":
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            cancel_result, _ = _best_effort_cancel(base_url, task_id, store)
            _emit(
                "finished",
                gateway_task_id=task_id,
                state="UNKNOWN" if not cancel_result else "CANCELED",
                error_category="timeout",
            )
            raise A2AError(
                f"Hermes task {task_id} did not finish within {task_timeout:.0f}s; cancel: {cancel_result}",
                category="timeout",
                task_id=task_id,
            )
        time.sleep(min(poll_interval, remaining))
        if not task_id:
            raise A2AError("Hermes task is WORKING but has no task id", category="unknown_state")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue
        try:
            task = _a2a_request(
                "tasks/get",
                {"id": task_id},
                base_url,
                timeout=min(HTTP_TIMEOUT, remaining),
            )
        except A2AError as exc:
            exc.task_id = task_id
            raise
        state = str((task.get("status") or {}).get("state", "")).upper()
        _emit(
            "state",
            gateway_task_id=task_id,
            gateway_state=state,
            state=_norm_state(state),
        )

    if state == "TASK_STATE_COMPLETED":
        if task_id:
            store.remove(task_id)
        try:
            reply = _extract_reply(task)
            _emit(
                "finished",
                gateway_task_id=task_id,
                gateway_state=state,
                state="COMPLETED",
                reply_summary=reply[:INBOUND_SUMMARY_CHARS],
            )
            return reply
        except A2AError as exc:
            exc.category = "empty_reply"
            exc.task_id = task_id
            raise
    if state in TERMINAL_FAILED_STATES:
        if task_id:
            store.remove(task_id)
        _emit(
            "finished",
            gateway_task_id=task_id,
            gateway_state=state,
            state=_norm_state(state),
            error_category="task_failed",
        )
        raise A2AError(
            f"Hermes task {task_id} ended in state {state}",
            category="task_failed",
            task_id=task_id,
        )
    raise A2AError(
        f"Unknown Hermes task state: {state or '(empty)'}",
        category="unknown_state",
        task_id=task_id,
    )


class _FrameError(ValueError):
    """Malformed input frame, retaining the framing used by that message."""

    def __init__(self, message: str, framing: str = "content-length"):
        super().__init__(message)
        self.framing = framing


def _looks_like_content_length_header(line: bytes) -> bool:
    """Return whether the first line identifies a legacy header-framed message."""
    name, separator, _ = line.partition(b":")
    return bool(separator and name.strip().lower() in _CONTENT_LENGTH_HEADER_NAMES)


def _read_message_with_framing(stream) -> tuple[object, str] | None:
    """Read one JSON message and return ``(message, framing)``.

    The first line distinguishes legacy ``Content-Length`` headers from
    newline-delimited JSON. ``None`` indicates clean EOF (or EOF mid-body).
    ``_FrameError`` carries the detected framing so parse errors can be
    returned using the same envelope.
    """
    first_line = stream.readline()
    if not first_line:
        return None

    if _looks_like_content_length_header(first_line):
        framing = "content-length"
        headers = {}
        header_bytes = 0
        line = first_line
        while True:
            header_bytes += len(line)
            if header_bytes > MAX_HEADER_BYTES:
                raise _FrameError("headers exceed size limit", framing)
            if line in (b"\r\n", b"\n"):
                break
            name, separator, value = line.partition(b":")
            if not separator or not name.strip():
                raise _FrameError("invalid header line", framing)
            headers[name.strip().lower()] = value.strip()
            line = stream.readline()
            if not line:
                return None  # EOF while reading a header block

        if b"content-length" not in headers:
            raise _FrameError("missing Content-Length header", framing)
        try:
            length = int(headers[b"content-length"])
        except (TypeError, ValueError) as exc:
            raise _FrameError(f"invalid Content-Length header: {exc}", framing) from exc
        if length <= 0 or length > MAX_FRAME_BYTES:
            raise _FrameError(f"invalid Content-Length value: {length}", framing)
        raw = stream.read(length)
        if len(raw) != length:
            return None  # EOF mid-frame
    else:
        framing = "newline"
        raw = first_line.rstrip(b"\r\n")
        if len(raw) > MAX_FRAME_BYTES:
            raise _FrameError("newline JSON frame exceeds size limit", framing)

    try:
        return json.loads(raw.decode("utf-8")), framing
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _FrameError(f"invalid JSON frame: {exc}", framing) from exc


def _read_frame(stream, *, return_framing: bool = False):
    """Read one JSON frame; preserve the old payload-only default API.

    Pass ``return_framing=True`` for ``(payload, "content-length"|"newline")``.
    """
    frame = _read_message_with_framing(stream)
    if frame is None or return_framing:
        return frame
    return frame[0]


def _write_frame(message, framing: str = "content-length", stream=None) -> None:
    """Write one response using the request's framing."""
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if stream is None:
        stream = getattr(sys.stdout, "buffer", sys.stdout)
    if framing == "newline":
        stream.write(body + b"\n")
    elif framing == "content-length":
        stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    else:
        raise ValueError(f"unknown framing: {framing}")
    stream.flush()


def _error_response(request_id, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _tool_error_result(request_id, text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }


def _handle_request(request: dict) -> dict | None:
    request_id = request.get("id")
    method = request.get("method")
    if not isinstance(method, str):
        return _error_response(request_id, -32600, "method must be a string")

    # Any request without an id is a JSON-RPC notification and must not get a
    # response. Keep the explicit MCP notification prefix behavior as well.
    if "id" not in request or method.startswith("notifications/"):
        return None

    params = request.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": requested if isinstance(requested, str) else PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": [TOOL_DEFINITION]},
        }

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        if name != "call_hermes":
            return _error_response(request_id, -32602, f"unknown tool: {name}")
        message = arguments.get("message")
        if not isinstance(message, str) or not message.strip():
            return _error_response(
                request_id, -32602, "argument 'message' must be a non-empty string"
            )
        profile = arguments.get("profile") or "default"
        context_id = arguments.get("context_id")
        try:
            reply = call_hermes(message, profile=profile, context_id=context_id)
        except A2AError as exc:
            _log(
                f"call_hermes error category={exc.category} code={exc.code} "
                f"task_id={exc.task_id} summary={_short(str(exc))!r}"
            )
            return _tool_error_result(request_id, str(exc)[:MAX_ERROR_TEXT_CHARS])
        except Exception as exc:  # keep the server alive on unexpected failures
            _log(f"call_hermes unexpected failure category=internal summary={_short(str(exc))!r}")
            return _tool_error_result(request_id, f"call_hermes failed: {_short(str(exc), MAX_ERROR_TEXT_CHARS)}")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": reply}]},
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}

    return _error_response(request_id, -32601, f"method not found: {method}")


def _handle_batch(requests: list) -> list | dict | None:
    """Handle a JSON-RPC batch as one response envelope.

    Notifications are omitted from the response array. A batch containing only
    notifications produces no response, while an empty batch is invalid.
    """
    if not requests:
        return _error_response(None, -32600, "empty batch")

    responses = []
    for item in requests:
        if not isinstance(item, dict):
            responses.append(
                _error_response(None, -32600, "request must be a JSON object")
            )
            continue
        try:
            response = _handle_request(item)
        except Exception as exc:
            _log(f"handler error: {exc!r}")
            response = _error_response(item.get("id"), -32603, f"internal error: {exc}")
        if response is not None:
            responses.append(response)
    return responses or None


def main() -> None:
    store = _default_store()
    _install_signal_handlers(store)
    _start_reconcile_thread(store)
    reporter = _get_reporter()
    _log(
        f"{SERVER_NAME} v{SERVER_VERSION} started; A2A endpoint {A2A_URL}; "
        f"state {STATE_FILE}; inbound report {'ON' if INBOUND_REPORT_TOKEN else 'OFF'}"
    )
    stream = sys.stdin.buffer
    try:
        while True:
            framing = "content-length"
            try:
                frame = _read_frame(stream, return_framing=True)
            except _FrameError as exc:
                framing = exc.framing
                _log(f"dropping malformed frame: {exc}")
                _write_frame(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": f"parse error: {exc}"},
                    },
                    framing,
                )
                continue
            if frame is None:
                break
            request, framing = frame

            if isinstance(request, list):
                response = _handle_batch(request)
                if response is not None:
                    _write_frame(response, framing)
                continue
            if not isinstance(request, dict):
                _write_frame(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "request must be a JSON object"},
                    },
                    framing,
                )
                continue

            try:
                response = _handle_request(request)
            except Exception as exc:
                _log(f"handler error: {exc!r}")
                response = _error_response(request.get("id"), -32603, f"internal error: {exc}")
            if response is not None:
                _write_frame(response, framing)
    finally:
        # EOF or unexpected exit: best-effort cancel recorded tasks. The
        # state file remains the backstop if we die without reaching here.
        try:
            _cancel_all_inflight(_default_store())
        except Exception as exc:
            _log(f"exit cancel sweep failed: {_short(exc)}")
        # 尽力补投剩余 inbound 事件
        try:
            reporter.flush(timeout=3.0)
            reporter.shutdown()
        except Exception as exc:
            _log(f"exit inbound flush failed: {_short(exc)}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
