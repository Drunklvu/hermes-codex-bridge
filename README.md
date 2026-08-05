# hermes-codex-bridge

> **English** | [简体中文 (Chinese)](README.zh-CN.md)

Bidirectional local collaboration between **Codex** and **Hermes Agent** over the
[A2A protocol](https://a2a-protocol.org) (JSON-RPC over HTTP, loopback only).

Two components in one repo:

| Component | File | What it does |
|---|---|---|
| **Codex → Hermes** | `hermes_mcp_server.py` | A pure-stdlib stdio **MCP server** exposing one tool, `call_hermes`, that sends a task to a local Hermes agent (A2A `message/send`), polls `tasks/get`, and returns Hermes's final reply as an MCP text result. |
| **Hermes → Codex** | `codex_a2a_bridge.py` | A localhost **HTTP bridge** exposing A2A `message/send` / `tasks/*` to Hermes; each task spawns the native Codex CLI (`codex exec`) with a bounded concurrency, token auth, heartbeat, and crash recovery. |

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

## Requirements

- Python 3.10+ (stdlib only)
- [Codex CLI](https://github.com/openai/codex) installed and authenticated (bridge only)
- Hermes Agent with the A2A inbound platform enabled (gateway `platforms.a2a` listening on 127.0.0.1)

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
# Full unit suite (44 tests, no Hermes needed)
python -m unittest discover -s tests -p "test_*.py" -v

# MCP protocol smoke test (initialize → tools/list → tools/call), needs a live
# Hermes A2A gateway on the default port
python tests/mcp_smoke_test.py

# Profile routing + invalid profile
python tests/mcp_profile_test.py

# Bridge unit tests (no Hermes needed; uses a fake bridge)
python tests/test_codex_a2a_bridge.py
```

## License

MIT
