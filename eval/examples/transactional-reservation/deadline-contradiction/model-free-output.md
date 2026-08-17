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
outcome = "A public abstract reservation model and canonical value domain define the conflict-independent state and proof architecture."
included_scope = ["Canonical identifiers, checked arithmetic, and request digests", "Abstract inventory, reservation, idempotency, payment, audit, checkpoint, and recovery state", "Coverage and non-vacuous proof conventions"]
excluded_scope = ["The unresolved exact-deadline transition", "Empirical capacity conclusions"]
dependencies = []
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/DEVELOPMENT.md", anchor = "3. Abstract state", requirement = "All successful public mutations are abstract transitions. All stable errors are explicit stuttering transitions." },
]
applicable_global_invariants = ["oxide-verification-policy", "capacity-conservation", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Define canonical value types, conflict-independent abstract state, and stable component views."]
verification_goals = ["Use Verus to prove canonical encoding, checked arithmetic, initial-state validity, and conflict-independent invariant preservation without choosing deadline semantics."]
readiness = "ready"

[[stages]]
id = "trusted-effect-boundaries"
outcome = "Storage, clock, authentication, and payment effects expose qualified observations without owning reservation policy."
included_scope = ["Trusted adapter interfaces", "Real storage conditional-publication qualification", "Clock, authentication, and payment observation qualification"]
excluded_scope = ["Reservation lifecycle decisions inside adapters"]
dependencies = ["verified-foundations"]
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/DEVELOPMENT.md", anchor = "17. Publication protocol", requirement = "The storage adapter supports guarded conditional insert or update such that at most one contender succeeds for one guard instance." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "24. Trusted effects", requirement = "Adapters cannot decide reservation state, capacity sufficiency, error precedence, idempotency, isolation, or publication order." },
]
applicable_global_invariants = ["oxide-verification-policy", "capacity-conservation", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Implement policy-free effect traits and qualify every trusted premise at its real boundary."]
verification_goals = ["Prove adapter observations cannot acquire product authority and reject last-writer-wins publication, fabricated durability, clock regression, and cross-tenant effect observations."]
readiness = "ready"

[[stages]]
id = "command-publication"
outcome = "Conflict-independent commands publish one durable and idempotent mutation or one stable stuttering error."
included_scope = ["Validation and error precedence", "Qualified idempotency", "Audit sequence and durable publication"]
excluded_scope = ["Exact-deadline lifecycle choice", "Payment capture"]
dependencies = ["verified-foundations", "trusted-effect-boundaries"]
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "12. Idempotency", requirement = "The first authoritative use binds the qualified identity to the canonical request digest and terminal result." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "18. Durability acknowledgment", requirement = "An acknowledged mutation survives process termination and recovery under the declared trusted-storage assumptions." },
]
applicable_global_invariants = ["oxide-verification-policy", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Implement scoped idempotency, canonical validation, guarded publication, and stable results."]
verification_goals = ["Prove deterministic error precedence, one qualified binding, contiguous audit order, and durable publication refinement."]
readiness = "ready"

[[stages]]
id = "inventory-allocation"
outcome = "Inventory and multi-line holds preserve capacity under arbitrary concurrent contention."
included_scope = ["Capacity revisions", "Canonical group lines", "All-or-nothing hold publication", "Last-unit races"]
excluded_scope = ["Confirmation at expiration", "Payment settlement"]
dependencies = ["command-publication"]
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "1. Purpose", requirement = "For every inventory unit, the sum of active held quantity and confirmed capacity-consuming quantity never exceeds the unit's admitted capacity." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "5. Reservation creation", requirement = "The hold publishes all line allocations atomically. There is no partial hold." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "5. Reservation creation", requirement = "Two concurrent requests for the final available quantity cannot both succeed." },
]
applicable_global_invariants = ["oxide-verification-policy", "capacity-conservation", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Implement a deadlock-free canonical multi-key allocation protocol and proved derived counters."]
verification_goals = ["Prove capacity conservation, canonical duplicate-free lines, all-or-nothing group allocation, one last-unit winner, and safe capacity revision."]
readiness = "ready"

[[stages]]
id = "deadline-semantics-resolution"
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
id = "payment-settlement"
outcome = "Authorization, capture, reconciliation, and refunds preserve single-use and bounded monetary authority after lifecycle semantics are resolved."
included_scope = ["Authorization binding", "Prepared effect authority", "Settlement and refund facts"]
excluded_scope = ["Exactly-once external network claims", "Implementation before the confirmation boundary is approved"]
dependencies = ["deadline-semantics-resolution", "trusted-effect-boundaries"]
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "7. Confirmation", requirement = "One authorization cannot confirm two reservations." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "10. Refunds", requirement = "The sum of authoritative refunds cannot exceed the settlement amount." },
]
applicable_global_invariants = ["oxide-verification-policy", "capacity-conservation", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Implement authorization, settlement, refund, and recoverable effect-attempt state after the confirmation guard is authoritative."]
verification_goals = ["Prove authorization single use, one settlement, bounded refunds, one effect executor, and recovery under every adapter observation after the blocked semantic dependency is resolved."]
readiness = "ready"

[[stages]]
id = "reads-and-isolation"
outcome = "Bounded conflict-independent reads expose coherent authorized snapshots without leaking foreign existence."
included_scope = ["Reservation and inventory projections", "Authenticated pagination", "Tenant-isolated telemetry"]
excluded_scope = ["Projection of a final exact-deadline outcome before it is specified"]
dependencies = ["command-publication", "inventory-allocation"]
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "14. Isolation", requirement = "Unauthorized, foreign, and absent identifiers share one public `not_found` result." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "13. Reads", requirement = "A page is internally consistent at its bound snapshot frontier." },
]
applicable_global_invariants = ["oxide-verification-policy", "capacity-conservation", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Implement bounded snapshot reads, authoritative filtering, scoped cursor tokens, and approved telemetry."]
verification_goals = ["Prove projection correctness, pagination order and scope, cursor binding, and absent-versus-foreign public observational equivalence for specified states."]
readiness = "ready"

[[stages]]
id = "checkpoint-and-recovery"
outcome = "Conflict-independent checkpoint and recovery machinery reconstructs the unique authoritative prefix and preserves retained observations."
included_scope = ["Checkpoint encoding and validation", "Durable-tail replay", "Protected compaction planning", "Invariant validation before readiness"]
excluded_scope = ["Treating either contradictory deadline transition as authoritative"]
dependencies = ["command-publication", "inventory-allocation", "reads-and-isolation"]
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "19. Crash recovery", requirement = "Recovery reconstructs the unique authoritative prefix admitted by durable publication evidence." },
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "20. Checkpoints", requirement = "A checkpoint cannot omit a live allocation, idempotency binding, payment fact, capacity revision needed for the current projection, or audit commitment." },
]
applicable_global_invariants = ["oxide-verification-policy", "capacity-conservation", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Implement canonical checkpoint construction, guarded installation, retained-frontier planning, and deterministic replay for the approved transition subset."]
verification_goals = ["Prove checkpoint equivalence, protected-retention safety, no invented state, acknowledged-prefix preservation, and deterministic reconstruction without silently selecting deadline behavior."]
readiness = "ready"

[[stages]]
id = "exact-tree-composition"
outcome = "The public kernel composes only after the deadline contradiction is resolved and every dependent component is complete."
included_scope = ["Public composition", "Production/proof parity", "Exact prospective-tree checks"]
excluded_scope = ["Composition over an inconsistent abstract transition relation"]
dependencies = ["payment-settlement", "reads-and-isolation", "checkpoint-and-recovery"]
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/PRODUCT.md", anchor = "25. Formal correctness boundary", requirement = "The exact production tree must pass the complete composition theorem before release." },
]
applicable_global_invariants = ["oxide-verification-policy", "capacity-conservation", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Compose all public paths only after one approved deadline rule regenerates affected contracts and proofs."]
verification_goals = ["Run complete Verus composition and deterministic integrity checks against the exact prospective tree after semantic alignment."]
readiness = "ready"

[[stages]]
id = "empirical-capacity"
outcome = "The exact formally accepted binary meets scoped throughput, latency, recovery, and fault objectives."
included_scope = ["Steady and burst workloads", "Crash, recovery, adapter, isolation, and exhaustion campaigns"]
excluded_scope = ["Capacity qualification before formal composition", "Relaxed durability"]
dependencies = ["exact-tree-composition"]
source_specifications = [
  { path = "eval/examples/transactional-reservation/deadline-contradiction/specs/RESEARCH.md", anchor = "18. Throughput objectives", requirement = "An implementation does not pass by shedding valid load as `invalid_request` or by relaxing durability." },
]
applicable_global_invariants = ["oxide-verification-policy", "capacity-conservation", "tenant-noninterference", "single-linearization"]
implementation_goals = ["Run immutable workload and fault campaigns only after the exact formal subject exists."]
verification_goals = ["Require exact formal evidence first, then independently measure all preregistered capacity and fault slices without turning finite observations into proof."]
readiness = "ready"
```
