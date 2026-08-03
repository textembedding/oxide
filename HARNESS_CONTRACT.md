# Harness Contract

## Purpose

The harness runs the memory roadmap while dogfooding the exact interface that
the production Rust journal kernel will replace. The Python implementation is
temporary and disposable. Its public coordination surface is permanent:

1. search journal state and text;
2. append journal text atomically.

## Exact worker interface

Every Codex invocation receives one required MCP server named `journal`. Its
tool list contains exactly `journal_search` and `journal_add`. Both accept a
single `yaml` string argument.

Seek ready work:

```yaml
query: queue:ready
```

Recover the stable worker slot:

```yaml
query: worker:worker-0
```

Claim one task atomically:

```yaml
text: claim: task:S0-STABLE-SEAMS
```

Persist implementation context:

```yaml
text: |-
  checkpoint: task:S0-STABLE-SEAMS
  Added the seam schema and initial goldens.
```

Publish the self-verification handoff:

```yaml
text: |-
  handoff: task:S0-STABLE-SEAMS
  Files: ...
  Checks: ...
  Pushed commit: ...
```

Complete after pushing the verified commit to the shared integration ref:

```yaml
text: |-
  complete: task:S0-STABLE-SEAMS
  commit: 0123456789abcdef0123456789abcdef01234567
  verified: true
```

Claims, checkpoints, handoffs, and completions are ordinary journal text. The
prototype interprets their exact first lines atomically. There are no worker
registration, heartbeat, submission, vote, release, lease, or acceptance APIs.

## Swappable backend

`src/swarm_harness/journal.py` is the only module containing journal semantics
or SQLite. It owns the run, task, and append-only entry projections and accepts
only operations named `journal_add` and `journal_search` over its local socket.
`journal_mcp.py` is the exact MCP facade used by Codex.

The launcher and host worker adapter use the same two operations. Replacing the
Python service with Rust therefore requires implementing those two operations,
not preserving a Python table layout or private lifecycle protocol.

## Worker ownership and recovery

Each worker slot owns one persistent independent Git clone. A worker claims at
most one task at a time. The claim has no duration and no lease timer. If Codex
or its host worker exits, the thin launcher starts the same slot again. The
replacement searches `worker:<slot>`, finds the claim, searches the task's
entries, and resumes the same clone immediately.

Pause stops all processes but preserves claims and clones. Reset is the only
operation that discards an active campaign, and it first moves the entire run
directory into the local archive.

## Self-directed integration

Workers perform their own task checks, commit their changes, fetch the shared
integration ref, rebase, rerun checks, and push. A rejected push means another
worker integrated first; the worker fetches, rebases, rechecks, and retries.
There is no central conflict-resolution or semantic merge actor.

The journal admits completion only from the claiming worker and only after that
worker has appended both checkpoint and handoff records plus an exact 40-byte
hex commit and `verified: true`. Completing a task makes its dependants ready.
Completing every task moves the run to `publishing`. The launcher proves that
every journaled task commit is an ancestor of the shared integration tip, then
fast-forwards the originally staged target branch from its exact base to that
tip. Only the resulting `control: published` journal entry makes the run
complete. A changed branch, tracked target edit, missing commit, non-linear
history, or failed fast-forward leaves the run uncompleted.

## Thin launcher

The persistent process has five mechanical jobs:

- start the Python journal prototype;
- create missing worker clones and the integration ref;
- ensure the configured worker slots have live host processes;
- publish the exact completed integration tip with a checked fast-forward;
- stop when the journal reports pause or completion.

It does not choose tasks, reclaim claims, judge implementation correctness,
resolve conflicts, create merge commits, change dependencies, or mark tasks
complete.

## Observability

Codex emits JSONL directly into each worker log. The observer renders model
messages, visible reasoning summaries, commands, file changes, and both journal
tool calls with terminal-safe syntax coloring. The queue view is a disposable
projection. It is single-column, at most 40 characters wide, prioritizes
working tasks, and omits blocked tasks.
