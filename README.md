<p align="center">
  <img src="docs/assets/oxide-logo.png" alt="Oxide logo" width="240">
</p>

<h1 align="center">Oxide</h1>

<p align="center"><strong>Build formally verified Rust programs with highly concurrent agent swarms.</strong></p>

## How it works

Oxide is a meta-harness for coordinating Codex agents in parallel. It compiles
natural-language specifications into executable contracts for implementation,
review, and formal verification.

```text
 human-written specifications
              |
              v
       Oxide contract generation
              |
              v
 executable verification contract
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

The contract defines tasks and their dependencies. The scheduler gives each idle
worker the highest-value ready assignment. With no fixed role pools,
implementation, review, and proof work can proceed concurrently.

Publishing an immutable Git candidate makes its reviews and unsatisfied checks
independently claimable. Evidence is bound to the exact candidate and check, then
reused across roles. Changing the candidate tree creates a new requirement.

Once every required review and check passes, Oxide constructs the exact tree that
would be merged and runs the whole-tree gate against it. A successful merge
unlocks dependent work.

## Verification philosophy

Oxide uses [Verus](https://verus-lang.github.io/verus/guide/) to prove that Rust
code satisfies its formal contracts. The
[verification primer](https://github.com/textembedding/oxide/blob/main/docs/VERIFICATION_PRIMER.md)
explains the reasoning behind this model.

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
│   ├── ROADMAP.md
│   └── specs/
├── src/ or crates/
└── verification/
    ├── contract.toml
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

Workflow meaning remains above this generic two-operation interface. The bundled
Python backend is the current prototype; it can be replaced by a Rust backend only
after that backend passes the same black-box conformance and multiprocess
qualification suites.

## Install and verify Oxide

```bash
uv sync --extra dev
./oxide verify
```

`oxide` is the local entry point and automatically uses `.venv` when available.
No global installation is required.

Before a run, qualify the journal backend under real multiprocess contention:

```bash
./oxide harness validate-concurrency --workers 8 --rounds 6
```

Then launch against a target Rust project:

```bash
./oxide harness run \
  --target /path/to/my-rust-product \
  --workers 8
```

The default contract is `verification/contract.toml`. Useful parallelism is
determined by the contract’s dependency graph and its live mix of implementation,
review, and proof work, up to the current 64-worker process limit.

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
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The rationale behind Oxide’s
pervasive-verification model is in the non-normative
[verification primer](docs/VERIFICATION_PRIMER.md).
