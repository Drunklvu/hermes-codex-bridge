"""Local A2A JSON-RPC bridge from Hermes to Codex CLI.

The bridge intentionally binds to loopback by default because Codex runs with
danger-full-access. It supports Hermes' v1-style PascalCase methods and the
legacy path-style aliases observed in older A2A clients.
"""

from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9998
# Workspace defaults to HERMES_A2A_WORKSPACE, then the current working
# directory. Pass --workspace to override explicitly.
DEFAULT_WORKSPACE = Path(
    os.environ.get("HERMES_A2A_WORKSPACE") or Path.cwd()
)
DEFAULT_SYNC_WAIT = 540
DEFAULT_CODEX_TIMEOUT = 1800
MAX_BODY_BYTES = 1_048_576
MAX_RESULT_CHARS = 1_000_000
MAX_STDERR_CHARS = 120_000
STDOUT_TAIL_LINES = 200
HEARTBEAT_INTERVAL_SECONDS = 5.0
MAX_QUERY_RESULTS = 200
TERMINAL_STATES = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
}

# A2A v1 JSON-RPC error code for unsupported content types.
CONTENT_TYPE_NOT_SUPPORTED = -32005

_WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'`\u4e00-\u9fff]*")
_UNIX_PATH_RE = re.compile(r"(?:/[\w.+-]+){3,}")
_ALLOWED_CARD_HOST_RE = re.compile(
    r"^(?:(?:127\.0\.0\.1|localhost|\[::1\])(?::\d{1,5})?)$",
    re.IGNORECASE,
)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def text_part(text: str) -> dict[str, Any]:
    return {"text": text, "mediaType": "text/plain"}


def agent_message(text: str, context_id: str) -> dict[str, Any]:
    return {
        "messageId": f"msg-{uuid.uuid4().hex}",
        "contextId": context_id,
        "role": "ROLE_AGENT",
        "parts": [text_part(text)],
    }


def extract_message_text(params: dict[str, Any]) -> str:
    message = params.get("message") or {}
    parts = message.get("parts") or []
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        value = part.get("text")
        if isinstance(value, str):
            chunks.append(value)
            continue
        nested = part.get("root")
        if isinstance(nested, dict) and isinstance(nested.get("text"), str):
            chunks.append(nested["text"])
    return "\n".join(chunk for chunk in chunks if chunk).strip()


def resolve_context_id(params: dict[str, Any]) -> str:
    message = params.get("message") or {}
    return str(
        message.get("contextId")
        or message.get("context_id")
        or params.get("contextId")
        or params.get("context_id")
        or f"ctx-{uuid.uuid4().hex}"
    )


def wants_immediate_return(params: dict[str, Any]) -> bool:
    """Return immediately only when the caller explicitly opted in."""
    config = params.get("configuration") or params.get("config") or {}
    return bool(config.get("returnImmediately") or config.get("return_immediately"))


class ContentTypeNotSupportedError(Exception):
    """Raised when a message contains non-text parts we cannot handle."""


def ensure_text_only_message(params: dict[str, Any]) -> None:
    """Reject any non-text part with the A2A ContentTypeNotSupported error."""
    message = params.get("message") or {}
    parts = message.get("parts") or []
    for part in parts:
        if not isinstance(part, dict):
            continue
        media = str(part.get("mediaType") or "text/plain")
        if not media.startswith("text/"):
            raise ContentTypeNotSupportedError(
                f"Unsupported part mediaType '{media}'; only text parts are supported"
            )
        has_text = (
            isinstance(part.get("text"), str)
            and bool(part["text"].strip())
        ) or (
            isinstance(part.get("root"), dict)
            and isinstance(part["root"].get("text"), str)
            and bool(part["root"]["text"].strip())
        )
        if not has_text:
            raise ContentTypeNotSupportedError("text part without text content")


def sanitize_error(message: str) -> str:
    """Strip local filesystem paths from user-visible error messages."""
    if not message:
        return message
    text = _WINDOWS_PATH_RE.sub("[path]", message)
    text = _UNIX_PATH_RE.sub("[path]", text)
    return text


def find_codex_executable(explicit: str | None = None) -> str:
    candidates = [
        explicit,
        os.environ.get("CODEX_CLI_PATH"),
        shutil.which("codex.exe"),
        shutil.which("codex.cmd"),
        shutil.which("codex"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate).resolve())
    raise FileNotFoundError("Codex CLI not found; set CODEX_CLI_PATH or pass --codex")


def parse_codex_jsonl(output: str) -> tuple[str | None, str]:
    """Extract the session id and final agent text from Codex JSONL output."""
    session_id: str | None = None
    final_message = ""
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            session_id = event["thread_id"]
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            final_message = item["text"]
    return session_id, final_message


def _iso_to_epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class SessionStore:
    def __init__(self, state_file: Path) -> None:
        self.state_file = state_file
        self.lock = threading.RLock()
        self.sessions: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
            for entry in payload.get("sessions", []):
                context_id = entry.get("context_id")
                session_id = entry.get("session_id")
                if isinstance(context_id, str) and isinstance(session_id, str):
                    self.sessions[context_id] = session_id
        except Exception:
            logging.exception("Could not load persisted Codex sessions")

    def _persist_locked(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessions": [
                {"context_id": context_id, "session_id": session_id}
                for context_id, session_id in self.sessions.items()
            ]
        }
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def get(self, context_id: str) -> str | None:
        with self.lock:
            return self.sessions.get(context_id)

    def set(self, context_id: str, session_id: str) -> None:
        with self.lock:
            self.sessions[context_id] = session_id
            self._persist_locked()


class TaskStore:
    """Task metadata store.

    Large result text is kept in ``state_dir/results/<task_id>.txt`` while
    ``tasks.json`` holds only metadata. ``get()`` materializes the result on
    demand instead of deep-copying the whole in-memory payload.
    """

    def __init__(self, state_file: Path, max_tasks: int = 100) -> None:
        self.state_file = state_file
        self.results_dir = state_file.parent / "results"
        self.max_tasks = max_tasks
        self.lock = threading.RLock()
        self.tasks: dict[str, dict[str, Any]] = {}
        self.events: dict[str, threading.Event] = {}
        self.processes: dict[str, subprocess.Popen[str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
            for task in payload.get("tasks", []):
                task_id = task.get("id")
                if not task_id:
                    continue
                state = (task.get("status") or {}).get("state")
                if state not in TERMINAL_STATES:
                    # Bridge restarted while this task was WORKING: it can never
                    # resume in-process, so mark it FAILED and ask for reconciliation.
                    context_id = task.get("contextId") or f"ctx-{uuid.uuid4().hex}"
                    task["status"] = {
                        "state": "TASK_STATE_FAILED",
                        "timestamp": utc_timestamp(),
                        "message": agent_message(
                            "桥服务重启，未完成的 Codex 任务已中断。可凭 session_id 在 Codex CLI 中续跑，"
                            "并请与实际产出对账（reconcile）。",
                            context_id,
                        ),
                    }
                    task["finished_at"] = utc_timestamp()
                self._normalize_locked(task)
                self.tasks[task_id] = task
                event = threading.Event()
                event.set()
                self.events[task_id] = event
        except Exception:
            logging.exception("Could not load persisted A2A tasks")

    def _persist_locked(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        ordered = list(self.tasks.values())[-self.max_tasks :]
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"tasks": ordered}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    @staticmethod
    def _extract_result_text(task: dict[str, Any]) -> str:
        status = task.get("status") or {}
        message = status.get("message") or {}
        for part in message.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
                return part["text"]
        for artifact in task.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            for part in artifact.get("parts") or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
                    return part["text"]
        return ""

    @staticmethod
    def _strip_result_text(task: dict[str, Any]) -> None:
        status = task.get("status") or {}
        message = status.get("message") or {}
        for part in message.get("parts") or []:
            if isinstance(part, dict):
                part["text"] = ""
        for artifact in task.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            for part in artifact.get("parts") or []:
                if isinstance(part, dict):
                    part["text"] = ""

    def _normalize_locked(self, task: dict[str, Any]) -> dict[str, Any]:
        """Move completed result text out of tasks.json into results/<id>.txt."""
        if (task.get("status") or {}).get("state") == "TASK_STATE_COMPLETED":
            text = self._extract_result_text(task)
            if text:
                self._write_result(task["id"], text)
                self._strip_result_text(task)
                task["_resultExternal"] = True
        return task

    def _write_result(self, task_id: str, text: str) -> None:
        try:
            self.results_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.results_dir / f".{task_id}.tmp"
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.results_dir / f"{task_id}.txt")
        except OSError:
            logging.exception("Could not write result file for %s", task_id)

    def _read_result(self, task_id: str) -> str | None:
        path = self.results_dir / f"{task_id}.txt"
        try:
            if path.exists():
                return path.read_text(encoding="utf-8")
        except OSError:
            logging.exception("Could not read result file %s", path)
        return None

    def add(self, task: dict[str, Any]) -> threading.Event:
        with self.lock:
            task_id = task["id"]
            self._normalize_locked(task)
            self.tasks[task_id] = task
            event = threading.Event()
            self.events[task_id] = event
            while len(self.tasks) > self.max_tasks:
                oldest = next(iter(self.tasks))
                self.tasks.pop(oldest, None)
                self.events.pop(oldest, None)
            self._persist_locked()
            return event

    def update(
        self,
        task_id: str,
        task: dict[str, Any],
        terminal: bool = False,
        if_not_state: str | None = None,
    ) -> bool:
        """Store a task update.

        ``if_not_state`` guards a race: if the stored task is already in that
        state (e.g. CANCELED), the update is rejected and False is returned.
        """
        with self.lock:
            current = self.tasks.get(task_id)
            if current is None:
                return False
            if if_not_state and (current.get("status") or {}).get("state") == if_not_state:
                return False
            self._normalize_locked(task)
            if terminal:
                task.setdefault("finished_at", utc_timestamp())
            self.tasks[task_id] = task
            self._persist_locked()
            if terminal:
                self.events.setdefault(task_id, threading.Event()).set()
            return True

    def _materialize_locked(self, task: dict[str, Any]) -> dict[str, Any]:
        """Shallow-reconstruct a task, injecting result text from disk."""
        result = dict(task)
        result.pop("_resultExternal", None)
        status = dict(result.get("status") or {})
        result["status"] = status
        message = status.get("message")
        if isinstance(message, dict):
            message = dict(message)
            message["parts"] = [dict(p) for p in message.get("parts") or [] if isinstance(p, dict)]
            status["message"] = message
        artifacts = result.get("artifacts")
        if isinstance(artifacts, list):
            result["artifacts"] = [dict(a) for a in artifacts if isinstance(a, dict)]
        if task.get("_resultExternal"):
            text = self._read_result(task["id"])
            if text is not None:
                parts = message.get("parts") if isinstance(message, dict) else []
                for part in parts:
                    if isinstance(part, dict):
                        part["text"] = text
                for artifact in result.get("artifacts") or []:
                    if not isinstance(artifact, dict):
                        continue
                    for part in artifact.get("parts") or []:
                        if isinstance(part, dict):
                            part["text"] = text
        return result

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return None
            return self._materialize_locked(task)

    def wait(self, task_id: str, timeout: float) -> bool:
        with self.lock:
            event = self.events.get(task_id)
        return bool(event and event.wait(timeout))

    def attach_process(self, task_id: str, process: subprocess.Popen[str]) -> None:
        with self.lock:
            self.processes[task_id] = process

    def detach_process(self, task_id: str) -> None:
        with self.lock:
            self.processes.pop(task_id, None)

    def is_canceled(self, task_id: str) -> bool:
        with self.lock:
            task = self.tasks.get(task_id)
            return bool(task and (task.get("status") or {}).get("state") == "TASK_STATE_CANCELED")

    def active_count(self) -> int:
        with self.lock:
            return sum(
                1
                for task in self.tasks.values()
                if (task.get("status") or {}).get("state") == "TASK_STATE_WORKING"
            )

    def update_heartbeat(self, task_id: str) -> bool:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task or (task.get("status") or {}).get("state") in TERMINAL_STATES:
                return False
            task["last_heartbeat"] = utc_timestamp()
            self._persist_locked()
            return True

    def query(self, states: list[str] | None = None) -> list[dict[str, Any]]:
        allowed: set[str] | None = None
        if states:
            allowed = {s for s in states if isinstance(s, str)}
        with self.lock:
            ordered = []
            for task in self.tasks.values():
                state = (task.get("status") or {}).get("state")
                if allowed and state not in allowed:
                    continue
                ordered.append(self._materialize_locked(task))
            ordered.sort(key=lambda t: (t.get("created_at") or ""), reverse=True)
            return ordered[:MAX_QUERY_RESULTS]

    def cancel(self, task_id: str) -> dict[str, Any] | None:
        """Cancel a task atomically with its process termination."""
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return None
            state = (task.get("status") or {}).get("state")
            if state in TERMINAL_STATES:
                return self._materialize_locked(task)
            context_id = task["contextId"]
            task["status"] = {
                "state": "TASK_STATE_CANCELED",
                "timestamp": utc_timestamp(),
                "message": agent_message("Codex 任务已取消。", context_id),
            }
            task["finished_at"] = utc_timestamp()
            self._persist_locked()
            self.events.setdefault(task_id, threading.Event()).set()
            # Kill the process tree while holding the store lock so a concurrent
            # completion update cannot race past the CANCELED state.
            process = self.processes.get(task_id)
            if process is not None:
                terminate_process_tree(process)
            return self._materialize_locked(task)


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        process.terminate()


class CodexRunResult:
    __slots__ = ("returncode", "session_id", "output", "stderr_tail", "stdout_tail", "timed_out")

    def __init__(
        self,
        returncode: int,
        session_id: str | None,
        output: str,
        stderr_tail: str,
        stdout_tail: str,
        timed_out: bool = False,
    ) -> None:
        self.returncode = returncode
        self.session_id = session_id
        self.output = output
        self.stderr_tail = stderr_tail
        self.stdout_tail = stdout_tail
        self.timed_out = timed_out


class CodexBridge:
    def __init__(
        self,
        *,
        codex: str | None,
        workspace: Path,
        state_dir: Path,
        model: str,
        sync_wait: int,
        codex_timeout: int,
        max_concurrent: int,
        token: str | None = None,
    ) -> None:
        # ``codex`` is a hint (explicit path or None). It is re-resolved with
        # find_codex_executable before every spawn so a stale path self-heals.
        self.codex_hint = codex
        self.workspace = workspace.resolve()
        self.state_dir = state_dir.resolve()
        self.model = model
        self.sync_wait = sync_wait
        self.codex_timeout = codex_timeout
        self.max_concurrent = max_concurrent
        self.token = token
        self.started_at = time.time()
        self.semaphore = threading.BoundedSemaphore(max_concurrent)
        self.store = TaskStore(self.state_dir / "tasks.json")
        self.sessions = SessionStore(self.state_dir / "sessions.json")

    def _resolve_codex(self) -> str:
        try:
            return find_codex_executable(self.codex_hint)
        except FileNotFoundError:
            time.sleep(1)
            return find_codex_executable(self.codex_hint)

    def _heartbeat_loop(self, task_id: str, stop_event: threading.Event) -> None:
        while not stop_event.wait(HEARTBEAT_INTERVAL_SECONDS):
            try:
                if not self.store.update_heartbeat(task_id):
                    return
            except Exception:
                logging.exception("Heartbeat update failed for %s", task_id)

    @staticmethod
    def _bridge_prompt(prompt: str) -> str:
        return (
            "You are Codex receiving a task from Hermes over a local A2A bridge. "
            "Complete the task in the configured workspace, use tools as needed, and return a concise "
            "result that states what changed and what was verified. Do not expose chain-of-thought. "
            "Do not call call_hermes or any A2A/MCP tool that invokes Hermes or this bridge back; "
            "不得通过 call_hermes 或任何 A2A/MCP 工具反向调用 Hermes。\n\n"
            f"Hermes task:\n{prompt}"
        )

    def _new_session_command(self, codex: str, result_file: Path) -> list[str]:
        command = [
            codex,
            "exec",
            "--sandbox",
            "danger-full-access",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--json",
        ]
        if self.model:
            command += ["--model", self.model]
        command += ["--cd", str(self.workspace), "--output-last-message", str(result_file), "-"]
        return command

    def _resume_command(self, codex: str, session_id: str, result_file: Path) -> list[str]:
        command = [codex, "exec", "resume", "--skip-git-repo-check", "--json"]
        if self.model:
            command += ["--model", self.model]
        command += ["--output-last-message", str(result_file), session_id, "-"]
        return command

    def _exec_codex(
        self,
        command: list[str],
        prompt: str,
        task_id: str,
        context_id: str,
        heartbeat_stop: threading.Event,
    ) -> CodexRunResult:
        """Spawn Codex, stream stdout line by line, and persist the thread id
        as soon as ``thread.started`` is seen (instead of buffering all output)."""
        result_file = self.store.results_dir / f"{task_id}.txt"
        self.store.results_dir.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=self.workspace,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.store.attach_process(task_id, process)
        started_session_id: list[str | None] = [None]
        final_message: list[str] = [""]
        stdout_tail: deque[str] = deque(maxlen=STDOUT_TAIL_LINES)
        stderr_tail: list[str] = []
        stderr_chars = [0]
        state_lock = threading.Lock()

        def drain_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                stripped = line.strip()
                if not stripped:
                    continue
                with state_lock:
                    stdout_tail.append(line)
                try:
                    event = json.loads(stripped)
                except (json.JSONDecodeError, TypeError):
                    continue
                if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                    if started_session_id[0] is None:
                        started_session_id[0] = event["thread_id"]
                        try:
                            self.sessions.set(context_id, event["thread_id"])
                        except Exception:
                            logging.exception("Could not persist session id early for %s", context_id)
                item = event.get("item")
                if (
                    event.get("type") == "item.completed"
                    and isinstance(item, dict)
                    and item.get("type") == "agent_message"
                    and isinstance(item.get("text"), str)
                ):
                    with state_lock:
                        final_message[0] = item["text"]

        def drain_stderr() -> None:
            assert process.stderr is not None
            for chunk in process.stderr:
                with state_lock:
                    if stderr_chars[0] < MAX_STDERR_CHARS:
                        stderr_tail.append(chunk)
                        stderr_chars[0] += len(chunk)

        stdout_thread = threading.Thread(
            target=drain_stdout, daemon=True, name=f"stdout-{task_id[-8:]}"
        )
        stderr_thread = threading.Thread(
            target=drain_stderr, daemon=True, name=f"stderr-{task_id[-8:]}"
        )
        stdout_thread.start()
        stderr_thread.start()

        timed_out = False
        try:
            assert process.stdin is not None
            try:
                process.stdin.write(prompt)
                process.stdin.write("\n")
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                process.wait(timeout=self.codex_timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_process_tree(process)
                process.wait()
        finally:
            stdout_thread.join(timeout=10)
            stderr_thread.join(timeout=10)
            self.store.detach_process(task_id)

        stdout_text = "".join(stdout_tail)
        stderr_text = "".join(stderr_tail)[-MAX_STDERR_CHARS:]
        output = ""
        try:
            if result_file.exists():
                output = result_file.read_text(encoding="utf-8").strip()
        except OSError:
            logging.exception("Could not read result file %s", result_file)
        if not output:
            output = (final_message[0] or stdout_text).strip()
        return CodexRunResult(
            returncode=process.returncode,
            session_id=started_session_id[0],
            output=output,
            stderr_tail=stderr_text,
            stdout_tail=stdout_text,
            timed_out=timed_out,
        )

    def card(self, base_url: str) -> dict[str, Any]:
        return {
            "name": "Codex CLI",
            "description": "Local coding agent exposed to Hermes through an A2A-to-Codex bridge.",
            "version": "1.0.0",
            "protocolVersion": "1.0",
            "url": base_url,
            "provider": {"organization": "Local Codex", "url": base_url},
            "supportedInterfaces": [
                {"url": base_url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
            ],
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
                "stateTransitionHistory": False,
                "extendedAgentCard": False,
            },
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [
                {
                    "id": "coding",
                    "name": "coding",
                    "description": "Plan, implement, debug, review, test, and verify coding tasks.",
                    "tags": ["coding", "debugging", "review", "testing"],
                }
            ],
        }

    def start_task(self, prompt: str, context_id: str) -> str:
        task_id = f"task-{uuid.uuid4().hex}"
        with self.store.lock:
            if self.store.active_count() >= self.max_concurrent:
                rejected = {
                    "id": task_id,
                    "contextId": context_id,
                    "status": {
                        "state": "TASK_STATE_REJECTED",
                        "timestamp": utc_timestamp(),
                        "message": agent_message(
                            f"Codex 任务队列已满（同时最多 {self.max_concurrent} 个任务），已拒绝本次请求。"
                            "请等待现有任务完成或结束后重试。",
                            context_id,
                        ),
                    },
                    "created_at": utc_timestamp(),
                    "finished_at": utc_timestamp(),
                }
                self.store.add(rejected)
                return task_id
            task = {
                "id": task_id,
                "contextId": context_id,
                "status": {
                    "state": "TASK_STATE_WORKING",
                    "timestamp": utc_timestamp(),
                    "message": agent_message("Codex 正在执行任务。", context_id),
                },
                "created_at": utc_timestamp(),
            }
            session_id = self.sessions.get(context_id)
            if session_id:
                task["session_id"] = session_id
            self.store.add(task)
        thread = threading.Thread(
            target=self._run_task,
            args=(task_id, prompt),
            name=f"codex-{task_id[-8:]}",
            daemon=True,
        )
        thread.start()
        return task_id

    def _run_task(self, task_id: str, prompt: str) -> None:
        with self.semaphore:
            current = self.store.get(task_id)
            if not current or current["status"]["state"] == "TASK_STATE_CANCELED":
                return
            context_id = current["contextId"]
            session_id = self.sessions.get(context_id)
            resumed_from_new = False
            heartbeat_stop = threading.Event()
            heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                args=(task_id, heartbeat_stop),
                name=f"hb-{task_id[-8:]}",
                daemon=True,
            )
            heartbeat_thread.start()
            try:
                codex = self._resolve_codex()
                result: CodexRunResult | None = None
                attempts = 0
                while True:
                    attempts += 1
                    if self.store.is_canceled(task_id):
                        return
                    use_resume = bool(session_id)
                    if use_resume:
                        command = self._resume_command(
                            codex, session_id, self.store.results_dir / f"{task_id}.txt"
                        )
                        logging.info("Resuming Codex session %s for %s", session_id, task_id)
                    else:
                        command = self._new_session_command(
                            codex, self.store.results_dir / f"{task_id}.txt"
                        )
                        logging.info("Starting new Codex session for %s in %s", task_id, self.workspace)
                    result = self._exec_codex(
                        command,
                        self._bridge_prompt(prompt),
                        task_id,
                        context_id,
                        heartbeat_stop,
                    )
                    if self.store.is_canceled(task_id):
                        return
                    if result.returncode == 0:
                        # codex exec resume can exit 0 while silently starting a
                        # different thread (e.g. the requested session no longer
                        # exists). Treat that as a failed resume and fall back.
                        if (
                            use_resume
                            and result.session_id
                            and result.session_id != session_id
                        ):
                            logging.warning(
                                "Resume did not continue session %s (new thread %s); "
                                "retrying with a fresh session",
                                session_id,
                                result.session_id,
                            )
                            session_id = None
                            resumed_from_new = True
                            continue
                        break
                    # Self-heal: a failed resume falls back to a fresh session once.
                    if use_resume and attempts < 2:
                        logging.warning(
                            "Resume failed for %s (rc=%s); retrying with a fresh session",
                            session_id,
                            result.returncode,
                        )
                        session_id = None
                        resumed_from_new = True
                        continue
                    detail = result.stderr_tail or result.stdout_tail or f"exit code {result.returncode}"
                    raise RuntimeError(detail[-12000:])

                if result is None:
                    raise RuntimeError("Codex did not run")
                if result.session_id:
                    self.sessions.set(context_id, result.session_id)
                    if not session_id:
                        session_id = result.session_id
                output = result.output or "Codex 已完成任务，但没有返回文本结果。"
                output = output[:MAX_RESULT_CHARS]
                completed = {
                    "id": task_id,
                    "contextId": context_id,
                    "session_id": session_id,
                    "status": {
                        "state": "TASK_STATE_COMPLETED",
                        "timestamp": utc_timestamp(),
                        "message": agent_message(output, context_id),
                    },
                    "artifacts": [
                        {
                            "artifactId": f"artifact-{uuid.uuid4().hex}",
                            "name": "Codex result",
                            "parts": [text_part(output)],
                        }
                    ],
                }
                if resumed_from_new:
                    completed["resumed_from_new"] = True
                if not self.store.update(
                    task_id, completed, terminal=True, if_not_state="TASK_STATE_CANCELED"
                ):
                    logging.info("Canceled after completion %s", task_id)
                    return
                logging.info("Completed %s", task_id)
            except Exception as error:
                logging.exception("Failed %s", task_id)
                failed = {
                    "id": task_id,
                    "contextId": context_id,
                    "status": {
                        "state": "TASK_STATE_FAILED",
                        "timestamp": utc_timestamp(),
                        "message": agent_message(
                            f"Codex 执行失败：{sanitize_error(str(error))}", context_id
                        ),
                    },
                }
                if session_id:
                    failed["session_id"] = session_id
                if resumed_from_new:
                    failed["resumed_from_new"] = True
                self.store.update(
                    task_id, failed, terminal=True, if_not_state="TASK_STATE_CANCELED"
                )
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=3)

    def metrics(self) -> dict[str, Any]:
        counts = {
            "total": 0,
            "active": 0,
            "completed": 0,
            "failed": 0,
            "canceled": 0,
            "rejected": 0,
        }
        durations: list[float] = []
        with self.store.lock:
            for task in self.store.tasks.values():
                state = (task.get("status") or {}).get("state")
                counts["total"] += 1
                if state == "TASK_STATE_WORKING":
                    counts["active"] += 1
                elif state == "TASK_STATE_COMPLETED":
                    counts["completed"] += 1
                elif state == "TASK_STATE_FAILED":
                    counts["failed"] += 1
                elif state == "TASK_STATE_CANCELED":
                    counts["canceled"] += 1
                elif state == "TASK_STATE_REJECTED":
                    counts["rejected"] += 1
                start = task.get("created_at") or task.get("started_at")
                end = task.get("finished_at")
                if start and end:
                    try:
                        durations.append(_iso_to_epoch(end) - _iso_to_epoch(start))
                    except ValueError:
                        continue
        avg_duration = sum(durations) / len(durations) if durations else 0.0
        return {
            "service": "codex-a2a-bridge",
            "tasks": counts,
            "avgDurationSeconds": round(avg_duration, 3),
            "uptimeSeconds": round(time.time() - self.started_at, 2),
            "timestamp": utc_timestamp(),
        }

    def query_tasks(self, states: list[str] | None = None) -> list[dict[str, Any]]:
        return self.store.query(states)

    def shutdown(self) -> None:
        with self.store.lock:
            processes = list(self.store.processes.values())
            self.store.processes.clear()
        for process in processes:
            try:
                terminate_process_tree(process)
            except Exception:
                logging.exception("Failed to terminate process during bridge shutdown")
        logging.info("Bridge shutdown: terminated %d tracked Codex processes", len(processes))


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], bridge: CodexBridge) -> None:
        super().__init__(address, A2AHandler)
        self.bridge = bridge


class A2AHandler(BaseHTTPRequestHandler):
    server: BridgeServer

    def log_message(self, fmt: str, *args: Any) -> None:
        logging.info("HTTP %s - %s", self.address_string(), fmt % args)

    def _authorized(self) -> bool:
        token = getattr(self.server.bridge, "token", None)
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        supplied = header[len("Bearer ") :].strip()
        return secrets.compare_digest(supplied, token)

    def _base_url(self) -> str:
        host = self.headers.get("Host") or ""
        if not _ALLOWED_CARD_HOST_RE.match(host.strip()):
            host = f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        return f"http://{host}"

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _rpc_error(self, req_id: Any, code: int, message: str, status: int = 400) -> None:
        self._send_json(
            {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
            status,
        )

    def _send_unauthorized(self) -> None:
        self._send_json(
            {"error": "unauthorized", "message": "missing or invalid Bearer token"},
            401,
        )

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/health" and not self._authorized():
            self._send_unauthorized()
            return
        if path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
            self._send_json(self.server.bridge.card(self._base_url()))
            return
        if path == "/health":
            self._send_json({"ok": True, "service": "codex-a2a-bridge"})
            return
        if path == "/metrics":
            self._send_json(self.server.bridge.metrics())
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self._authorized():
            self._send_unauthorized()
            return
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            self._rpc_error(None, -32600, "invalid request body size", 413)
            return
        try:
            request = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._rpc_error(None, -32700, "parse error")
            return
        if not isinstance(request, dict):
            self._rpc_error(None, -32600, "request must be an object")
            return

        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            self._rpc_error(req_id, -32602, "params must be an object")
            return

        if method in ("SendMessage", "message/send"):
            try:
                ensure_text_only_message(params)
            except ContentTypeNotSupportedError as exc:
                self._rpc_error(req_id, CONTENT_TYPE_NOT_SUPPORTED, sanitize_error(str(exc)))
                return
            prompt = extract_message_text(params)
            if not prompt:
                self._rpc_error(req_id, -32602, "message must contain a non-empty text part")
                return
            immediate = wants_immediate_return(params)
            context_id = resolve_context_id(params)
            task_id = self.server.bridge.start_task(prompt, context_id)
            if not immediate:
                self.server.bridge.store.wait(task_id, self.server.bridge.sync_wait)
            task = self.server.bridge.store.get(task_id)
            if not task:
                self._rpc_error(req_id, -32603, "task disappeared")
                return
            if task["status"]["state"] == "TASK_STATE_WORKING":
                task["status"]["message"] = agent_message(
                    f"Codex 仍在执行。请稍后使用 GetTask 查询任务 {task_id}。", context_id
                )
            # Hermes accepts the bare Task and explicitly relies on this shape.
            self._send_json({"jsonrpc": "2.0", "id": req_id, "result": task})
            return

        if method in ("GetTask", "tasks/get"):
            task_id = str(params.get("id") or params.get("taskId") or params.get("task_id") or "")
            task = self.server.bridge.store.get(task_id)
            if not task:
                self._rpc_error(req_id, -32001, f"task not found: {task_id}", 404)
                return
            self._send_json({"jsonrpc": "2.0", "id": req_id, "result": task})
            return

        if method in ("CancelTask", "tasks/cancel"):
            task_id = str(params.get("id") or params.get("taskId") or params.get("task_id") or "")
            task = self.server.bridge.store.cancel(task_id)
            if not task:
                self._rpc_error(req_id, -32001, f"task not found: {task_id}", 404)
                return
            self._send_json({"jsonrpc": "2.0", "id": req_id, "result": task})
            return

        if method in ("QueryTasks", "tasks/query"):
            state_filter = params.get("state")
            states = params.get("states")
            if isinstance(state_filter, str):
                states = [state_filter]
            if states is None:
                tasks = self.server.bridge.query_tasks(None)
            elif isinstance(states, list):
                tasks = self.server.bridge.query_tasks(states)
            else:
                self._rpc_error(req_id, -32602, "states must be a list")
                return
            self._send_json({"jsonrpc": "2.0", "id": req_id, "result": {"tasks": tasks}})
            return

        self._rpc_error(req_id, -32601, f"method not found: {method}")


def configure_logging(log_file: Path, verbose: bool) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    ]
    if verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def resolve_token(explicit: str | None, token_file: Path | None, state_dir: Path) -> str | None:
    value = explicit or os.environ.get("A2A_BRIDGE_TOKEN")
    if value and value.strip():
        return value.strip()
    path = token_file or (state_dir / "bridge.token")
    try:
        if path.is_file():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                return content
    except OSError:
        logging.warning("Could not read token file %s", path)
    return None


def cleanup_residual_state(state_dir: Path) -> None:
    try:
        for leftover in state_dir.glob("codex-result-*.txt"):
            leftover.unlink(missing_ok=True)
            logging.info("Removed residual file %s", leftover.name)
    except OSError:
        logging.exception("Could not clean residual codex-result files in %s", state_dir)


def tighten_state_dir_permissions(state_dir: Path) -> None:
    """Best-effort ACL hardening on Windows; never fatal."""
    if os.name != "nt":
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", str(state_dir), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        logging.info("Tightened ACL on %s", state_dir)
    except Exception as exc:
        logging.warning("Could not tighten ACL on %s (best-effort): %s", state_dir, exc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A2A bridge from Hermes to Codex CLI")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--state-dir", type=Path, default=Path(__file__).resolve().parent / ".codex-a2a")
    parser.add_argument("--codex", help="Path to codex.exe or codex.cmd")
    parser.add_argument("--model", default="")
    parser.add_argument("--sync-wait", type=int, default=DEFAULT_SYNC_WAIT)
    parser.add_argument("--codex-timeout", type=int, default=DEFAULT_CODEX_TIMEOUT)
    parser.add_argument("--max-concurrent", type=int, default=1)
    parser.add_argument(
        "--token",
        help="Shared Bearer token (overrides A2A_BRIDGE_TOKEN and the token file)",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        help="File containing the Bearer token (default: <state-dir>/bridge.token)",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("Refusing non-loopback bind: danger-full-access Codex must remain local")
    if not args.workspace.is_dir():
        raise SystemExit(f"Workspace does not exist: {args.workspace}")
    if args.sync_wait < 1 or args.codex_timeout < args.sync_wait:
        raise SystemExit("Require 1 <= sync-wait <= codex-timeout")
    if args.max_concurrent < 1:
        raise SystemExit("max-concurrent must be positive")

    state_dir = args.state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    tighten_state_dir_permissions(state_dir)
    configure_logging(state_dir / "bridge.log", args.verbose)

    token = resolve_token(args.token, args.token_file, state_dir)
    if token:
        logging.info("Auth enabled via shared Bearer token (A2A_BRIDGE_TOKEN/--token/token file)")

    # Fail fast at startup, then re-resolve before every spawn (with one retry).
    codex_hint = args.codex or os.environ.get("CODEX_CLI_PATH")
    find_codex_executable(codex_hint)

    cleanup_residual_state(state_dir)
    bridge = CodexBridge(
        codex=codex_hint,
        workspace=args.workspace,
        state_dir=state_dir,
        model=args.model,
        sync_wait=args.sync_wait,
        codex_timeout=args.codex_timeout,
        max_concurrent=args.max_concurrent,
        token=token,
    )
    server = BridgeServer((args.host, args.port), bridge)
    logging.info(
        "Codex A2A bridge listening at http://%s:%s (workspace=%s)",
        args.host,
        args.port,
        bridge.workspace,
    )

    def stop_server(_signum: int, _frame: Any) -> None:
        bridge.shutdown()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, stop_server)
        except (ValueError, OSError):
            logging.warning("Could not register handler for signal %s", sig)

    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        bridge.shutdown()
        server.server_close()
        logging.info("Codex A2A bridge stopped")


if __name__ == "__main__":
    main()
