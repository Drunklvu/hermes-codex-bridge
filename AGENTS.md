# AGENTS.md — guidance for AI coding agents working in this repo

> This file is read automatically by AI agents (Codex, Claude Code, etc.) when
> they open this repository. It tells you how the project is structured, how to
> test it, and what to respect while working here.

## What this project is

A **bidirectional A2A bridge** that lets two AI agents on the same machine talk
to each other:

- **`codex_a2a_bridge.py`** — localhost HTTP bridge (`:9998`): Hermes → Codex.
  Each task spawns the native Codex CLI (`codex exec`), with bounded concurrency,
  token auth, heartbeat and crash recovery. Also serves the live monitoring
  dashboard (`/ui`).
- **`hermes_mcp_server.py`** — pure-stdlib stdio MCP server: Codex → Hermes.
  Exposes one tool `call_hermes` that sends a task to a local Hermes agent.
- **`a2a_sidecar.py`** (optional) — standard A2A 1.0 gateway (`:10000`) for
  third-party standard agents: JSON-RPC/gRPC transports, Bearer auth, tenant
  isolation, push notifications, persistence, AgentCard signing, tracing.

Architecture principle: **the bridge is the single executor and state
authority.** The sidecar translates standard A2A into internal calls
(`internal/*` endpoints) — it does not manage the Codex process, the dashboard,
or bridge state directly; it only uses the narrowed internal interface.

## Hard constraints (never violate)

1. **Zero-dependency core.** `codex_a2a_bridge.py` and `hermes_mcp_server.py`
   must stay pure Python stdlib — no third-party imports at module load time.
   Optional features (a2a-sdk, grpc, sqlalchemy) live only behind
   `try/except ImportError` in `a2a_sidecar.py` and are declared as
   `[project.optional-dependencies]` in `pyproject.toml`.
2. **Loopback only by default.** The bridge binds `127.0.0.1`. Non-loopback
   binding must require `--require-token` and a dedicated sidecar token —
   never reuse the bridge admin token for remote access.
3. **No secrets in the repo.** `*.token`, `auth.json`, `*.env`, `*.db` are
   gitignored runtime state. Never commit them. Never log token values.
4. **The sidecar is optional.** The bridge must work without `a2a-sdk`
   installed — degrade with a clear error message, never crash the bridge.

## Testing

```bash
# Unit suite (no external services; use the discovered test count)
python -m unittest discover -s tests -p "test_*.py"

# Contract tests for the SDK sidecar (needs a2a-sdk + a running sidecar
# + the bridge on :9998 + a usable Codex CLI + auth config — they submit
# real tasks to the bridge)
python a2a_sdk_contract_check.py

# MCP smoke tests (need a live Hermes A2A gateway)
python tests/mcp_smoke_test.py
python tests/mcp_profile_test.py
```

For real test requests, use a pure reply prompt: `Reply with exactly "OK". Do not call any tools or run any commands.` Live contract and smoke tests send real agent tasks; run them only within the authorized integration-test scope. Documentation-only edits need content and link checks, not live services.

Test files live in `tests/`; the bridge module lives at the repo root, so
tests load it via `importlib.util.spec_from_file_location` (see
`tests/test_codex_a2a_bridge.py` for the pattern).

## Code conventions

- Python 3.10+, type annotations on public signatures (`dict[str, Any]`).
- Internal persistence uses `TASK_STATE_*` prefixed state strings; the API
  layer also accepts bare names (`COMPLETED`) and both JSON-RPC name variants
  (`SendMessage` / `message/send`, `GetTask` / `tasks/get`).
- State transitions are validated by `TaskStore._can_transition` — terminal
  states can never go back to WORKING.
- Async work in the sidecar uses `asyncio.to_thread` for blocking calls
  (submit / get / cancel); never block the event loop.
- JSON-RPC methods are PascalCase (`SendMessage`, `GetTask`, ...).
- When editing `a2a_sidecar.py`: keep the zero-dependency fallback working —
  the module must import cleanly with `A2A_AVAILABLE = False`.

## Releasing / publishing (maintainers only)

- Public repo = **one-way door**: once pushed, history is hard to retract.
- Push only with explicit user authorization. For code releases, scan for private data and run the full unit suite; use an authorized end-to-end request when protocol or runtime behavior changes. For documentation-only releases, validate the documentation without starting services. If available, `github-release-prep` provides release-specific checks.
- Prefer direct `git push`; fall back to a proxy only if direct fails.

## Known environment notes

- **protobuf pin**: the sidecar venv pins protobuf 6.33.x (a2a-sdk 1.1.2 needs
  `>=6.33.5,<7.0`). grpcio/grpcio-tools must stay at **1.74.x** (1.74 supports
  protobuf `>=6.31.1,<7.0`; 1.83 wants `>=7.35.1` and breaks `pip check`).
  Lock: `grpcio==1.74.0 grpcio-tools==1.74.0`.
- **AgentCard signing is fail-closed**: a `--sign-key` failure aborts startup
  (no unsigned fallback). `/health` and AgentCard stay anonymously reachable
  in remote mode (discovery endpoints are anonymous per the A2A spec).

## Common pitfalls (learned the hard way)

- `TaskState.COMPLETED` does not exist — the SDK enum is `TASK_STATE_COMPLETED`.
- A `-> None` async function cannot return a Task to the caller; push events
  into the event queue instead.
- f-strings: write `f"[::]:{port}"` — an un-prefixed string like `"[::]:{port}"`
  stays literal and never interpolates.
- Without grpcio, the gRPC interceptor class must not be defined (guard with
  `A2A_AVAILABLE and _grpc_aio is not None`).
- Tests that `import` root modules from `tests/` need
  `sys.modules[...] = module` registration before dataclass processing.
