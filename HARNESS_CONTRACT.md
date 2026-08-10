# Harness Contract

## Responsibility boundary

- **Repository:** authoritative workload, roadmap/specification, and product code.
- **Harness:** scheduling, Git/worktree management, process control, prompts,
  workflow replay, and destructive-rewind coordination.
- **Journal:** durable generic records containing workflow events and swarm-created
  discoveries, decisions, failures, reviews, and evidence.
- **Journal backend:** replaceable implementation of one configured ADD/SEARCH
  contract.

Repository specification text is never copied into the journal as authority. A
journal record may cite a repository path, commit, or blob, but agents inspect
the checked-out repository with ordinary repository tools.

## Fixed journal boundary

The journal backend has exactly two immutable operations:

1. `journal_add(namespace, author, text)` synchronously appends one generic
   record.
2. `journal_search(namespace, query)` returns one bounded union of qualifying
   generic records.

`src/swarm_harness/journal.py` is the disposable Python prototype and the only
module that imports SQLite. `src/swarm_harness/journal_backend.py` is the only
harness module allowed to import that prototype. Every other runtime component
depends on the `JournalPort` two-operation protocol.

The backend has no task, claim, review, verification, generation, merge,
dependency, ownership, retry, or lifecycle semantics. `workflow.py` derives all
of them by deterministic replay. Semantic records are not an advisory subsystem:
they occupy the same store and are returned by the same SEARCH operation.

## Backend transport and frozen configuration

An external command receives:

- `SWARM_JOURNAL_DATABASE`: durable store path;
- `SWARM_JOURNAL_SOCKET`: Unix socket path;
- `SWARM_JOURNAL_MIN_EXACT`: configured exact-match floor;
- `SWARM_JOURNAL_MAX_RESULTS`: configured response cap.

The socket accepts one JSON request per connection and returns one JSON response.
Requests have `request_id`, `operation`, and `arguments`; the only legal
operations are:

```json
{"operation":"journal_add","arguments":{"namespace":"...","author":"...","text":"..."}}
{"operation":"journal_search","arguments":{"namespace":"...","query":"..."}}
```

Responses echo `request_id` and contain either `{"ok":true,"result":...}` or
`{"ok":false,"error":"..."}`. ADD returns `saved` and the stable journal
sequence. SEARCH records expose the immutable text plus namespace, author,
creation time, stable record identity, journal sequence, and whether the match
was exact or semantic. Workflow routing, run, and epoch metadata remain generic
stored text and are surfaced separately by the workflow-facing client.

The defaults are `min_exact = 5` and `max_results = 10`. An authoritative
harness run requires `1 <= min_exact <= max_results`. The effective pair is
frozen in run metadata and the qualification receipt, supplied whenever the
backend starts, and reused after restart. There is no operation for discovering
or mutating it mid-run.

## Bounded SEARCH

Exact and semantic retrieval share one search space. Exact substring matches
are always eligible. Other records are eligible only when their semantic score
meets the backend's inclusive threshold; score controls eligibility only.

For one query, let:

```text
required_exact = min(min_exact, number_of_exact_matches, max_results)
```

SEARCH guarantees all of the following:

1. It returns at most `max_results` records.
2. It reserves at least `required_exact` exact records, choosing the most recent
   exact anchors when a choice is required.
3. If fewer than `min_exact` exact records exist, it returns every exact record.
4. It fills remaining capacity with the most recent other qualifying exact or
   semantic records.
5. A record qualifying both ways appears once and counts as exact.
6. After selection, the complete union is ordered strictly oldest-to-newest by
   stable journal sequence. Score never affects selection or position.
7. More qualifying records than capacity is normal: SEARCH returns useful
   partial evidence, never an overflow error.

SEARCH does not promise exhaustiveness, an overflow flag, a cursor, or
pagination. Ordinary clients use returned identifiers and concepts for
iterative, multi-hop searches. Exact replay is complete without any of those
features.

## Complete deterministic replay from bounded SEARCH

Every workflow append stores a private run-specific replay root, run and epoch
identity, a stable identity, and one unique fixed-width replay leaf. The full
leaf spelling also makes the record an exact match for every prefix on its path.

Recovery starts at the replay root. At each partition it ignores semantic extras
when deciding whether the partition exists. If at least one exact anchor is
present, it visits every deterministic child; otherwise that partition is empty.
Traversal continues to unique leaves, then deduplicates by stable identity and
sorts by journal sequence before projecting workflow state.

This remains complete when the backend is configured with `min_exact = 1`: one
exact anchor proves a nonempty branch, and every child of that branch is still
visited. Correctness does not depend on receiving every exact match at once, a
large result cap, relevance order, an overflow signal, wildcard history scans,
or pagination. Startup and crash recovery replay stable history while holding
the run's existing serialization lock before accepting a new authoritative
workflow append.

A record returned because it is semantically related cannot affect workflow
state unless its namespace, run, epoch, schema/routing metadata, stable identity,
and journal sequence validate during replay. Visibility is not authority.

## Product-owned workloads

The CLI resolves a workload only from
`<target>/swarm-harness/<workload>.yaml`. At run creation it freezes:

- target repository identity;
- base Git commit;
- workload path;
- workload Git blob identity;
- the target's complete `swarm-harness/` tree identity;
- harness implementation identity.
- the target repository's configured Git author/committer identity.

The bootstrap record contains only this reference, never serialized workload or
specification text. Each reconstruction loads the exact workload blob from Git
and verifies that the target's current `swarm-harness/` tree is unchanged. A
candidate that changes that directory is rejected mechanically; an intentional
specification change starts a new run.

Workloads fail closed when they are missing, disabled, uncommitted, malformed,
cyclic, path-escaping, or changed after run creation. Task identifiers are safe
and unique, dependencies remain within one acyclic graph, and every task
supplies at least one exact check command. `stage_gate` is a required list of
additional final integration commands and may be empty.

The frozen target identity supplies both author and committer metadata for all
worker and prospective merge commits.

Runtime state remains in this checkout under `.swarm/`. No databases, logs,
sockets, worker clones, or assignments are generated into the target repository.

## Permissionless workflow

Queue search is not a reservation. All roles race by appending ordinary claim
records; deterministic replay makes one claim effective and losing workers
search again. Implementation, review, verification, candidate invalidation,
merge authorization, dependency release, and completion all remain workflow
semantics above the journal.

Each mutating task has its own branch. Authoring produces and pushes an immutable
candidate before acceptance checks begin. Publication exposes the configured
reviews and one assignment for every declared check at the same time. Reviewers
inspect the diff and repository contract and may run targeted diagnostics, but
review does not imply acceptance-command execution.

The existing `verify:<task>:<candidate-commit>:<check-ordinal>` claim is the
single acceptance requirement identity. Its ordinal resolves the exact command from
the frozen workload; the immutable commit resolves its tree; the run and
external epoch resolve workload and qualification authority; and commands run
from the checked-out repository root. Atomic claim replay selects one owner, and
the qualified harness process—not a model instruction—executes that command in
an isolated clone. One `verify-pass` or `verify-fail` terminal record bound to
the same identity satisfies the attempt. A durable result is never scheduled
again after restart.

When a frozen check declares `receipt_required`, its exact requirement also
binds that policy. The command must emit one bounded regular JSON-object receipt
through `SWARM_EVIDENCE_RECEIPT`; the harness content-addresses it with the other
attempt artifacts. Absence, malformed JSON, a non-regular file, or an oversized
receipt converts the attempt to infrastructure failure regardless of exit code.
The same generic rule applies to a frozen prospective-tree checker through
`prospective_receipt_required`.

Acceptance requires every declared check to pass and every configured review to
approve the same candidate. A real command failure or rejecting review makes
that immutable candidate revision-required and obsoletes its remaining work; a
new commit creates new check identities even when command text is unchanged.
The thin launcher verifies repository and prospective-tree invariants in an
isolated clone without rerunning candidate checks, then imports only that exact
commit object. Dependencies release after merge, and only additional commands
listed in `stage_gate` run before completion. There is no integration branch or
persistent semantic orchestrator, and every model role starts a fresh context.

On a local machine, directly observed process liveness drives immediate crash
reclamation; ownership is not kept alive by a long fixed-duration lease. A dead
winner is reclaimed only after its process can no longer execute. If it had
durably published a terminal result, replay suppresses replacement execution;
otherwise a replacement may run. A live process that loses connectivity retains
its completed terminal text and retries publication without rerunning the
command.

## Administrative destructive rewind

Rewind is explicit operator action, never scheduler or crash-recovery policy. It
pauses the run, obtains exclusive ownership, stops launcher, workers, and
observers, restores the checkpoint's real journal database and sequence, Git
frontier, task refs, worker state, assignments, and artifacts, then increments a
run epoch stored outside the rewound journal history. Assignments are rewritten
for the new epoch before restart. Observers reopen from the beginning on epoch
change, and stale-epoch workers cannot append claims or terminal results.
External epoch frontiers bind every restored sequence interval to the epoch that
was authoritative when it was written, so a rejected stale record cannot become
valid after a later rewind.

The operation refuses to overwrite any uncommitted target worktree changes.
Archiving discarded state is optional; synthetic monotonic sequence preservation
is forbidden.

## Explicit non-goals

The current harness does not provide remote workers, distributed failure
detection, hostile-code containment, hosted pull-request objects, deployment or
production authority, automatic conversion of prose into task graphs, or
language/framework-specific implementation policy. It does not journalize or
live-migrate repository specifications, split semantic and deterministic memory,
offer per-query search modes, scan history with wildcards, or depend on early
pagination. Archived demo campaigns have no compatibility guarantee.

Review quorum, shared exact-check execution, candidate invalidation, merge
authorization, dependency release, and completion are workflow semantics—not
journal-backend features. There is no check cache, evidence database, lease
service, or second claim protocol.

## Qualification and honest compatibility

Every workload binds a current multiprocess qualification receipt, including the
backend implementation and effective capacity pair. The campaign races all
workflow roles, injects winning-worker crashes, audits protected work, compares
independent replay digests, and recovers a history much larger than one bounded
response.

`tests/test_journal_conformance.py` is the backend-neutral contract suite. It
runs against the Python MVP by default and can run unchanged against an external
adapter through `SWARM_CONFORMANCE_JOURNAL_COMMAND`. The port and launch boundary
are replaceable now; no external backend is honestly drop-in compatible until
that same suite and the concurrency campaign pass against it.

The complete model-free repository suite is `./swarmctl verify`. It enforces the
Git, journal, workflow, review, verification, merge, recovery, rewind, reset,
responsibility-boundary, and backend-conformance contracts described here.
