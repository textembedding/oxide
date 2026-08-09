# Swarm Harness

Swarm Harness is a local, specification-driven coding swarm for arbitrary Git
repositories. The target repository owns product intent and verification gates;
the harness supplies scheduling, Git/worktree management, review, verification,
merge, recovery, and observability.

Workers coordinate through exactly two journal tools:

- `journal_search`
- `journal_add`

The disposable Python backend in `src/swarm_harness/journal.py` is a generic
append-only store. Workflow concepts are interpreted in
`src/swarm_harness/workflow.py` above the fixed two-operation port in
`src/swarm_harness/journal_backend.py`.

## Setup

```bash
uv sync --extra dev
./swarmctl verify
```

`swarmctl` is the repository-local entry point. It automatically re-executes
under `.venv` when that environment exists, so installing a separate global CLI
is unnecessary.

## Target layout

Every workload belongs to the implementation repository:

```text
my-product/
├── src/
├── tests/
└── swarm-harness/
    ├── web-foundation.yaml
    └── release-hardening.yaml
```

The harness only loads `<target>/swarm-harness/<workload>.yaml`, and the file
must be committed. At run creation it freezes the repository identity, base
commit, workload path and blob, complete `swarm-harness/` tree, and harness
version. The bootstrap journal record stores that reference—not copied
specification text. Candidate edits to the frozen directory fail closed and an
intentional specification change requires a new run.

Runtime databases, logs, sockets, assignments, and worker clones stay in this
checkout under `.swarm/`.

The target repository must have its GitHub-linked `user.name` and `user.email`
configured before a run starts. The harness freezes that identity in run
metadata and applies it to every worker and merge commit.

## Workload contract

A workload is a committed verification-gated graph. Required top-level fields
are `stage`, `enabled`, `goal`, `tasks`, and `stage_gate`. Every task requires a
safe unique `id`, `title`, `prompt`, `depends_on`, and one or more exact
`checks`; dependencies must form an acyclic graph.

```yaml
stage: foundation
enabled: true
goal: Deliver an authenticated web application slice.
tasks:
  - id: API
    title: Implement the API
    prompt: Implement the API described in docs/product.md.
    depends_on: []
    checks:
      - npm test -- api
  - id: UI
    title: Implement the interface
    prompt: Implement the interface described in docs/product.md.
    depends_on: [API]
    checks:
      - npm test -- ui
stage_gate:
  - npm test
  - npm run build
```

Checks execute from the target repository root. The contract supplies product
intent; the harness does not infer a language, framework, package manager, file
layout, or deployment model. The only bundled workload is the model-free test
fixture at `tests/fixtures/workloads/smoke.yaml`.

## One bounded ADD/SEARCH journal

Exact and threshold-eligible semantic records share one SEARCH response. The
defaults reserve five exact anchors when available and cap the union at ten
records:

```text
min_exact = 5
max_results = 10
```

When capacity is exceeded, SEARCH selects recent qualifying records while
preserving the exact floor, then returns the selected union oldest-to-newest by
journal sequence. Semantic score controls eligibility only. Overflow returns
useful partial evidence rather than an error, and callers follow returned task
IDs, errors, hashes, components, decisions, or concepts with another search.

Workflow recovery remains complete even with `min_exact = 1`: private fixed-width
replay identities form a deterministic partition tree, and one exact anchor is
enough to prove that every child of a nonempty branch must be traversed. Semantic
extras remain visible but cannot affect authoritative state unless their run,
epoch, routing, identity, and sequence metadata validate.

## Qualify a backend

Every workload requires a current real-multiprocess concurrency receipt. The
capacity pair is part of that receipt:

```bash
./swarmctl harness validate-concurrency \
  --workers 8 \
  --rounds 6
```

The campaign races implementation, revision, review, verification, and merge
claims; injects winning-worker crashes; proves losers perform no protected work;
compares independent replay; and recovers complete history through bounded
SEARCH.

The backend-neutral suite runs against the Python MVP by default:

```bash
.venv/bin/pytest -q tests/test_journal_conformance.py
```

Set `SWARM_CONFORMANCE_JOURNAL_COMMAND` to run that identical suite against an
external adapter. Exposing similarly named operations is not enough; an adapter
is drop-in compatible only after the contract suite and concurrency campaign
pass.

## Run any workload

```bash
./swarmctl harness run \
  --workload web-foundation \
  --target /path/to/my-product \
  --workers 8 \
  --reviews 3
```

Tasks use independent branches. After implementation checks, the configured
internal reviews and exact-frontier verification must pass. A worker then
requests merge; the launcher verifies a prospective merge in isolation and
fast-forwards the target to that exact object. Dependencies release after merge,
and the workload gate runs in another isolated clone before completion.

To select a compatible external backend, pass the same command during
qualification and run creation:

```bash
./swarmctl harness validate-concurrency \
  --workers 8 \
  --journal-command '/path/to/journal-adapter serve'

./swarmctl harness run \
  --workload web-foundation \
  --target /path/to/my-product \
  --workers 8 \
  --journal-command '/path/to/journal-adapter serve'
```

The command receives `SWARM_JOURNAL_DATABASE`, `SWARM_JOURNAL_SOCKET`,
`SWARM_JOURNAL_MIN_EXACT`, and `SWARM_JOURNAL_MAX_RESULTS`. The complete wire and
semantic contract is in [HARNESS_CONTRACT.md](HARNESS_CONTRACT.md).

## Observe and control

```bash
./swarmctl harness observe --workload web-foundation --slot worker-0
./swarmctl harness observe --workload web-foundation --slot orchestrator
./swarmctl harness observe-queue --workload web-foundation
./swarmctl harness status --workload web-foundation

./swarmctl harness pause --workload web-foundation
./swarmctl harness resume --workload web-foundation
./swarmctl harness checkpoint --workload web-foundation --name productive
./swarmctl harness rewind --workload web-foundation --to productive
./swarmctl harness reset --workload web-foundation
```

Pause and resume preserve history. Reset archives a run and removes only its task
branches. Destructive rewind restores the checkpoint's real journal sequence,
Git frontier, task refs, assignments, and artifacts, then advances an external
run epoch so discarded workers and observers cannot write into restored history.
Persisted epoch frontiers keep older sequence intervals bound to their original
authoritative epochs across repeated rewinds.
It refuses to overwrite uncommitted target worktree changes; `--archive`
optionally preserves the discarded run state.

Run the complete model-free harness suite with `./swarmctl verify`.
