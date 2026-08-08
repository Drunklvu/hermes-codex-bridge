# Contributing

Thanks for your interest! This project is a small, focused bridge between two
AI agents. Before opening a PR, please read the architecture notes below —
they explain the non-negotiable design constraints.

## Project overview

Three components, one principle (**the bridge is the single executor and
state authority**):

| Component | Role |
|-----------|------|
| `codex_a2a_bridge.py` | Localhost HTTP bridge (`:9998`), Hermes → Codex. Spawns Codex CLI, serves the monitoring UI. |
| `hermes_mcp_server.py` | Pure-stdlib stdio MCP server, Codex → Hermes. Exposes `call_hermes`. |
| `a2a_sidecar.py` | Optional standard A2A 1.0 gateway (`:10000`) for third-party agents. Translates A2A into internal `internal/*` calls; never touches Codex/UI/bridge state directly. |

## Hard constraints (do not violate)

1. **Zero-dependency core**: `codex_a2a_bridge.py` and `hermes_mcp_server.py`
   must remain pure Python stdlib. No third-party imports at module load time.
   Optional deps (a2a-sdk, grpc, sqlalchemy) live only behind
   `try/except ImportError` in `a2a_sidecar.py` and in `[project.optional-dependencies]`.
2. **Loopback by default**: the bridge binds `127.0.0.1`. Non-loopback
   requires `--require-token` **and** a dedicated `--sidecar-token`
   (never reuse the bridge admin token).
3. **No secrets / no local paths / no business-specific words** in the repo,
   including commit messages. See `github-release-prep` workflow for the scan.
4. **Sidecar is optional**: the bridge must work without `a2a-sdk` installed.

## Development workflow

```bash
# 1. Run the full test suite (128 tests, no external services needed)
python -m unittest discover -s tests -p "test_*.py"

# 2. Coverage gate (must not regress below 60% branch)
python -m coverage run -m unittest discover -s tests -p "test_*.py"
python -m coverage report --fail-under=60

# 3. Compile check (catch invalid escapes / syntax warnings)
python -W error -m py_compile codex_a2a_bridge.py hermes_mcp_server.py a2a_sidecar.py
```

### Contract tests (sidecar only, needs a real environment)

`a2a_sdk_contract_check.py` submits **real tasks to a running bridge + Codex
CLI**. It is not part of CI. Run it only if you changed the sidecar:

```bash
# needs: a2a-sdk installed, bridge on :9998, Codex CLI available
python a2a_sdk_contract_check.py
```

> **Test-prompt discipline**: any real task sent to Codex during testing must
> be a pure reply instruction (`Reply with exactly "OK". Do not call any
> tools.`) — vague prompts get interpreted as real work.

## Code style

- Python 3.10+, type annotations on public signatures.
- Internal persistence uses `TASK_STATE_*` prefixed states; the API layer
  also accepts bare names (`COMPLETED`) and both JSON-RPC variants
  (`SendMessage` / `message/send`).
- State transitions go through `TaskStore._can_transition` — terminal states
  never go back to WORKING.
- Blocking calls in the sidecar use `asyncio.to_thread`; never block the loop.
- JSON-RPC methods are PascalCase.

## Commit messages

- One logical change per commit.
- **Never paste secrets, local paths, or business-specific words into commit
  messages** — they are public forever.

## Before opening a PR

- [ ] `python -m unittest discover -s tests -p "test_*.py"` passes
- [ ] Coverage not below 60% (branch)
- [ ] `python -W error -m py_compile` on all three modules, zero warnings
- [ ] No secrets / paths / business words in files **or commit messages**
- [ ] Updated CHANGELOG.md under [Unreleased] if user-facing behavior changed
