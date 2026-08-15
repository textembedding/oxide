# Transactional reservation and settlement kernel

## 1. Purpose

The kernel allocates finite inventory to mutually distrustful tenants without
overselling it. A caller may place a temporary hold, confirm a held reservation,
cancel an outstanding reservation, settle an accepted payment authorization, or
refund a settled reservation. Every successful mutation is durable, ordered, and
auditable.

The product is a Rust library and process boundary. Its public contract is a
state machine, not a user interface, marketplace, payment processor, or clock
service. Transport adapters may expose the operations, but the kernel's result is
defined independently of a particular transport.

The central safety claim is:

> For every inventory unit, the sum of active held quantity and confirmed
> capacity-consuming quantity never exceeds the unit's admitted capacity.

The kernel preserves this claim under concurrent calls, retry, cancellation,
process crash, storage retry, recovery, and payment-adapter duplication.

## 2. Terminology

An **inventory unit** is the smallest independently capacity-bounded product. It
is identified by a tenant-scoped `InventoryId`.

A **capacity revision** is an immutable declaration of the nonnegative quantity
available for one inventory unit. Capacity changes do not rewrite prior
revisions.

A **reservation** is a durable aggregate with one or more requested lines.

A **reservation line** binds one inventory unit to a strictly positive quantity.

A **hold** is a reservation whose lines consume inventory until confirmation,
cancellation, or expiration.

For capacity accounting, **active held quantity** means every line whose durable
reservation state is `held`, including a due hold awaiting publication of its
expiration transition.

A **confirmation** converts all live hold lines into confirmed allocation in one
atomic transition.

A **settlement** records that the payment authorization associated with one
confirmed reservation has been captured by the trusted payment adapter.

A **refund** records a monetary reversal. It does not make inventory available
unless the caller separately cancels an allocation under the cancellation rules.

An **owner** is the authenticated principal permitted to observe and mutate one
reservation.

A **tenant** owns an isolated inventory namespace and policy domain.

An **idempotency key** identifies one intended operation within an authenticated
tenant, owner, and operation-kind binding.

**Logical time** is a monotone integer supplied through the declared clock
adapter contract. Wall-clock representations are presentation metadata only.

An **acknowledged result** is a response returned after the corresponding durable
transition is authoritative.

## 3. Public identity

Every tenant, inventory unit, reservation, operation, payment authorization,
settlement, refund, and audit entry has a stable typed identifier.

Identifiers are opaque to callers. Their byte encodings are canonical and reject
non-canonical aliases.

An identifier from one tenant never resolves in another tenant. The observable
result is the same whether the foreign identifier exists or never existed.

Reservation identifiers are assigned exactly once. A failed or rejected create
operation does not publish a reservation identifier.

Audit sequence numbers are unsigned 128-bit integers scoped to one admitted
kernel instance. They increase strictly by one for each published mutation.

Sequence exhaustion fails closed before any mutation is published.

## 4. Inventory admission

`DEFINE_INVENTORY` admits a new inventory unit with an initial capacity revision
and immutable allocation policy.

The allocation policy identifies the permitted currency domain and whether a
fully refunded confirmed allocation may be cancelled to release inventory.

Capacity revision does not rewrite the allocation policy of existing
reservations.

The initial capacity is an integer in the closed interval from zero through the
configured per-unit capacity limit.

The inventory unit becomes visible atomically with its initial revision.

An exact retry through the same qualified idempotency binding returns the stored
`created` result.

After idempotency resolution, defining an inventory identifier that already
exists through a different unbound key returns `inventory_already_exists`
without comparing the new definition with the stored one. Reusing one qualified
idempotency identity with different canonical input remains
`idempotency_conflict`.

`REVISE_CAPACITY` appends a capacity revision for an existing inventory unit.

A capacity increase may take effect immediately.

A capacity decrease is accepted only when the new capacity is at least the sum
of live held quantity and confirmed capacity-consuming quantity at the operation's
linearization point.

A rejected decrease leaves the previous effective capacity unchanged.

Capacity revision history remains queryable for audit.

Revision numbers are assigned in authoritative publication order and are unique
and contiguous within one inventory unit. Concurrent revisions may both succeed
only as distinct serialized revisions, each evaluated against the capacity state
at its own linearization point.

No public operation deletes an inventory unit or erases its history.

## 5. Reservation creation

`PLACE_HOLD` takes one tenant, owner, idempotency key, currency, nonempty ordered
line list, and expiration deadline.

Each line contains a canonical inventory identifier, strictly positive quantity,
and nonnegative unit price in the request currency.

The authenticated caller must hold the tenant's price-submission capability for
the frozen allocation-policy revision. The kernel freezes that capability
binding, unit price, and policy revision as price evidence, but does not infer
market price or price fairness.

The request currency must be permitted by every referenced inventory unit's
allocation policy. Each reservation line freezes that policy with its price.

The kernel computes one checked canonical total from quantity and unit price in
canonical line order.

The request rejects duplicate inventory identifiers. A caller must combine their
quantities before submission.

The request rejects a line quantity above the configured per-line bound.

The request rejects a line list above the configured maximum line count.

The request rejects a deadline that is not strictly greater than the logical time
observed for the operation.

The request rejects a deadline farther in the future than the configured maximum
hold duration.

All requested inventory units must belong to the authenticated tenant.

All requested lines are evaluated against one logical state and one logical time.

The hold succeeds only if every line has sufficient unallocated capacity.

The hold publishes all line allocations atomically. There is no partial hold.

If any line lacks capacity, the operation returns `insufficient_capacity` and
publishes no reservation, line, or allocation.

The response identifies the created reservation, its ordered lines, expiration
deadline, owner, and publication sequence.

Two concurrent requests for the final available quantity cannot both succeed.

## 6. Hold lifetime

A hold is confirmable exactly while its state is `held` and operation logical
time is strictly less than `expires_at`.

A `held` reservation continues to consume capacity until a confirmation,
cancellation, or expiration transition becomes authoritative. Passing the
deadline alone never releases inventory from durable state.

At logical time equal to `expires_at`, confirmation is no longer permitted.

Expiration is a logical transition, not merely a comparison performed by reads.

`EXPIRE_DUE` may publish expiration for one or more due holds in deterministic
reservation-identifier order.

A foreground mutation targeting a due `held` reservation must either publish its
expiration within the same serialized authority boundary or observe the terminal
transition won by a concurrent contender before returning.

Exactly one effective terminal transition may leave `held` for a reservation.

Concurrent confirm, cancel, and expiration contenders have one winner selected by
the state at their linearization points.

`CONFIRM` at or after `expires_at` returns `reservation_expired`; it cannot be
rescued by an authorization issued earlier. Availability may conservatively
continue to count a due reservation until expiration is published, but it may
never treat deadline passage as an unrecorded release.

Expiration releases every held line atomically.

An expired reservation remains visible with its original lines and deadline.

Clock observations that move backward relative to the kernel's last admitted
logical time return `clock_regression` and cannot change state.

## 7. Confirmation

`CONFIRM` takes the reservation identifier, owner binding, operation idempotency
key, and a payment authorization reference.

Only the reservation owner may confirm it.

Confirmation requires the reservation state to be `held` and the operation's
logical time to be strictly before `expires_at`.

Confirmation binds exactly one payment authorization identity and amount summary
to the reservation.

The authorization currency must equal the reservation currency.

The authorization amount must equal the canonical total computed from the frozen
price evidence on all reservation lines.

The kernel never asks a payment adapter to decide inventory policy.

The payment adapter reports a signed authorization observation. Verified kernel
logic validates its identity, tenant, owner, amount, currency, freshness, and
single-use binding.

One authorization cannot confirm two reservations.

One reservation cannot bind two authorizations.

Confirmation converts all held quantities into confirmed quantities atomically.

The allocation remains capacity-consuming after confirmation.

Confirmation does not itself imply monetary settlement.

## 8. Cancellation

`CANCEL` takes a reservation identifier, owner binding, idempotency key, and a
typed cancellation reason.

A `held` reservation may be cancelled by its owner only before its deadline. At
or after the deadline, the required expiration reconciliation wins instead.

A confirmed but unsettled reservation may be cancelled only when every line's
frozen allocation policy permits post-confirmation release and no prepared,
executing, or outcome-unknown capture attempt exists.

A settled reservation cannot be cancelled while any non-refunded settlement
amount remains or any refund attempt has an unresolved external outcome.

Cancellation blocked by an unresolved payment outcome returns
`temporarily_unavailable` and releases no inventory. Recovery must reconcile the
attempt before cancellation is reconsidered.

Cancellation releases all capacity-consuming lines atomically.

Cancellation never creates a refund implicitly.

Cancellation of an already cancelled reservation is an idempotent success only
for the same qualified idempotency binding.

Cancellation of an expired reservation returns `reservation_expired`.

Cancellation of a reservation in another terminal state returns
`invalid_reservation_state`.

## 9. Settlement

`SETTLE` takes a confirmed reservation, owner binding, operation idempotency key,
and the frozen authorization identity, and requests capture through the trusted
payment adapter.

Before invoking the adapter, the kernel durably prepares one attempt identity and
binds it to the qualified operation. The trusted payment adapter performs the
external capture under that stable attempt identity and reports a signed
observation to the kernel.

The kernel publishes settlement only after validating the observation against
the frozen confirmation authorization.

The captured amount must equal the confirmed amount.

Partial settlement is not supported.

Over-settlement is rejected.

A payment capture observation for another tenant, owner, authorization, currency,
or amount is rejected without revealing whether its referenced object exists.

At most one settlement becomes authoritative for a reservation.

At most one nonterminal capture attempt may exist for a reservation. Concurrent
`SETTLE` calls under different idempotency keys cannot cause two physical capture
requests.

Repeated delivery of the same valid payment observation is idempotent.

Conflicting delivery for the same payment observation identity returns
`payment_observation_conflict`.

A proved adapter decline returns `payment_declined`. A not-performed or unknown
outcome returns `temporarily_unavailable` without asserting that no capture
occurred; the qualified operation remains bound for controlled reconciliation.

Settlement does not change inventory consumption.

## 10. Refunds

`REFUND` takes a settled reservation, owner binding, operation idempotency key,
positive amount, currency, reason, and refund identity, and requests the effect
through the trusted payment adapter.

Each refund has a stable refund identity, positive amount, currency, reason, and
payment-adapter receipt digest.

The refund currency must equal the settlement currency. Currency conversion is
outside the kernel and cannot be represented by choosing a different refund
currency.

Before the adapter call, the kernel durably reserves refund authority. The sum of
authoritative refunds and amounts reserved by prepared, executing, or
outcome-unknown refund attempts cannot exceed the settlement amount.

The same publication uniquely binds the tenant-scoped refund identity to its
reservation, amount, currency, reason, and external attempt identity. A second
request with the same refund identity and different content returns
`payment_observation_conflict` before any adapter call.

The sum of authoritative refunds cannot exceed the settlement amount.

Refund publication is atomic with incrementing the reservation's refunded total.

Concurrent refunds that would jointly exceed the settlement amount cannot both
acquire effect authority, so the kernel never sends both refund requests.

A succeeded observation converts its reserved amount into one authoritative
refund. A proved decline or not-performed observation releases only the reserved
refund authority; an unknown outcome retains that authority until reconciliation.
None of these bookkeeping transitions releases inventory.

Repeated delivery of one valid refund observation is idempotent.

Conflicting content under one refund identity returns
`payment_observation_conflict`.

A fully refunded reservation remains in `settled` state and its allocation remains
confirmed and capacity-consuming until a separate permitted cancellation releases
its inventory.

Refund history is append-only and remains visible after full refund.

## 11. Group reservation semantics

A reservation with multiple lines is one indivisible allocation unit.

Line order is canonical inventory-identifier byte order, independent of request
order.

Price totals are computed in canonical line order with checked integer arithmetic.

No operation may leave a subset of lines held, confirmed, cancelled, expired, or
released.

A capacity conflict on one line rejects the complete `PLACE_HOLD` operation.

A validation failure on one line rejects the complete request before any capacity
is consumed.

Confirmation, cancellation, and expiration apply to the entire line set.

The V1 product does not support splitting, merging, resizing, or transferring a
reservation.

## 12. Idempotency

Every mutating operation requires a nonempty idempotency key within the configured
byte bound.

The qualified identity is the tuple of authenticated tenant, owner, operation
kind, and idempotency-key bytes.

Before any external effect, the first authoritative use binds the qualified
identity to the canonical request digest and a durable operation identity.

When the operation reaches a terminal result, that same binding records the
result. For operations that are terminal upon admission, the following rule
applies.

The first authoritative use binds the qualified identity to the canonical request
digest and terminal result.

Reusing that qualified identity with byte-identical canonical input returns the
same terminal public result without executing an external effect again. If the
bound operation is still prepared, executing, or reconciling, the retry returns
`temporarily_unavailable` and cannot acquire a second effect authority.

Reusing it with different canonical input returns `idempotency_conflict`.

The lookup is scoped to the authenticated tenant and owner. It cannot reveal a
binding owned by another principal.

An infrastructure failure before durable publication does not bind a successful
result.

A payment effect whose observation is uncertain remains an explicit reconciliation
case; the kernel does not retry it blindly.

The exact-retry guarantee applies during the configured idempotency-retention
horizon. A compacted binding leaves a scoped non-reuse tombstone; a later request
with that qualified identity returns `idempotency_expired` rather than executing
the old intent again.

Idempotency records are durable authority, not a cache.

## 13. Reads

`GET_RESERVATION` returns one authorized reservation projection.

The projection includes identity, owner, ordered lines, frozen price evidence,
deadline, current state, confirmation, settlement, refund total, and relevant
publication sequences.

`LIST_OWNER_RESERVATIONS` returns an owner-scoped bounded page in ascending
reservation-identifier order.

`GET_INVENTORY` returns current capacity, live held quantity, confirmed
capacity-consuming quantity, and latest capacity revision.

`LIST_CAPACITY_REVISIONS` returns bounded immutable revision history in ascending
revision order.

`LIST_AUDIT` returns authorized mutation records in ascending publication sequence.

Read pagination tokens bind tenant, owner scope, query kind, last returned key,
and the snapshot frontier.

Using a token with another scope returns `invalid_page_token` without revealing
foreign state.

A page is internally consistent at its bound snapshot frontier.

## 14. Isolation

All inventory, reservations, idempotency bindings, payment observations, read
cursors, and audit records are tenant-scoped.

Idempotency tombstones and payment-effect attempts inherit the same tenant and
owner scope as the binding or observation they protect.

An owner may observe only reservations permitted by the tenant's immutable access
policy revision bound at reservation creation.

Unauthorized, foreign, and absent identifiers share one public `not_found`
result.

Timing, error detail, logs, metrics, and traces must not intentionally distinguish
foreign existence from absence.

Aggregate telemetry may expose tenant-independent counts only after applying the
declared minimum aggregation threshold.

Logs never contain owner tokens, idempotency keys, payment tokens, raw price
evidence, or reservation-line metadata.

## 15. Error precedence

Malformed canonical encoding is checked before authentication-sensitive lookup.

Authentication failure precedes tenant, owner, inventory, reservation, and
payment existence checks.

Authorization and tenant isolation precede state-specific error detail.

Idempotency resolution precedes invoking any external effect.

For an authenticated and authorized mutation, request-shape validation precedes
logical-time validation.

Logical-time validation precedes current-state transition validation.

Current-state validation precedes capacity and amount validation.

For a target that is still `held`, the expiration boundary is part of
current-state validation. It precedes authorization amount validation, so a
`CONFIRM` at or after the deadline returns `reservation_expired`.

An existing nonterminal effect binding is resolved before any replacement
adapter call. An unresolved capture or refund outcome precedes cancellation
release and returns `temporarily_unavailable`.

Capacity and amount validation precede publication.

When multiple line-capacity failures exist, `insufficient_capacity` identifies no
specific line.

An adapter infrastructure failure is returned only after verified logic has
established that the adapter call is permitted.

## 16. Public result classes

Successful mutation results are `created`, `held`, `confirmed`, `cancelled`,
`expired`, `settled`, `refunded`, or `capacity_revised`.

Stable caller errors are:

- `invalid_request`;
- `unauthenticated`;
- `not_found`;
- `idempotency_conflict`;
- `idempotency_expired`;
- `inventory_already_exists`;
- `insufficient_capacity`;
- `reservation_expired`;
- `invalid_reservation_state`;
- `authorization_mismatch`;
- `payment_declined`;
- `payment_observation_conflict`;
- `refund_exceeds_settlement`;
- `clock_regression`;
- `invalid_page_token`;
- `resource_exhausted`;
- `temporarily_unavailable`.

Stable internal adapter failures map to `temporarily_unavailable` unless a more
specific stable caller error is mandated above.

No error response claims that an uncertain external payment effect did not occur.

## 17. Concurrency and linearization

Every successful mutating operation has exactly one linearization point.

`PLACE_HOLD` linearizes when all requested capacity is atomically reserved.

`CONFIRM`, `CANCEL`, and `EXPIRE_DUE` linearize when one legal successor state
becomes authoritative.

`SETTLE` and `REFUND` linearize when their validated observations become durable
reservation facts.

`REVISE_CAPACITY` linearizes when the effective revision advances.

Reads observe an allowed state at one declared snapshot frontier.

Concurrent operations may complete in an order different from invocation order,
but their responses must agree with the single authoritative linearization order.

No process-local lock, cache, or branch is authority.

## 18. Durability acknowledgment

An acknowledged mutation survives process termination and recovery under the
declared trusted-storage assumptions.

The kernel does not acknowledge after only buffering data in volatile process
memory.

The durable publication record binds the complete canonical mutation and its
sequence.

The storage adapter reports persistence observations; verified kernel logic
decides whether acknowledgment is justified.

An interrupted publication is either absent or recoverable as the one intended
mutation. It cannot become a different mutation.

## 19. Crash recovery

Recovery reconstructs the unique authoritative prefix admitted by durable
publication evidence.

Recovery never invents a reservation, allocation, payment observation, capacity
revision, idempotency binding, or audit sequence.

Recovery never loses an acknowledged mutation.

Unacknowledged durable fragments may be completed only when their identity and
authority are unambiguous under the storage contract; otherwise they are ignored
or quarantined without publication.

Recovery reestablishes all capacity conservation invariants before accepting a
new mutation.

Recovery reestablishes the last admitted logical time before consuming a new clock
observation.

Recovery is deterministic for one durable image and one frozen configuration.

## 20. Checkpoints

A checkpoint is a derived, content-addressed representation of an authoritative
publication prefix.

Checkpoint creation does not pause successful foreground publication.

Installing a checkpoint is atomic with binding its covered audit frontier.

A checkpoint cannot omit a live allocation, idempotency binding, payment fact,
capacity revision needed for the current projection, or audit commitment.

This requirement includes non-reuse tombstones, unresolved effect attempts and
reserved refund authority, the last admitted logical time, and the next audit
sequence required to extend the covered prefix.

Recovery validates checkpoint identity and replays the bounded durable tail.

Corrupt or stale checkpoints are rejected without changing journal authority.

Before compaction removes any covered history, checkpoint installation validates
the complete checkpoint against that still-retained authoritative prefix. After
such compaction, the installed checkpoint and its guarded install record are the
durable recovery representation of the removed prefix and must themselves remain
retained and corruption-detectable.

## 21. Compaction and retention

Compaction may remove physical history only under a declared retention policy and
only after installing a checkpoint that preserves every required public
observation.

The minimum retention policy preserves all capacity-consuming reservations,
current capacity revisions, idempotency bindings within their guaranteed retry
horizon, scoped non-reuse tombstones after that horizon, unresolved payment
attempts and observations, refund history within its legal horizon, and the audit
commitments required by external policy.

Compaction never rewrites public identifiers or publication sequences.

Compaction never changes the result of an authorized read at a retained frontier.

A cursor older than the retained frontier returns `invalid_page_token`.

Retention duration and audit export policy are configuration, not inferred from
storage pressure.

## 22. Resource bounds

The kernel validates finite bounds for request bytes, line count, quantity,
currency precision, idempotency-key bytes, result-page size, in-flight operations,
payment observations, checkpoint tail, and recovery work.

Exhaustion returns `resource_exhausted` before consuming inventory or invoking a
payment effect.

Backpressure cannot grant capacity, bypass idempotency, or reorder publication.

An oversized request is rejected without allocating proportional unbounded
memory.

All loops on an admitted public request have a bound derived from validated input
or frozen configuration.

## 23. Configuration

Configuration is validated before the kernel accepts traffic.

The configuration binds maximum capacity, maximum lines, maximum hold duration,
page limits, retention horizons, checkpoint tail, adapter identities, currency
domain, price-evidence format, and resource ceilings.

Changing a semantic configuration creates a new configuration revision and does
not reinterpret prior reservations.

An invalid or internally inconsistent configuration fails process readiness.

Configuration cannot weaken tenant isolation, arithmetic checks, durability, or
capacity conservation.

## 24. Trusted effects

The storage adapter is trusted only for the explicitly declared atomicity,
durability, corruption-detection, and observation guarantees.

The clock adapter is trusted only to report a monotone logical-time observation or
an explicit regression/error.

The payment adapter is trusted only to perform an externally requested effect and
return an authenticated observation about that effect.

The authentication adapter is trusted only to report an authenticated tenant,
owner, and capability set.

Price-submission authority is one such authenticated capability. Verified logic,
not the authentication or payment adapter, checks that it is bound to the tenant
and frozen allocation-policy revision.

Adapters cannot decide reservation state, capacity sufficiency, error precedence,
idempotency, isolation, or publication order.

The kernel remains safe when an adapter reports a permitted failure.

## 25. Formal correctness boundary

All production logical components and public entry points must refine one public
abstract reservation model under the declared trusted-effect assumptions.

Formal proof establishes capacity conservation, legal state transitions,
idempotent result reuse, tenant noninterference at the specified observation
boundary, sequence uniqueness, bounded arithmetic, checkpoint preservation, and
recovery refinement.

Formal proof does not establish payment-network honesty, physical storage
durability beyond its contract, wall-clock accuracy, operating-system fairness,
throughput, tail latency, hardware capacity, or business demand.

The exact production tree must pass the complete composition theorem before
release.

## 26. Empirical capacity boundary

Benchmarks and fault campaigns determine whether one exact release binary meets
its throughput, latency, memory, storage, checkpoint, and recovery objectives on
declared hardware.

Payment sandbox campaigns determine whether the trusted adapter correctly binds
duplicate, delayed, reordered, and uncertain observations.

Clock campaigns determine whether configured clock sources satisfy their assumed
monotonicity and failure behavior.

No finite benchmark or fault campaign substitutes for a formal safety obligation.

No formal safety proof substitutes for empirical capacity evidence.

## 27. Explicit exclusions

V1 does not support waitlists, auctions, dynamic pricing, reservation transfer,
partial confirmation, partial cancellation, partial settlement, inventory
substitution, cross-tenant reservations, or automatic refund-triggered release.

V1 does not promise an exactly-once external payment network. It promises exactly
one authoritative interpretation of qualified adapter observations.

V1 does not expose administrative history deletion.

V1 does not infer business policy from historical demand.

V1 does not use semantic retrieval to decide inventory authority.

## 28. Acceptance intent

Acceptance requires executable examples for boundary quantities, last-unit races,
group all-or-nothing behavior, confirm-versus-expire races, cancel-versus-confirm
races, cancellation-versus-unknown-capture races, capacity decreases,
authorization reuse across reservations, duplicate idempotency keys, duplicate
payment observations, concurrent refund-authority reservation, tenant isolation,
crash cut points, checkpoint replacement, and bounded resource exhaustion.

Acceptance requires real storage campaigns at every declared publication cut.

Acceptance requires deterministic mutation tests that break capacity conservation,
authorization single-use, idempotency binding, terminal-transition exclusion,
sequence uniqueness, recovery prefix selection, checkpoint completeness, and
tenant isolation.

Acceptance requires separate proof and empirical receipts for the exact release
candidate.
