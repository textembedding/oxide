# One toolchain, one meaning of correctness

## Oxide's normative policy for pervasive formal verification

> **Normative status:** The **Normative policy** section of this document is
> Oxide's verification policy. Planning, contract generation, admission, proof
> execution, and authoritative merge must enforce it for every target. Target
> specifications define the program's behavior, constraints, and success
> semantics; they do not need to repeat these universal assurance rules. A target
> may strengthen this policy but may not weaken it. The later engineering rationale
> explains the policy and adds no requirements.

Within the **Normative policy** section, the terms **MUST**, **MUST NOT**,
**REQUIRED**, **SHOULD**, **SHOULD NOT**, and **MAY** are normative.

## Normative policy

### Scope and assurance claim

Oxide MUST establish this refinement chain for the exact production program under
consideration:

```text
approved observable requirements
        ↓
public abstract program model
        ↓
component contracts and mathematical views
        ↓
executable Rust component refinement
        ↓
program-wide composition theorem
        ↓
explicit trusted effects and environmental assumptions
```

The assurance claim applies only to the exact immutable source tree, target,
features, specifications, contracts, proof closure, trusted boundary, Verus
toolchain, solver policy, and judge identified by its evidence. It does not transfer
to a similar tree or configuration.

The public abstract model MUST define a valid initial state, reachable success and
failure behavior, relevant fault and progress behavior, and the observations named
by the approved requirements. It MUST remain independent of incidental layouts,
private formats, algorithms, caches, thread counts, system-call interfaces, and
other optimizations unless one of those details is itself approved observable
behavior. Representation changes MUST refine the same abstract program.

Verus is the sole authoritative source-level formal-verification frontend. The
pinned verifier, solver, Rust compilation chain, standard libraries, hardware, and
declared environmental assumptions remain in the trusted computing base. Verus
does not prove external semantic relevance, throughput, latency, storage-device
behavior, operating-system fairness, or any property absent from the abstract
model.

A program MUST NOT be described as formally verified until every production
logical component and public entry point is covered, every applicable component
theorem and the exact-tree composition theorem pass, every trusted boundary and
assumption is declared, and the production and verified closures agree.

### Uniform production coverage

Every executable path MUST be classified as exactly one of:

1. verified production logic;
2. a trusted effect adapter; or
3. non-authoritative tooling.

All production logic is verified by default. There is no typed-but-unverified,
low-risk, low-complexity, business-logic, helper, legacy, or trivial-logic
exemption. Private leaf helpers MAY be covered by an enclosing component proof
when their complete behavior is visible to it. Cross-component, exported,
stateful, or semantically reusable logic MUST expose a stable meaningful contract.

Uniform coverage does not require uniform proof size. Automatically discharged
contracts are valid when they state the component's real responsibility. Proof
volume and ghost-code ratios are not assurance metrics.

Trusted effect adapters are the only production-code exemption. Each MUST expose
a narrow explicit contract, report observations rather than decide program policy,
name every assumption on which it depends, and remain inside the declared trusted
boundary. Moving logic into a trusted adapter is an assurance-boundary change and
MUST NOT be justified merely by proof difficulty. Non-authoritative tooling MUST
have no unchecked path to production authority.

Unsafe Rust, generated code, procedural macros, build scripts, foreign code,
linked components, conditional compilation, and fallbacks MUST be included in the
verified production closure or explicitly classified inside the trusted boundary.
The production and verified feature, source, generated-code, target, and entry-point
paths MUST agree.

A machine-readable coverage declaration MUST connect each production logical
component to its production source closure, public entry points, approved target
requirements, abstract specification, implementation contracts, proof roots,
component refinement theorem, composition participation, trusted assumptions,
features, and target. Coverage MUST fail closed for unclassified production code,
missing or unreachable roots, undeclared trust, omitted public paths, or material
production/proof divergence.

### Meaningful refinement and composition

Every production logical component MUST identify its executable inputs and state,
mathematical view, preconditions, success and failure postconditions, preserved
invariants, abstract operation, trusted assumptions, and refinement theorem. Each
component MUST participate transitively in the program-wide composition theorem,
and every public production entry point MUST be reachable from that theorem.

`ensures true`, impossible or artificially restrictive preconditions, empty or
trivialized state spaces, disconnected theorems, proof-only substitute
implementations, assumptions that imply the desired result, and equivalent vacuous
obligations do not count as verification. Important initial, success, failure,
boundary, and recovery states MUST have constructive reachability or non-vacuity
evidence appropriate to the model.

Every claimed progress or liveness property MUST identify the transition, fairness
and environmental assumptions, and whether it is machine-proved, delegated to a
trusted effect contract, empirically supported, or intentionally unsupported. A
safety theorem MUST NOT be presented as proving eventual completion.

Incremental programs MAY prove only currently exposed behavior. Unimplemented
behavior MUST remain absent or return an explicitly specified unsupported result.
Placeholder axioms, trusted proof stubs, fabricated postconditions, and dormant
public paths MUST NOT stand in for unimplemented behavior.

Component contracts and mathematical views SHOULD state stable semantic behavior,
not solver-facing or representation-specific detail. Cross-component proofs MUST
depend on exported contracts and the smallest necessary lemmas rather than an
accidental global solver context. Proof roots SHOULD remain independently
checkable with bounded relevant context. Pinned solver options and declared
resource limits are authoritative; repeated unrelated breakage or global rewrites
for contract-preserving local changes indicate a proof-abstraction defect.

### Proof and specification change control

Implementation, implementation-attached contracts, abstract specifications,
component proofs, coverage declarations, trusted-boundary declarations, and
composition MUST evolve together. Local and candidate commits MAY be temporarily
unverified while implementation and proof repair proceed.

Proof repair and specification change are different operations. A repair MUST NOT
silently strengthen a precondition, weaken a postcondition or invariant, narrow an
abstract operation or reachable state space, alter a fault assumption, broaden a
trusted assumption, disconnect composition, or move policy into trusted code.
Product-semantic changes require an approved human-readable source change before a
downstream roadmap or contract may rely on them. Verification-contract or abstract-
model changes require explicit review as judge-facing changes even when observable
product prose does not change.

Every trusted assumption MUST be precise, named, localized to the smallest
practical boundary, connected to every dependent theorem, justified by a contract
or concrete evidence, reviewed, and bound into proof identity. An assumption MUST
NOT merely assert the product property being proved. When digests identify source,
specifications, proofs, evidence, or tools, the digest algorithm and collision-
resistance assumption belong to the trusted context; digest equality is not
unconditional mathematical identity.

Every normative target behavior MUST remain visibly classified as formally proved,
dependent on an explicit trusted assumption, supported only by supplementary
evidence, or intentionally unsupported. Omitting a requirement from a proof plan
MUST NOT silently turn it into a non-requirement.

### Planning and contract generation

Planning MUST ingest this exact policy independently of the target specification
corpus. A roadmap MUST introduce verification foundations before implementation
proliferates and MUST plan implementation, meaningful contracts, proofs, coverage,
and composition together rather than defer proof to a cleanup phase. It MUST keep
formal correctness and empirical capacity as separate gates.

Before broad implementation proliferates, the roadmap MUST establish the pinned
verification context, the public abstract model and proof conventions, and real
representative proofs for applicable pure authoritative logic and the hardest
ownership, concurrency, effect, persistence, or recovery transitions. It MUST then
develop each production component together with its contracts, proofs, coverage,
trusted-boundary connection, and composition path. A placeholder proof or toy
component cannot satisfy this foundation rule.

Contract generation MUST ingest the same exact policy. Generated contracts MUST
bind its digest, preserve the target's approved behavior and success semantics,
and include the proof and deterministic-policy work needed to enforce both the
target requirements and this policy. Oxide-supplied proof classification,
traceability, evidence, dependency, recovery, and execution constraints are
mechanical assurance requirements; they are not new product behavior and need not
be quoted from target specifications.

A target requirement, task, or acceptance check that claims product behavior MUST
still trace to the approved target semantic closure. The policy MUST NOT be used as
authority to invent product behavior, acceptance semantics, external guarantees,
or missing domain requirements.

### Authoritative gates and exact evidence

The authoritative gate MUST run against an immutable candidate. Before merge, the
complete composition proof MUST pass against the exact prospective authoritative
tree. A collection of component proofs, a proof of a reference implementation, or
a successful proof-only build does not establish the production-program claim.

Proof evidence MUST bind every input capable of changing the result, including the
candidate and prospective trees, applicable target requirements, this policy,
abstract specifications, contracts, proof closure, coverage declaration, target,
features, toolchain, solver/resource policy, theorem roots, trusted assumptions,
judge implementation, result, and content-addressed logs or artifacts.

Evidence MAY be reused only when all relevant identity inputs are exactly equal.
A changed source, specification, contract, proof, feature, target, trusted boundary,
toolchain, judge, or prospective tree invalidates every dependent result. Textual or
semantic similarity is insufficient. A logical proof failure is a product result;
repeating the same immutable subject to seek a pass is prohibited. An
infrastructure-failed attempt MAY be replaced only after the old attempt can no
longer produce authoritative evidence.

### Deterministic integrity enforcement

The authoritative checker MUST be deterministic and fail closed. It MUST reject
undeclared `assume`, `admit`, axioms, external bodies or specifications, unchecked
executable/specification bridges, proof-only implementations, production/proof
divergence, unclassified production code, unreachable proof roots, missing public-
API composition, stale or malformed evidence, and candidate-defined judge changes.

A timeout, resource exhaustion, unknown result, skipped or missing obligation, or
cached result without exact identity is an infrastructure failure, never proof
success. Every permitted trusted construct MUST be uniquely declared, narrowly
scoped, included in the evidence identity, and reviewed.

Changes to the verifier, solver policy, toolchain, checker closure, coverage or
receipt schema, trusted-boundary policy, or evidence validator change the judge.
The new judge MUST be established independently and freshly qualified before it
can evaluate product candidates; it cannot approve itself in the same change.

### Review and supplementary evidence

Review MUST independently assess product-to-model fidelity, implementation-to-
proof fidelity and non-vacuity, and the systems/trust boundary. Reviews MAY proceed
concurrently with independent proof execution, but all required gates must pass
before authoritative merge. Agent or LLM review is supplementary evidence and
MUST NOT substitute for Verus or deterministic policy checks.

Fixtures, ordinary tests, deterministic fuzzing, bounded exploration, real crash
campaigns, and benchmarks remain necessary for concrete executions, trusted
boundaries, counterexample discovery, and performance. They MUST be accurately
classified and MUST NOT satisfy a Verus proof obligation.

Supplementary evidence MUST identify its exact subject and environment and remain
reproducible where the method permits it. Fuzzing and bounded exploration SHOULD
use stable seeds and retain minimized regressions; crash and benchmark evidence
MUST state its fault, workload, and machine scope without generalizing finite
observations into a theorem.

Critical invariants SHOULD have deterministic proof-sensitivity checks or
controlled negative mutations demonstrating that removal or inversion of the
protection breaks verification. These checks support integrity assessment; they
do not replace the theorem.

Formal correctness and empirical capacity are independent release gates. Formal
proof establishes logical refinement under declared assumptions. Benchmarks and
fault campaigns establish throughput, latency, memory, storage, retention,
recovery time, and environmental behavior for an exact binary and workload.
Neither gate substitutes for the other.

Where a target makes structural resource or scalability guarantees, Verus MUST
establish the applicable finite bounds, safe exhaustion behavior, bounded recovery
work, and authority-preserving representation changes. Wall-clock throughput,
latency, hardware capacity, and external fairness remain empirical claims unless a
separately approved formal cost model makes them logical requirements.

## Engineering rationale

Software teams usually discuss formal verification as a trade: more assurance in exchange for more engineering effort. That framing is incomplete for a codebase expected to evolve for years through large, semi-autonomous agent swarms.

In that environment, the hardest problem is not simply preventing today’s bugs. It is preserving the meaning of the system while thousands of future changes are proposed by agents that did not participate in the original design, may see only part of the history, and may be optimized to satisfy immediate acceptance gates rather than protect architectural intent.

For a journal kernel, that concern is especially acute. The product’s value depends on deceptively compact rules—append-only authority, sequence uniqueness, exact-search guarantees, bounded result selection, chronological output, safe concurrency, durable publication, crash recovery, checkpointing, and compaction. A local change that subtly weakens one of those rules may pass a conventional test suite and remain latent until the system is operating under extreme concurrency or recovering from an unusual failure.

The project therefore needs more than memory-safe Rust, more than extensive fixtures, and more than a large regression suite. It needs a durable, machine-checkable meaning of correctness.

Our conclusion is intentionally strong:

> **Every production logical component is verified with Verus under one uniform assurance regime. Only narrow trusted effect adapters and code that cannot affect production behavior are exempt.**

Uniform coverage does not mean uniform proof size. Some components will need extensive ghost state and concurrency arguments. Others may need only a small contract that Verus proves automatically. The goal is not to maximize proof code. The goal is to make it mechanically difficult for production semantics to drift outside the verified refinement chain.

This primer explains how we reached that position, why we selected Verus over other Rust verification approaches, what broad verification does and does not buy us, and which engineering disciplines make it maintainable for agent swarms.

---

## 1. The real problem: semantic drift, not just defects

Traditional software verification practices usually assume a relatively stable human team. Architectural context is carried through design reviews, institutional memory, senior maintainers, and social norms. Tests act as executable examples of expected behavior, while reviewers infer the unstated intent around those examples.

A long-horizon agent swarm weakens those informal safeguards.

An agent may correctly optimize a helper function while misunderstanding why its output had to be ordered in a particular way. Another may move logic into a utility that appears too trivial to justify formal treatment. A third may “fix” a failing proof by strengthening a precondition, weakening a postcondition, or adding a trusted assumption. Each local change can look reasonable. Over time, however, the product contract can dissolve into whatever the current implementation and tests happen to accept.

The failure pattern is gradual:

```text
original product invariant
    → implementation convention
    → helper assumption
    → partial test coverage
    → refactor changes helper behavior
    → downstream code adapts
    → original invariant becomes historical folklore
```

The usual response is more documentation and more tests. Both help, but neither fully closes the loop:

- Documentation can be ignored, misread, duplicated, or updated after the implementation has already drifted.
- Tests establish behavior only for the executions they cover.
- Property-based tests sample a larger space but generally do not establish a theorem over every permitted state.
- Fixture-based crash tests are indispensable evidence about real integrations, yet remain finite experiments.
- Code review is probabilistic, especially when the reviewer is another agent working from the same incomplete context.

Formal verification changes the shape of the maintenance problem. Instead of depending primarily on future agents remembering an invariant, we require executable code to remain connected to a mathematical contract. A semantic change then causes one of two visible events:

1. the implementation no longer proves the existing contract; or
2. the contract itself changes and must be reviewed as a specification change.

That is the central long-horizon benefit. Formal verification turns hidden semantic drift into an explicit proof or specification event.

---

## 2. What “formal verification” means here

Formal verification does not mean “the verifier ran successfully” in the abstract. It means that a precisely identified executable implementation has been shown, by a machine-checked argument, to satisfy a precisely identified formal specification under precisely identified assumptions.

The assurance structure is a refinement chain:

```text
observable product contract
        ↓
abstract journal state machine
        ↓
component contracts and mathematical views
        ↓
executable Rust component refinement
        ↓
kernel-wide composition theorem
        ↓
explicit trusted effects and environmental assumptions
```

Each layer answers a different question.

### The product contract

What must an external caller observe? Examples include append-only history, sequence order, exact-search behavior, durability acknowledgments, and recovery results.

### The abstract journal

What mathematical state and transitions capture those observations without depending on Rust structures, file layouts, thread counts, or index implementations?

### Component contracts

What does each component require, guarantee, preserve, and refine?

### Executable refinement

Does the actual Rust code—not a reference function or proof-only substitute—satisfy those component contracts?

### Composition

Do the proved components form the actual public kernel, with all shared invariants and effect boundaries represented?

### Trusted boundary

Which facts are assumed rather than proved? This includes the verifier and solver, compiler, hardware, operating system, storage semantics, cryptographic collision resistance, and narrow external-effect adapters.

A proof claim is always relative to those assumptions. A formally verified logical recovery algorithm does not prove that a storage device honors flushes. It proves that, if the device behavior satisfies the declared adapter contract, the kernel interprets observations and advances authority correctly.

This distinction is not a weakness. It is what makes the assurance claim honest and reviewable.

---

## 3. Why Rust’s type system is necessary but insufficient

Rust already provides unusually strong static guarantees. Ownership, borrowing, lifetimes, algebraic data types, and exhaustive pattern matching eliminate broad classes of memory and aliasing errors. Verus builds on those strengths: its official overview describes static verification of full functional correctness using Rust-like executable, specification, and proof code, with SMT solving for the generated verification conditions.[^verus-overview]

But ordinary Rust typing cannot express the complete journal contract.

Rust can help establish that a reference does not outlive its owner. It does not automatically prove that:

- two concurrent lanes can never possess overlapping sequence authority;
- a reader observes only states permitted by the abstract publication protocol;
- protected exact results cannot be displaced incorrectly by semantic candidates;
- every permitted crash image recovers to an allowed logical state;
- compaction preserves every future observable search result;
- a durable acknowledgment is never issued before the corresponding abstract durability transition;
- recovery from the same persisted history is deterministic;
- a revised index cannot override the append-only log.

These are relational and history-dependent properties. Many quantify over arbitrary sequences of transitions or interleavings. Some relate multiple representations of the same abstract state. They require specifications, inductive invariants, refinement arguments, or equivalent formal reasoning beyond enhanced type checking.

The project should exploit Rust’s type system aggressively. Invalid local states should be difficult or impossible to represent. Ownership tokens and typestate should encode protocol structure where practical. But type safety is the foundation of the proof, not a replacement for the proof.

---

## 4. The Rust verification landscape

Several Rust verification tools are credible, but they optimize for different assurance problems. Choosing among them requires identifying the properties that matter most, not merely selecting the tool with the lowest annotation count on a local benchmark.

### Verus: deductive functional verification and concurrent protocols

Verus is designed for static functional verification of Rust systems code. It provides mathematical specifications, proof code, executable Rust, linear ghost state, and facilities for concurrent state-machine reasoning.[^verus-overview] Its transition-system framework includes `state_machine!` and `tokenized_state_machine!`, developed primarily for nontrivial ownership disciplines in concurrent code.[^verus-state-machines] Verus also supports logical atomic specifications that describe state immediately before and after a linearization point.[^verus-atomic]

That combination matches the journal’s difficult properties:

- abstract state refinement;
- sequence and lane authority;
- publication linearization;
- tokenized ownership;
- crash and recovery state transitions;
- functional correctness of selection and compaction algorithms;
- composition from component contracts to the public API.

Verus is not cheap in proof structure. Complex proofs may require mathematical views, ghost state, invariants, triggers, lemmas, and explicit solver guidance. Its documentation also makes clear that it does not intend to support every Rust feature or verify the verifier, rustc, or LLVM.[^verus-overview]

The decisive point is that Verus can express both the local functional contracts and the global systems protocols we need under one toolchain.

### Kani: excellent bounded and finite-state bug finding, but not the global concurrency proof

Kani uses model checking over Rust proof harnesses. It is especially useful for panics, arithmetic overflow, undefined behavior, assertions, and concrete counterexamples.[^kani-intro]

That workflow is attractive for agents because proof harnesses resemble property tests. A failing property can produce a concrete trace rather than an opaque proof obligation.

However, Kani’s current official feature documentation states that concurrent features are out of scope and that concurrent code is compiled as if sequential.[^kani-concurrency] That limitation is disqualifying for the central production theorem of a concurrent journal kernel. Kani could still provide valuable supplementary checks, but adopting it would create a second formal toolchain without removing the need for Verus or another concurrency-capable deductive verifier.

We chose consistency over accumulating specialized verifiers.

### Flux: elegant refinement typing for local invariants

Flux extends Rust with refinement types and liquid inference. Its research demonstrates that many low-level properties can be verified with substantially less annotation and solver time than a more general program logic for the evaluated cases.[^flux-paper]

Flux is appealing for properties such as:

- an index is within a buffer;
- a result count is below capacity;
- a sequence is increasing;
- a typestate transition is legal;
- a collection has a known logical length;
- a local arithmetic relation holds.

Those are important and pervasive. But Flux’s natural strength is local type-level refinement. It is not, by itself, the most direct framework for proving that an arbitrary concurrent execution history refines an abstract journal, or that crash recovery and compaction preserve global observational equivalence.

Selecting Flux as the primary verifier would likely force us to add a second method for the hardest global protocol properties. That would undermine the one-toolchain objective and require agents to understand where one assurance regime ends and another begins.

### Creusot: the strongest alternative, but not clearly the better systems fit

Creusot is a deductive verifier for Rust with contracts, ghost code, mathematical logic, and Why3-based proof discharge.[^creusot-home] It is the most serious alternative to Verus for full functional correctness.

Creusot also illustrates a universal issue in proof-oriented Rust: the relationship between the verified function and the compiled function must remain explicit. Its erasure mechanism exists to check that ghost-enriched verified code corresponds to the executable function after logical constructs are removed.[^creusot-erasure]

Creusot’s concurrency support is advancing. In 2026 it introduced initial atomic-invariant support, while noting current restrictions such as sequential consistency and ongoing work on relaxed memory models.[^creusot-concurrency]

Creusot may offer an attractive contract style for sequential components. But the project’s defining difficulty is not only sequential functional correctness. It is the combination of concurrency, ownership authority, publication, persistence, and recovery. Verus already provides a coherent state-machine and linear-ghost-state story for that domain. Switching to Creusot would not eliminate ghost code or specification governance, and would introduce a broader Why3/prover toolchain to freeze and teach to agents.

### The comparison in one table

| Tool | Primary strength | Where it fits our problem | Why it is not the selected sole tool |
|---|---|---|---|
| **Verus** | Deductive functional verification, ghost ownership, concurrent state machines, executable refinement | Local contracts and the global journal protocol | Selected; proof volume and solver discipline must be governed |
| **Kani** | Model checking, counterexamples, panic/overflow/unsafe checks | Supplementary finite or bounded checks | Current concurrency model is insufficient for the central theorem; would add a second toolchain |
| **Flux** | Refinement types and low-annotation local invariants | Excellent for local arithmetic, bounds, and typestate | Does not naturally close the global concurrency, history, and recovery proof by itself |
| **Creusot** | Deductive contracts and functional verification through Why3 | Plausible full-verification alternative | Concurrency story is newer; additional toolchain layers; no decisive advantage for our hardest protocols |

The conclusion is not that the other tools are poor. It is that design consistency and closed end-to-end refinement matter more than optimizing every local proof with a different specialist tool.

---

## 5. The first debate: is Verus overkill?

The initial conservative recommendation was to verify only a narrow correctness core:

- abstract journal transitions;
- sequence and publication authority;
- search selection;
- crash recovery;
- checkpointing and compaction;
- kernel composition.

Everything else would be strongly typed, tested, or isolated behind verified interfaces.

That recommendation followed a common human-engineering assumption: proof code is expensive to write and maintain, so it should be concentrated where its benefit is greatest.

The objection was powerful and specific to agent swarms:

> If agents can cheaply read and write ghost code, why force every future agent to decide whether a component is important enough to verify? Would not a uniform rule create less ambiguity and less drift than a selective boundary?

That question changes the optimization target.

A selective policy creates recurring classification decisions:

- Is this helper “core” or merely support?
- Did this refactor move policy outside the verified boundary?
- Is ordinary Rust typing sufficient here?
- Does a component that was once harmless now participate in an invariant?
- Is this business rule too high-level to count as systems logic?

Over a long horizon, those judgments are opportunities for assurance erosion. The verified core can shrink semantically without shrinking visibly: important decisions migrate into unverified convenience functions, adapters, configuration logic, or pre-processing.

A uniform rule is simpler:

> If executable code contributes to production kernel behavior, it is Verus-verified. If it performs an external effect that Verus cannot prove, it is a narrow trusted adapter. If it cannot affect production behavior, it is tooling.

No agent needs to rank the code’s importance. No “typed but unverified” middle category exists. The boundary is based on production reachability and effects, not subjective risk.

Under the assumption that ghost-code quantity is not the scarce resource, this uniformity is a major maintenance advantage.

---

## 6. What pervasive verification actually means

“Verify everything” is easy to misinterpret. It does **not** mean every function needs a large hand-written proof. It means every production logical component is inside the same machine-checked refinement regime.

### Uniform coverage, variable proof depth

Consider a pure identity operation:

```rust
fn identity(x: Entry) -> (out: Entry)
    ensures out@ == x@
{
    x
}
```

Verus may discharge this contract automatically. There may be no explicit proof body. That is still a valid proof because the contract is meaningful and the executable function satisfies it.

Now consider a concurrent publication protocol. It may need:

- a mathematical state machine;
- tracked ownership tokens;
- invariants relating physical and abstract state;
- an atomic specification around a linearization point;
- lemmas for sequence disjointness;
- recovery transitions;
- a composition argument.

Both components follow the same policy. Their proof depth differs because their semantics differ.

### No minimum ghost-code ratio

Ghost-code quantity is not an assurance metric.

A ten-line proof may be complete. A thousand-line proof may be vacuous or disconnected. The goal is not to demonstrate effort; it is to establish meaningful behavior.

This is acceptable:

```text
meaningful contract
+ production executable path
+ automatically discharged verification condition
= verified component
```

This is not:

```rust
fn normalize(x: Input) -> Output
    ensures true
```

A no-op proof must still state the no-op’s semantic responsibility. “No-op” is an implementation description, not a license for a vacuous contract.

### Every logical path stays closed

Pervasive verification closes a common loophole in selective verification:

```text
proved public function
    → unproved helper
        → unproved configuration rule
            → product decision
```

Under the uniform model, every logical arrow remains in Verus. Only the final effect boundary is trusted:

```text
proved public function
    → proved orchestration
        → proved helper
            → proved state transition
                → narrow trusted effect contract
```

That produces a cleaner assurance statement:

> All production decisions are verified; only the verification stack and physical effects remain trusted.

---

## 7. Why broad verification can improve maintainability

### 7.1 One rule is easier to preserve than a risk taxonomy

The default becomes mechanical. A new production logical module without Verus coverage fails. An agent does not need to infer whether it belongs to a privileged core.

This matters because classification drift is often silent. Uniform coverage makes omission visible.

### 7.2 Contracts become durable semantic memory

For an agent arriving years later, a meaningful precondition, postcondition, mathematical view, and refinement theorem provide a more reliable account of intended behavior than comments alone.

The contract is not merely prose. Calls must satisfy it, implementations must establish it, and dependent proofs encode how it composes with the rest of the system.

### 7.3 Semantic changes become explicit

Without contracts, a helper can change and callers may quietly adapt. With pervasive contracts, the change either preserves the old meaning or forces a contract change.

A contract change is then reviewed as a specification event rather than hidden inside proof repair.

### 7.4 Refactors gain a mathematical boundary

A component with a stable abstract view can replace its internal representation without changing its callers’ proofs. The proof becomes a refactoring boundary.

When a contract-preserving local refactor forces widespread proof changes, that is diagnostic evidence that the abstraction boundary is too leaky.

### 7.5 The composition theorem remains closed

Selective verification tends to introduce assumed helper contracts. Broad verification discharges those assumptions. The public API can be traced through verified orchestration, verified components, and verified helpers until it reaches an explicitly trusted effect.

### 7.6 Friction for semantic changes is desirable

A genuine change to append semantics, recovery, or search behavior should require coordinated changes to:

- product requirements;
- abstract operations;
- component contracts;
- executable implementation;
- component proofs;
- composition proof;
- fixtures and capacity evidence.

That is slower than changing one function and adjusting a test. For a long-lived reliability-critical product, the friction is a feature. It prevents product semantics from changing accidentally as a side effect of local optimization.

---

## 8. Why broad verification can still become unmaintainable

Cheap agent labor does not make every verification cost disappear. The hardest costs are structural.

### 8.1 Solver coupling

SMT solving is not linear in proof size. Verus’s guidance notes that adding facts can increase solver work nonlinearly and that breaking proofs into smaller pieces can turn a timeout into a fast proof.[^verus-breaking-proofs] Its troubleshooting guide also discusses proofs that become flaky after unrelated changes and recommends proof isolation techniques.[^verus-checklist]

A large global context can cause:

- long verification times;
- resource-limit failures;
- trigger instability;
- unrelated proof breakage;
- difficult diagnosis;
- serialized development around shared lemmas.

The mitigation is not less coverage. It is better proof architecture:

- small component proof roots;
- opaque semantic views;
- narrow exported lemma conclusions;
- local solver contexts;
- stable triggers and resource limits;
- cross-component reasoning through contracts rather than unfolding bodies.

Verus’s Cargo integration supports crate-level verification, incremental re-verification, and a complete verification pass before shipping.[^cargo-verus] That supports a two-speed workflow within one toolchain: focused proof development locally, complete proof closure before authoritative acceptance.

### 8.2 Specification coupling

A contract that exposes implementation details turns every refactor into a specification change.

Bad component contract:

```text
perform exactly three rotations and update bucket 7
```

Better component contract:

```text
the resulting exact index contains precisely the accepted entries represented by the input state
```

Local proofs may mention rotations and buckets. Callers should depend on the stable semantic property.

The maintainability rule is:

> Specify each component at the highest stable abstraction level sufficient for composition.

### 8.3 Architectural ossification

The easiest implementation to prove is not always the fastest implementation. A team can unconsciously optimize for solver convenience and freeze an architecture that cannot meet production capacity.

The remedy is a separate capacity gate. A proof-passing implementation that misses throughput or latency targets is not releasable. A fast implementation that lacks proof is also not releasable.

Correctness constrains optimization; it must not replace optimization.

### 8.4 Trusted escape pressure

When a proof is difficult, an agent may try to add:

- `assume`;
- `admit`;
- `external_body`;
- an axiom;
- an overly strong external specification;
- a hidden feature path;
- a proof-only implementation;
- a weaker contract.

Verus’s own guidance for LLM-assisted proof development recommends a cheat checker and explicitly calls out these shortcuts, including specification and executable-code changes made merely to get verification to pass.[^verus-llm]

Broad verification is sustainable only when these escape hatches fail closed.

### 8.5 The wrong specification can be proved perfectly

Formal verification establishes implementation-to-specification consistency. It does not guarantee that the specification expresses the desired product.

A vacuous precondition, omitted failure state, or incomplete abstract operation can make a false assurance claim look rigorous.

The response is specification governance:

- product-to-model review;
- reachability witnesses;
- explicit success and failure semantics;
- proof sensitivity checks;
- independent review of contract changes;
- requirement traceability;
- composition coverage of the actual public API.

### 8.6 The verifier remains trusted

Verus does not claim to verify itself, rustc, or LLVM.[^verus-overview] The solver and compilation chain remain in the trusted computing base. Pervasive proof narrows the unproved product logic; it does not eliminate environmental assumptions.

Honest assurance requires those boundaries to remain explicit.

---

## 9. The proof architecture that makes uniform coverage viable

Pervasive verification succeeds or fails on modularity.

Each component should expose:

- a stable mathematical view;
- meaningful preconditions and postconditions;
- representation invariants hidden from callers;
- a small set of refinement theorems;
- explicit ownership or capability inputs where relevant;
- explicit trusted effects, if any;
- a clear path into the composition theorem.

A useful protocol decomposition might look like this:

```text
sequence allocator
    guarantees disjoint ordered ranges

append preparer
    consumes one valid range
    produces a prepared batch satisfying entry invariants

publisher
    consumes a prepared batch and publication capability
    performs one legal abstract publication transition

search selector
    consumes validated candidates
    preserves exact protection, capacity, deduplication, and chronology

recovery planner
    consumes durable observations
    reconstructs an abstractly permitted state
```

The recovery proof should not depend on the allocator’s internal data structure. It should depend on the allocator’s exported disjoint-range theorem.

The publisher should not know how semantic candidates were generated. It should consume only validated authoritative identities.

This is conventional software modularity, strengthened by machine-checked contracts.

### Stable contracts, regenerable local proofs

For long-horizon maintenance, not every proof lemma deserves the same stability as the product contract.

- Product semantics should be extremely stable.
- Abstract component contracts should be stable across implementation refactors.
- Local lemmas may be rewritten or regenerated freely as long as they preserve the component theorem.
- Solver-oriented proof scaffolding is implementation detail.

This hierarchy lets agents regenerate verbose proof code without casually redefining the product.

---

## 10. Deterministic cheat checking for agent-written proofs

An LLM can be an excellent proof author and reviewer. It should not be the only authority that decides whether another LLM cheated.

The authoritative cheat checker must be deterministic and fail closed.

### 10.1 Forbidden trusted escapes

The gate should detect unapproved uses of:

- `assume`;
- `admit`;
- external bodies;
- axioms;
- external specifications;
- unchecked executable/specification bridges.

A permitted trusted construct must be explicitly declared at a narrow effect boundary and included in the evidence identity.

### 10.2 Contract-change separation

A proof repair must not silently alter the obligation.

The gate should detect changes to:

- abstract operations;
- preconditions;
- postconditions;
- invariants;
- fault assumptions;
- composition coverage.

A contract change is not automatically forbidden. It is routed through a separate specification review.

The dangerous directions deserve special scrutiny:

- **strengthening a precondition**, because it covers fewer inputs;
- **weakening a postcondition**, because it guarantees less;
- **weakening an invariant or theorem conclusion**;
- **narrowing the reachable state space**;
- **adding an assumption that implies the desired result**.

### 10.3 Production/proof parity

The verified implementation must be the production implementation.

The gate must reject:

- proof-only replacement functions;
- hidden feature differences;
- unverified fallback paths;
- generated or linked code outside the closure;
- public wrappers that bypass proved contracts;
- toolchain or policy changes judged by the same changed judge.

### 10.4 Coverage and reachability

The gate must establish that:

- every production logical component is classified and covered;
- every public entry point reaches the composition theorem;
- every theorem root is reachable;
- important preconditions have witnesses;
- major success, failure, boundary, and recovery states are represented;
- a trivial `ensures true` does not count as the component’s semantic contract.

### 10.5 Inconclusive is not success

A timeout, solver resource exhaustion, unknown result, skipped proof, missing proof, or stale cached result is an infrastructure failure. It is never proof success.

The Verus documentation emphasizes that general SMT proof is undecidable and treats resource-limit exhaustion as failure rather than evidence.[^verus-smt]

### 10.6 Proof-sensitivity checks

For critical invariants, the system should perform controlled negative mutations or equivalent checks:

- remove sequence uniqueness;
- allow overlapping authority;
- remove exact-result protection;
- admit stale publication;
- omit durable-tail reconstruction;
- disconnect a public path from composition.

Verification should fail.

This does not prove the theorem. It demonstrates that the theorem and proof closure are sensitive to the property they claim to protect.

### 10.7 The role of an LLM judge

An independent agent remains valuable for questions a syntactic checker cannot answer reliably:

- Does the abstract model capture product intent?
- Was an invariant weakened in a semantically subtle way?
- Did proof convenience drive a poor production design?
- Is the trusted adapter contract realistic?
- Does a new abstraction omit an important failure mode?

The LLM’s output is review evidence. It supplements Verus and deterministic policy enforcement. It does not replace them.

---

## 11. Do we need model checking too?

Model checking and deductive verification have complementary strengths.

A model checker explores a state space and can produce concrete counterexamples. It is excellent for finding a missing transition or surprising interleaving within the modeled bounds.

A deductive verifier proves inductive properties from an initial state and transition-preservation argument. It can establish an unbounded safety theorem over every state represented by the model, subject to its assumptions.

The project does not need a second third-party model-checking toolchain as an authoritative gate if Verus successfully proves the state-machine invariants and executable refinement.

It should still use repository-owned bounded exploration and deterministic fuzzing as supplementary discovery tools. A small explorer can enumerate short histories, crash points, reordered completions, and corrupted hints. These experiments are useful because they find counterexamples and challenge the model. They remain fixtures or fuzz evidence, not a second formal assurance regime.

The settled division is:

```text
Verus
    → authoritative deductive proof of all production logic

repository-owned bounded exploration and fuzzing
    → counterexample discovery and boundary testing

real crash campaigns
    → operating-system and adapter evidence

benchmarks
    → empirical capacity evidence
```

One formal toolchain, several accurately classified forms of supplementary evidence.

---

## 12. Effects, persistence, and semantic search

Pervasive verification does not require pretending that Verus proves the operating system or a storage device.

The architecture should separate logical decisions from physical effects.

### Effect adapters report; verified logic decides

A storage adapter may report:

- bytes written;
- flush completion;
- an error code;
- observed metadata;
- a device state.

The verified production logic decides:

- whether that observation matches an outstanding operation;
- whether a durability frontier may advance;
- whether recovery should complete, reject, or roll back a partial state;
- whether an entry may become authoritative.

The adapter contract is trusted. The journal policy is proved.

### Semantic search remains a hint source

Human semantic relevance is not a practical theorem for an embedding model. The correct verification boundary is narrower and stronger.

An external semantic system may propose candidates. Verified production logic proves that:

- each returned identity resolves to an accepted journal entry;
- stale or fabricated candidates are rejected;
- duplicates are removed;
- capacity is respected;
- the protected exact-result rule is preserved;
- final results are chronologically ordered;
- semantic-source failure cannot corrupt journal authority.

This keeps the useful accelerator outside the trusted authority boundary.

---

## 13. Correctness and the trillion-interaction objective

One trillion interactions per day is approximately 11.57 million interactions per second on average. If each interaction causes one `ADD` and one `SEARCH`, the kernel would need to process roughly 23.15 million such operations per second before peak-load headroom, maintenance, replication, or recovery work is considered.

Formal verification cannot prove that throughput without an unrealistically detailed and verified hardware cost model.

What it can prove are structural properties that make the target plausible and robust:

- no ordinary full-history scan;
- bounded hot-path loops;
- bounded in-flight authority;
- safe resource exhaustion;
- non-overlapping lane ownership;
- batching that refines the same abstract journal;
- checkpointed recovery with a bounded tail;
- compaction that preserves observations;
- derived indexes that cannot override authority.

The release process therefore needs two independent gates.

### Formal gate

The exact implementation refines the journal contract under the declared assumptions.

### Capacity gate

The exact release binary meets throughput, latency, memory, durability, retention, and recovery objectives on a precisely described server and workload.

A proof-passing implementation that is too slow is not ready. A fast implementation without proof is not ready.

This separation prevents proof convenience from ossifying an uncompetitive design while preventing performance pressure from silently weakening correctness.

---

## 14. How the journal itself supports the verification program

The verification workflow is also a product demonstration.

A Verus proof can be expensive. It should execute once for an exact immutable requirement, not once per author, reviewer, and verifier role.

The journal can hold the shared evidence:

```text
exact candidate and proof context
    → one journaled proof assignment
    → deterministic claim winner
    → one Verus execution
    → immutable receipt and artifact digests
    → reuse by reviewers, restart, replay, and integration
```

The evidence identity binds the exact tree, specifications, proof closure, toolchain, feature set, target, trusted boundary, theorem roots, and verifier result.

A changed input creates a new requirement. An unchanged exact context can reuse the durable result.

This is more than an optimization. It demonstrates the journal’s purpose: many agents coordinate through bounded shared memory, recover completed work, and avoid duplicate execution without a separate coordination database.

---

## 15. The settled engineering policy

The debate produced the following alignment.

### One verification toolchain

Verus is the sole project-level formal verification frontend. Other tools may inspire design or remain future options, but they are not parallel acceptance systems.

### All production logic is verified

There is no risk-ranked, low-levelness, business-logic, or typed-only exemption. If code contributes to production behavior, it is within the Verus source and proof closure.

### Only effects and tooling are exempt

Trusted effect adapters are narrow, explicit, and policy-free. Non-authoritative tooling must be unable to affect production behavior.

### Proof depth follows semantics

Simple components may verify automatically. Complex protocols may require substantial ghost state. No ghost-code quota exists.

### Contracts must be meaningful

A proof may be small; the obligation may not be vacuous. Every component’s responsibility must be stated and connected to product behavior.

### Contracts are more stable than local proofs

Product and component semantics change through explicit specification review. Local proof scaffolding may be regenerated as implementations evolve.

### Proofs are modular

Cross-component reasoning uses stable contracts and views, not globally visible implementation facts. Solver coupling is treated as an architecture defect.

### Cheat checking is deterministic

Forbidden assumptions, contract changes, production/proof divergence, missing coverage, skipped obligations, and self-defined judges fail closed.

### LLM review is supplementary

Agents can write and review proofs, but probabilistic judgment is not the proof authority or sole cheat checker.

### Composition closes the claim

A set of verified components is insufficient until the actual public kernel and exact authoritative tree satisfy the complete composition theorem.

### Correctness and capacity are independent

Verus proves logical behavior. Benchmarks prove empirical performance. Both gates must pass.

### Evidence is exact and reusable

Proof results are bound to immutable context, stored durably, and executed once per exact requirement.

---

## 16. What remains difficult

The strategy is coherent, not effortless.

The project must still discover:

- the best abstract journal state and transition decomposition;
- which representation views remain stable under high-performance optimization;
- how to express the concurrency and durability protocols cleanly in Verus;
- which external effects require trusted contracts;
- how to keep solver contexts modular as the codebase grows;
- how to measure and control proof runtime;
- how to make contract changes explicit without blocking legitimate product evolution;
- how to design proof-sensitivity tests that are useful rather than ceremonial;
- how to maintain exact production/proof feature parity;
- how to meet the empirical single-server capacity target.

Those are engineering and research tasks. They do not undermine the verification model. They define the work required to realize it honestly.

---

## 17. Closing perspective

The strongest reason to adopt pervasive Verus verification is not that every line of business logic individually deserves a theorem of equal sophistication.

It is that a long-running agent swarm needs one durable answer to the question:

> What does it mean for this production change to be correct?

A selective verification regime answers that differently depending on where the change lands. A multi-tool regime answers it in several languages with several trust boundaries. A test-only regime answers it with the finite examples accumulated so far.

The chosen architecture answers it once:

> The production logic must refine its meaningful contract, the component contracts must compose into the abstract journal, and the exact authoritative tree must pass the pinned Verus proof under an explicit trusted boundary.

That rule is intentionally uniform. It gives future agents one toolchain, one proof vocabulary, one composition model, and one machine-checkable meaning of semantic continuity.

The quantity of ghost code is secondary. The quality of the contracts, the modularity of the proof architecture, the honesty of the trusted boundary, and the integrity of the acceptance gate are what determine whether pervasive verification remains an asset over decades.

---

## Primary references

The following primary sources informed the tool comparison and engineering conclusions. Tool capabilities evolve; any future toolchain decision should revalidate current official documentation.

[^verus-overview]: Verus, “Verus overview,” official tutorial and reference: https://verus-lang.github.io/verus/guide/overview.html

[^verus-state-machines]: Verus, “Verus Transition Systems,” official guide: https://verus-lang.github.io/verus/state_machines/

[^verus-atomic]: Verus, “Atomic specifications,” official tutorial and reference: https://verus-lang.github.io/verus/guide/logatom-spec.html

[^verus-breaking-proofs]: Verus, “Breaking proofs into smaller pieces,” official tutorial and reference: https://verus-lang.github.io/verus/guide/breaking_proofs_into_pieces.html

[^verus-checklist]: Verus, “Checklist: what to do when proofs go wrong,” official tutorial and reference: https://verus-lang.github.io/verus/guide/checklist.html

[^verus-smt]: Verus, “SMT solving, automation, and where automation fails,” official tutorial and reference: https://verus-lang.github.io/verus/guide/smt_failures.html

[^cargo-verus]: Verus, “Using Verus via Cargo,” official tutorial and reference: https://verus-lang.github.io/verus/guide/cargo_verus.html

[^verus-llm]: Verus, “Using LLMs to develop proofs,” official tutorial and reference: https://verus-lang.github.io/verus/guide/llmforverusproof.html

[^kani-intro]: Kani, official documentation: https://model-checking.github.io/kani/

[^kani-concurrency]: Kani, “Rust feature support — Concurrency,” official documentation: https://model-checking.github.io/kani/rust-feature-support.html

[^flux-paper]: Nico Lehmann, Adam Geller, Niki Vazou, and Ranjit Jhala, “Flux: Liquid Types for Rust,” PLDI 2023: https://arxiv.org/abs/2207.04034

[^creusot-home]: Creusot, official project site and guide: https://creusot.rs/ and https://guide.creusot.rs/

[^creusot-erasure]: Creusot, “Erasure check,” official user guide: https://guide.creusot.rs/erasure.html

[^creusot-concurrency]: Creusot, “Creusot 0.9.0: Concurrency — atomic invariants,” official devlog, January 19, 2026: https://devlog.creusot.rs/2026-01-19/
