# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Open-source housekeeping: CONTRIBUTING.md, SECURITY.md, issue/PR templates.

## [0.2.0] - 2026-08-08

### Added

- **A2A SDK sidecar** (`a2a_sidecar.py`): optional standard A2A 1.0 gateway
  (`:10000`) for third-party agents — JSON-RPC/gRPC transports, Bearer auth,
  tenant isolation (`--tenant-mode`), SQLite persistence (`--db`), push
  notifications (`--push`), AgentCard JWT signing (`--sign-key`),
  OpenTelemetry tracing (`--tracing`), reasoning streaming (`--reasoning`),
  SDK update check (`--check-update`).
- **GitHub Actions CI**: three jobs — unit tests (128), coverage gate
  (>= 60% branch), secrets & path scan (tracked files + git history).
- **AGENTS.md**: guidance for AI coding agents working in this repo
  (hard constraints, testing, conventions, pitfalls).
- **Cross-platform launchers** (best-effort): `start_a2a_sidecar.sh`,
  `scripts/start_bridge.example.sh` for Linux/macOS.
- `--max-tasks` configurable task-store limit.

### Changed

- **Monitoring UI split out** of the bridge into `monitor_ui.html`
  (codex_a2a_bridge.py: 3807 → 2635 lines) — UI edits no longer touch the
  Python file; template is read at startup with a fallback error page.
- README rewritten: three-component architecture (MCP server / bridge /
  sidecar) now visible up front, bilingual (EN + zh-CN), platform support table.
- Coverage config added (`[tool.coverage]`, branch stats, `fail_under = 60`).

### Fixed

- **gRPC security**: Bearer auth interceptor; loopback-only by default;
  non-loopback binding now **requires** a dedicated `--sidecar-token` and
  refuses to reuse the bridge admin token.
- **Reasoning leak**: `--reasoning` defaults to off — model internal reasoning
  is not exposed to external clients unless explicitly enabled.
- **Tenant isolation**: cross-tenant cancel refused (owner must match).
- **Cancel semantics**: cancels use the bridge task id (was: SDK uuid).
- **Timeout semantics**: a timed-out task is marked `failed` (real timeout),
  not a fabricated cancel.
- **SyntaxWarning**: invalid `\d` escape in the UI template's JS fixed.
- **Idempotency**: reverted an unverified implementation rather than shipping
  it half-tested (contract tests kept as the safety net).

## [0.1.0] - 2026-08-05

### Added

- **Initial release**: bidirectional A2A bridge between Codex and Hermes.
- `codex_a2a_bridge.py` — localhost HTTP bridge (Hermes → Codex), spawns the
  native Codex CLI with bounded concurrency, token auth, heartbeat, crash
  recovery, and the live monitoring dashboard (`/ui`).
- `hermes_mcp_server.py` — pure-stdlib stdio MCP server (Codex → Hermes),
  exposing the `call_hermes` tool with profile routing and durable outbox.
- **Live monitoring UI**: conversation grouping, direction chips, status
  badges, stats, filters, search, deletion (Bearer-protected).
- **Reverse-link reporting**: MCP server reports inbound events so
  Codex → Hermes calls show up on the dashboard.
- **Session management**: workstream registry (`ctx-<name>#NN`), health
  checks, archive/cleanup.
- **Prompt convention**: four-part structured task template
  (goal / context / boundary / deliverable), enforced for tool-using and
  multi-step tasks.
- Windows installer `install.ps1`.
