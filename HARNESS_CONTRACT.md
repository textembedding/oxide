# Harness Contract

## Architectural boundary

The harness runs the memory roadmap while dogfooding the exact interface that
the production Rust journal kernel will replace. The Python kernel is temporary;
its public abstraction is fixed:

1. `journal_add(namespace, author, text)` appends one immutable generic record;
2. `journal_search(namespace, query)` returns generic matching records.

`src/swarm_harness/journal.py` owns only that append-only store, its generic
search, and transport. It contains no task, PR, review, reviewer-count,
verification, merge, generation, dependency, ownership, or lifecycle concept.
It is the only module that imports SQLite.

`src/swarm_harness/workflow.py` is a pure ordered-log reducer above the kernel.
It interprets generic records into the current swarm projection. The CLI,
worker adapter, and MCP facade use that reducer; none may add workflow policy to
the journal kernel. A future workflow feature that seems to require changing
`journal.py` requires a workflow-layer redesign instead.

Because the kernel never rejects application semantics, competing claims are
all durably appended. Deterministic record order makes the first valid claim
effective; later conflicting records have no projected effect and the workflow
facade reports an error to their callers.

Queue search is never a reservation. Multiple workers may observe the same
ready author, revision, internal-review, or merge item. Every role submits the
item's existing `claim:` text through `journal_add`; the workflow reducer uses
the same ordered replay path to accept one owner. A loser searches again. No
role introduces a lease, cursor, preassignment, or alternate tool operation.

## Exact worker interface

Every Codex invocation receives one required MCP server named `journal`. Its
tool list contains exactly `journal_search` and `journal_add`. Both accept a
single `yaml` string argument. Workers never receive claim, review, merge, or
completion APIs.

Seek or recover work:

```yaml
query: queue:ready
```

```yaml
query: worker:worker-0
```

Claim the exact returned work identity:

```yaml
text: claim: task:S0-STABLE-SEAMS
```

The same generic append operation carries review claims, merge claims,
checkpoints, handoffs, PR openings, decisions, and merge requests. Searches
return workflow projections plus the generic record history required to resume
work.

## Per-task PR workflow

Each mutating task has one dedicated branch. No shared integration branch
exists.

1. An author claims a ready task, edits its branch, records a checkpoint, runs
   every task check, commits and pushes, then records a handoff.
2. The author opens an internal PR by appending its exact branch, base, head,
   and `verified: true`. This creates one immutable candidate generation.
3. The workflow creates the configured number of read-only internal reviews.
   The default three roles are specification, adversarial, and integration.
   Reviewers must be distinct from the author and from one another for that
   generation. Each checks the complete exact-head diff and runs every task
   check before approving or challenging.
4. Any challenge invalidates the remaining review work and returns the task to
   revision. The revised head is a new generation and requires the complete
   configured review count again.
5. Only a fully approved generation exposes merge work. A worker claims it and
   appends the exact generation and candidate head as merge authorization.
6. The thin launcher proves that the task branch still equals the approved
   head, constructs a prospective merge into the current target branch, runs
   every task check there, and then merges that exact tree to the target branch.
   A conflict, changed head, or failed check returns the task to revision.
7. A dependency becomes ready only after its prerequisite PR is merged. After
   every task has merged, the launcher runs the stage gate in the target
   checkout; only its passing publication record completes the run.

The review count is configured when the run is staged:

```bash
./swarmctl harness run --workload stage0 --target /path/to/memory \
  --workers 7 --reviews 3
```

At least `reviews + 1` worker slots are required so one author and the required
distinct reviewers can make progress. Roles are otherwise flexible: every new
session seeks whichever author, revision, review, or merge item is ready. The
separate external/independent-review provider path is intentionally omitted.

## Ownership and recovery

A worker owns at most one projected work item at a time. Ownership has no fixed
duration and no lease timer. The launcher directly observes host-worker
liveness and restarts a missing stable slot. The replacement searches
`worker:<slot>`, reconstructs the exact claim from generic records, and resumes
the existing clone immediately. Pause stops processes without expiring or
transferring ownership. Reset is the explicit operation that archives an entire
campaign.

## Thin launcher

The persistent process performs only host and Git effects that models cannot
make authoritative themselves:

- serve the generic journal prototype;
- create worker clones and supervise the configured native processes;
- consume an approved worker merge request;
- verify the exact prospective merge tree and perform that merge to the target
  branch;
- run the final stage gate and record publication;
- stop on pause or completion.

It does not select tasks or reviewers, decide semantic correctness, waive a
review, change dependencies, invent candidate generations, or authorize a
merge without the required worker approvals.

## Observability

Codex emits JSONL directly into each worker log. The observer renders model
messages, visible reasoning summaries, commands, diffs, and both journal tool
calls with terminal-safe syntax coloring. The queue is a disposable workflow
projection, not journal authority. It is single-column, at most 40 characters
wide, prioritizes active work, and omits blocked tasks.
