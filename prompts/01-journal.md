# Journal Authority

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
- No acceptance or completion authority outside journal transactions.

## Required behavior

- atomic task claim
- opaque claim token
- stale-token rejection
- valid result submission
- lease expiry and reclaim
- persistence across service restart
- dependency-aware runnable selection
- storage of proposed follow-ups without queue insertion
- permissionless proposals for acceptance, retry, decomposition, dependency
  change, and stage completion
- author exclusion, one vote per validator, and deterministic 2-of-3 commit
- small JSON protocol over a local socket

Stop after the journal tests pass. A missing requirement is a blocker report,
not permission to expand scope.
