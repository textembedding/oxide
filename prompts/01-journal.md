# Milestone 1: Journal Only

Implement only the disposable journal and backend-neutral client.

## Allowed files

- `src/swarm_harness/protocol.py`
- `src/swarm_harness/sqlite_service.py`
- `src/swarm_harness/journal_client.py`
- `tests/test_journal.py`
- `pyproject.toml`

## Hard limits

- At most 800 non-test source lines added in this milestone.
- No source file longer than 500 lines.
- No additional modules.
- No controller, worker launcher, Codex integration, or Stage 0 execution.

## Required behavior

- atomic task claim
- opaque claim token
- stale-token rejection
- valid result submission
- lease expiry and reclaim
- persistence across service restart
- dependency-aware runnable selection
- storage of proposed follow-ups without queue insertion
- small JSON protocol over a local socket

Stop after the journal tests pass. A missing requirement is a blocker report,
not permission to expand scope.
