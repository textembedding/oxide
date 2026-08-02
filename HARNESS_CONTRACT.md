# Harness Contract

## Purpose

The harness coordinates coding workers implementing Stages 0–3 in a
separate target repository. It dogfoods a minimal Python journal prototype
while those workers build the production Rust kernel that will replace it.

## Worker-visible tool surface

Workers receive exactly two tools.

### `claim_task`

Request:

```json
{"worker_id": "worker-1", "ownership_mode": "observable"}
```

Response when work is available:

```json
{
  "status": "claimed",
  "task_id": "S0-01",
  "claim_token": "opaque-token",
  "prompt": "Implement the requested task.",
  "worktree_path": "/absolute/path",
  "acceptance_checks": ["cargo test --workspace"],
  "ownership_mode": "observable",
  "lease_expires_at": null
}
```

Response when no work is available:

```json
{"status": "idle"}
```

### `submit_result`

Request:

```json
{
  "task_id": "S0-01",
  "claim_token": "opaque-token",
  "outcome": "completed",
  "summary": "Implemented the task.",
  "commit_sha": "git-sha",
  "blockers": [],
  "proposed_followups": []
}
```

Response:

```json
{"recorded": true, "state": "submitted"}
```

Submission is not acceptance. The controller runs checks and decides whether
to merge, retry, or move the task to planning.

## Journal boundary

The harness talks to the journal through a small implementation-neutral JSON
protocol over a local socket. Harness code must not import SQLite classes or
depend on table layout.

The Python service is temporary. The Rust service later implements the same
protocol so the backend can be swapped without changing worker prompts,
stage manifests, or controller behavior.

## Durable state

The journal persists only externally meaningful orchestration state:

- runs
- tasks and dependencies
- claims, ownership mode, and optional lease expiry
- submissions and acceptance state
- append-only operator/controller events
- worker-proposed blockers and follow-up work

Subprocess implementation details are not journal state.

## Ownership and recovery

Claims use an opaque token. Only the active token may submit. Local macOS
workers use `observable` ownership with no expiry: the controller directly
observes each worker process, atomically fences its claim when that process
disappears, terminates any orphaned Codex child for the task worktree, and
immediately starts a replacement worker. A successful submission releases
ownership before controller verification begins.

An expiry exists only when a caller explicitly requests `lease` ownership for
a distributed or otherwise ambiguous worker whose liveness cannot be directly
observed, supplying both `"ownership_mode": "lease"` and a positive
`lease_seconds`. Controller restart reconstructs ownership from the journal and
reconciles it against the currently observed local worker processes.

## Planning authority

Only the controller may create executable tasks. Workers may report blockers,
missing prerequisites, decomposition suggestions, and follow-up proposals.
Those proposals are stored but do not enter the queue automatically.
Expansion occurs only during an explicit planning phase.

## Git isolation

Each coding task receives its own branch and Git worktree. Workers never
write directly to the target repository's main checkout. The controller runs
acceptance checks against the submitted commit and merges only after success.

## Observability

Each real worker runs in its own visible terminal emulator window. Standard
Codex output and tool activity remain visible. The harness does not create a
custom trace-capture or terminal-multiplexing system.
