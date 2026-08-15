# Development Contract

## Authority and scope

This document defines the implementation architecture, verification obligations,
trusted boundaries, deterministic gates, and contributor rules for the offline
collaborative document engine.

`PRODUCT.md` is authoritative for observable behavior.

`RESEARCH.md` is authoritative for empirical methods and unresolved questions.

The governing Oxide verification policy applies to every production logical
component whether or not this document repeats a rule.

No fixture, benchmark, fuzz campaign, or review substitutes for a Verus proof
obligation.

No Verus theorem substitutes for empirical capacity or effect-adapter evidence.

## Implementation closure

### Workspace crates

The initial workspace contains:

| Crate | Responsibility | Classification |
| --- | --- | --- |
| `collab-model` | Mathematical views and executable identifiers | Verified production logic |
| `collab-codec` | Canonical wire decoding and encoding | Verified production logic |
| `collab-auth` | Capability and replica-key decisions | Verified production logic |
| `collab-causal` | Version vectors, readiness, and topological order | Verified production logic |
| `collab-tree` | Stable nodes, text atoms, and conflict selection | Verified production logic |
| `collab-schema` | Catalog validation and deterministic migration | Verified production logic |
| `collab-sync` | Knowledge summaries and missing-set selection | Verified production logic |
| `collab-snapshot` | Snapshot model and compaction eligibility | Verified production logic |
| `collab-kernel` | Public composition and state transition authority | Verified production logic |
| `collab-effects` | Storage, crypto, transport, and process adapters | Trusted effects only |
| `collab-cli` | Diagnostic operator interface | Non-authoritative tooling |
| `collab-bench` | Workload generation and measurement | Non-authoritative tooling |

Crate boundaries MAY be combined when proof interfaces and ownership remain
equivalent.

They MUST NOT be split in a way that moves policy into `collab-effects`.

### Production features

The authoritative feature set is `kernel-v1`.

The verified and compiled production source closures MUST be byte-identical for
the exact candidate.

No fallback feature may bypass verification.

Generated code, macros, unsafe blocks, build scripts, foreign libraries, and
conditional modules must be classified in the coverage manifest.

### Non-authoritative tooling

Tools may inspect content-addressed evidence and invoke public APIs.

Tools have no direct write path to storage tables, replica counters, capability
state, schema catalogs, snapshots, or publication markers.

Diagnostic rendering is explicitly outside the kernel refinement theorem.

Tool output is not an authoritative recovery input.

## Abstract state

### Kernel state

The public abstract state includes:

- document creation records;
- admitted operation groups keyed by document and group dot;
- pending groups and exact missing-predecessor sets;
- rejected digest dispositions;
- greatest durable counter binding per document and replica;
- document-and-replica counter-to-digest bindings;
- quarantined unknown-schema envelopes and their bounded disposition;
- schema definitions and compatibility edges;
- active and revoked capability records;
- replica-key bindings and rotations;
- stable node and text identities;
- tombstones and retained ancestry;
- candidate parent moves and selected acyclic parents;
- attribute assignment frontiers;
- materialized document views;
- synchronization knowledge summaries;
- durable transfer cursors;
- snapshot identities, frontiers, and state views;
- retention acknowledgments;
- monotone per-document admission floors and frozen acknowledger policy;
- compaction eligibility witnesses;
- storage generation identity;
- configured structural and capacity bounds.

Pending and quarantined entries retain their exact canonical envelope and stable
digest identity. Replica-key bindings are document-scoped; no state member is
shared across documents merely because `ReplicaId` bytes match.

Two abstract states differing in any listed authority-relevant member are not
silently identified.

### Initial state

The initial state contains no documents, groups, schemas other than the frozen
built-in catalog, capabilities, cursors, or snapshots.

Replica-counter bindings are empty.

Configured bounds and built-in schema digests are explicit parameters.

The initial state is constructively reachable.

### Observations

The abstract observation function exposes only PRODUCT-defined public results.

It erases allocation addresses, map layout, thread ownership, file offsets,
process timestamps, adapter retry counts, and proof ghost state.

It preserves stable identifiers, causal contexts, canonical materialization,
public status, selected errors, and authorized audit output.

## Mathematical views

### Identifiers

Executable identifier types expose total injective byte views.

`ReplicaCounter` arithmetic proves no wraparound on accepted increments.

`OperationId` comparison refines the PRODUCT canonical tuple order.

Stable element derivation is injective over accepted creating members and scalar
offsets.

### Version vectors

The mathematical version vector is a finite map with default zero.

Join is componentwise maximum.

Dominance is reflexive, antisymmetric, and transitive.

Concurrency is symmetric and irreflexive.

Adding a group dot strictly advances its replica component.

### Operation groups

The group view binds canonical members, causal context, schema identity,
authorization witness, and digest.

Member indices are contiguous and unique.

The group-dot view excludes `MemberIndex` and names one atomic publication.

### Document view

The document view is a finite causally closed operation set plus exact schema
catalog and configuration.

Materialization is a pure total function over every valid document view.

Invalid or incomplete inputs have typed outcomes before materialization.

### Snapshot view

The snapshot view contains the covered frontier, authoritative state projection,
retained identity support, exact pending and quarantined envelopes, retained
rejection dispositions, counter bindings, schema digests, and storage generation.

Snapshot equality is structural equality of canonical decoded fields, not file
path or creation time.

## Core transitions

### Create document

The executable transition must refine abstract document creation.

Preconditions include canonical input, matching authenticated creator,
document-scoped replica identity, valid initial signature and key, unused exact
document identity, valid built-in schema, and available capacity.

Success atomically adds exactly one creation group, initial replica-key and
counter bindings, and initial capability set.

Idempotent replay changes no abstract member.

Every specified failure preserves the complete pre-state.

### Reserve replica counter

Counter reservation is a guarded durable transition.

One guard instance is evaluated against one observed prior replica binding.

At most one contender can advance a given prior counter to its successor.

The storage adapter may report observations but cannot choose a document-scoped
counter or digest binding.

### Admit operation group

Admission follows PRODUCT Section 7 error precedence.

The proof exposes one branch for every stable result.

A successful ready admission adds exactly one counter binding and one admitted
group atomically. A qualified unknown-schema disposition adds exactly one
counter binding and quarantined envelope atomically. A pending disposition adds
an exact digest-keyed envelope but no counter binding until causal replica-key
validation and guarded promotion succeed.

No rejection adds a group, counter binding, capability, schema, cursor, or
snapshot.

Duplicate exact bytes stutter.

Conflicting bound counter bytes return the conflict result without revealing
another document. Multiple unqualified pending envelopes cannot choose a winner
by arrival order; only an envelope that passes complete causal key and capability
validation may contend for the guarded binding.

### Resolve pending group

Resolution requires every exact predecessor to be admitted.

It validates replica-key binding and capability state against the now-complete
causal state, then validates the exact schema identity, operation tags, and
members in PRODUCT order.

Ready success removes the envelope from pending, wins the guarded counter binding,
and adds the group to the ready closure in one abstract transition. A valid group
whose exact schema is unavailable instead moves atomically to quarantine with the
same guarded binding.

Semantic failure removes the envelope from pending, records a rejected
digest disposition, and does not create a counter binding that the pending
state never owned. Rejection after re-evaluating an already bound quarantine
retains that quarantine binding.

Resolution cannot change canonical group bytes.

Resolution is idempotent after either terminal outcome.

### Materialize

Materialization is pure with respect to admitted history.

An incremental executable update must refine full pure recomputation.

The implementation may cache indexes only when invalidating a cache leaves
authority unchanged and recomputation returns the same bytes.

## Canonical codec

### Decoder contract

The decoder consumes a bounded byte slice and either returns one exact typed value
plus the consumed length or a stable decode error.

It rejects non-minimal integers, invalid UTF-8, duplicate keys, unsorted maps,
trailing bytes, count overflow, depth overflow, and unknown closed fields.

It allocates only after validating the governing bounded count.

### Encoder contract

The encoder emits one canonical representation for each valid mathematical view.

Decode after encode returns the original view.

Encode after canonical decode returns identical bytes.

The proof covers every production wire type.

### Digest boundary

Digest input is the exact canonical unsigned group encoding.

Signature bytes are excluded and key identity is included.

The executable slice passed to the crypto adapter is proved equal to the abstract
digest preimage.

## Causality and readiness

### Context validation

Context entries are sorted and unique after canonical decode.

The author's component is strictly below the submitted group counter.

No counter exceeds the maximum representable accepted value.

The context may name predecessors absent locally.

Every component denotes an admitted, causally closed prefix in the author's
state. Pending, quarantined, and reserved counters are not context knowledge.

After structural context validation, admission rejects a context below the
document's current admission floor before computing ordinary missing
predecessors.

### Missing predecessors

Missing-set computation is exact over the retained gap index and contiguous
frontier.

Every reported missing range has positive length.

Ranges are disjoint, sorted, and maximal.

No present group is reported missing.

### Topological order

The ready-group order contains every and only causally closed admitted group.

Every predecessor precedes its descendants.

Concurrent ties use the canonical group-dot order.

The result contains no duplicates.

### Promotion scheduling

The scheduler is an optimization outside semantic authority.

It may discover newly ready groups in any physical order.

Every committed promotion must validate the exact current predecessor predicate.

Under declared fair local scheduling and available resources, a ready pending
group is eventually promoted.

## Sequence and tree algorithms

### Gap insertion

The pure insertion model consumes stable boundary identities and concurrent child
sets.

Its output is finite, duplicate-free, and contains every live atom exactly once.

Descending group-dot and ascending scalar-offset order match PRODUCT Section
10.2.

Incremental index insertion refines pure traversal.

### Tombstones

Tombstoning changes visibility but never stable identity or causal presence.

Applying the same tombstone twice stutters.

Edit-versus-delete materialization is independent of evaluation order.

No constructor turns an existing tombstone back into a live creation.

### Parent selection

Candidate moves form finite ranked lists per node.

The selection algorithm considers nodes in stable identifier order.

Every selected edge names a retained parent. A visible edge names a live parent;
an edge retained under a tombstoned ancestor preserves identity while the
descendant remains invisible.

The selected relation is rooted and acyclic.

Each fallback decreases a finite candidate rank.

The fixed-point loop terminates.

Selection is deterministic for permutation-equivalent candidate input.

### Attributes

The maximal causal frontier for one `(node, key)` is finite and nonempty when an
assignment exists.

Causally dominated assignments cannot win.

Concurrent maxima select the greatest operation identifier.

Map representation and insertion order do not affect the winner.

### Composition

Tree, text, tombstone, move, and attribute algorithms compose into one canonical
materialization theorem.

The theorem covers empty documents, nested structures, all conflict pairs, and
multi-way concurrency.

No component theorem assumes its own desired output as an input invariant.

## Authorization

### Capability resolution

Capability resolution uses the exact causal state referenced by the operation.

It returns a typed set of active grants and revocations for the actor.

The active predicate implements PRODUCT grant visibility, named revocation, and
causal expiration exactly. Dominating or equal contexts are expired; concurrent
contexts remain governed by their own causal view.

It does not consult receipt time or the current materialized head when evaluating
offline work.

### Grant and delegation

The grant transition proves the grantor owns each delegated right.

Delegable `admin` requires `admin` in the same causal state.

Independent grant identifiers remain independent.

### Revocation

Revocation affects only its named grant and causal descendants relying on it.

Concurrent operations see the causally prior grant state.

The proof includes concurrent grant, revoke, edit, and reconnect traces.

### Isolation

Lookup authorization maps absent and inaccessible documents to the same public
outcome and allowed side-effect shape.

Unauthorized results contain no payload, participant, schema, count, cursor, or
timing detail beyond the declared boundary.

Cross-document caches are keyed by authenticated authority and document.

### Replica-key rotation

Rotation preserves verification of causally prior and concurrent signed groups
under their referenced binding state.

Causally later use of an old key fails.

The crypto adapter does not choose the binding state.

## Synchronization

### Summary construction

Summary construction includes the greatest truly contiguous counter and an
advertised observed ceiling for each visible replica.

Explicit gaps are sorted, disjoint, bounded, above the contiguous prefix, and at
or below the advertised ceiling.

If a gap would be omitted, the ceiling is lowered below that gap. Truncation
never turns a missing group into a claimed present group.

### Difference selection

Difference selection returns only ranges the remote summary does not claim.

It may conservatively resend known groups.

It must not omit a group solely because the bounded gap list was truncated.

The selected range order is canonical and independent of network arrival.

### Frame admission

Each frame is authenticated, bounded, and decoded before iteration.

Each enclosed group passes ordinary admission independently unless an explicitly
declared frame-atomic mode is used.

V1 exposes only independent group admission for synchronization frames.

Malformed frame structure admits no enclosed group.

### Cursor safety

A cursor advances only through frames durably acknowledged by the receiver.

Cursor persistence failure causes retransmission.

Cursor corruption returns a typed error and cannot advance knowledge.

Cursors are hints and never prove operation durability by themselves.

### Liveness boundary

Verus proves safety for finite exchange traces and conditional progress of local
state machines.

Fair transport delivery, remote cooperation, device availability, and elapsed
completion bounds are trusted or empirical assumptions.

No theorem claims network fairness.

## Schema catalog and migration

### Definition validation

Definitions are signed, canonical, content-addressed, and immutable.

Compatibility edges name exact predecessor digests.

An operation tag or attribute identity cannot be redefined.

Parent-child constraints are finite and decidable.

### Unknown schemas

Unknown-schema groups remain outside admitted history.

Quarantine metadata cannot authorize or materialize content.

Re-evaluation uses original canonical bytes after exact schema installation.

Capacity rejection never evicts admitted history.

### Migration functions

Every production migration is verified as deterministic, total over its declared
input domain, schema-valid on success, and failure-preserving.

Migration preserves stable provenance or explicitly maps it through a proved
injective correspondence.

Migration cannot rewrite operation history.

### Opaque nodes

The codec proves byte-preserving round trip for known opaque extension nodes.

Opaque payloads cannot execute or mutate kernel state outside their node value.

Unknown operation tags remain rejected.

## Snapshot, compaction, and recovery

### Closed frontier

Frontier validation proves every included counter range is gap-free and every
causal predecessor is covered.

A frontier from another document or storage generation is invalid.

### Snapshot construction

Construction reads one immutable abstract state view at the selected frontier.

Concurrent publication above the frontier does not change snapshot bytes.

The snapshot codec binds all fields named by PRODUCT Section 14.

The resulting state digest covers authoritative fields and retained identity
support.

### Restore

Restore is a prepare-validate-publish protocol.

No candidate state becomes authoritative before all checks pass.

Published restored state refines replay from initial state through the snapshot
frontier and retained suffix.

Operator restore validates current `snapshot` authority and proves that the
candidate image plus suffix covers every acknowledged identity in the currently
published generation. Crash recovery uses the same coverage predicate without
inventing a caller.

Invalid input preserves the previously published generation.

### Compaction

Eligibility requires a valid covering snapshot, retention acknowledgments, and a
proof that no admissible retained reference needs discarded physical bytes.

Verified logic computes admission-floor advances from the frozen acknowledger
policy and admitted acknowledgment records. An advance is monotone, is covered
by the selected snapshot, and becomes effective atomically with generation
publication.

The compactor receives an explicit eligible set from verified logic.

The storage adapter cannot enlarge that set.

After compaction, every retained public observation equals the pre-compaction
observation.

### Crash recovery

Recovery enumerates durable generations and validates them newest to oldest.

It chooses the newest complete compatible snapshot-plus-suffix image.

Partial writes and unpublished generations are ignored.

Recovery never synthesizes a counter binding or admitted group.

Repeated recovery without new durable input is idempotent.

## Trusted effects

### Storage adapter

The storage adapter may atomically append bytes, conditionally bind a key against
one observed prior state, flush a generation, enumerate durable files, and publish
a prepared generation.

It must not decide authorization, causal readiness, conflict winners, compaction
eligibility, or recovery compatibility.

Its premises include atomic conditional exclusion, no torn acknowledged record,
read-after-flush persistence, generation publication atomicity, and honest error
reporting.

### Crypto adapter

The crypto adapter verifies exact messages under exact public keys and computes
declared digests.

It must not resolve replica bindings or capabilities.

Cryptographic soundness and collision resistance are explicit assumptions.

### Transport adapter

The transport adapter supplies authenticated session identity and bounded frames.

It may reorder, duplicate, delay, truncate, or drop frames.

It cannot mark a group admitted or durable.

Fair delivery is assumed only in the reconnect liveness statement.

### Process adapter

The process adapter supplies monotonic local durations for timeouts and resource
accounting.

Wall-clock values do not enter conflict or causal semantics.

Scheduling fairness is not assumed for safety.

### Boundary governance

Every adapter call and return type is included in the trusted-boundary manifest.

Adding an adapter method, broadening a premise, or moving policy across the
boundary changes the judge and requires independent qualification.

## Verification architecture

### Coverage manifest

Every production logical component names:

- source closure;
- public entry points;
- mathematical view;
- preconditions;
- success and failure postconditions;
- preserved invariants;
- abstract operation;
- refinement theorem;
- composition theorem path;
- trusted assumptions;
- target and features;
- proof roots;
- non-vacuity witnesses.

Unclassified production code fails the gate.

### Representative proof foundations

Before broad component work, real proofs establish:

- canonical operation-id and version-vector algorithms;
- one counter-allocation race with one winner;
- one pending-to-ready ownership transition;
- one snapshot publication and crash-recovery transition.

Toy substitutes do not satisfy these obligations.

### Component proofs

Each component evolves with implementation, contract, proof, coverage, and
composition connection together.

Simple functions may discharge contracts automatically when contracts state real
responsibilities.

No component may use `ensures true`, impossible preconditions, disconnected
theorems, unapproved axioms, or proof-only substitute logic.

### Composition theorem

The exact production public API refines the PRODUCT abstract transitions for the
exact prospective authoritative tree.

Every public entry point is reachable from the theorem.

Every trusted assumption is explicit and transitively connected.

The theorem covers success, error, recovery, and bounded-resource behavior.

### Proof sensitivity

Critical invariants have deterministic rejecting mutations.

Removing atomic group publication must break group-atomicity verification.

Allowing duplicate counter binding must break uniqueness verification.

Reversing concurrent insertion order must break convergence fixtures or proofs.

Skipping authorization must break isolation verification.

Accepting a cyclic move must break acyclicity verification.

Dropping snapshot frontier closure must break recovery verification.

## Deterministic gates

### Gate catalog

| Gate | Subject | Required oracle |
| --- | --- | --- |
| `VERIFY-CODEC` | Canonical wire types | Round trip, rejection, bounded allocation |
| `VERIFY-CAUSAL` | Vectors and readiness | Partial order, exact gaps, topological order |
| `VERIFY-TREE` | Text and node materialization | Permutation convergence and conflict rules |
| `VERIFY-AUTH` | Capabilities and rotations | Causal authorization and isolation |
| `VERIFY-SYNC` | Summary and exchange | No omission, duplicate safety, cursor safety |
| `VERIFY-SCHEMA` | Catalog and migration | Identity, compatibility, total migration |
| `VERIFY-SNAPSHOT` | Snapshot and compaction | Replay equivalence and observation preservation |
| `VERIFY-RECOVERY` | Durable generation selection | Prefix integrity and publication atomicity |
| `VERIFY-COMPOSE` | Exact public kernel tree | Complete public refinement theorem |
| `VERIFY-EFFECTS` | Trusted adapters | Real boundary fixtures and fault injection |

### Authoritative command

`verification/bin/verify --manifest verification/manifest.toml --candidate <sha>`
is the one release-blocking verifier entry point.

It pins Verus, solver, Rust toolchain, target, features, resource policy, proof
roots, checker closure, trusted boundary, and prospective tree identity.

Until the command and manifest exist, implementation admission remains planned.

### Cheat checking

The checker rejects undeclared `assume`, `admit`, axioms, external bodies,
unchecked bridges, proof-only implementations, unclassified paths, unreachable
roots, stale evidence, missing composition, and feature mismatch.

A timeout, solver unknown, resource exhaustion, skipped root, or missing artifact
is infrastructure failure, never success.

### Exact-tree rule

Component evidence may guide development.

Authoritative merge requires the full composition proof against the exact
prospective tree after candidate integration.

Evidence for another tree, schema set, feature set, toolchain, or trusted boundary
cannot be reused.

## Fixtures and negative cases

### Codec fixtures

Fixtures cover minimal and maximal values, malformed lengths, invalid UTF-8,
duplicate keys, unsorted maps, trailing bytes, unknown fields, nesting depth,
member-count overflow, and digest mismatch.

Each named rejection clause has at least one frozen rejecting fixture.

### Concurrency fixtures

Fixtures permute delivery of two through eight replicas editing the same gap,
deleting edited containers, moving ancestors, assigning attributes, granting and
revoking capabilities, and rotating replica keys.

All permutations with equal admitted sets must converge to one canonical digest.

At least one mutant reverses each specified tie rule.

### Storage fixtures

Real-adapter fixtures race counter allocation and document creation from separate
processes.

Exactly one guarded contender succeeds for one observed prior state.

Faults occur before write, during write, after write before flush, after flush
before publish, and after publish before response.

### Synchronization fixtures

Fixtures include duplicate frames, reordered frames, lost cursors, truncated gap
summaries, long offline branches, unknown schemas, delayed schema installation,
and backpressure.

Truncation fixtures force more gaps than the summary bound and assert that the
advertised ceiling retreats before the first omitted gap.

No fixture bypasses ordinary admission.

### Snapshot fixtures

Fixtures include every crash boundary, wrong document, wrong generation, stale
schema, missing retained identity, partial suffix, concurrent publication, and
compaction under active references.

The known-good oracle compares canonical public observations before and after.

### Malformed-peer isolation

Malformed peers send adversarial counts, recursive values, invalid signatures,
counter conflicts, unauthorized document guesses, cyclic moves, schema
redefinitions, and cursor corruption.

The oracle asserts no unauthorized authoritative mutation and bounded processing
under the declared structural limits.

## Contributor workflow

### Semantic changes

A change to PRODUCT behavior requires approved source changes before
implementation or proof contracts may rely on it.

Changing a tie rule, error precedence, causal assumption, public bound, or trusted
premise is a semantic or judge-facing change, not an ordinary proof repair.

### Proof repair

A proof repair may change implementation structure and lemmas while preserving
the approved abstract contract.

It must not strengthen preconditions, weaken postconditions, shrink reachable
states, add trust, or disconnect composition.

### Review

Review independently examines product-to-model fidelity, implementation-to-proof
fidelity and non-vacuity, and systems/trust boundaries.

Agent review is supplementary and cannot waive a deterministic gate.

### Evidence

Proof and test evidence binds exact source, specification, schema, manifest,
toolchain, solver, trusted boundary, candidate tree, prospective tree, command,
environment, result, and content-addressed logs.

Reusable evidence requires equality of every relevant identity input.

## Release criteria

The release candidate must satisfy all deterministic gates against the exact
prospective tree.

The coverage manifest must classify every production path.

Every trusted adapter must pass its real-boundary fixtures.

All required negative mutations must be rejected.

The empirical qualification in `RESEARCH.md` must pass independently.

A formally correct candidate that misses capacity thresholds is not released.

A fast candidate that lacks complete proofs is not released.
