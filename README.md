<p align="center">
  <img src="docs/assets/oxide-logo.png" alt="Oxide logo" width="240">
</p>

<h1 align="center">Oxide</h1>

<p align="center"><strong>Build formally verified Rust programs with highly concurrent agent swarms.</strong></p>

## How it works

Oxide is a meta-harness for coordinating Codex agents in parallel. Its planning
agent turns human-written specifications into a staged roadmap. For one approved
stage, its contract agent then derives executable implementation, review, proof,
and acceptance work.

```text
 specification directory
              |
              v
 planning agent <-------- user feedback
              |
              v
 approved ROADMAP.md
              |
              v
 contract agent <-------- user feedback
        |     |
        |     +---- ambiguity or gap ----> approved source/roadmap revision
        |                                      |
        +--------------------------------------+
              |
              v
 stage contract + exact semantic trace
              |
              v
 agent attestation + user approval + mechanical qualification receipts
              |
              v
 +-----------------------------------------------------------+
 |                 shared append-only journal                |
 | ready work | atomic claims | candidates | proof evidence  |
 +--------+----------------+----------------+----------------+
          |                |                |
        claim            claim            claim
          |                |                |
 +--------v-------+ +------v---------+ +----v-------------+
 | worker A       | | worker B       | | worker C         |
 | implements API | | reviews code   | | runs exact check |
 +--------+-------+ +------+---------+ +----+-------------+
          \                |                /
           \_______________|_______________/
                           |
                           v
               exact prospective-tree gate
                           |
                           v
                         merge
                           |
                           v
                  newly unblocked work
```

The user can challenge stage ordering, scope, tasks, or verification goals during
either session. An ambiguity is never silently resolved. The contract agent must
propose a concrete source change, obtain approval, write it back to the
human-readable specification and roadmap, and regenerate the contract.

The generated receipts separately record planning approval, the contract agent’s
contractibility attestation, the user’s approval, and mechanical qualification.
They bind the selected stage, applicable global invariants, exact cited source
sections, semantic trace, and generated contract. A relevant change invalidates
admission; an unrelated edit confined to a deferred future stage does not.

Only after approval and isolated mechanical qualification does Oxide create run
state or start the journal and workers. The contract defines tasks and their
dependencies. With no fixed role pools, implementation, review, and proof work
can proceed concurrently.

Publishing freezes an immutable Git candidate, but does not expose it to the
swarm immediately. Oxide first runs its deterministic policy against that exact
tree. This rejects unclassified files, proof escapes, stale manifests, changed
judge inputs, and other structurally incomplete candidates before reviews or
acceptance checks can be claimed.

After admission, reviews and unsatisfied checks become independently claimable.
Evidence is bound to the exact candidate and check, then reused across roles.
Changing the candidate tree creates a new requirement.

Once every required review and check passes, Oxide constructs the exact tree that
would be merged and runs the whole-tree gate against it. A successful merge
unlocks dependent work.

## Verification philosophy

Oxide uses [Verus](https://verus-lang.github.io/verus/guide/) to prove that Rust
code satisfies its formal contracts. Its normative
[verification policy](https://github.com/textembedding/oxide/blob/main/docs/VERIFICATION_PRIMER.md)
applies to every target independently of its product specifications.

Every production logical component belongs to one refinement chain:

```text
 observable product behavior
              |
              v
     abstract state machine
              |
              v
 meaningful component contracts
              |
              v
 production Rust + Verus proofs <--- declared assumptions
              |                          from narrow,
              v                          policy-free
 whole-program composition proof         effect adapters
              |
              v
 exact prospective-tree proof
              |
              v
     formal correctness gate ---------+
                                       +--> release-ready tree
     empirical capacity gate --------+
        (benchmarks and faults)
```

- All production logic is Verus-verified by default. There is no “typed but
  unverified” or low-risk exemption.
- Uniform coverage does not mean uniform proof size. Simple, meaningful contracts
  may be discharged automatically; complex protocols may need substantial proof.
- Trusted effect adapters are narrow, explicit, and contain no journal policy.
- Deterministic policy checks reject vacuous contracts, undeclared assumptions,
  source/proof divergence, and attempts to redefine the judge.
- Agent review supplements proof; it cannot replace Verus or deterministic policy.
- Formal correctness and empirical capacity are separate gates.

## Target project

A Rust project using Oxide keeps its verification inputs under `verification/`;
it does not need an Oxide-specific directory:

```text
my-rust-product/
├── docs/
│   └── specs/
├── ROADMAP.md
├── src/ or crates/
└── verification/
    ├── contract.toml
    ├── roadmap-approval.json
    ├── contract-attestation.json
    ├── contract-approval.json
    ├── contract-qualification.json
    ├── manifest.toml
    ├── toolchain.lock.toml
    ├── contracts/
    ├── models/
    ├── proofs/
    ├── fixtures/
    └── fuzz/
```

`verification/contract.toml` identifies the immutable specification and toolchain
closure, classifies production and proof roots, defines an acyclic implementation
graph, and assigns formal or supplementary checks to coherent candidates. Oxide
constructs formal Verus commands itself, so a candidate cannot redefine the judge
that accepts it.

The generated JSON files are immutable evidence, not semantic specifications.
Specifications remain authoritative for product behavior, `ROADMAP.md` is the
approved staged plan, and the stage contract is their enforceable derivation.

At run creation, Oxide freezes the target commit, contract and immutable closure,
verification-engine digest, execution policy, journal capacity, qualification
receipt, and Git identity. Changing the specification, toolchain, contract, or
judge starts a new run.

## The journal backend

The shared journal backend makes parallel execution safe. Claims, candidates,
check evidence, reviews, merges, and recovery form one durable chronological
history. Atomic claims select one owner per assignment, while deterministic replay
lets every worker recover the same state without a privileged orchestrator.

Workers see one append-only journal through exactly two operations:

- `journal_add` appends records and atomically arbitrates competing claims.
- bounded `journal_search` returns exact and threshold-eligible semantic context
  in chronological order.

The Python prototype dogfoods the planned production Exact and lexical indexes.
Both are generic, rebuildable projections of immutable records; neither knows
about Oxide tasks or changes the two-operation interface.

Workflow meaning remains above this generic two-operation interface. The bundled
Python backend is the current prototype; it can be replaced by a Rust backend only
after that backend passes the same black-box conformance and multiprocess
qualification suites.

Each launcher generation performs one indexed recovery, then shares that warm,
journal-derived projection with every worker and observer on the host. Fresh agent
contexts therefore recover their assignment without replaying the full run.

## Install and verify Oxide

```bash
uv sync --extra dev
./oxide verify
```

`oxide` is the local entry point and automatically uses `.venv` when available.
No global installation is required.

Create the approved roadmap in an interactive planning session:

```bash
./oxide harness plan --target /path/to/my-rust-product/docs/specs/
```

The planning agent reads the complete specification corpus, derives as many or as
few implementation phases as the work requires, and revises `ROADMAP.md` until
the user enters `/approve`. An approved roadmap may retain future work as
`planned`, `deferred`, or `blocked`; only a `ready` phase can generate a contract.

Every roadmap uses the same validated schema, regardless of the source material.
The root `ROADMAP.md` presents that data as an overview table and concise phase
sections for human readers. Its exact TOML representation is kept in a collapsed
section for Oxide. The readable view is generated from the TOML, so there is only
one plan to approve and no second representation that can drift.

Maintain an approved roadmap without reopening the whole plan:

```bash
./oxide harness plan \
  --target /path/to/my-rust-product/docs/specs/ \
  --update stage-1
```

Oxide asks for the intended change, locks every unselected phase and all stable
phase IDs, then shows the exact diff and approval impact. Repeat `--update` to
permit a coordinated multi-phase change. A readiness-only change preserves
dependent approvals; a semantic change invalidates approvals for dependent phases.
Nothing is written until `/approve`.

Use maintenance mode for readiness, dependencies, and allocation of
requirements already present in the specifications. If program behavior or
success semantics change, update the specification first and then maintain the
roadmap; the roadmap cannot introduce that behavior by itself.

Generate one stage contract in a second interactive session:

```bash
./oxide harness generate-contract /path/to/my-rust-product/ROADMAP.md stage-0
```

The contract agent resolves the selected stage’s cited requirements and global
invariants, explains its tasks and verification goals, and accepts free-form user
feedback. `/approve` writes any approved upstream refinements, regenerates
`verification/contract.toml`, records the agent attestation and user approval,
and mechanically qualifies the exact artifact set. Commit the modified target
files before execution.

Before a run, qualify the journal backend under real multiprocess contention:

```bash
./oxide harness validate-concurrency --workers 8 --rounds 6
```

Then launch against the committed target:

```bash
./oxide harness run \
  --target /path/to/my-rust-product \
  --workers 8
```

The default contract is `verification/contract.toml`. Useful parallelism is
determined by the contract’s dependency graph and its live mix of implementation,
review, and proof work, up to the current 64-worker process limit.

Neither interactive command creates a run, journal, worker, or observer. `run`
fails before any of those exist when the roadmap approval, attestation, user
approval, qualification, trace, or selected-stage binding is missing or stale.

## Observe and control

```bash
./oxide harness observe --workload verified-map --slot worker-0
./oxide harness observe-queue --workload verified-map
./oxide harness status --workload verified-map

./oxide harness pause --workload verified-map
./oxide harness resume --workload verified-map
./oxide harness checkpoint --workload verified-map --name productive
./oxide harness rewind --workload verified-map --to productive
./oxide harness reset --workload verified-map
```

Runtime databases, logs, evidence, worker clones, and sockets remain under the
ignored `.oxide/` directory. Product files remain in the target project.

The normative system boundaries and acceptance invariants are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Oxide's universal assurance rules
and their rationale are in the normative
[verification policy](docs/VERIFICATION_PRIMER.md).
