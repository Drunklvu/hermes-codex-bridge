# hermes-codex-bridge

> **English** | [简体中文 (Chinese)](README.zh-CN.md)

Bidirectional local collaboration between **Codex** and **Hermes Agent** over the
[A2A protocol](https://a2a-protocol.org) (JSON-RPC over HTTP, loopback only).

> **The core idea: each agent gains the ability to use another agent.**
> Hermes can call Codex mid-task (like you directing an assistant), and Codex can
> call Hermes the other way — so you are no longer the human conveyor belt
> copy-pasting context between them.

## What is this? (plain English)

**Two AI agents on your machine, talking to each other.**

- **Codex** (the coding agent) and **Hermes** (your assistant agent) usually live in
  separate worlds. This project builds a two-way bridge between them:
  - Codex can ask Hermes to do things (query a knowledge base, fetch a page,
    run a local check) and get a final answer back.
  - Hermes can ask Codex to write or review code, and get a finished result back.
- It is **local-first**: everything runs on your machine over loopback; nothing is
  sent to a cloud broker.
- It comes with a **live monitoring dashboard** (`/ui`) that shows every call in
  both directions — grouped by conversation, with status, timing, and full
  transcripts. Think of it as *a phone bill that also shows you what was said*.
- It keeps related calls in the **same conversation** (workstreams), so a follow-up
  task remembers the context instead of starting from scratch every time.

> In short: **one control room for two agents.** Use it when you want Codex and
> Hermes to cooperate on a task without copy-pasting context between them.

### What you can do with it

| Goal | How this project helps |
|---|---|
| **Hand tasks between agents** | Codex asks Hermes to research / fetch / verify something and gets a clean answer; Hermes asks Codex to build or review code and gets a finished result — no manual copy-paste. |
| **Keep conversations coherent** | Related calls automatically stay in the same workstream (`ctx-<name>#01`, `#02`, ...), so follow-ups remember context. Independent reviews get their own thread (`-review`) to avoid bias. |
| **See everything happening** | The live dashboard (`/ui`) shows every call both ways: status, timing, who said what, and full transcripts — with stats, filters, and search. |
| **Clean up when done** | Delete a single call or a whole conversation from the dashboard; stale sessions auto-hide; connectivity-test noise is grouped separately for one-click cleanup. |
| **Stay safe locally** | Loopback-only, token-authenticated, with crash recovery and heartbeat monitoring — nothing leaves your machine. |

Two components in one repo:

| Component | File | What it does |
|---|---|---|
| **Codex → Hermes** | `hermes_mcp_server.py` | A pure-stdlib stdio **MCP server** exposing one tool, `call_hermes`, that sends a task to a local Hermes agent (A2A `message/send`), polls `tasks/get`, and returns Hermes's final reply as an MCP text result. |
| **Hermes → Codex** | `codex_a2a_bridge.py` | A localhost **HTTP bridge** exposing A2A `message/send` / `tasks/*` to Hermes; each task spawns the native Codex CLI (`codex exec`) with a bounded concurrency, token auth, heartbeat, and crash recovery. |
| **Standard A2A access** (optional) | `a2a_sidecar.py` | An optional **A2A 1.0 gateway** (`:10000`) for third-party standard agents: JSON-RPC/gRPC transports, Bearer auth, tenant isolation, push notifications, persistence, AgentCard signing and OpenTelemetry tracing — all talking to the same bridge under the hood. |

```
┌────────────┐   stdio MCP    ┌──────────────────┐   A2A JSON-RPC   ┌──────────────────┐
│  Codex CLI │ ◄────────────► │ hermes_mcp_server │ ◄──────────────► │  Hermes Agent    │
│ (client)   │  tools/call    │  (this repo)     │   message/send   │ (A2A gateway)    │
└────────────┘                └──────────────────┘   tasks/get      └──────────────────┘
                                                                          ▲
┌────────────┐                ┌──────────────────┐   A2A JSON-RPC        │
│  Hermes    │ ◄────────────► │ codex_a2a_bridge │ ◄─────────────────────┘
│ (A2A peer) │   message/send │  (this repo)     │   codex exec (subprocess)
└────────────┘                └──────────────────┘
       ▲
       │ A2A 1.0 (optional)
┌──────────────────┐
│  a2a_sidecar     │  ← third-party standard A2A agents
│  (:10000, SDK)   │
└──────────────────┘
```

## Features

**MCP server (`hermes_mcp_server.py`)**
- Pure Python stdlib — zero dependencies.
- Speaks MCP over stdio with **both** framings: legacy `Content-Length` headers and newline-delimited JSON (each response echoes the request's framing).
- JSON-RPC batch support; `initialize` / `tools/list` / `tools/call` / `ping`.
- Profile routing: one tool → multiple Hermes instances (each profile maps to a separate A2A endpoint).
- In-flight task tracking: on timeout it best-effort calls `tasks/cancel`; on EOF / SIGINT / SIGTERM it cancels all recorded tasks; on next startup it reconciles stale records (cancels WORKING, drops terminal, keeps unreachable). Recovery never blocks startup.
- Input limits (message length, `context_id` charset/length), response size caps, log redaction.

**Bridge (`codex_a2a_bridge.py`)**
- Localhost-only by default (`127.0.0.1`; non-loopback hosts are rejected unless `--host` is explicitly passed).
- Optional shared Bearer token (via `--token`, `--token-file`, or `A2A_BRIDGE_TOKEN`).
- Bounded concurrency (`--max-concurrent`), heartbeat-based crash detection, task queue with rejection when full, `/health` endpoint.
- Handles terminal task states (completed / failed / canceled / rejected) and streams results back to Hermes.

**Live monitoring UI (`/ui`)**
- Real-time dashboard: tasks grouped by conversation (context_id), direction chips (inbound/outbound), status badges, generation markers, noise-grouping for connectivity/echo-check traffic.
- Stats cards, status distribution bar, direction donut (pure CSS), recent activity feed.
- Conversation timeline per role (user / assistant / tool / system), local-time display.
- Filter by state + keyword search; delete single call or whole conversation via a styled `<dialog>` confirm (Bearer-token protected DELETE).
- Orphan filtering: tasks whose Codex session file no longer exists are hidden automatically.

**A2A SDK sidecar (`a2a_sidecar.py`, optional)** — standard A2A 1.0 access for third-party agents
- Standard A2A 1.0 protocol: `SendMessage` / `GetTask` / `ListTasks` / `CancelTask` + AgentCard discovery (`/.well-known/agent-card.json`).
- **Two transports**: JSON-RPC over HTTP (`:10000`) and gRPC (`--grpc-port`, with Bearer auth interceptor).
- **Security**: loopback-only by default; `--require-token` forces Bearer auth on every endpoint (incl. gRPC); non-loopback binding requires auth and forbids reusing the bridge admin token.
- **Tenant isolation** (`--tenant-mode`): requests without a tenant are rejected; cross-tenant cancel is refused.
- **Persistence** (`--db`): SQLite-backed task store, survives restarts.
- **Push notifications** (`--push`): task status changes pushed to client-configured webhooks.
- **AgentCard signing** (`--sign-key`): JWT-signed AgentCard so clients can verify authenticity.
- **OpenTelemetry tracing** (`--tracing`): SDK spans to console (no external collector needed).
- **SDK update check** (`--check-update`): queries PyPI for the latest `a2a-sdk`.
- Process-isolated: sidecar crash never affects the bridge `:9998`; zero-dependency fallback (no `a2a-sdk` installed → clear error, bridge still works).

**Reverse-link reporting (Codex → Hermes visibility)**
- `POST /inbound/events` on the bridge (separate `--inbound-token`): the MCP server reports started/accepted/state/finished events so reverse calls show up on the dashboard.
- Durable outbox on the MCP side (JSONL + ack + exponential backoff): no event is lost on crash; idempotent by operation_id; tombstone prevents deleted tasks from resurrecting.

**Session management**
- Workstream registry: callers pass a logical name (e.g. `demo-project`), the bridge resolves generations (`ctx-<name>#01`, `#02`, ...) and resumes the same Codex session for follow-ups.
- Health checks (estimated tokens / message count / file size / idle days) with warning markers; optional auto-rotation (`WS_AUTO_ROTATE`); archive + cleanup.

**Prompt convention (both directions)**
- Recommended structured message template: 【目标】【上下文与输入】【边界与授权】【交付与验收】 — default required for tool-using/multi-step/state-changing work; compact prose allowed only for trivial one-shot tasks (never just `hi`). See the examples below.

**Message examples**

*Full task (default):*
```text
请帮我完成以下任务：
【目标】检查 codex_a2a_bridge.py 的 delete_task，找出共享 context 误删会话文件的漏洞
【上下文与输入】文件 codex_a2a_bridge.py，删除逻辑在 delete_task 方法
【边界与授权】只读分析，不修改任何文件；不访问外网
【交付与验收】给出漏洞点（带行号）+ 修复建议，300 字内
```

*Compact task (trivial one-shot only):*
```text
请直接回答：解释 A2A 的 tasks/get 轮询为什么拿不到中间思考链。只读，3 点以内。
```

*Continuing an existing conversation (reuse the same context_id):*
```text
请帮我完成以下任务：
【已确认】上一轮已确认：白屏根因是 JS 转义，已修复并验证
【本轮目标】现在检查监控页切换对话不更新的问题
【上下文与输入】同上，codex_a2a_bridge.py
【边界与授权】只读分析，不改文件
【交付与验收】根因 + 行号 + 修复建议
```

## Requirements

- Python 3.10+ (stdlib only)
- [Codex CLI](https://github.com/openai/codex) installed and authenticated (bridge only)
- Hermes Agent with the A2A inbound platform enabled (gateway `platforms.a2a` listening on 127.0.0.1)

## Quick install (Windows)

```powershell
git clone https://github.com/Drunklvu/hermes-codex-bridge.git
cd hermes-codex-bridge
powershell -ExecutionPolicy Bypass -File scripts/install.ps1
```

The installer auto-detects Python / Codex / Hermes, registers the MCP server in
`~/.codex/config.toml`, registers the `codex` peer in Hermes (capabilities written
as a real YAML list via Python+PyYAML), creates a startup shortcut, generates a
real `scripts/start_bridge.ps1` from the example (placeholder paths and port
filled in), and starts the bridge.

It is **idempotent**: re-running on an already-configured setup skips unchanged
settings. Uninstall with `-Uninstall` (stops the bridge, removes the MCP
registration and startup shortcut); dry-run with `-DryRun` (detect only, write
nothing).

| Parameter | Purpose |
|---|---|
| `-Python <path>` | Python interpreter for the MCP server (default: auto-detect `python.exe`; the bridge itself uses `pythonw.exe`) |
| `-Codex <path>` | Codex executable (default: auto-detect) |
| `-HermesCli <path>` | Hermes CLI (default: auto-detect; point at a stub for sandbox testing) |
| `-BridgeDir <path>` | Directory containing the bridge scripts (default: repo root) |
| `-Workspace <path>` | Codex working directory (default: parent of bridge dir) |
| `-Port <int>` | Bridge port (default 9998) |

> ⚠️ The generated `scripts/start_bridge.ps1` contains your local absolute paths —
> it is a machine-local file and is not meant to be committed.

## Setup

1. **Hermes side** — enable the A2A inbound platform in `config.yaml` so the Hermes gateway listens on `127.0.0.1:9900` (one port per profile/instance you want to reach):

   ```yaml
   # Hermes config.yaml — inbound platform (others can call this Hermes)
   gateway:
     platforms:
       a2a:
         enabled: true
         extra:
           port: 9900        # one profile per port, e.g. 9901 for a second instance
   ```

   Restart the gateway (`hermes gateway restart`). Confirm with:
   `curl -s http://127.0.0.1:9900/.well-known/agent-card.json`

2. **MCP server** — register `hermes_mcp_server.py` as an MCP server in your Codex config:

   ```toml
   # Codex config.toml
   [mcp_servers.hermes]
   command = "C:\\Path\\To\\Python311\\python.exe"   # explicit interpreter, NOT the .py file
   args = ["C:\\path\\to\\hermes-codex-bridge\\hermes_mcp_server.py"]
   startup_timeout_sec = 15
   tool_timeout_sec = 300          # max seconds Codex waits for call_hermes
   enabled = true
   required = false
   enabled_tools = ["call_hermes"]
   ```

   Two gotchas from real usage:
   - Put the **Python interpreter in `command`** and the script in `args` — pointing `command` directly at the `.py` file breaks initialization.
   - New Codex (RMCP) sends newline-delimited JSON frames; this server accepts **both** that and legacy `Content-Length` framing, and replies in the same framing.

   Then Codex gets a `call_hermes` tool. See `tests/mcp_smoke_test.py` for a standalone protocol walkthrough.

3. **Bridge** — copy `scripts/start_bridge.example.ps1`, edit the paths, and run it (or launch `codex_a2a_bridge.py --help` directly). The bridge needs the native `codex.exe` and a workspace directory. On the Hermes side, register the bridge as an outbound peer:

   ```yaml
   # Hermes config.yaml — outbound peer (this Hermes calls Codex)
   a2a_agents:
     codex:
       url: "http://127.0.0.1:9998"
       timeout: 1800
       # if the bridge runs with a token file, mirror its content here:
       auth: { type: bearer, token: "<bridge.token content>" }
   ```

## Verification

Quick end-to-end checks after setup:

```bash
# Hermes Agent Card reachable (inbound)
curl -s http://127.0.0.1:9900/.well-known/agent-card.json

# Hermes inbound accepts a JSON-RPC task (use a file to avoid shell quoting)
curl -s -X POST http://127.0.0.1:9900 -H "Content-Type: application/json" -d @a2a_inbound_test.json

# Bridge health
curl -s http://127.0.0.1:9998/health
```

The full protocol walkthrough (initialize → tools/list → real `call_hermes`) is `tests/mcp_smoke_test.py`; profile routing incl. invalid profile is `tests/mcp_profile_test.py`.

## Configuration (environment variables)

`hermes_mcp_server.py`:

| Variable | Default | Purpose |
|---|---|---|
| `HERMES_A2A_URL` | `http://127.0.0.1:9900` | Primary A2A endpoint |
| `HERMES_PROFILE_URLS` | `{"default": ...9900, "web-dev": ...9901}` | JSON dict overriding profile → endpoint map |
| `HERMES_A2A_TOKEN` | *(empty)* | Bearer token sent to Hermes A2A endpoints |
| `HERMES_TASK_TIMEOUT` | `300` | Max seconds to wait for a task |
| `HERMES_POLL_INTERVAL` | `1.0` | Seconds between `tasks/get` polls |
| `HERMES_HTTP_TIMEOUT` | `10.0` | Per-HTTP-request timeout |
| `HERMES_STATE_FILE` | `~/.hermes/mcp_inflight.json` | In-flight task state file |

`codex_a2a_bridge.py`:

| Variable | Default | Purpose |
|---|---|---|
| `HERMES_A2A_WORKSPACE` | current dir | Workspace passed to Codex (or `--workspace`) |
| `A2A_BRIDGE_TOKEN` | *(empty)* | Shared Bearer token (or `--token` / `--token-file`) |

## Security notes

- Both servers bind to **127.0.0.1 only** by design — do not expose them beyond loopback.
- `call_hermes` executes real agent work: the MCP client grants the tool only to trusted calling agents. The tool description itself warns against delegating back to the calling agent (prevents infinite agent-to-agent loops).
- The bridge's state directory holds the Bearer token and task records — keep it out of version control (see `.gitignore`).
- `context_id` values are stored only as a SHA-256 prefix in the state file, never raw.
- Hermes's own A2A platform adds defense in depth (defaults on): no token ⇒ loopback-only binding; outbound text is redacted for credential-shaped strings (`sk-`, `ghp_`, …); inbound text is filtered and marked as untrusted peer input; a per-identity rate limit and a max ping-pong turn cap (default 5) break agent-to-agent loops; every exchange is appended to `~/.hermes/a2a_audit.jsonl`.

## Known limitations & troubleshooting

- **Empty replies from upstream** — the `default` Hermes profile occasionally completes a task with an empty reply; the server reports it as `empty_reply` with the `task_id` instead of inventing content. This is an upstream behavior, not a bridge bug. Retry, or route the task to a profile that responds reliably.
- **`context_id` continuation bound by gateway lifecycle** — session continuity depends on the Hermes gateway keeping the conversation; gateway restarts or manual session cleanup end it. Reuse of a `context_id` after that starts a fresh conversation.
- **`--last` vs. bridge sessions** — don't run `codex exec --last` while the bridge is using a session; the two may fight over the same thread.
- **Resume degradation** — a bridged `codex exec resume` fails persistently if Codex has cleaned up that session; the bridge falls back to a new thread (marked `resumed_from_new`) but there is no full self-heal yet.
- **Model mismatch** — the bridge passes no `--model` by default so it inherits your Codex config; hardcoding a model in the bridge causes mismatches when you switch providers.
- **Windows process-kill semantics** — on Windows, SIGTERM via TerminateProcess does not run Python handlers; the state file + next-startup reconciliation is the recovery backstop.

## Testing

```bash
# Full unit suite (128 tests, no Hermes needed)
python -m unittest discover -s tests -p "test_*.py" -v

# MCP protocol smoke test (initialize → tools/list → tools/call), needs a live
# Hermes A2A gateway on the default port
python tests/mcp_smoke_test.py

# Profile routing + invalid profile
python tests/mcp_profile_test.py

# Bridge unit tests (no Hermes needed; uses a fake bridge)
python tests/test_codex_a2a_bridge.py
```

## A2A SDK Sidecar (optional, standard A2A access layer)

The core bridge (`:9998`) speaks a **private protocol on loopback** and serves Codex only.
If you ever need **third-party standard A2A agents** (remote deployment, open ecosystem),
an optional sidecar provides the standard A2A 1.0 entry point:

```
Standard A2A client → sidecar (:10000, A2A 1.0) → bridge (:9998) → Codex
```

### Architecture (non-breaking)

- The sidecar is a **separate process**, off by default; the existing bridge keeps working as-is.
- The bridge stays the **single executor and state authority** (Codex process, dashboard, private protocol untouched).
- The sidecar only does **standard A2A transport translation**, calling the bridge via `internal/*` endpoints.
- SDK tasks and existing tasks **share the bridge state source** (visible in the dashboard).

### Install (optional)

```bash
pip install "hermes-codex-bridge[a2a]"   # or: pip install a2a-sdk[http-server,fastapi]
```

### Start

```powershell
# Local minimal (HTTP :10000, in-memory)
powershell -File start_a2a_sidecar.ps1

# Full features (persistence + gRPC + push)
powershell -File start_a2a_sidecar.ps1 -Db sidecar_tasks.db -GrpcPort 50051 -Push

# Remote deployment (Bearer auth + dedicated token)
powershell -File start_a2a_sidecar.ps1 -RequireToken -SidecarToken <your-token>
```

Parameter reference:

| Param | Default | Description |
|-------|---------|-------------|
| `-Port` | 10000 | HTTP listen port |
| `-Db` | empty | SQLite file path (persistence; empty = in-memory, lost on restart) |
| `-GrpcPort` | 0 | gRPC listen port (0 = disabled; suggested 50051) |
| `-Push` | off | Enable push notifications (task status pushed to client webhook) |
| `-RequireToken` | off | Remote hardening (all endpoints require Bearer token) |
| `-SidecarToken` | empty | Sidecar's own token (when RequireToken; empty = reuse bridge token) |
| `-Reasoning` | off | Expose Codex reasoning events to clients (off by default: keep internal reasoning private) |
| `-BridgeUrl` | :9998 | Internal bridge address |

> Security: binding a non-loopback address (`-Host 0.0.0.0`) **requires** `-RequireToken`,
> and reusing the bridge admin token is forbidden (use a dedicated `-SidecarToken`).

### Verify

```bash
curl http://127.0.0.1:10000/health          # {"ok":true,"service":"a2a-sidecar"}
curl http://127.0.0.1:10000/.well-known/agent-card.json   # standard AgentCard
```

### Security

- Binds `127.0.0.1` by default; remote deployment must use `--require-token`.
- Sidecar crash/shutdown does **not** affect the bridge `:9998` (process isolation).
- Private routes (`/inbound/events`, admin endpoints) are **never exposed** to the sidecar.

### Files

| File | Role |
|------|------|
| `a2a_sidecar.py` | Sidecar entry (AgentExecutor + FastAPI + auth) |
| `http_task_service.py` | HttpTaskService (HTTP implementation of TaskService calling bridge `internal/*`) |
| `task_service.py` | TaskService narrow interface (Protocol + DTOs + adapters) |
| `a2a_sdk_contract_check.py` | SDK client contract tests (integration; needs sidecar + a2a-sdk) |
| `start_a2a_sidecar.ps1` | Start script |

## License

MIT
