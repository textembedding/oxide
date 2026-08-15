# Research contract for the reservation kernel

## 1. Purpose and evidence classes

This document defines empirical questions, workload profiles, fault campaigns,
and measurement requirements for the reservation kernel. It does not weaken or
replace the normative product behavior.

Every reported conclusion is classified as one of:

- a normative product guarantee traced to `PRODUCT.md`;
- a machine-proved property under explicit assumptions;
- a trusted-adapter assumption;
- a finite empirical observation;
- an unresolved research question.

An empirical pass cannot satisfy a Verus proof obligation.

A proof pass cannot establish wall-clock throughput, latency, storage-device
behavior, payment-network behavior, operating-system fairness, or workload
representativeness.

## 2. Target deployment question

The primary capacity question is whether one reservation-kernel cluster can serve
a globally distributed ticketing and compute-capacity marketplace while
preserving the product contract during burst contention.

The initial target is 20 million reservation interactions per minute sustained
for 30 minutes, with a ten-second burst at three times that rate.

An interaction is not an operation. The workload generator records the exact
operation expansion for each interaction class.

The benchmark must not quote interactions per second without also reporting
kernel operations, storage transactions, bytes, lines, and payment effects.

The target is provisional until production traces establish a representative
distribution.

## 3. Reference hardware

Capacity reports identify exact server count, CPU model, core count, memory type
and capacity, NUMA topology, network interfaces, storage devices, filesystem,
kernel, database version, and firmware.

The reference environment begins with three identical database servers and six
stateless kernel servers in one region.

Each database server has two 64-core processors, 512 GiB ECC memory, four mirrored
enterprise NVMe pairs, and two 100 Gb/s network interfaces.

The exact storage topology remains an empirical profile, not a product guarantee.

Scaling claims must state whether capacity came from additional kernel workers,
database connections, shards, replicas, servers, or weaker durability settings.

No report extrapolates linearly beyond measured hardware.

## 4. Workload identity

A workload profile is immutable and content addressed.

It binds operation mix, tenant distribution, inventory popularity, line-count
distribution, quantity distribution, hold duration, confirmation delay, payment
outcomes, refund behavior, request bytes, retry behavior, logical-time schedule,
fault schedule, and seed.

It also binds configuration, binary, database schema, adapter versions, hardware,
and retention state.

Changing any bound input creates a new workload identity.

Reports never combine observations from different workload identities into one
percentile series.

## 5. Interaction expansion

The reference interaction classes are:

| Interaction | Kernel operations |
| --- | --- |
| browse availability | one to twelve `GET_INVENTORY` reads |
| begin checkout | one `PLACE_HOLD` and one `GET_RESERVATION` |
| complete checkout | one `CONFIRM`, one `SETTLE`, and one read |
| abandon checkout | zero or one `CANCEL`; expiration may be background work |
| customer refund | one `REFUND` and one read |
| operator capacity change | one `REVISE_CAPACITY` and one inventory read |
| recovery poll | one owner-list page and up to four reservation reads |

The generator reports actual operation counts rather than assuming this table's
maximum.

Background expiration, checkpoint, reconciliation, audit export, and compaction
operations are measured separately and included in system load.

## 6. Operation mix

The initial steady-state mix by interaction is:

- 58 percent browse availability;
- 18 percent begin checkout;
- 11 percent complete checkout;
- 7 percent abandon checkout;
- 2 percent customer refund;
- 1 percent operator capacity change;
- 3 percent recovery poll.

Sensitivity profiles vary each class independently while holding total operation
rate constant.

The capacity conclusion must identify the first resource that saturates for each
material mix.

## 7. Tenant and owner distribution

The reference profile contains 100,000 tenants.

Tenant request frequency follows a sealed heavy-tail distribution with the top 1
percent producing 45 percent of operations.

Within a tenant, owners follow a separate sealed heavy-tail distribution.

At least 10 percent of generated identifiers refer to absent objects.

At least 10 percent of authorized probes use a foreign-tenant object identity
that must be observationally indistinguishable from absence.

No generator may route foreign probes differently based on whether the object
exists.

## 8. Inventory popularity and contention

The reference catalog contains ten million inventory units.

Popularity uses a sealed Zipf-like distribution plus scheduled flash events.

The steady profile sends 50 percent of hold lines to the hottest 0.1 percent of
inventory units.

The flash profile sends 90 percent of hold lines to one hundred inventory units
for ten seconds.

The last-unit race profile starts one unit at capacity one and submits 64, 256,
and 1,024 simultaneous single-quantity holds.

Exactly one success is a product oracle; completion time and abort rate are
empirical measurements.

The group-contention profile overlaps multi-line requests on a controlled graph
to measure deadlock avoidance and abort amplification.

## 9. Reservation shape

The line-count distribution is 70 percent one line, 20 percent two lines, 8
percent three to eight lines, and 2 percent nine through the configured maximum.

Quantity is one for 90 percent of lines, two through five for 9 percent, and a
boundary or invalid value for 1 percent.

The request-byte distribution includes minimum encodings, median production-like
metadata, maximum valid encodings, and one-byte-over-limit mutations.

Canonical line order is randomized in submitted requests to exercise sorting.

Duplicate-line mutants occur in at least one per ten thousand hold attempts.

## 10. Logical-time profile

Hold durations use a mixture of five seconds, two minutes, fifteen minutes, and
the configured maximum.

Confirmation delay is sampled independently and includes values one tick before,
exactly at, and one tick after expiration.

The campaign separately observes a due reservation before its expiration
transition and verifies that it remains capacity-consuming, that confirmation
returns `reservation_expired`, and that exactly one later terminal transition
releases or converts its lines.

The normal clock profile advances monotonically with controlled jumps.

Fault profiles repeat observations, skip ranges, regress by one tick, regress to
zero, approach integer maximum, return malformed observations, and become
temporarily unavailable.

Fixture-virtualized logical time is used for deterministic boundary oracles.

Wall-clock skew measurements are adapter evidence and do not redefine logical
time semantics.

## 11. Payment profile

Authorization and capture observations use a sandbox adapter with authenticated
record/replay support.

The normal outcome distribution is 94 percent success, 4 percent decline, 1
percent not performed, and 1 percent initially unknown.

The campaign injects duplicate identical observations, conflicting observations,
cross-tenant observations, wrong amounts, wrong currencies, expired authorization,
delayed success, reordered success and timeout, and adapter restart.

It races distinct reservations on one authorization identity, races cancellation
against every nonterminal capture-attempt state, and races refund requests whose
combined amount exceeds the remaining settlement. No losing refund request may
reach the adapter.

Wrong-currency refund attempts and same-refund-identity conflicting observations
are included as deterministic rejecting cases.

The same refund identity is also submitted concurrently with identical and
conflicting requests; at most one external attempt identity may be observed.

Each unknown result has a sealed eventual reconciliation outcome.

The kernel is evaluated on whether it preserves authority while uncertainty is
unresolved, not on guessing the hidden outcome.

External sandbox idempotency is independently verified for stable attempt
identities.

## 12. Retry profile

Clients retry after transport loss using the same qualified idempotency identity
and canonical request.

At least 5 percent of mutations experience a retry after the kernel has durably
published but before the client receives the response.

Conflicting idempotency reuse occurs at a controlled low rate.

Cross-owner reuse of identical key bytes is included and must behave as a fresh
owner-scoped binding.

Price submissions include valid, absent, attenuated, foreign-tenant, and stale
allocation-policy capabilities. Rejection behavior is deterministic contract
evidence; workload price distributions and market realism remain empirical
inputs rather than kernel correctness claims.

The generator records physical attempts, logical operations, returned results,
and external effect calls separately.

Retries while an effect is prepared, executing, or reconciling must observe the
same durable operation binding and must not increase external effect-call count.
After the configured exact-retry horizon, retained non-reuse tombstones are
exercised and must return `idempotency_expired` rather than execute the old
intent.

A reduction in apparent latency caused by duplicate execution is invalid.

## 13. Durability profile

The default capacity run uses the strongest supported synchronous database commit
profile admitted by the storage contract.

Alternative durability profiles are reported separately and cannot qualify the
default release claim.

Every acknowledged mutation is followed by randomized process termination in a
sampled crash campaign.

The recovered state is compared with the authoritative acknowledged-prefix oracle.

Storage firmware cache settings and flush semantics are recorded.

Power-loss claims require a real power-cut or equivalent vendor-supported fault
facility, not process kill alone.

## 14. Crash-cut campaign

The campaign injects faults:

- before command admission;
- after idempotency lookup;
- after logical authority reservation;
- before storage write;
- after partial physical writes;
- before commit response;
- after durable commit but before response;
- before external payment request;
- after payment-attempt and refund-authority reservation but before adapter call;
- during payment request;
- after payment success but before local publication;
- during checkpoint construction;
- during checkpoint pointer advance;
- during compaction deletion;
- during recovery replay;
- immediately before readiness publication.

Each cut has a deterministic allowed-outcome oracle derived from the product and
trusted adapter contracts.

The campaign retains the smallest failing durable image and exact replay seed.

## 15. Recovery objectives

Recovery time is measured from process start until the kernel may safely accept a
new mutation.

The reference objective is 30 seconds at the 99th percentile with a checkpoint
tail of ten million publication records.

The campaign reports checkpoint bytes, tail records, tail bytes, validation work,
reconciliation attempts, CPU time, storage reads, and peak memory.

Recovery is also measured without a usable checkpoint to characterize the
degraded full-retained-history path.

The no-checkpoint result is not the primary readiness objective.

Safety is checked for every run regardless of whether the time objective passes.

## 16. Checkpoint research

Checkpoint interval is varied by publication count and wall-clock scheduling.

The study measures foreground throughput impact, write amplification, checkpoint
construction time, install contention, tail length, and recovery time.

Checkpoint builders are raced with high-contention holds, terminal transitions,
refunds, and capacity revisions.

Input states include pending captures, outcome-unknown refunds with reserved
amount, compacted idempotency tombstones, logical time near its maximum, and a
nonzero next audit sequence. Recovery must preserve each without duplicate
effect authority or sequence reuse.

Corrupt digest, missing object, stale frontier, internal aggregate mismatch, and
truncated checkpoint mutants must be rejected.

The campaign crashes after checkpoint validation, after guarded installation,
and after each compaction deletion cut to prove that the system retains either
the original covered prefix or one durable validated checkpoint representing it.

The study seeks a configuration that bounds recovery without unacceptable
foreground cost.

Its result is empirical configuration evidence, not a replacement for checkpoint
preservation proof.

## 17. Compaction research

Retention profiles vary idempotency retry horizon, payment reconciliation horizon,
audit horizon, and cursor lifetime.

The study measures retained bytes per mutation, deletion throughput, read
amplification, checkpoint growth, and recovery behavior.

Protected-frontier mutants attempt to delete live allocation state, current
capacity basis, idempotency bindings, non-reuse tombstones, unresolved payment
attempts, reserved refund authority, refund facts, audit commitments, and live
cursor history.

Every protected deletion must be rejected before the storage effect.

Compaction is measured under concurrent foreground publication.

No retention recommendation may violate the normative minimum retained state.

## 18. Throughput objectives

The reference steady objective is the operation expansion of 20 million
interactions per minute at no more than 70 percent CPU and storage utilization.

The burst objective is three times steady interaction rate for ten seconds without
oversell, unbounded queues, or loss of acknowledged mutations.

The report includes successful and rejected operations separately.

It reports logical operations, physical retries, database transactions, rows,
bytes, lock waits, conflicts, and adapter calls.

An implementation does not pass by shedding valid load as `invalid_request` or by
relaxing durability.

Backpressure and `resource_exhausted` rates are reported per preregistered workload
slice.

## 19. Latency objectives

Under the steady reference workload, the provisional objectives are:

| Operation class | p50 | p95 | p99 |
| --- | ---: | ---: | ---: |
| inventory read | 2 ms | 8 ms | 20 ms |
| single-line hold | 4 ms | 15 ms | 40 ms |
| multi-line hold | 7 ms | 25 ms | 70 ms |
| confirm without capture | 5 ms | 20 ms | 50 ms |
| settle or refund including sandbox adapter | 30 ms | 120 ms | 300 ms |
| cancel or expire | 4 ms | 18 ms | 45 ms |

Latency is measured end to end at the kernel boundary and decomposed into queue,
verified logic, adapter, storage, and response time.

Percentiles exclude no successful request based on size or contention.

Invalidity and infrastructure-failure rates are reported alongside latency.

## 20. Fairness and starvation

The scheduler campaign mixes hot-unit holds, cold-unit holds, reads, expirations,
capacity revisions, and payment reconciliation.

It measures per-class queue delay and completion under sustained contention.

Safety does not depend on fairness.

Any progress claim states explicit executor, storage, clock, and payment-adapter
fairness assumptions.

The product does not promise strict caller fairness unless later source semantics
define it.

Starvation observations inform implementation work but do not silently create a
new public scheduling guarantee.

## 21. Tenant-isolation experiments

The experiment compares authorized probes for absent and foreign identifiers.

It records stable result code, response shape, adapter calls, storage query count,
log fields, metric labels, trace fields, allocation size, and timing distribution.

Deterministic differences in public result, detail, side effects, or telemetry are
release failures.

Timing is evaluated statistically over preregistered samples and noise controls.

A timing distinction is empirical evidence about an implementation, not a theorem
of perfect constant time.

Secret canaries include owner tokens, idempotency keys, payment tokens, raw price
evidence, and reservation line content.

## 22. Resource-exhaustion experiments

The campaign reaches every configured finite bound independently and in selected
combinations.

It covers request bytes, lines, quantities, idempotency bytes, in-flight commands,
database connections, prepared effects, checkpoint work, compaction work,
recovery tail, page size, and telemetry buffers.

The oracle requires a stable bounded error before inventory consumption or payment
effect when the product contract so requires.

The campaign measures memory high-water mark and recovery after pressure subsides.

Out-of-memory process termination is a failed capacity result, not acceptable
backpressure.

## 23. Proof runtime measurements

Every proof run records theorem roots, component, source digest, toolchain, solver
options, resource limits, wall time, CPU time, peak memory, cache status, and
result.

The study reports median, p95, and maximum proof time by component.

It measures complete exact-tree composition separately from local component roots.

Contract-preserving implementation mutations measure proof locality.

Unrelated component edits should not routinely force proof changes outside their
declared dependency closure.

Solver timeouts and instability are defects to investigate, never proof success.

## 24. Proof maintenance measurements

Representative tasks modify arithmetic representation, inventory indexing,
reservation storage layout, pagination encoding, checkpoint representation, and
payment-adapter plumbing without changing public semantics.

The study measures production lines changed, contract lines changed, proof lines
changed, components reverified, agent turns, elapsed time, and escaped defects.

It separately measures approved semantic changes that intentionally revise public
behavior.

Proof-to-code ratio is reported descriptively and is not a target.

The primary maintenance question is whether stable contracts confine proof repair
to coherent components.

## 25. Agent proof experiments

Agent trials cover creating a new proof, repairing a broken invariant-preserving
implementation, diagnosing an invalid trusted assumption, and refusing a semantic
contract weakening.

Trials use frozen prompts, model identity, reasoning effort, tool access, and
repository state.

Outcomes distinguish correct proof, vacuous proof, trusted escape, contract
weakening, production-code regression, timeout, and honest blocker.

Independent deterministic checking remains authoritative.

The study reports success rate and cost without promoting LLM judgment to proof
authority.

## 26. Proof sensitivity

Controlled mutants remove or invert:

- capacity conservation;
- canonical line uniqueness;
- all-or-nothing group publication;
- terminal-transition exclusion;
- authorization single use;
- idempotency binding uniqueness;
- effect-attempt uniqueness;
- pending-refund authority bounds;
- cumulative refund bound;
- audit sequence uniqueness;
- logical-time monotonicity;
- tenant isolation;
- checkpoint completeness;
- recovery prefix validation.

Each mutant is bound to the exact theorem or deterministic gate expected to fail.

A surviving mutant is an assurance defect even when ordinary fixtures fail it.

## 27. Statistical discipline

Every capacity experiment preregisters workload identity, hypotheses, metrics,
slices, warm-up, duration, repetitions, exclusion rules, and analysis method.

Invalid runs are retained and classified.

Invalidity cannot be conditioned on observed performance or per-case payload
content except a frozen fixture-integrity predicate evaluated before execution.

Confidence intervals accompany reported percentiles and rates where applicable.

Multiple-comparison corrections are used when selecting among many configurations.

Raw bounded receipts and content-addressed aggregate artifacts remain available
for reproduction.

## 28. Capacity recommendation rules

A recommended configuration must pass formal acceptance for the exact binary and
configuration semantics.

It must pass the steady workload, burst workload, last-unit race, crash campaign,
recovery objective, resource-exhaustion campaign, and trusted-adapter qualification.

It must not hide failures by excluding a preregistered tenant, inventory,
reservation shape, contention, or fault slice.

The recommendation records headroom and the first observed saturation resource.

It does not extrapolate beyond tested server count, hardware, workload, or
durability profile.

## 29. Open design questions

The most efficient guarded multi-inventory publication representation is open.

The best checkpoint cadence under flash contention is open.

The operational choice between one database authority and partitioned tenant
authorities is open, provided each configuration preserves the same tenant-local
product semantics and audit contract.

The payment reconciliation interface for providers without durable attempt lookup
is open; such providers cannot be admitted until the trusted contract is precise.

The maximum useful reservation line bound is open and requires workload evidence.

The retention horizon for audit and idempotency remains a deployment policy input,
not an inferred default.

Strict cross-tenant global audit ordering is intentionally absent from the current
product contract.

## 30. Required artifacts

The research harness emits immutable workload profiles, environment manifests,
binary and configuration digests, aggregate measurements, bounded failure
receipts, minimized fault images, and statistical reports.

Every artifact states whether it is formal, trusted-assumption qualification,
fixture, fuzz, crash, benchmark, or review evidence.

Artifacts never claim stronger scope than the exact subject and environment.

The release evidence set links formal correctness and empirical capacity as
independent required gates.

## 31. Research completion rule

A research question is complete only when its command exists, inputs are sealed,
the declared campaign runs, output validates against its schema, and the result is
reviewed for scope and reproducibility.

A missing runner, fixture, environment, oracle, or output schema leaves the
question planned.

A failed capacity objective does not weaken product semantics; it triggers design,
implementation, or deployment revision.

A failed proof obligation does not become an empirical question.

The kernel is release-ready only when both the exact formal gate and exact
empirical capacity gate pass.
