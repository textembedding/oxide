# Oxide architecture contract

This document defines Oxide’s durable responsibility boundaries and acceptance
invariants. It is normative for the harness. Concrete schemas, field lists, CLI
parser details, and internal module layouts are enforced by code and tests rather
than duplicated here.

The [verification policy](VERIFICATION_PRIMER.md) is normative for planning,
contract generation, admission, proof execution, and merge. It supplies universal
assurance invariants independently of target product specifications.

The terms **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative.

## 1. Responsibility boundary

Oxide collaboratively derives a staged roadmap and Rust/Verus implementation
contract from a target's human-readable specifications, then turns an approved
multi-phase contract into an authoritative Git tree with maximal useful parallelism.

- The **target repository** owns product requirements, approved roadmap, verification
  contract and DAG, production code, abstract models, implementation
  contracts, proof sources, coverage manifest, pinned toolchain, and empirical
  subjects.
- **Oxide** owns contract validation, the deterministic verification judge,
  scheduling, isolated Git execution, process authority, prompts, review,
  evidence execution, exact-tree merge gating, replay, and rewind.
- Oxide's universal verification-policy digest is a judge input bound by planning
  approval, generated contracts, admission results, frozen runs, and proof
  evidence. A target specification need not repeat that policy and cannot weaken it.
- The **journal** stores generic immutable records created by the swarm: workflow
  events, discoveries, decisions, failures, reviews, and evidence.
- The **journal backend** implements one configured ADD/SEARCH contract and MUST
  remain ignorant of tasks, roles, claims, reviews, proofs, merges, and retries.

The target MUST NOT contain an Oxide runtime directory or depend on Oxide source.
Repository files remain the authoritative specification. Journal records may cite
paths and Git identities, but copied specification prose cannot become authority.

Machine proof establishes implementation-to-contract refinement. It does not
establish that a generated contract faithfully captures prose. Contract alignment
is therefore a separate, explicit admission gate.

## 2. Planning, contract alignment, and admission

The target's natural-language specification set is the sole human-readable
semantic authority for program behavior and success. `ROADMAP.md` is the canonical
staged plan derived from those specifications. A generated phase contract is a
machine-executable derivation, not a second source of product intent.

`oxide harness plan --target <specification-directory>` starts a collaborative
planning-agent session. The agent MUST read the complete Markdown corpus, preserve
an explicit source-defined boundary when one genuinely exists, otherwise derive
an arbitrary number of coherent phases from capabilities and dependencies, explain
scope and dependencies, and accept free-form user pushback. It MUST NOT assume
that specifications use planning phases or a fixed numbering scheme. Current,
future, deferred, and blocked work MUST remain represented without inventing
missing semantics. It MUST write no roadmap until the user explicitly approves
the current exact proposal. The standardized roadmap MUST give every phase a stable identifier,
outcome, included and deferred scope, dependencies, exact source requirements,
applicable global invariants, implementation and verification goals, and readiness.
Roadmap approval does not make every phase contractible; only a phase marked
`ready` may enter contract generation.

The roadmap file MUST remain human-first without weakening that schema. Oxide
renders a deterministic Markdown overview and phase-by-phase view from the
validated embedded TOML, then places the authoritative TOML in a collapsed
machine-data section. Agent-authored prose outside the schema block is discarded.
The rendered Markdown has no independent semantic authority and MUST be
regenerated whenever the machine data changes.

When `plan` receives one or more `--update <stage-id>` arguments, it MUST operate
as constrained maintenance rather than whole-plan regeneration. The existing
approved roadmap is the baseline. Phase identity and order, top-level fields,
global invariants, and every unselected phase MUST remain unchanged. Oxide MUST
collect a concrete update request before invoking the agent, display changed
fields and approval impact, and write nothing before explicit approval. A
readiness-only change invalidates only the changed phase approval. Any other
phase change also invalidates approvals for its transitive dependents. Adding,
removing, renaming, or globally restructuring phases requires a new full planning
session.

`oxide harness generate-contract <ROADMAP.md>` first presents a dependency-aware
phase selector. Only ready phases may be checked, and each dependency must be
checked before its dependent. After confirmation, one contract-agent session
generates one contract spanning the checked phases. The agent MUST resolve their
exact source requirements and applicable global invariants, explain the proposed
executable tasks and verification goals, and accept free-form user refinement.
Agent contractibility attestation and user approval are separate and neither
substitutes for the other.

Before a contract can be admitted, its generation agent MUST inspect the complete
declared specification set and classify it as aligned or not aligned. Ambiguity,
missing acceptance criteria, unsupported assumptions, and other semantic gaps MUST
remain explicit. The agent MUST propose concrete prose revisions instead of
silently choosing an interpretation. A revision becomes authoritative only after
the user approves it, it is written back to the specification, and the exact
source/roadmap/contract set is committed before admission. The contract MUST be
regenerated from exactly that approved source content.

Any approved clarification MUST be written back into the source specification,
reflected in the selected roadmap stage, and followed by complete contract
regeneration. Every generated goal, task, and acceptance check MUST cite an exact
requirement in the selected stage's semantic closure. Its trace MUST contain both
those citations and the exact generated goal, task, and check content. Mechanical
derivations MAY add enforcement details, including dependency edges, source
classifications, proof obligations, evidence identity, and recovery checks. They
MUST NOT add or change product behavior, failure behavior, or success semantics.
Citation identity MUST preserve wording, case, punctuation, links, and code while
normalizing presentation-only Markdown such as soft wrapping, indentation, list
markers, heading markers, emphasis, and line-ending convention.

Admission requires three strict machine-readable inputs:

- the user-approved `ROADMAP.md` and selected phase closure;
- an agent attestation that the stage is contractible, all gaps are resolved, and
  the derivation introduces no product semantics absent from approved sources;
- explicit user approval of the current phase meanings, generated contract, and
  verification goals.

The attestation and approval are bound inside `contract.toml` to its semantic
payload. They are evidence of exact decisions, not additional product semantics.
Oxide recomputes mechanical qualification at admission; it does not trust a
qualification assertion stored by the generator. Deterministic validation proves
identity, trace closure, and absence of unresolved fields; the agent and user
remain responsible for the natural-language entailment judgment.

Oxide MUST validate alignment before backend qualification. It MUST then complete
contract and toolchain qualification in disposable admission state before creating
a run directory, journal, worker, observer, or paid workload process. A changed
selected stage, applicable global invariant, cited source section, contract, trace,
or alignment policy invalidates approval and requires regeneration. The full
repository revision remains provenance, but an edit confined to an explicitly
deferred later-stage requirement does not invalidate an earlier stage unless it
changes that stage's dependency or global-invariant closure. No live specification
migration exists.

## 3. Target contract and frozen run identity

The default entry point is `<target>/verification/contract.toml`. Another
target-relative path under `verification/` MAY be selected explicitly.

The contract MUST identify:

- its run identity, goal, minimum review count, and evidence policy;
- immutable product, verification, manifest, and toolchain inputs;
- production, contract, abstract-model, proof, trusted-adapter, and
  non-authoritative roots;
- production features, target, entry point, and composition theorem;
- one acyclic implementation DAG with at least one evidence requirement per task;
- bounded execution, artifact, and receipt policies;
- the selected roadmap phases, applicable global invariants, exact cited source
  requirements, embedded attestation and approval, and one or more source
  citations for every generated semantic unit.

Formal checks select an Oxide-supported Verus operation and target proof root.
They MUST NOT supply an alternate verifier command. Explicit command checks are
supplementary empirical evidence and MUST NOT satisfy a formal proof obligation.

At run creation, Oxide MUST freeze at least:

- target repository identity, base commit, and Git author identity;
- contract path, blob, and every immutable input blob;
- a canonical immutable-closure digest;
- Oxide implementation, verification engine, and evidence-schema digests;
- effective execution and evidence policy;
- journal capacity and backend qualification receipt.

The immutable closure MUST be materialized outside candidate worktrees. A
candidate that changes a frozen input fails closed. Specification, contract,
toolchain, judge, or policy changes require a new independently qualified run;
there is no live contract migration.

## 4. Verification authority

Oxide, not the target candidate, constructs and executes authoritative Verus
commands. The bundled judge MUST:

- use the exact pinned Verus context and deterministic solver limits;
- enforce a non-weakenable anti-cheat and anti-vacuity floor;
- validate source classification, production/proof parity, proof reachability,
  component refinement, composition coverage, assumptions, and trusted boundaries;
- distinguish product failure from infrastructure failure;
- emit and validate bounded, integrity-protected proof evidence.

The target MAY add restrictions and declare semantic roots, theorem names,
features, and target assumptions. It MUST NOT replace command construction,
weaken mandatory policy, define its own authoritative receipt validator, or let a
candidate-defined judge approve the same candidate.

For targets governed by pervasive verification:

1. Every production logical component is Verus-verified.
2. The only production-code exemption is a narrow, policy-free trusted effect
   adapter with an explicit contract.
3. Genuinely non-authoritative tooling cannot affect production authority.

There is no typed-but-unverified or low-risk production category. Uniform
coverage does not require uniform proof size, but every contract MUST be
meaningful and connected to the exact-tree composition theorem.

Agent review, fixtures, fuzzing, crash campaigns, and benchmarks supplement proof
but cannot replace it. Formal correctness and empirical capacity are independent
release gates.

## 5. Candidate and integration lifecycle

The authoritative lifecycle is:

```text
implementation and proof development
    → immutable candidate commit and tree
    → exact-tree deterministic policy qualification
    → concurrent proof execution and independent review
    → accepted candidate
    → exact prospective authoritative tree
    → complete policy and composition gate
    → authoritative Git frontier
```

Candidate publication MUST first enter a non-claimable qualification state. Oxide
MUST run the frozen deterministic policy against that exact candidate commit and
tree. The resulting integrity-protected receipt MUST bind the run, epoch, frozen
contract and judge, task, generation, candidate base/commit/tree, operation, and
qualified execution environment. Only a passing receipt may make independent
review slots and unsatisfied evidence slots claimable. A product-policy failure
returns the task to revision with a bounded diagnostic; an infrastructure failure
blocks it for explicit recovery. Neither outcome may fan out review or check work.

Policy qualification MUST cover every governed production, contract,
abstract-model, proof, trusted-adapter, fixture, fuzz, and tooling path through
the applicable classification or closure rule. Every production or adapter
source and every file under a non-authoritative fixture, fuzz, or tooling root
MUST have exactly one valid coverage-manifest owner. Models and proof sources MUST
satisfy the declared proof-closure rules. Merely placing a file below a configured
root satisfies none of these obligations. These invariants apply to every
candidate, including an otherwise honest partial implementation.

After qualification, candidate publication makes every independent review slot
and every unsatisfied evidence slot claimable at the same time. The author is not
required to serially execute the acceptance list before publication.

Review and proof execution answer different questions. Review evaluates
product-model fidelity, implementation-proof connection, non-vacuity,
maintainability, and the systems/trust boundary. Reviewers consume shared evidence
and MUST NOT rerun the acceptance list merely for role symmetry. Review never
substitutes for Verus.

A candidate is acceptable only when every required evidence slot passes and
every required review approves that same immutable candidate. A product-failing
check or rejecting review makes the candidate revision-required and obsoletes its
remaining unclaimed work. A revision creates a new commit/tree and new evidence
requirements.

Merge MUST occur through an isolated prospective authoritative tree constructed
from the current target frontier and accepted candidate. Oxide MUST run policy and
the complete composition theorem against that exact tree, import the exact verified
Git object, and advance the target only to that object. Dependencies release only
after this integration succeeds.

## 6. Exact shared evidence

> Acceptance is check-granular, evidence is shared through the journal, and
> execution occurs only for an unsatisfied evidence slot.

An evidence identity MUST bind every input that can affect validity, including:

- run, external epoch, frozen contract closure, and judge identity;
- task, exact candidate base/commit/tree, and prospective tree where applicable;
- check slot, formal operation or canonical command, working directory, declared
  environment, artifacts, and receipt policy;
- qualification context, timeout, failure classification, and evidence limits.

Before execution, workers recover any valid terminal result. If none exists,
ordinary journal records claim the stable evidence identity. Deterministic replay
selects exactly one winner and one qualified process attempt. Only that attempt
may execute. Losing claimants MUST NOT perform the protected command.

The harness-owned process publishes the terminal result; a worker’s self-reported
pass is never proof evidence. Results distinguish passed, product/assertion
failure, infrastructure failure, and abandoned/replaced execution.

A product failure is evidence against the immutable candidate and is not retried
until revision. Infrastructure failure permits replacement only after recovery
establishes that the old process can no longer publish authority. A durable result
survives restart and MUST NOT execute again for the same exact identity.

Logs and artifacts MUST be bounded and stored outside routing metadata using
content digests. A required machine-readable receipt is bounded, regular, and
validated against the frozen judge.

## 7. Global productive frontier

Workers MUST NOT be partitioned into fixed implementation, review, or verification
pools. The scheduler derives one global frontier containing:

- dependency-ready implementation and revision work;
- independent review slots on published candidates;
- unsatisfied evidence slots on published candidates;
- required merge and integration work.

Each worker owns one assignment in one fresh model context. The scheduler
recomputes the frontier after every terminal event, avoids duplicate satisfied
evidence, prioritizes work that unlocks the critical path, and prevents review or
proof starvation. It MUST NOT leave a worker idle while eligible productive work
exists or manufacture redundant checks to inflate utilization.

The process limit is currently 64 workers. The target DAG and live review/evidence
frontier determine useful width.

## 8. Journal port, bounded search, and replay

The backend exposes exactly two operations:

1. `journal_add(namespace, author, text)` appends one immutable generic record and
   returns stable identity and journal sequence.
2. `journal_search(namespace, query)` returns one bounded union of qualifying
   generic records.

Only the backend adapter may import the disposable Python prototype. Workflow
semantics are derived above the port by deterministic replay.

The Python prototype deliberately dogfoods two planned production retrieval
components behind this unchanged port. Its Exact index maps generic aligned byte
fingerprints to record identities and always confirms candidates against the
immutable source text. Its lexical index maps generic case-folded terms to record
identities, uses the threshold predicate to derive a sufficient candidate cover,
and confirms full eligibility before selection. Neither index stores or interprets
workflow fields.

Both indexes are versioned, rebuildable projections of the ordered record table.
They are published in the same transaction as an acknowledged append, checked on
restart, and rebuilt from immutable records when absent, stale, or incomplete.
Their postings, counts, fingerprints, and candidate order are never authority;
stable records and journal sequence remain the source of every returned result.
Exact and lexical qualification MUST demonstrate clean-rebuild equivalence and
may be run independently.

Authoritative runs require `1 <= min_exact <= max_results`; defaults are
`min_exact = 5` and `max_results = 10`. Capacity is frozen in run metadata and
qualification and preserved across restart.

SEARCH MUST:

- admit exact matches unconditionally and semantic matches only above the
  inclusive threshold;
- use semantic score for eligibility only, never selection or ordering;
- return no more than `max_results` while preserving
  `min(min_exact, exact_count, max_results)` exact anchors;
- include all exact matches when fewer than `min_exact` exist;
- deduplicate exact/semantic overlap by stable record identity;
- select the most recent qualifying union consistent with the exact floor;
- present the selected union strictly oldest-to-newest by journal sequence;
- return useful partial evidence under overflow rather than an overflow error.

SEARCH has no exhaustiveness promise, cursor, pagination, relevance order,
wildcard history mode, or per-query intent flag.

Every workflow record MUST contain a run-specific replay root, run and epoch,
stable identity, sequence, and unique fixed-width replay ordinal. Ordinals are
dense, monotonic, and allocated under the serialized workflow lock. Recovery uses
one exact root anchor to discover the newest ordinal, queries every unique leaf
from zero through that high-water mark, validates contiguity and identity, and
projects records in journal-sequence order. It does not probe internal tree nodes.

Replay MUST remain complete when `min_exact = 1`, each leaf exposes only one exact
anchor, and responses include semantic noise. Semantic results stay visible for
iterative agent search but gain no workflow authority without valid run, epoch,
schema, routing, identity, and sequence metadata.

The launcher MUST recover this projection once before admitting workflow writes
for a host generation. It serves the disposable projection over a run- and
epoch-bound local socket to every worker host, fresh Codex/MCP process, supervisor,
and observer. Incremental exact-leaf recovery keeps that single projection current.
A launcher restart discards and rebuilds it from the journal; no worker-local
projection is authoritative or independently replayed.

## 9. Process authority, restart, and rewind

On one host, the supervisor directly observes process liveness. Oxide MUST NOT use
wall-clock leases as ownership authority. A crashed assignment is reclaimed only
after its old worker and protected child process can no longer execute or publish.
A live process that loses journal connectivity may retry publication but MUST NOT
rerun the command.

The launcher coordinates processes and Git; it is not a semantic orchestrator.
Claims, reviews, candidate invalidation, evidence validity, merge authorization,
and completion come from deterministic journal projection.

Destructive rewind is explicit administration. It MUST:

1. pause the workstream and acquire exclusive run ownership;
2. stop attached workers, supervisors, and observers;
3. restore the checkpoint’s real journal sequence, Git frontier, refs, assignments,
   artifacts, and worker state;
4. increment an epoch stored outside the rewound journal;
5. rewrite assignments and reset observers before restart;
6. reject all claims and terminal results from prior epochs.

Rewind MUST refuse to overwrite uncommitted target changes. It MUST NOT preserve
synthetic monotonic journal numbers across restored history.

## 10. Qualification and backend compatibility

Every run binds a current real-multiprocess qualification receipt for the exact
Oxide implementation, judge digest, backend, worker count, and capacity pair. The
campaign races every workflow role, injects winner crashes, proves that losing
claimants perform no protected work, compares independent replay, and recovers
history larger than one SEARCH response.

The backend-neutral black-box conformance suite MUST run unchanged against the
Python MVP and any future adapter. A Rust backend is not drop-in compatible until
it passes that suite and the multiprocess qualification campaign with the same
configuration.

## 11. Current scope and honest claims

Oxide enforces exact admission for agent-generated contracts, but it does not prove
natural-language entailment. That semantic judgment is explicitly shared by the
contract-generation agent and approving user. Oxide also does not provide
distributed failure detection, contain hostile target code, deploy products, or
prove hardware capacity, operating-system fairness, device behavior, or human
semantic relevance.

The complete model-free harness suite is `./oxide verify`. Incompatible historical
run metadata fails closed rather than receiving a compatibility layer.
