# Development contract for the reservation kernel

## 1. Engineering objective

Implement the product contract as a production Rust kernel whose logical behavior
is verified with Verus. The implementation must preserve one closed refinement
chain from public observations to executable code.

The production binary must not contain an alternate unverified reservation path.

Local optimization is permitted when it refines the same abstract state machine
and preserves the public results, ordering, durability, and isolation rules.

Trusted code is restricted to narrow adapters for storage, logical time,
authentication, and payment effects. Adapters report observations; verified code
decides their meaning.

## 2. Repository organization

Production logic is divided into cohesive Rust crates or modules for:

- canonical identifiers and encodings;
- checked money and quantity arithmetic;
- inventory and reservation state;
- command validation and error precedence;
- idempotency bindings;
- concurrency authority and publication;
- read projections and pagination;
- checkpoint construction and validation;
- recovery planning;
- trusted adapter interfaces;
- public API composition.

Every production logical module must appear in the verification coverage manifest.

Every public entry point must be reachable from the composition theorem.

Generated code, enabled features, build scripts, unsafe code, and foreign links
must be verified, explicitly trusted, or excluded from production reachability.

## 3. Abstract state

The public abstract model contains:

- a frozen configuration revision;
- the last admitted logical time;
- a strictly ordered audit history;
- the next audit sequence;
- tenant and owner capability views;
- inventory definitions and capacity-revision histories;
- the next per-inventory capacity revision;
- reservations and canonical ordered lines;
- live held quantities by inventory unit;
- confirmed capacity-consuming quantities by inventory unit;
- idempotency bindings in pending or terminal state and scoped non-reuse
  tombstones;
- payment authorization bindings;
- settlement observations;
- refund observations and refunded totals;
- installed checkpoint identity and covered frontier;
- retained-history frontier;
- pagination snapshot bindings;
- outstanding effect attempts and reconciliation status;
- refund authority reserved by each nonterminal effect attempt.

The initial state has validated configuration, empty tenant data, zero audit
history, sequence zero, no outstanding effect attempts, and no installed
checkpoint.

The abstract model distinguishes logical authority from physical rows, indexes,
caches, locks, and adapter handles.

All successful public mutations are abstract transitions. All stable errors are
explicit stuttering transitions.

## 4. Global invariants

For each admitted inventory unit, effective capacity is nonnegative and within
the configured bound.

For each inventory unit, held quantity plus confirmed capacity-consuming quantity is at
most effective capacity.

Every active reservation line contributes exactly once to the corresponding held
or confirmed aggregate.

No cancelled or expired reservation contributes to capacity consumption.

Every reservation has a nonempty duplicate-free line set in canonical order.

Every reservation state is reachable through the declared transition relation.

Every reservation leaves `held` at most once.

Every confirmation binds exactly one authorization and every authorization binds
at most one confirmation.

Every settlement binds exactly one confirmation.

Refund totals are nonnegative and never exceed settlement amount.

For each settled reservation, authoritative refunded amount plus refund authority
reserved by every prepared, executing, or outcome-unknown attempt never exceeds
the settlement amount.

At most one prepared, executing, or outcome-unknown capture attempt exists for a
reservation.

Every refund identity binds at most one canonical refund request and one external
attempt identity.

Every qualified idempotency identity binds at most one canonical request digest
and one terminal public result.

Audit sequences are unique, contiguous, and consistent with publication order.

The last admitted logical time never decreases.

Tenant-owned state is unreachable through another tenant's authenticated view.

Every installed checkpoint represents exactly its declared audit prefix.

## 5. Canonical types

Identifiers use fixed-size tagged byte encodings.

Decoding rejects unknown tags, wrong lengths, reserved values, and non-canonical
forms before constructing a typed identity.

Quantities use a checked unsigned integer type whose maximum equals the configured
domain maximum, never a saturating arithmetic type.

Money uses a checked integer minor-unit representation paired with a validated
currency code and currency-specific precision.

Logical time uses a checked unsigned integer count in the configured epoch.

Canonical request digests cover the operation kind and every semantically relevant
field in a length-delimited encoding.

Map iteration order is never used as a canonical order.

Canonical line order is bytewise inventory identity order.

## 6. State transition table

The reservation states are `held`, `confirmed`, `settled`, `cancelled`, and
`expired`.

The legal state transitions are:

| Current | Operation | Next | Additional guard |
| --- | --- | --- | --- |
| absent | `PLACE_HOLD` | held | all lines valid and capacity sufficient |
| held | `CONFIRM` | confirmed | operation time is before expiration and authorization validates |
| held | `CANCEL` | cancelled | caller is authorized |
| held | `EXPIRE_DUE` | expired | operation time is at or after expiration |
| confirmed | `SETTLE` | settled | capture observation validates |
| confirmed | `CANCEL` | cancelled | frozen policy permits release and no nonterminal capture attempt exists |
| settled | `REFUND` | settled | cumulative refund remains bounded |
| settled | `CANCEL` | cancelled | refund is complete, no refund outcome is unresolved, and frozen policy permits release |

`REFUND` appends monetary facts without changing the reservation state.

No other transition is legal.

The implementation must not encode cancellation and expiration as deletion.

## 7. Operation validation pipeline

Each public command follows one deterministic validation pipeline:

1. bound raw request bytes;
2. decode canonical syntax and typed identifiers;
3. authenticate the caller through the adapter observation;
4. establish tenant and owner capability scope;
5. resolve the qualified idempotency identity;
6. validate request shape and static bounds;
7. admit one logical-time observation;
8. resolve authorized current state;
9. apply transition-specific guards;
10. reserve any required effect authority;
11. prepare one canonical mutation;
12. publish through the storage contract;
13. bind the idempotency result;
14. return the stable public result.

If a step rejects, later steps do not execute.

The error returned for any reachable multi-failure input is determined by the
first rejecting step.

Effect authority is never reserved before idempotency and logical-state checks
permit the effect.

For `SETTLE` and `REFUND`, steps 10 through 14 are a durable multi-step protocol:
the idempotency binding, prepared attempt, and any monetary authority reservation
publish before the adapter call; an authenticated observation later advances the
same binding to a terminal result. A retry that finds the nonterminal binding
cannot invoke the adapter again.

## 8. Inventory implementation

The inventory component exports a mathematical view from physical inventory rows
and live-allocation counters to abstract capacities and revisions.

Capacity revisions are immutable records with monotonic revision numbers.

Revision numbers are allocated contiguously in successful publication order for
one inventory unit. Competing proposals against one predecessor revision have at
most one successful guard instance; a loser must reread authority before it can
publish a later distinct revision.

The effective revision is the unique highest published revision.

The implementation may maintain derived held and confirmed counters.

Derived counters are never authority without a proof that they equal the fold of
authoritative reservation lines at the publication frontier.

A capacity decrease and all competing allocation changes share one guarded
publication discipline.

The guard instance is evaluated against one observed prior authoritative state.

At most one contender may successfully commit against one guard instance.

## 9. Group allocation algorithm

`PLACE_HOLD` canonicalizes and validates all lines before inspecting capacity.

It computes all checked aggregate deltas before attempting publication.

The concurrency algorithm acquires logical authority in canonical inventory order
or uses an equivalent atomic multi-key conditional publication.

The algorithm must be deadlock-free under its declared scheduling assumptions.

An unsuccessful contender releases all temporary authority and publishes no line.

The linearization proof relates the physical guarded publication to one abstract
all-lines allocation transition.

A fault between physical subwrites cannot expose a partial abstract hold.

The storage representation may use an intention record, but recovery must either
complete the exact intention or leave it absent.

## 10. Expiration and logical time

The clock adapter exports `observe() -> ClockObservation` and contains no
reservation policy.

Verified logic classifies an observation as admissible, regressive, malformed, or
unavailable.

The last admitted logical time advances monotonically to the maximum validated
observation.

Hold activity is evaluated using one admitted time value per command.

Deadline passage never mutates counters by itself. A reservation in durable
`held` state remains capacity-consuming until one terminal transition publishes,
although confirmation is rejected at or after its deadline.

The expiration worker submits ordinary `EXPIRE_DUE` commands; it has no privileged
mutation path.

Due holds are selected through a derived deadline index and revalidated against
authoritative state before publication.

The deadline index may omit or duplicate candidates transiently without affecting
safety.

Progress of background expiration depends on declared worker and clock fairness
and is not asserted as unconditional liveness.

A foreground mutation targeting a due held reservation must publish expiration or
observe a concurrently published terminal transition before returning. Inventory
availability may conservatively include other due-but-unexpired-in-state holds;
eventual release depends on the declared expiration-worker fairness assumption.

## 11. Idempotency implementation

The idempotency table key is the full authenticated tenant, owner, operation kind,
and idempotency-byte tuple.

The value contains the canonical request digest, durable operation identity,
pending or terminal status, optional public result and publication sequence, and
any effect-attempt and observation identities.

Lookup never performs a key-bytes-global probe.

A repeated canonical request returns the stored terminal public result. A retry
of a prepared, executing, or reconciling effect operation returns
`temporarily_unavailable` without creating a second attempt.

A conflicting request returns `idempotency_conflict` without revalidating foreign
or later state and without invoking an effect.

For commands without an external effect, the initial terminal binding and
mutation publication are one authoritative transaction or one recoverably
coupled protocol.

For effect commands, the initial pending binding, prepared attempt, stable
external attempt identity, and any reserved monetary authority are one
authoritative publication before execution begins. The terminal observation and
result update that same binding.

There is no interval in which a published mutation or executable effect authority
lacks its required idempotency binding.

There is no interval in which an idempotency success is authoritative without its
mutation.

After the configured exact-retry horizon, compaction may replace a terminal
binding with a tenant/owner/operation-scoped non-reuse tombstone. Tombstone lookup
returns `idempotency_expired`; absence and another principal's tombstone remain
indistinguishable.

## 12. Confirmation and authorization

The authorization observation includes adapter identity, authorization identity,
tenant, owner, amount, currency, issued logical time, expiry logical time, and
signature evidence digest.

The payment adapter verifies external authenticity within its trusted contract.

Verified code validates all policy fields and single-use binding.

The authorization must be live at the confirmation operation's admitted logical
time.

The authorization amount is compared with a checked fold of frozen line-price
evidence.

The confirmation publication atomically changes state, moves aggregate capacity
from held to confirmed, binds authorization use, and appends audit evidence.

Authorization use is a guarded global binding within its tenant and payment
adapter domain. Confirmations of distinct reservations racing on one
authorization identity have at most one successful binding.

A confirmation retry cannot call the payment adapter again when a terminal
idempotency result exists.

## 13. Payment effect protocol

External capture and refund cannot be included in a local database transaction.

The kernel therefore uses explicit effect-attempt identities and reconciliation.

Before calling the adapter, verified logic publishes a unique prepared effect
attempt bound to reservation, operation, amount, currency, and adapter identity.

Preparing a capture requires the reservation to have no other nonterminal capture
attempt. Preparing a refund atomically reserves its amount against the remaining
settlement balance and uniquely binds its refund identity before any external
call.

Exactly one live process authority may execute one prepared attempt.

The adapter request carries the stable attempt identity as its external
idempotency identity.

The adapter returns `succeeded`, `declined`, `not_performed`, or `unknown` with an
authenticated observation.

`succeeded` may be published as settlement or refund only after complete binding
validation.

A succeeded refund converts exactly its reserved authority into an authoritative
refund fact. A proved `declined` or `not_performed` refund releases exactly that
reserved authority. An `unknown` refund retains the reservation until
reconciliation.

`declined` is a stable product failure when the adapter proves no effect occurred.

`not_performed` is an infrastructure failure eligible for controlled replacement.

`unknown` requires reconciliation and cannot be blindly repeated.

Recovery reconciles a prepared or unknown attempt before granting replacement
effect authority.

No production code claims exactly-once behavior from an adapter that does not
contractually provide idempotent attempt identities.

## 14. Cancellation and refunds

Cancellation guards are evaluated against the frozen allocation policy revision on every reservation line.

Release is permitted only when every frozen line allocation policy permits it.

The cancellation transition atomically releases every capacity-consuming line,
sets terminal state, and appends its audit record.

Cancellation of a confirmed reservation is disabled while a capture attempt is
prepared, executing, or outcome-unknown. Cancellation after settlement is
disabled while a refund outcome is unresolved.

Refund validation computes the checked sum of prior authoritative refunds,
amounts reserved by nonterminal refund attempts, and the proposed amount.

It first proves refund currency identity with the settlement; no numeric amount
comparison crosses currency domains.

Concurrent refund contenders share one guarded update over the observed prior
refunded and reserved totals.

Attempt preparation binds the monetary reservation. Terminal publication either
converts that reservation into the adapter observation and aggregate refunded
total or releases it after a proved no-effect outcome.

Refund facts remain append-only after complete refund.

No refund code mutates inventory counters.

Only the explicit permitted cancellation transition may release confirmed
inventory after refund.

## 15. Read model

Read projections are derived from one authoritative snapshot frontier.

The reservation projection is computed from the reservation, canonical lines,
authorization binding, settlement fact, refund history, and terminal state.

Inventory availability is effective capacity minus held and confirmed
capacity-consuming aggregates using checked subtraction proved nonnegative by the
global invariant.

Owner list order is canonical reservation identity order.

Audit list order is publication sequence order.

Pagination tokens are authenticated encodings of query kind, scope, snapshot
frontier, and last returned key.

Token decoding and scope validation occur before any cursor lookup.

Derived read indexes may return candidates only. Verified filtering validates
identity, scope, frontier, and authoritative state.

## 16. Tenant isolation

Every physical primary key includes tenant identity or belongs to an explicitly
tenant-independent configuration table.

Every authenticated query carries one unforgeable tenant capability into verified
logic.

Owner capabilities are attenuated values and cannot be upgraded by data from a
request body.

The isolation theorem establishes observational equivalence between absent and
foreign objects for the declared public result and bounded side-effect surface.

The theorem excludes uncontrolled hardware timing. Statistical timing campaigns
remain supplementary evidence.

Metrics use approved aggregate labels and never use tenant, owner, reservation,
inventory, idempotency, authorization, settlement, or refund identifiers.

## 17. Publication protocol

The publisher consumes a verified prepared mutation and a unique publication
authority token.

The prepared mutation binds its predecessor frontier and expected next sequence.

The storage adapter supports guarded conditional insert or update such that at
most one contender succeeds for one guard instance.

The adapter supports atomic publication of the mutation record, audit record,
idempotency binding, and required state projection changes, or a recoverable
protocol with the same abstract effect.

The publisher advances the abstract frontier only after receiving a qualifying
durability observation.

An ambiguous storage result becomes a recovery or reconciliation input, never a
second unguarded publication attempt.

## 18. Trusted storage contract

The storage adapter must provide:

1. atomic visibility of declared transaction members;
2. durable acknowledgment semantics tied to an explicit flush or commit result;
3. guarded conditional publication with at most one successful contender per
   guard instance;
4. stable primary-key uniqueness;
5. snapshot reads at a declared committed frontier;
6. corruption detection for content-addressed checkpoint and publication records;
7. ordered recovery enumeration of committed publication records;
8. a failure result that never fabricates successful durability.

The adapter may return uncertainty after connection loss.

Verified recovery logic resolves uncertainty from durable observations.

The adapter may not assign audit order, choose a reservation transition, interpret
capacity, or resolve idempotency conflicts.

Real-database qualification must test every trusted premise.

## 19. Recovery model

The recovery input is a storage observation containing the latest valid checkpoint,
the ordered durable tail, durable effect-attempt records, and adapter metadata
needed to validate them.

The recovery planner validates configuration identity before interpreting data.

It validates checkpoint digest, covered frontier, and internal invariants.

It rejects gaps, duplicate sequences, conflicting records, invalid predecessor
frontiers, malformed identities, and impossible transition histories.

It replays the durable tail through the same abstract transition validators used
for live commands.

It reconstructs derived counters and indexes from authoritative facts.

It computes reconciliation work for prepared or unknown payment attempts.

It publishes readiness only after the complete reconstructed state satisfies all
global invariants.

## 20. Checkpoint implementation

Checkpoint construction reads one immutable snapshot frontier.

The checkpoint contains canonical projections for frozen configuration identity,
last admitted logical time, next audit sequence, current inventories, capacity
revisions required by current state, reservations, lines, idempotency bindings
and non-reuse tombstones, payment facts, outstanding effect attempts and reserved
refund authority, pagination retention state, and audit commitment.

The checkpoint contains no adapter secrets or process-local authority.

The builder computes a content digest over a canonical length-delimited encoding.

Installation uses a guarded pointer advance from the previous checkpoint frontier.

Concurrent checkpoint builders may produce equivalent content; only one valid
frontier advance is authoritative.

Foreground publication after the checkpoint snapshot remains in the durable tail.

Checkpoint validation proves its abstract view equals the folded authoritative
prefix at the covered frontier.

This equality is established while the covered publication records remain
available. Compaction may remove them only after the guarded checkpoint install
and its qualifying durability observation are authoritative; it cannot remove
the sole valid installed checkpoint needed to represent that prefix.

## 21. Compaction implementation

The compaction planner is verified production logic.

It computes the minimum retained frontier from active state, retry horizons,
payment reconciliation, audit policy, installed checkpoint coverage, and live
pagination guarantees.

The retained projection preserves scoped non-reuse tombstones for compacted
idempotency bindings so physical deletion cannot authorize replay of an old
intent.

The storage adapter performs physical deletion only for the proved-safe range.

Deletion is a trusted effect whose requested range is chosen by verified logic.

Compaction failure leaves authority unchanged.

Recovery from the post-compaction image must refine the same abstract retained
state as recovery immediately before deletion.

## 22. Concurrency proof structure

Linear ghost authority represents inventory-guard ownership, reservation-state
transition rights, idempotency-binding rights, payment-attempt execution rights,
authorization-use rights, refund-amount reservation rights, publication sequence
authority, and checkpoint-frontier authority.

Each token has one creation rule, transfer rules, and terminal consumption rule.

No token can be duplicated by executable or proof code.

Atomic specifications identify the before and after abstract state at each
linearization point.

The multi-line hold proof establishes all-or-nothing allocation and deadlock-free
authority ordering under declared fairness.

The terminal-transition proof establishes mutual exclusion among confirm, cancel,
and expiration.

The refund proof establishes bounded cumulative amount under arbitrary contender
interleavings, including external effects that are pending or outcome-unknown.

The recovery proof establishes that no live pre-crash authority survives without
durable reconciliation.

## 23. Arithmetic proof obligations

All quantity addition, subtraction, and aggregation are checked in executable
code and connected to mathematical integer views.

All money addition and comparison preserve currency identity.

The price fold proves independence from request order after canonicalization.

Deadline addition proves configured duration cannot overflow logical time.

Audit sequence allocation proves successor existence before publication.

Page bound multiplication and buffer sizing prove allocation limits.

No proof assumes machine arithmetic equals unbounded mathematical arithmetic
without a checked range bridge.

## 24. Error proofs

Each stable public error is constructively reachable from a valid authenticated
input and an allowed state.

Each error transition leaves abstract state unchanged except for separately
specified reconciliation bookkeeping.

Error precedence is proved from the validation pipeline.

Foreign and absent identity cases map to the same public result.

Infrastructure uncertainty never maps to a false product decline or success.

An exact retry after terminal-binding compaction reaches
`idempotency_expired`, and an exact-deadline confirmation reaches
`reservation_expired`, under their declared precedence positions.

Mutation tests invert every precedence boundary and must cause deterministic
verification or fixture failure.

## 25. Adapter implementation rules

Adapter crates contain no reservation state enum, capacity calculation,
idempotency policy, transition matrix, error precedence, tenant access decision,
or audit-order decision.

Adapter inputs and outputs are typed, bounded, and fully recorded in qualification
fixtures except secret bytes.

The storage adapter may translate database errors into typed observations but may
not retry ambiguous writes without the same guarded identity.

The clock adapter may normalize a configured clock source into logical ticks but
may not extend a hold.

The payment adapter may authenticate observations but may not accept a mismatched
amount on business grounds.

The authentication adapter may validate credentials but may not read reservation
state.

## 26. Verification coverage

The coverage manifest maps every production component to:

- source roots;
- public entry points;
- product requirements;
- abstract operations;
- contracts and mathematical views;
- proof roots;
- component refinement theorem;
- composition participation;
- trusted assumptions;
- enabled features and target.

Coverage fails closed on unknown production source, missing proof roots,
unreachable public paths, feature mismatch, generated-code omission, or
undeclared trust.

Simple components may have automatically discharged proofs, but their contracts
must still state meaningful behavior.

## 27. Deterministic integrity checks

The checker rejects `assume`, `admit`, undeclared axioms, unapproved external
bodies, proof-only substitute implementations, and contract weakening disguised
as repair.

The checker rejects `ensures true`, impossible preconditions, empty reachable
state, and disconnected theorems.

The checker compares production and verified features, source closure, target,
generated code, and public entry points.

The checker binds the pinned Verus, solver, Rust toolchain, resource policy, proof
roots, coverage manifest, and trusted-boundary declaration.

A timeout, unknown solver result, missing proof, or resource exhaustion is an
infrastructure failure.

## 28. Component verification commands

The repository must provide deterministic commands for:

- canonical type and arithmetic proofs;
- abstract model invariant preservation;
- inventory allocation refinement;
- terminal reservation-transition exclusion;
- idempotency binding refinement;
- payment-attempt authority;
- read projection and pagination refinement;
- tenant isolation at the public observation boundary;
- publication and durability refinement;
- checkpoint preservation;
- compaction preservation;
- recovery reconstruction;
- public API composition.

Each command identifies immutable inputs and emits a bounded machine-readable
receipt with content-addressed logs.

The complete composition command runs against the exact prospective release tree.

## 29. Required deterministic fixtures

Fixtures cover:

- zero and maximum capacity;
- one-unit last-capacity races with at least 64 contenders;
- group holds with failure at every line ordinal;
- duplicate and unsorted line requests;
- missing, foreign, attenuated, and wrong-policy-revision price-submission
  capabilities;
- confirm at one tick before, exactly at, and after expiration;
- due-but-not-yet-transitioned holds remaining capacity-consuming;
- concurrent confirm, cancel, and expiration;
- at least 64 confirmations of distinct reservations racing on one authorization;
- cancellation racing a prepared, executing, and outcome-unknown capture;
- capacity decrease racing with hold and cancellation;
- idempotent exact retry and conflicting reuse;
- duplicate, delayed, reordered, and uncertain payment observations;
- concurrent refund-authority reservations at and above the settlement bound,
  including unknown outcomes;
- same refund identity raced with identical and conflicting canonical content;
- foreign and absent identifier indistinguishability;
- publication failure at each trusted-storage cut;
- recovery from every permitted durable image;
- checkpoint replacement during publication and checkpoints containing pending
  payment attempts, refund reservations, logical time, and next sequence;
- compaction at every protected-retention boundary;
- bounded resource exhaustion.

Every named oracle clause has at least one rejecting mutant.

## 30. Real adapter qualification

The storage qualification runs against the exact supported database and storage
configuration, not an in-memory substitute.

It races at least 64 writers on one guarded inventory update, one idempotency
binding, one authorization-use binding, one payment-attempt creation, one refund
authority reservation, one sequence allocation, and one checkpoint advance. It
also injects failure after every physical member of a multi-inventory hold.

Exactly one contender may succeed for each shared guard instance.

Here success means the conditional commit, not merely a successful public return.
Losing calls may return an idempotent stored result, a stable conflict, or reread
the new frontier and later publish a distinct legal operation; those outcomes do
not count as a second success against the original guard instance.

It injects connection loss before request, during write, before commit response,
after durable commit, and during recovery reads.

It validates snapshot visibility, primary-key uniqueness, corruption detection,
ordered enumeration, and acknowledged durability.

Clock qualification injects repeated, skipped, regressive, maximum, and malformed
observations.

Payment qualification injects duplicate, conflicting, delayed, reordered,
declined, not-performed, and unknown observations.

Authentication qualification tests foreign existence non-disclosure and
capability attenuation.

## 31. Prospective-tree acceptance

Candidate-local component proofs are development feedback, not release authority.

Before merge, the integration gate constructs the exact prospective authoritative
tree containing the candidate and current accepted frontier.

It validates source trace, coverage, trusted boundaries, production/proof parity,
all component proofs, public composition, fixtures, and required adapter evidence
against that exact tree.

Evidence from a similar commit, rebased tree, different feature set, different
toolchain, or different adapter profile cannot satisfy the gate.

Review remains independent of proof and fixture execution.

## 32. Review responsibilities

Specification review checks that the abstract model captures public semantics,
including errors, faults, and exclusions.

Implementation review checks maintainability, unsafe boundaries, arithmetic,
concurrency structure, and absence of hidden policy in adapters.

Proof review checks non-vacuity, assumption direction, reachability, refinement,
and composition.

Systems review checks trusted contracts against real adapter behavior and fault
campaigns.

Passing commands do not imply review acceptance.

Reviewers may inspect shared exact evidence rather than rerunning accepted checks.

## 33. Build and feature parity

The production binary and proof build use the same logic source, enabled features,
target architecture, generated code, and conditional compilation.

Proof-only ghost code erases without changing executable control flow.

No `cfg` branch may replace production behavior with a simpler proof subject.

Fallback implementations are either included in the proof closure or absent from
the production artifact.

The release manifest records compiler, Verus, solver, target, features, and source
digests.

## 34. Resource discipline

Verification commands have pinned per-root solver and memory limits.

Proof decomposition keeps component contexts bounded and avoids a monolithic
global solver environment.

Runtime request loops are bounded by validated request or configuration values.

Background expiration, checkpoint, reconciliation, and compaction work uses
bounded batches and durable cursors.

Resource exhaustion preserves authority and exposes explicit backpressure.

The implementation does not rely on unbounded queues for safety or progress.

## 35. Completion conditions

The development contract is satisfied only when production source, abstract
model, component contracts, proofs, coverage, trusted adapters, fixtures, and
composition evolve together.

All declared public success and failure behaviors must be executable and traced.

All production logical components must be formally covered.

Every trusted premise must have a real-boundary qualification fixture.

The exact prospective tree must pass formal correctness independently of the
empirical capacity gate.

The empirical gate must pass independently on its declared hardware and workload.
