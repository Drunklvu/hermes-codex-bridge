# Security Policy

## Reporting a vulnerability

**Do not open a public issue for security vulnerabilities.**

Please report privately instead — this project is small and maintained by one
person, so email is the fastest path:

- **Email**: `bridge-security@users.noreply.github.com` *(maintainer's GitHub
  noreply address — the exact mailbox is resolved at the maintainer's side;
  if this address bounces, open a private issue via the GitHub security
  advisory flow: repo → Security → Report a vulnerability)*

You should receive an acknowledgment within **3 business days**. If you do not
hear back, escalate via a private issue on the repository (GitHub's
confidential reporting).

## What to include

- Affected version / commit SHA
- Steps to reproduce (minimal)
- Impact description
- Suggested fix (optional)

## Security posture of this project

- **Loopback-only by default** — the bridge and sidecar bind `127.0.0.1`;
  non-loopback binding is refused unless `--require-token` is set **and** a
  dedicated sidecar token is provided (the bridge admin token is never
  reusable for remote access).
- **Zero-dependency core** — `codex_a2a_bridge.py` / `hermes_mcp_server.py`
  import stdlib only, reducing the supply-chain surface.
- **Secrets scan in CI** — every push is scanned for credential patterns,
  local paths and business-specific words (tracked files + git history).
- **Token handling** — runtime tokens (`bridge.token`, `inbound.token`) are
  gitignored, generated on first run, and never logged.

## Known trade-offs (documented, not silently ignored)

- **AgentCard signing is fail-open**: a `--sign-key` failure logs and
  continues serving an unsigned AgentCard (discovery endpoints are anonymous
  per the A2A spec). If you require strict verification, gate clients
  accordingly.
- **`/health` and AgentCard stay anonymous** in remote mode — they are
  discovery endpoints; they expose no task data.

## Supported versions

Only the latest `main` is actively maintained. There are no LTS releases.
