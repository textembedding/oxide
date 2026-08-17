<!-- oxide-roadmap-schema:1 -->
```toml
schema = 1
title = "Roadmap"
status = "ready"
specification_root = "eval/examples/transactional-reservation/deadline-contradiction/specs"

[[global_invariants]]
id = "oxide-verification-policy"
statement = "Production logic has meaningful contracts, component refinement, complete coverage, and exact-tree composition; trusted effects remain narrow and policy-free."
sources = []

[[global_invariants]]
id = "capacity-conservation"
statement = "Held and confirmed capacity-consuming quantities never exceed admitted capacity for any inventory unit."
sources = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "1. Purpose", requirement = "For every inventory unit, the sum of active held quantity and confirmed capacity-consuming quantity never exceeds the unit's admitted capacity." },
]

[[global_invariants]]
id = "tenant-noninterference"
statement = "Tenant-owned authority and observations remain isolated across authenticated tenant boundaries."
sources = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "14. Isolation", requirement = "All inventory, reservations, idempotency bindings, payment observations, read cursors, and audit records are tenant-scoped." },
]

[[global_invariants]]
id = "single-linearization"
statement = "Every successful mutation has one authoritative linearization point and one compatible audit order."
sources = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "17. Concurrency and linearization", requirement = "Every successful mutating operation has exactly one linearization point." },
]

[[stages]]
id = "verified-foundations"
outcome = "A public abstract reservation model, canonical value domain, and non-vacuous verification architecture define every reachable success and failure transition."
included_scope = ["Canonical identifiers, quantities, money, logical time, and request digests", "Abstract inventory, reservation, idempotency, payment, audit, checkpoint, and recovery state", "Global invariants, transition reachability, component contracts, coverage, and proof conventions"]
excluded_scope = ["Physical adapter implementations", "Empirical throughput conclusions"]
dependencies = []
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/DEVELOPMENT.md", anchor = "3. Abstract state", requirement = "All successful public mutations are abstract transitions. All stable errors are explicit stuttering transitions." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/DEVELOPMENT.md", anchor = "5. Canonical types", requirement = "Canonical request digests cover the operation kind and every semantically relevant field in a length-delimited encoding." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/DEVELOPMENT.md", anchor = "26. Verification coverage", requirement = "Simple components may have automatically discharged proofs, but their contracts must still state meaningful behavior." },
]
applicable_global_invariants = ["oxide-verification-policy", "capacity-conservation", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Define executable canonical value types and the public abstract state before broad runtime implementation.", "Establish stable component views, transition contracts, and complete production coverage metadata."]
verification_goals = ["Use Verus to prove initial-state validity, constructive reachability, canonical encoding, checked arithmetic, and preservation of every global invariant for the abstract transitions.", "Qualify deterministic checks that reject vacuous contracts, undeclared trust, disconnected proofs, and production/proof divergence."]
readiness = "ready"

[[stages]]
id = "trusted-effect-boundaries"
outcome = "Storage, clock, authentication, and payment effects expose narrow qualified observations without owning reservation policy."
included_scope = ["Trusted adapter interfaces and assumptions", "Real storage guard, durability, snapshot, corruption, and enumeration qualification", "Clock, authentication, and payment observation qualification"]
excluded_scope = ["Reservation transition policy inside adapters", "Capacity or error-precedence decisions inside adapters"]
dependencies = ["verified-foundations"]
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/DEVELOPMENT.md", anchor = "17. Publication protocol", requirement = "The storage adapter supports guarded conditional insert or update such that at most one contender succeeds for one guard instance." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "24. Trusted effects", requirement = "Adapters cannot decide reservation state, capacity sufficiency, error precedence, idempotency, isolation, or publication order." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/DEVELOPMENT.md", anchor = "30. Real adapter qualification", requirement = "Exactly one contender may succeed for each shared guard instance." },
]
applicable_global_invariants = ["oxide-verification-policy", "capacity-conservation", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Implement policy-free adapter traits and exact typed observation boundaries.", "Build real-boundary fixtures for every declared trusted premise and failure class."]
verification_goals = ["Prove verified logic consumes adapter observations only through declared contracts and preserves authority for every permitted failure.", "Require real-database and adapter campaigns to reject last-writer-wins guards, fabricated durability, cross-tenant observations, clock regressions, and ambiguous payment retries."]
readiness = "ready"

[[stages]]
id = "command-publication"
outcome = "Validated commands publish one durable, idempotent, strictly ordered mutation or one stable stuttering error."
included_scope = ["Validation and error-precedence pipeline", "Qualified idempotency binding", "Publication sequence authority and durability acknowledgment", "Bounded public result mapping"]
excluded_scope = ["Inventory-specific transition bodies", "External payment capture and refund"]
dependencies = ["verified-foundations", "trusted-effect-boundaries"]
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "12. Idempotency", requirement = "The first authoritative use binds the qualified identity to the canonical request digest and terminal result." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "18. Durability acknowledgment", requirement = "An acknowledged mutation survives process termination and recovery under the declared trusted-storage assumptions." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/DEVELOPMENT.md", anchor = "7. Operation validation pipeline", requirement = "If a step rejects, later steps do not execute." },
]
applicable_global_invariants = ["oxide-verification-policy", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Implement canonical validation, scoped idempotency resolution, mutation preparation, guarded publication, audit sequencing, and stable results.", "Ensure uncertain storage outcomes enter recovery rather than unguarded retry."]
verification_goals = ["Prove error precedence, stuttering failures, one binding per qualified idempotency identity, contiguous audit order, and durability-frontier refinement.", "Use concurrent claim and crash fixtures to show one physical publication becomes authoritative for each guard instance."]
readiness = "ready"

[[stages]]
id = "inventory-allocation"
outcome = "Inventory definition, capacity revision, and multi-line holds preserve capacity under arbitrary concurrent contention."
included_scope = ["Inventory and capacity-revision state", "Canonical group reservation lines", "Atomic all-or-nothing hold publication", "Last-unit and capacity-decrease concurrency"]
excluded_scope = ["Hold expiration and confirmation", "Payment settlement"]
dependencies = ["command-publication"]
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "1. Purpose", requirement = "For every inventory unit, the sum of active held quantity and confirmed capacity-consuming quantity never exceeds the unit's admitted capacity." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "5. Reservation creation", requirement = "The hold publishes all line allocations atomically. There is no partial hold." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "5. Reservation creation", requirement = "Two concurrent requests for the final available quantity cannot both succeed." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "4. Inventory admission", requirement = "A capacity decrease is accepted only when the new capacity is at least the sum of live held quantity and confirmed capacity-consuming quantity at the operation's linearization point." },
]
applicable_global_invariants = ["oxide-verification-policy", "capacity-conservation", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Implement checked capacity revisions and a deadlock-free canonical multi-inventory allocation protocol.", "Maintain only proved derived counters and indexes over authoritative reservation facts."]
verification_goals = ["Use Verus linear authority and atomic specifications to prove capacity conservation, duplicate-free canonical lines, all-or-nothing group allocation, one last-unit winner, and safe capacity decreases.", "Run rejecting mutants for partial group publication, counter drift, reversed lock order, and last-writer-wins capacity guards."]
readiness = "ready"

[[stages]]
id = "expiration-boundary"
outcome = "Obtain one approved rule for whether confirmation succeeds or expiration wins at logical time equal to the hold deadline."
included_scope = ["The contradictory exact-expiration-boundary requirements", "Consequent confirm-versus-expire transition and error semantics"]
excluded_scope = ["Privately selecting one rule in implementation or proof", "Unrelated inventory allocation work"]
dependencies = ["inventory-allocation"]
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "6. Hold lifetime", requirement = "At logical time equal to `expires_at`, confirmation is no longer permitted." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/BOUNDARY-CONFLICT.md", anchor = "Confirmation at the deadline", requirement = "At logical time equal to `expires_at`, confirmation remains permitted when the payment authorization was issued before `expires_at`." },
]
applicable_global_invariants = ["oxide-verification-policy", "capacity-conservation", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Ask the user to resolve the direct contradiction, persist the approved semantics in the human-readable specification, and regenerate dependent planning artifacts."]
verification_goals = ["After approval, state one non-vacuous exact-boundary transition and prove confirm/cancel/expire mutual exclusion against it; no proof is admissible while both rules remain authoritative."]
readiness = "blocked"

[[stages]]
id = "reservation-lifecycle"
outcome = "Logical-time admission, expiration publication, terminal-transition exclusion, and cancellation implement the contractible reservation lifecycle."
included_scope = ["Monotone logical-time admission and clock-regression rejection", "Deterministic EXPIRE_DUE ordering and authoritative candidate reconciliation", "Recorded expiration and atomic capacity release", "Expired-reservation visibility", "Terminal-transition mutual exclusion", "Atomic cancellation release"]
excluded_scope = ["Exact-deadline confirmation semantics", "Payment capture and refund observations", "Wall-clock accuracy promises"]
dependencies = ["inventory-allocation"]
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "6. Hold lifetime", requirement = "`EXPIRE_DUE` may publish expiration for one or more due holds in deterministic reservation-identifier order." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "6. Hold lifetime", requirement = "A foreground mutation targeting a due `held` reservation must either publish its expiration within the same serialized authority boundary or observe the terminal transition won by a concurrent contender before returning." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "6. Hold lifetime", requirement = "Exactly one effective terminal transition may leave `held` for a reservation." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "6. Hold lifetime", requirement = "Availability may conservatively continue to count a due reservation until expiration is published, but it may never treat deadline passage as an unrecorded release." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "6. Hold lifetime", requirement = "Expiration releases every held line atomically." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "6. Hold lifetime", requirement = "An expired reservation remains visible with its original lines and deadline." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "6. Hold lifetime", requirement = "Clock observations that move backward relative to the kernel's last admitted logical time return `clock_regression` and cannot change state." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "8. Cancellation", requirement = "Cancellation releases all capacity-consuming lines atomically." },
]
applicable_global_invariants = ["oxide-verification-policy", "capacity-conservation", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Implement admitted logical time, clock-regression rejection, and ordinary EXPIRE_DUE commands over revalidated deadline-index candidates.", "Publish expiration and cancellation as recorded guarded whole-reservation transitions while preserving expired-reservation visibility."]
verification_goals = ["Prove monotone logical time, clock-regression stuttering, deterministic EXPIRE_DUE order and reconciliation, and mutual exclusion of confirm/cancel/expire without choosing the exact-deadline confirmation rule.", "Prove deadline passage alone never releases capacity, recorded expiration releases every line atomically, cancelled capacity is released exactly, and expired records remain visible."]
readiness = "ready"

[[stages]]
id = "payment-settlement"
outcome = "Authorization, capture, reconciliation, and refunds preserve single-use and bounded monetary authority."
included_scope = ["Authorization binding and validation", "Prepared payment-effect authority and reconciliation", "Settlement and append-only refund facts", "Refund-complete cancellation guard"]
excluded_scope = ["Exactly-once payment-network claims", "Partial settlement"]
dependencies = ["expiration-boundary", "reservation-lifecycle", "trusted-effect-boundaries"]
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "7. Confirmation", requirement = "One authorization cannot confirm two reservations." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "10. Refunds", requirement = "The sum of authoritative refunds cannot exceed the settlement amount." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/DEVELOPMENT.md", anchor = "13. Payment effect protocol", requirement = "Exactly one live process authority may execute one prepared attempt." },
]
applicable_global_invariants = ["oxide-verification-policy", "capacity-conservation", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Implement checked authorization, settlement, refund, and effect-attempt state with stable external idempotency identities.", "Reconcile unknown effects before replacement and keep payment facts distinct from inventory release."]
verification_goals = ["Prove authorization single use, one settlement, bounded cumulative refunds, one authoritative effect executor, and safe recovery from every effect outcome.", "Qualify duplicate, conflicting, delayed, reordered, declined, not-performed, unknown, and cross-tenant adapter observations."]
readiness = "ready"

[[stages]]
id = "reads-and-isolation"
outcome = "Bounded reads expose coherent authorized snapshots without leaking foreign tenant existence."
included_scope = ["Reservation and inventory projections", "Owner, capacity-revision, and audit pagination", "Authenticated cursor binding", "Telemetry and tenant noninterference"]
excluded_scope = ["Unbounded list operations", "Perfect hardware constant-time claims"]
dependencies = ["command-publication", "inventory-allocation"]
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "14. Isolation", requirement = "Unauthorized, foreign, and absent identifiers share one public `not_found` result." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "13. Reads", requirement = "A page is internally consistent at its bound snapshot frontier." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/DEVELOPMENT.md", anchor = "15. Read model", requirement = "Derived read indexes may return candidates only. Verified filtering validates identity, scope, frontier, and authoritative state." },
]
applicable_global_invariants = ["oxide-verification-policy", "capacity-conservation", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Implement bounded snapshot projections, authenticated cursor encodings, and authoritative filtering over derived candidates.", "Constrain logs, metrics, traces, and side effects to the approved isolation surface."]
verification_goals = ["Prove projection correctness, page order and scope, cursor binding, checked availability arithmetic, and absent-versus-foreign public observational equivalence.", "Run statistical timing and content-canary campaigns as supplementary isolation evidence without presenting them as proof."]
readiness = "ready"

[[stages]]
id = "checkpoint-and-recovery"
outcome = "Checkpoints, compaction, and crash recovery reconstruct exactly the permitted authoritative state and bounded tail."
included_scope = ["Canonical checkpoint construction and install", "Protected-retention compaction planning", "Durable-tail replay and invariant validation", "Payment-effect reconciliation before readiness"]
excluded_scope = ["Recovery-time capacity claims", "Unverified physical deletion policy"]
dependencies = ["command-publication", "inventory-allocation", "payment-settlement", "reads-and-isolation"]
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "19. Crash recovery", requirement = "Recovery reconstructs the unique authoritative prefix admitted by durable publication evidence." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "20. Checkpoints", requirement = "A checkpoint cannot omit a live allocation, idempotency binding, payment fact, capacity revision needed for the current projection, or audit commitment." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/DEVELOPMENT.md", anchor = "21. Compaction implementation", requirement = "Recovery from the post-compaction image must refine the same abstract retained state as recovery immediately before deletion." },
]
applicable_global_invariants = ["oxide-verification-policy", "capacity-conservation", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Implement content-addressed checkpoints, guarded frontier installation, verified retention planning, and deterministic durable-tail replay.", "Restore all logical authority and reconcile uncertain effects before publishing readiness."]
verification_goals = ["Prove checkpoint view equivalence, protected-retention safety, deterministic recovery, acknowledged-prefix preservation, no invented state, and post-recovery global invariants.", "Exercise every storage cut, corrupt image, stale checkpoint, tail conflict, compaction boundary, and repeated recovery in real fault campaigns."]
readiness = "ready"

[[stages]]
id = "exact-tree-composition"
outcome = "The exact production kernel composes every public path, proof, coverage declaration, and trusted boundary into one release authority."
included_scope = ["Public API composition theorem", "Production/proof feature and source parity", "Deterministic integrity and proof-sensitivity checks", "Exact prospective-tree acceptance"]
excluded_scope = ["Empirical throughput qualification"]
dependencies = ["reservation-lifecycle", "payment-settlement", "reads-and-isolation", "checkpoint-and-recovery"]
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "25. Formal correctness boundary", requirement = "The exact production tree must pass the complete composition theorem before release." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/DEVELOPMENT.md", anchor = "31. Prospective-tree acceptance", requirement = "Evidence from a similar commit, rebased tree, different feature set, different toolchain, or different adapter profile cannot satisfy the gate." },
]
applicable_global_invariants = ["oxide-verification-policy", "capacity-conservation", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Compose the actual public kernel from verified components and declared effect contracts.", "Bind all exact source, proof, toolchain, target, feature, adapter, and evidence inputs into release identity."]
verification_goals = ["Run the complete Verus composition theorem and deterministic integrity checker against the exact prospective authoritative tree.", "Require controlled negative mutations for every critical authority, isolation, recovery, and coverage invariant."]
readiness = "ready"

[[stages]]
id = "empirical-capacity"
outcome = "The exact formally accepted binary meets its declared throughput, latency, recovery, isolation, and resource objectives on frozen hardware and workloads."
included_scope = ["Steady and burst workloads", "Contention, payment, clock, crash, recovery, checkpoint, compaction, and exhaustion campaigns", "Proof runtime and maintenance measurements", "Scoped capacity recommendation"]
excluded_scope = ["Extrapolation beyond measured subjects", "Replacing proof with finite experiments"]
dependencies = ["exact-tree-composition"]
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/RESEARCH.md", anchor = "18. Throughput objectives", requirement = "An implementation does not pass by shedding valid load as `invalid_request` or by relaxing durability." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/RESEARCH.md", anchor = "15. Recovery objectives", requirement = "The reference objective is 30 seconds at the 99th percentile with a checkpoint tail of ten million publication records." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/RESEARCH.md", anchor = "28. Capacity recommendation rules", requirement = "It must pass the steady workload, burst workload, last-unit race, crash campaign, recovery objective, resource-exhaustion campaign, and trusted-adapter qualification." },
]
applicable_global_invariants = ["oxide-verification-policy", "capacity-conservation", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Build reproducible workload, adapter, crash, benchmark, and proof-maintenance harnesses with immutable identities and bounded artifacts.", "Characterize saturation, headroom, and failure by preregistered workload slice."]
verification_goals = ["Require the exact-tree proof receipt before capacity qualification, then independently validate real boundary oracles and empirical objectives without weakening product semantics.", "Reject reports that hide valid load, relax durability, omit invalidity, condition exclusions on outcomes, or generalize beyond tested scope."]
readiness = "ready"
```
