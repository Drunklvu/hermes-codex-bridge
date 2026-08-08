## What does this PR do?

<!-- One or two sentences. -->

## Related issue

<!-- Fixes #123 or N/A -->

## Checklist

- [ ] `python -m unittest discover -s tests -p "test_*.py"` passes
- [ ] Coverage not below 60% (branch): `python -m coverage report --fail-under=60`
- [ ] `python -W error -m py_compile codex_a2a_bridge.py hermes_mcp_server.py a2a_sidecar.py` — zero warnings
- [ ] No secrets / local paths / business-specific words in files **or commit messages**
- [ ] CHANGELOG.md updated under [Unreleased] if user-facing behavior changed

## Notes for the reviewer

<!-- Anything tricky, design decisions, trade-offs. -->
