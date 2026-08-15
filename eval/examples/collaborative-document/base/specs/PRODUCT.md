# Offline Collaborative Document Engine

## Purpose

This document defines the externally observable behavior of a Rust engine for
editing structured documents while replicas may be disconnected.

The engine accepts authenticated operations, preserves them durably, merges
concurrent work deterministically, and synchronizes replicas without requiring a
single online coordinator.

The engine is a library and a wire protocol.

It does not define an editor user interface.

It does not infer user intent from ambiguous edits.

It does not use wall-clock arrival order to resolve concurrent edits.

The normative words MUST, MUST NOT, REQUIRED, SHOULD, SHOULD NOT, and MAY carry
their ordinary requirements meanings.

## Observable boundary

The public boundary contains these operations:

- create a document;
- open a replica session;
- submit an atomic operation group;
- read a materialized document;
- read operation status;
- summarize synchronization knowledge;
- exchange missing operation groups;
- create a durable snapshot;
- restore from operations and a compatible snapshot;
- enumerate an actor-visible audit history.

Storage, transport, cryptographic verification, and monotonic process time are
trusted effects behind explicit adapters.

Conflict selection, authorization decisions, validation, causal readiness,
merge order, materialization, compaction eligibility, and recovery decisions are
authoritative logic.

## Terminology

### Principal

A principal is a stable authenticated identity represented by 32 canonical
bytes.

Principal equality is byte equality.

Display names are non-authoritative metadata.

### Replica

A replica is one durable logical writer identified by a `ReplicaId`.

Replica identity and every replica-key binding are scoped by `DocumentId`.
Reusing the same `ReplicaId` bytes in another document creates an independent
writer namespace and grants no authority in either document.

A restarted process retains its replica identity only when it can recover the
durable counter and key binding for that identity.

A copied database MUST NOT silently operate as the same replica.

### Document

A document is identified by an unpredictable 256-bit `DocumentId`.

A document contains an immutable creation record, an append-only set of admitted
operation groups, access-control state, schema state, and a deterministic
materialized view.

### Operation identifier

An `OperationId` is the tuple `(ReplicaId, ReplicaCounter, MemberIndex)`.

`ReplicaCounter` is a positive unsigned 128-bit integer allocated within one
`(DocumentId, ReplicaId)` namespace.

`MemberIndex` is a zero-based unsigned 32-bit integer within one operation group.

Within one document, the canonical total order of operation identifiers compares
`ReplicaCounter`, then the 32 bytes of `ReplicaId`, then `MemberIndex`.

A `GroupDot` is the document-scoped tuple `(ReplicaId, ReplicaCounter)` and uses
the same order without `MemberIndex`.

### Operation group

An operation group is the atomic publication unit.

Every member shares one `DocumentId`, `ReplicaId`, `ReplicaCounter`, causal
context, schema version, capability proof, and payload digest.

Members are ordered by `MemberIndex` and indices form exactly `0..member_count`.

### Causal context

A causal context is a finite version vector from `ReplicaId` to the greatest
admitted `ReplicaCounter` for that replica in the author's causally closed
document state.

A component value `n` asserts causal knowledge of every published group in that
replica namespace through `n`; pending, quarantined, and merely reserved
counters do not advance it.

An absent replica has counter zero.

The componentwise partial order defines causal ancestry.

Two groups are concurrent when neither context plus its own dot dominates the
other.

### Stable element

A stable element is a document node or text atom with an immutable identifier
derived from its creating `OperationId`.

Identifiers are never reused after deletion.

### Tombstone

A tombstone records that a stable element is not visible while retaining enough
identity and ancestry to integrate later operations.

A tombstone is not an authorization revocation.

### Snapshot

A snapshot is a content-addressed encoding of a causally closed engine state and
its covered frontier.

It is derived state and never overrides a retained admitted operation.

### Materialized view

The materialized view is the canonical visible tree, text, attributes, and
access-control projection obtained from all causally ready admitted groups.

## Global invariants

Every acknowledged operation group remains durable and discoverable after a
successful restart under the declared storage assumptions.

Two replicas with the same admitted operation groups and schema catalog produce
byte-identical canonical materialized documents.

No operation affects a document unless its capability was authorized by the
causally prior access-control state named by that operation.

No admitted group is partially visible.

No rejected group changes durable document, authorization, schema, counter, or
snapshot authority. A bounded digest-keyed rejection disposition may support
`GET_STATUS` and idempotent diagnostics without reserving an unbound counter.

Every visible element has exactly one stable creation identity.

Deleting an element never permits its identifier to denote a different element.

Materialization depends on causal and canonical identifiers, never on receipt
time, thread scheduling, transport route, or hash-map iteration order.

Every public result is scoped to exactly one authenticated principal and one
document.

A snapshot represents exactly the same authoritative state as replaying its
covered operation closure from the initial document state.

Compaction never changes any result observable through the public boundary for
retained history.

Unknown or malformed peer input cannot mutate authoritative state.

## Identity and encoding

### Canonical bytes

Unsigned integers use fixed-width big-endian encoding.

Strings are valid Unicode scalar-value sequences encoded as UTF-8.

Canonical map keys are sorted by their encoded bytes.

Duplicate map keys are invalid.

Floating-point values are not valid authoritative fields.

Unknown fields in a known wire version are rejected unless the enclosing type
explicitly defines an extension map.

### Digests

The content digest is SHA-256 over the canonical operation-group bytes excluding
the signature field.

Digest equality is relied on only under the declared collision-resistance
assumption.

A payload with a mismatching digest is malformed.

### Signatures

Each operation group carries a signature by the key bound to its `ReplicaId`.

Signature validity authenticates bytes and replica binding.

It does not authorize the requested document operation.

### Counter allocation

A replica durably reserves each document-scoped counter before exposing a signed
group using that counter.

A qualified writer exposes counter `n` only after the corresponding group is
locally admitted against its causal state, and it does not expose a successor
while any lower counter lacks an admitted group. Draft validation failures occur
before exposure and do not create a public counter hole.

The same `(DocumentId, ReplicaId)` identity MUST NOT admit or quarantine two
different group digests at one counter.

Reusing an already bound counter with identical canonical bytes is an idempotent
retry.

Reusing an already bound counter with different canonical bytes returns
`counter_conflict`. A retained rejection reached before binding does not reserve
the counter.

## Document creation

### Request

Creation supplies:

- a fresh `DocumentId`;
- the creator principal;
- the creator's document-scoped `ReplicaId` and initial verification key;
- an initial owner capability;
- a supported schema identifier and version;
- an optional bounded title;
- a signature over the canonical creation bytes by that initial key;
- a request idempotency key.

### Result

Successful creation produces a creation group at replica counter one and an empty
root container.

The authenticated creator, replica identity, initial key, and signature must
agree before document publication. Creation atomically establishes the first
replica-key binding and counter-to-digest binding with the document record.

The creator receives `admin`, `write`, `read`, `share`, and `snapshot`
capabilities.

The creation group is causally first for the document.

Repeating the exact request returns the existing document identity.

Reusing the idempotency key for different canonical creation bytes returns
`idempotency_conflict` within the same principal namespace.

### Collision

Creating an already existing `DocumentId` with different creation bytes returns
`document_exists`.

No caller can use this error to enumerate documents outside its authenticated
namespace; inaccessible and absent identifiers both return `not_found` at the
ordinary lookup boundary.

## Operation-group admission

### Validation order

Admission evaluates these checks in order:

1. wire size and structural bounds;
2. canonical decoding;
3. digest equality;
4. cryptographic signature validity against the declared verification key;
5. document visibility to the authenticated peer;
6. counter replay or conflict;
7. causal-context well-formedness;
8. causal admission-floor eligibility, yielding `history_expired` when the
   context is too old;
9. causal-dependency availability, yielding `pending_dependencies` when incomplete;
10. replica-key binding validity in the now-complete referenced causal state;
11. capability validity in that same causal state;
12. supported schema identity, yielding `unsupported_schema` quarantine when
    the exact definition is unavailable;
13. supported operation tags and member-specific preconditions;
14. durable atomic publication.

The first failing check determines the public error.

Checks after the selected error perform no authoritative effect.

### Atomicity

An admitted group publishes all members or none.

Members observe the same pre-group state for authorization.

Structural validation of member `n` observes the pre-group state plus the
validated effects of members below `n`.

Within-group semantic dependencies follow ascending `MemberIndex`.

An invalid later member rejects the entire group.

No member from a rejected group becomes a causal dependency.

### Causal readiness

A structurally valid signed group whose causal predecessors are absent is stored
as a non-admitted pending envelope and returns `pending_dependencies`.

That pending transition atomically records the exact envelope by digest before
returning. It does not bind the document-and-replica counter before the causal
replica-key state is available and validated.

Pending storage is durable but does not alter the materialized view.

When all predecessors become available, admission resumes with causal
replica-key, capability, schema, and member validation without a new client
submission.

A group that then fails semantic validation moves from pending to a retained
rejected digest disposition without creating a counter binding. A rejection
reached while re-evaluating an already bound quarantine retains that binding.

A ready or qualified-quarantine outcome performs one guarded
counter-to-digest bind. Under the required non-equivocation rule for a qualified
replica, at most one promoted envelope is valid for a group dot; invalid or
conflicting envelopes cannot win authority by arriving first.

Ready groups are materialized in a deterministic topological order whose tie is
the canonical group-dot order.

### Duplicate delivery

An identical admitted, pending, quarantined, or retained rejected group returns
its current status and performs no second effect.

Duplicate delivery does not change audit ordering.

Duplicate delivery does not consume an additional capacity unit.

## Document structure

### Node kinds

The initial schema defines:

- document root;
- section;
- paragraph;
- ordered list;
- unordered list;
- list item;
- code block;
- quote block;
- text run;
- inline mention;
- opaque extension node.

The root cannot be deleted or reparented.

Only schema-permitted parent-child pairs materialize.

### Text atoms

Text is represented as immutable Unicode scalar-value atoms.

An insertion names a stable left neighbor and a stable right neighbor defining a
gap observed by the author.

Both neighbors must belong to the same text container in the author's causal
state.

The inserted string must contain between 1 and 16,384 scalar values.

Each scalar receives a stable identity from the inserting member plus its scalar
offset.

### Node insertion

A node insertion names a stable parent and a stable sibling gap.

The parent must be live in the author's causal state.

The new node identifier is derived from the inserting operation.

### Deletion

A delete names one or more stable element identifiers.

Deleting an already tombstoned element is a successful no-op.

Deleting an unknown identifier whose creator is not in the causal context returns
`unknown_element`.

Deleting an identifier whose creator is known but whose creation group is absent
keeps the group pending.

### Moves

A move names a live node, a target parent, and a sibling gap.

Root and text atoms cannot be moved.

A move that creates a cycle in the author's causal state returns `cycle`.

Concurrent moves are resolved by the rule in Section 10.4.

### Attributes

An attribute assignment names a node, a schema-defined key, and a typed value.

An attribute deletion is an assignment of the schema's absent marker.

Attributes do not contain executable code.

## Access control

### Capability records

A capability record binds a document, subject principal, set of rights, stable
grant identifier, optional causal expiration frontier, granting operation, and
delegation flag.

Rights are `read`, `write`, `share`, `admin`, and `snapshot`.

`admin` does not imply rights absent from the same capability record.

For an operation, a grant is active exactly when its granting operation is in the
operation's causal state, no named revocation is in that state, and either no
expiration frontier exists or the operation's causal context does not dominate
it. Equality reaches expiration; a context concurrent with the frontier does not.

An expiration frontier is document-scoped, causally at or after the grant, and
passes the same bounded version-vector validation as an operation context.

### Grant

A grant requires `share` for ordinary rights and `admin` for `admin` or delegable
rights.

A principal cannot delegate a right it does not possess in the referenced causal
state.

The grant takes effect causally after its operation.

### Revoke

A revocation names a stable grant identifier.

Revocation prevents operations causally after the revocation from relying on the
grant.

An operation concurrent with a revocation is authorized by its own causal state
and remains valid if the grant was then active.

This causal rule is intentional and does not promise retroactive invalidation of
offline work.

### Key rotation

A replica-key rotation requires an authorized principal and names the prior key
binding.

Operations signed by the old key and causally after the rotation are rejected.

Operations causally before or concurrent with rotation are evaluated using their
referenced binding state.

### Read isolation

An unauthorized principal cannot read document contents, operation payloads,
participant identities, access lists, or existence.

Audit records redact payloads for principals lacking `read`.

## Deterministic conflict semantics

### General rule

The engine preserves every admitted operation even when its visible effect loses
a deterministic conflict.

A losing operation remains in audit history and causal contexts.

Conflict resolution never deletes evidence.

### Concurrent text insertion

Concurrent insertions into the same stable gap are ordered by descending canonical
group-dot order and then ascending member-local scalar offset.

An insertion causally after another insertion observes and may target the newly
created gaps, so it is not reordered as concurrent work.

Nested gap traversal is depth first from left boundary to right boundary.

This ordering is independent of transport arrival.

### Delete against edit

Deletion makes the target element invisible once both operations are present,
whether the edit is before or concurrent with the delete.

An attribute assignment to a tombstoned node remains retained but invisible.

An insertion whose container is concurrently deleted remains retained under that
container and is invisible while the container is tombstoned.

No operation implicitly resurrects a tombstone.

### Concurrent moves

For each moved node, candidate moves are considered by descending operation-id
order.

The first candidate whose target is live and whose selection keeps the complete
materialized parent relation acyclic wins.

If the highest candidate would create a cycle only because of another concurrent
winning move, the next candidate is considered.

If no candidate is valid, select the greatest operation identifier among the
causally maximal valid prior parent assignments. If no prior move exists, retain
the creation parent. A retained tombstoned parent keeps the relation but makes
the descendant invisible; it is not an implicit resurrection.

This global selection repeats in stable node-id order until a fixed point; the
finite candidate set and strictly decreasing candidate ranks guarantee
termination.

### Concurrent attributes

For one node and attribute key, the assignment with greatest canonical operation
identifier wins among pairwise concurrent maximal assignments.

Causally later assignments supersede causal predecessors regardless of identifier
order.

The absent marker participates like any other typed value.

### Concurrent access changes

Grant and revoke operations address stable grant identifiers rather than mutable
subject slots.

A revoke wins only over the grant it names and causal descendants that rely on
that grant.

Independent grants to the same subject remain independent.

### Multi-member conflicts

Members of one atomic group are never treated as concurrent with each other.

Group-local order resolves dependencies before inter-group conflict selection.

The group is still one audit and durability unit.

## Materialization and convergence

### Input set

Materialization consumes the causally closed set of admitted ready groups and the
exact schema catalog referenced by those groups.

Pending, malformed, rejected, and unauthorized groups are excluded.

### Determinism

Canonical materialization emits the stable tree, visible text, winning typed
attributes, active capabilities, schema identity, and covered causal frontier.

Canonical output contains no process timestamps or storage offsets.

### Strong convergence

Any two qualified replicas with identical admitted-group sets and identical schema
catalogs return identical canonical materialized bytes.

Delivery order may affect temporary pending status but not the converged result.

A replica that permanently lacks a predecessor is not claimed to have converged.

### Intent preservation boundary

The product guarantees the specified deterministic outcome.

It does not guarantee that concurrent edits match human intent.

Interface layers may present conflicts or offer user-authored corrective
operations without changing authoritative history.

## Synchronization

### Knowledge summary

A knowledge summary contains the document identity, schema catalog digest, and,
for each replica, a greatest contiguous counter, an advertised observed ceiling,
and bounded ranges of known gaps at or below that ceiling.

It is authenticated by the session but is not itself an admitted document
operation.

A peer MUST NOT claim a contiguous counter through a missing group.

Counters above the advertised ceiling are unclaimed. When all gaps cannot fit,
the sender lowers the advertised ceiling to precede the first omitted gap;
truncation therefore loses compression but never turns absence into presence.

### Missing-set exchange

Given two summaries, each peer computes counter ranges that the other may lack.

Groups are transferred in bounded frames that may arrive out of order or repeat.

The receiver applies normal admission to every group.

Transport delivery never bypasses authorization, decoding, or causal readiness.

### Reconnect completion

If two authorized replicas remain connected, exchange fair delivery, possess
compatible schemas, and stop creating new operations, synchronization eventually
leaves them with equal admitted-group sets.

This completion claim also requires that qualified replica keys do not sign two
different canonical groups for the same document-scoped group dot. Equivocation
is detected as `counter_conflict`, but an offline protocol without consensus does
not promise automatic reconciliation of two already admitted conflicting dots.

This liveness statement depends on explicit fair-delivery, durable-storage, and
resource-availability assumptions.

The kernel proves safety for every finite exchange prefix.

Network fairness and completion time are empirical or trusted assumptions, not
pure program theorems.

### Durable cursors

Each peer session may persist a cursor summarizing acknowledged transfer frames.

Cursor loss causes safe retransmission, not skipped groups.

A cursor cannot make an unacknowledged group appear durable.

### Backpressure

The receiver advertises a bounded byte and group window.

A sender exceeding the window receives `backpressure` and must retain unsent
groups.

Backpressure never changes causal priority or drops admitted history.

## Schema evolution

### Catalog

The schema catalog is an append-only set of signed schema definitions identified
by `(SchemaId, Version)` and content digest.

A definition names node kinds, parent-child rules, attribute keys and types,
operation tags, migration functions, and compatibility predecessors.

### Unknown-schema admission

A group using an unknown schema remains quarantined as `unsupported_schema` and
does not enter authoritative document history.

Quarantine occurs only after causal replica-key and capability validation. Its
durable transition atomically binds the exact group dot to the quarantined digest
so later delivery cannot substitute different bytes at that counter.

After the exact signed schema becomes available, the group is re-evaluated from
canonical bytes through ordinary admission.

Quarantine is bounded by deployment policy and may reject new unknown-schema
input with `quarantine_full`.

### Migration

A migration is a deterministic total function from one canonical materialized
schema view to another or a typed `migration_rejected` result.

Migration never rewrites admitted operation bytes.

The migration operation records source and target schema digests.

All replicas select the same migration function by exact signed identity.

### Forward compatibility

Known opaque extension nodes round-trip byte-for-byte even when ordinary clients
cannot interpret their payload.

Unknown operation tags are not treated as opaque effects.

No schema may redefine an existing operation tag or attribute type.

## Snapshots and compaction

### Snapshot frontier

A snapshot frontier is a version vector for which every included group and every
causal predecessor is present.

The snapshot captures canonical authoritative state, retained tombstone metadata,
schema identities, access-control state, exact pending and quarantined envelopes,
retained rejected dispositions, durable counter-to-digest bindings, and every
index required to reconstruct those records without inventing authority.

### Snapshot creation

Snapshot creation requires `snapshot` capability.

The encoded snapshot binds document identity, frontier, state digest, schema
catalog digest, format version, and producer build identity.

Creation is copy-on-write with respect to concurrent operation admission.

The snapshot represents one exact closed frontier even if newer groups commit
during encoding.

### Restore

Restore validates structure, digests, schema compatibility, document identity,
and frontier closure before publishing recovered state.

An operator-initiated restore requires `snapshot` capability in the currently
published document state. Its candidate snapshot and retained suffix must cover
every acknowledged group and binding in that storage generation; restore cannot
roll a live document back past acknowledged history.

Restore then replays retained groups above the frontier through the same abstract
transitions used by normal admission.

An invalid snapshot returns `snapshot_invalid` and leaves the last durable state
unchanged.

### Compaction eligibility

A group below a snapshot frontier may be physically discarded only when all
configured retention acknowledgments cover it and every stable identifier needed
by live operations remains represented in snapshot metadata.

Tombstone metadata remains until no retained or admissible operation can reference
the identifier under the declared retention horizon.

Compaction is an implementation effect, not a logical deletion from audit history
within the retained product window.

### Retention floor

Each document has a monotone causal `admission_floor`, initially zero. The frozen
retention policy names the replica acknowledgments required to advance it. An
advance is an authenticated admitted record, is covered by a valid snapshot, and
cannot exceed any required acknowledgment.

After an advance, a newly submitted group whose causal context does not dominate
the floor returns `history_expired` before missing-predecessor classification.
This is the sole mechanism by which an offline reference becomes inadmissible.
Compaction may discard stable identity support only when every retained operation
is above the floor and no retained operation above it references that identity.

Changing the required acknowledger policy starts a new storage generation; it is
not an adapter decision or a wall-clock timeout.

### Recovery

After a crash, recovery selects the newest fully durable valid snapshot and log
suffix.

Partial snapshot files, partial log frames, and unacknowledged group writes are
ignored.

Recovery never combines a snapshot with a suffix from another document or storage
generation.

## Status and audit

### Group status

`GET_STATUS` returns `pending_dependencies`, `unsupported_schema`, `ready`,
`materialized`, `rejected` with a stable error, or `unknown`.

Submission may enter `pending_dependencies`, `unsupported_schema`, `ready`, or a
retained `rejected` disposition. `unsupported_schema` may transition to `ready`
or `rejected` after schema installation; `pending_dependencies` may transition
to `unsupported_schema`, `ready`, or `rejected` as dependencies become complete.

Ready never returns to pending, and materialized and rejected are terminal for one
exact submitted digest while its disposition is retained.

`unknown` is an observation that the queried identity has no stored disposition;
it is not a stored terminal state.

### Audit order

Audit enumeration orders admitted groups by the deterministic topological order
used for materialization.

Concurrent topological ties use canonical group-dot order.

Audit includes losing conflict operations, capability changes, and schema
migrations in that operation order.

Snapshot maintenance records follow operation records and are ordered by covered
frontier canonical bytes, then snapshot digest.

Physical storage order is not exposed as semantic chronology.

### Redaction

Callers with `read` see operation kinds, actors, causal contexts, and payloads.

Callers with audit-only deployment permission may see digests, kinds, and status
without content.

No caller sees another document through cursor or count side channels at the
logical API boundary.

## Errors

### Stable errors

The public error catalog contains:

- `invalid_encoding`;
- `payload_too_large`;
- `digest_mismatch`;
- `signature_invalid`;
- `not_found`;
- `counter_conflict`;
- `idempotency_conflict`;
- `unsupported_schema`;
- `quarantine_full`;
- `causal_context_invalid`;
- `history_expired`;
- `permission_denied`;
- `unknown_element`;
- `cycle`;
- `group_invalid`;
- `pending_dependencies`;
- `backpressure`;
- `snapshot_invalid`;
- `migration_rejected`;
- `capacity_exhausted`;
- `temporarily_unavailable`.

### Error privacy

`not_found` covers both absent and unauthorized document identities for lookup.

Detailed malformed-input errors are available only after the caller is authorized
to submit to the named document.

Error detail MUST NOT disclose principals, schema payloads, or operation content
outside the caller's rights.

### Infrastructure failure

An adapter failure before durable publication returns
`temporarily_unavailable` and acknowledges nothing.

An adapter uncertainty after the durability decision is resolved by recovery and
idempotent retry using the exact group identity.

The engine does not report success unless the durability adapter confirms the
declared acknowledgment point.

## Resource contract

### Declared bounds

One operation group contains at most 256 members.

Canonical group bytes are at most 4 MiB.

One text insertion contains at most 16,384 Unicode scalar values.

One causal context names at most 65,536 replicas.

One knowledge summary names at most 65,536 replica ranges and 4,096 explicit
gaps.

One synchronization frame carries at most 8 MiB and 512 groups.

One pending group names at most 4,096 missing predecessor ranges.

Exceeding a structural bound fails before authoritative allocation proportional
to the claimed oversized length.

### Complexity guarantees

Canonical decoding is linear in accepted input bytes plus declared collection
members.

Operation admission performs no scan over unrelated documents.

Materialization work is bounded by the document's stable elements plus
deterministic index operations, except explicit full snapshot construction.

Insert, delete, and attribute implementations SHOULD demonstrate affected-region
costs empirically, but that optimization is not a semantic guarantee.

The exact constants and production capacity are empirical release properties.

### Denial resistance

Pending, quarantine, retained-rejection, session, and transfer-window capacity
are configured per tenant and globally.

Evicting a bounded rejection diagnostic may change only that digest's status from
`rejected` to `unknown`; it cannot remove a bound counter or any document
authority.

Capacity rejection is deterministic for one frozen configuration and state.

Eviction never removes admitted authoritative groups.

## Formal and empirical claims

### Formal claims

The implementation must establish under declared assumptions:

- group atomicity;
- counter uniqueness;
- authorization safety;
- causal readiness safety;
- deterministic conflict selection;
- tree acyclicity;
- strong convergence for equal admitted sets and schemas;
- snapshot replay equivalence;
- compaction observational preservation;
- recovery prefix integrity;
- malformed-input non-interference;
- public API refinement to this abstract behavior.

### Trusted assumptions

Formal claims depend on explicitly bounded adapter contracts for atomic durable
conditional publication, persistence after acknowledgment, authenticated
signature verification, digest collision resistance, transport origin binding,
secret isolation, and fair delivery only for stated reconnect liveness.

### Empirical claims

Benchmarks and campaigns, not Verus, establish operations per second, end-to-end
synchronization latency, memory and disk amplification, snapshot duration,
recovery time, compaction throughput, device-fault behavior, and performance under
skewed document and replica distributions.

Passing empirical thresholds does not satisfy a proof obligation.

Passing Verus does not establish throughput, latency, storage-device reliability,
network fairness, or human intent preservation.

## Non-goals

The engine does not provide rich-text rendering.

The engine does not provide semantic search.

The engine does not promise immediate revocation of causally prior offline work.

The engine does not promise progress without resource availability and fair
delivery where those assumptions are named.

The engine does not use machine learning to resolve conflicts.

The engine does not claim that deterministic merging matches every user's
preferred edit.
