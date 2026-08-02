# Harness Contract

## Purpose

The harness coordinates coding workers implementing Stages 0–3 in a
separate target repository. It dogfoods a minimal Python journal prototype
while those workers build the production Rust kernel that will replace it.

## Worker-visible tool surface

The Codex worker receives exactly two journal tools. MCP startup is mandatory;
the invocation fails if this closed surface cannot initialize.

### `journal_search`

Request:

```json
{"yaml": "query: task:S0-01"}
```

Response:

```yaml
matches:
  - journal_id: 17
    author_kind: seed
    task_id: S0-01
    body: "task:S0-01 ..."
```

Search is a bounded, literal projection over authorized durable entries. It is
for context recovery only and never grants lifecycle authority.

### `journal_add`

Request:

```json
{
  "yaml": "text: |-\n  checkpoint: task:S0-01\n  durable state"
}
```

Response:

```yaml
saved: true
journal_id: 18
```

Free text records observations, checkpoints, and handoffs. It cannot claim,
submit, accept, retry, vote, or complete a stage. Submission requires at least
one `journal_search`, an exact `checkpoint: task:<id>` entry, and an exact
`handoff: task:<id>` entry from the current fenced attempt.

The host adapter performs atomic claim, typed submission, and validation-vote
transitions outside the model-visible process. Those private calls are not
worker tools and cannot be invoked through MCP. Submission is not acceptance;
it opens a proposal for independent validation.

## Journal boundary

The harness talks to the journal through a small implementation-neutral JSON
protocol over a local socket. Harness code must not import SQLite classes or
depend on table layout.

The Python service is temporary. The Rust service later implements the same
protocol so the backend can be swapped without changing worker prompts,
stage manifests, or launcher behavior.

## Durable state

The journal persists only externally meaningful orchestration state:

- runs
- tasks and dependencies
- claims, ownership mode, and optional lease expiry
- submissions, proposals, validation claims, votes, and committed decisions
- append-only operator/launcher events
- worker-proposed blockers and follow-up work

Subprocess implementation details are not journal state.

## Ownership and recovery

Claims use an opaque token. Only the active token may submit. Local macOS
workers use `observable` ownership with no expiry: the launcher directly
observes each worker process, atomically fences its claim when that process
disappears, terminates any orphaned Codex child for the task worktree, and
immediately starts a replacement worker. A successful submission releases
implementation ownership before independent proposal validation begins.

An expiry exists only when a caller explicitly requests `lease` ownership for
a distributed or otherwise ambiguous worker whose liveness cannot be directly
observed, supplying both `"ownership_mode": "lease"` and a positive
`lease_seconds`. Launcher restart reconstructs ownership from the journal and
reconciles it against the currently observed local worker processes.

## Proposal and decision authority

There is no privileged planning or acceptance actor. Any worker may open a
closed proposal for candidate acceptance, retry, task decomposition, or a
dependency change. The journal admits at most three validators, excludes the
author, accepts one vote per worker, and commits on two matching votes. A split
therefore requires the third validator. Rejected candidate-acceptance proposals
atomically queue a retry. Approved graph proposals atomically change the task
graph. When all tasks are accepted, the same mechanism validates and commits
stage completion.

Direct `accept_task`, `reject_task`, and terminal `set_run_state` operations are
fail-closed. Neither a launcher process nor a worker process can bypass quorum.

## Git isolation

Each coding task receives its own branch and Git worktree. Workers never write
directly to the target repository's main checkout. Independent validators run
the task checks against the exact clean candidate. The launcher may merge only
a proposal whose quorum decision is already committed in SQLite, and it reports
the mechanical merge result back to the journal.

## Thin launcher boundary

The long-lived local process has exactly four jobs: serve the journal socket,
observe worker process liveness, prepare journal-authorized worktrees, and apply
already-committed Git merges. It does not run acceptance checks, choose retry,
change dependencies, create tasks, or mark a stage complete.

## Observability

Each real worker runs in its own visible terminal emulator window. Standard
Codex output and tool activity remain visible. The harness does not create a
custom trace-capture or terminal-multiplexing system.
