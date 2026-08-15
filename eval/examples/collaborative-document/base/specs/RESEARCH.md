# Research and Empirical Qualification

## Authority

This document defines empirical questions, preregistered workload families,
capacity thresholds, fault campaigns, proof-maintenance measurements, and open
design questions for the offline collaborative document engine.

It does not weaken PRODUCT requirements.

It does not create a proof exemption.

It does not turn measured outcomes into universal theorems.

Every report classifies each claim as one of:

- normative product behavior;
- machine-proved property under named assumptions;
- trusted environmental assumption;
- finite empirical evidence;
- unresolved research question.

## Reproducibility

### Sealed campaign identity

Every campaign binds:

- source commit and tree;
- production binary digest;
- feature set and target triple;
- schema catalog digest;
- verification manifest and toolchain digests;
- workload generator digest;
- fixture corpus digest;
- random seeds;
- machine inventory;
- operating-system and filesystem versions;
- storage and network topology;
- adapter configuration;
- dataset identity;
- declared warmup and measurement intervals;
- acceptance thresholds;
- report schema version.

Changing any bound field produces a new campaign identity.

### Outcome retention

Reports retain raw observations or content-addressed references to them.

Reports include every attempted case, including setup, timeout, invalid,
infrastructure-failed, and rejected outcomes.

Invalidity rules are evaluated without conditioning on measured success.

### Determinism

Deterministic fixtures use stable seeds and canonical input ordering.

Stochastic studies report seed distributions and confidence intervals.

Post-hoc exclusions are prohibited unless the complete original and amended
analyses remain visible.

## Workload model

### Actors and replicas

The standard workload matrix uses:

- 2, 8, 32, 128, 1,024, and 16,384 principals per tenant;
- 1, 2, 8, 32, 256, and 4,096 replicas per document;
- 1, 100, 10,000, and 1,000,000 active documents;
- one through 128 concurrent writers per hot document;
- connected, intermittently connected, and long-offline replicas.

The one-million-document point may use trace replay when physical clients would
measure the driver rather than the subject.

### Document shapes

The corpus contains:

- empty documents;
- short notes;
- long reports;
- deeply nested lists;
- broad flat paragraphs;
- code-heavy documents;
- attribute-heavy structured records;
- sparse tombstone histories;
- tombstone-dense histories;
- schema-migrated documents;
- access-control-heavy documents.

Depth percentiles are 4, 16, 64, and the declared maximum.

Visible sizes are 1 KiB, 100 KiB, 10 MiB, and 1 GiB where machine capacity
permits.

### Operation mix

The default interactive mix is:

- 48 percent text insertion;
- 18 percent text deletion;
- 8 percent node insertion;
- 5 percent node deletion;
- 4 percent node move;
- 7 percent attribute assignment;
- 3 percent capability grant;
- 2 percent capability revoke;
- 1 percent replica-key rotation;
- 2 percent schema-related operation;
- 2 percent snapshot or audit request.

Alternative mixes isolate every operation family.

### Payload distributions

Text insertion length uses measured buckets at 1, 8, 64, 1,024, and 16,384
Unicode scalar values.

Group member count uses 1, 2, 8, 32, 128, and 256.

Causal context cardinality uses 1, 8, 64, 1,024, and 65,536 replicas.

Inputs include ASCII, combining marks, right-to-left scripts, emoji sequences,
non-BMP scalars, and maximum valid UTF-8 encodings.

### Concurrency distributions

The conflict matrix includes:

- independent paragraphs;
- the same text gap;
- overlapping deletes;
- edit against deleted container;
- two-way and multi-way node moves;
- moves that jointly form cycles;
- concurrent attribute assignments;
- grant against revoke;
- grant expiration against operations before, equal to, concurrent with, and
  causally after its frontier;
- revoke against offline edit;
- key rotation against signed offline work;
- schema migration against ordinary edits.

Every conflict family is tested under all delivery permutations feasible below
the campaign bound and randomized permutations above it.

### Connectivity

Connected replicas use 0.1, 1, 10, and 100 ms one-way delay.

Impaired links add duplication, loss, reordering, burst delay, and partitions.

Offline intervals use 1 minute, 1 hour, 1 day, 30 days, and a frontier gap of up
to ten million groups.

## Convergence campaign

### Question

Does every tested delivery schedule with the same valid admitted operation set and
schema catalog produce the same canonical materialized digest?

This campaign searches for counterexamples to the proved model and adapter
integration.

It does not establish convergence for untested executions.

### Method

Generate a valid causally annotated operation DAG.

Deliver it through independent randomized schedules.

Include duplicates, pending predecessors, reconnect windows, and process restarts.

Compare canonical document bytes, active capability views, schema identity,
frontier, pending set, audit order, and retained tombstone support.

### Exact corpus

Bounded exhaustive exploration covers every equivalence-class-reduced trace in a
sealed finite model with:

- up to four replicas;
- up to twelve operation groups;
- up to four members per group;
- all two-way conflict families;
- every crash boundary in the in-memory effect model.

The finite model fixes symbolic domains for principals, replica and element
identities, text scalars, schema tags, capability rights, and adapter outcomes.
The report proves that its generator enumerated the complete declared quotient;
it does not claim to enumerate the unbounded production input domain.

Randomized exploration covers at least ten million larger schedules per release
candidate.

### Acceptance

Zero unequal canonical outcomes are permitted for equal admitted sets and schema
catalogs.

Zero unauthorized visible operations are permitted.

Zero partial groups are permitted.

Every discovered counterexample is minimized and retained as a deterministic
regression.

The zero-tolerance result is finite empirical evidence supporting integration,
not the formal theorem.

## Synchronization campaign

### Safety questions

Can a truncated knowledge summary cause permanent omission?

Can a lost or corrupted cursor make an unacknowledged group appear durable?

Can duplicate frames duplicate authoritative effects?

Can backpressure reorder causal authority?

Can an unauthorized relay learn or publish plaintext state?

### Progress questions

Under a declared fair-delivery schedule, how many round trips and bytes are needed
to equalize admitted sets?

How do long gaps, large replica vectors, schema quarantine, and hot-document skew
affect completion?

### Methods

Use a deterministic network simulator for reproducible packet schedules.

Repeat selected cases across real loopback, LAN, and impaired-WAN environments.

Inject loss from 0 through 30 percent, duplication from 0 through 100 percent,
and reorder windows from 1 through 10,000 frames.

Crash either endpoint before frame send, after send, before durable acknowledgment,
after acknowledgment, and after cursor persistence.

### Acceptance

Every fair finite test with quiescent writers must equalize admitted sets within
the preregistered simulator bound.

No unfair schedule is mislabeled a product progress failure.

No safety violation is waived because the schedule is unfair.

## Malformed-peer and isolation campaign

### Corpus

The frozen corpus contains:

- truncated and overlong frames;
- noncanonical integers;
- invalid UTF-8;
- duplicate and unsorted keys;
- declared lengths near every integer boundary;
- recursive values at, below, and above the depth bound;
- invalid signatures and digest mismatches;
- counter reuse with same and different bytes;
- contexts with duplicate replicas and impossible author counters;
- unauthorized document identifiers;
- unknown schemas and redefined operation tags;
- unknown stable elements;
- cyclic moves;
- corrupted cursors;
- wrong-document snapshots;
- wrong-generation suffixes.

### Mutation method

Structure-aware generators preserve enough outer validity to reach every decoder
and validation clause.

Byte-level fuzzing explores parser behavior.

Every deterministic rejection clause has a seed fixture and a soundness-violating
mutant.

### Isolation oracle

The oracle records authoritative state before and after each attempt.

Rejected input must leave admitted groups, counter bindings, capabilities, schema
catalog, cursors, snapshots, and publication generation unchanged.

Unauthorized absent and present document probes expose the same allowed logical
result and response-shape class.

Resource use must remain within the declared input-size and collection bounds.

### Acceptance

No crash, panic across the public boundary, unauthorized mutation, cross-document
content observation, or unbounded allocation is permitted.

Sanitizer, Miri where applicable, and platform hardening findings are retained as
supplementary evidence.

## Storage and recovery campaign

### Real adapter matrix

The qualifying matrix includes the production filesystem and storage adapter on
the declared Linux deployment target.

In-memory doubles do not qualify persistence assumptions.

Each adapter records filesystem, mount options, flush primitive, device model,
firmware, queue depth, and power-loss setup.

### Two-writer races

Separate processes race:

- document creation;
- replica counter reservation;
- counter-to-digest binding;
- group publication;
- schema installation;
- snapshot generation publication;
- compaction generation publication.

For one observed guard instance, at most one contender may report success.

A last-writer-wins adapter must fail the campaign.

### Crash points

Crashes occur before write, during record write, after record write, before flush,
after flush, before publication marker, after marker, before response, during
snapshot creation, during suffix replay, and during compaction reclamation.

Abrupt process exit is mandatory.

Selected environments add machine reset or controlled power interruption.

### Recovery oracle

The recovered state must be one integrity-valid acknowledged prefix plus any fully
durable acknowledged concurrent groups allowed by the adapter contract.

No partial group, invented counter, cross-generation suffix, or invalid snapshot
may become authoritative.

Repeated recovery is idempotent.

### Acceptance

Zero acknowledged-group loss is permitted under the declared durable-storage
assumptions.

Zero unacknowledged partial publication is permitted.

Every adapter premise must have at least one fault capable of falsifying it.

## Snapshot and compaction campaign

### Equivalence

For each generated history, compare:

1. replay from creation through all retained operations;
2. restore the selected snapshot and replay its suffix;
3. restore after eligible compaction;
4. restore after repeated snapshot and compaction cycles.

Authorized public observations must match byte-for-byte.

### Reference retention

Generate late operations that reference old live elements, old tombstones, moved
ancestors, revoked grants, prior schemas, and replica keys.

Compaction must retain every identity needed to classify them correctly.

Cases beyond the configured retention horizon are reported separately and must
follow the PRODUCT policy.

Boundary cases exercise contexts immediately below, equal to, concurrent with,
and above the causal admission floor. Every non-dominating context, including a
concurrent one, returns `history_expired`; equal or dominating contexts continue
through ordinary admission. No wall-clock duration substitutes for the frozen
acknowledger policy.

The campaign includes pending and quarantined envelopes, retained rejected
dispositions, and counter bindings in every snapshot/restart equivalence check;
materialized bytes alone are not a sufficient recovery oracle.

### Concurrent publication

Create snapshots while writers publish above, at, and outside the selected
frontier.

The snapshot digest must correspond to one exact closed frontier.

It must not mix values from multiple views.

### Acceptance

Zero observation mismatch, cross-generation restore, lost retained reference, or
invalid compaction deletion is permitted.

Snapshot duration and disk reclamation are evaluated separately from correctness.

## Schema evolution campaign

### Compatibility matrix

The corpus contains compatible additions, incompatible redefinitions, diamond
compatibility graphs, missing predecessors, stale signatures, unknown operation
tags, known opaque nodes, and valid and failing migrations.

### Determinism

Run each migration across all supported target architectures and randomized map
layouts.

Canonical output digests must match.

No migration may consult wall time, randomness, transport arrival, or local file
paths.

### Quarantine

Exercise delayed schema delivery, duplicate schema definitions, quarantine
capacity, process restart, and re-evaluation.

No quarantined group enters admitted history before exact schema validation.

### Acceptance

Zero accepted schema redefinition, nondeterministic migration output, opaque-node
byte loss, or unknown-tag execution is permitted.

## Capacity qualification

### Target server

The reference target is one dual-socket x86-64 server with:

- 64 physical cores;
- 512 GiB ECC memory;
- four enterprise NVMe devices;
- 100 Gbit/s network interface;
- production filesystem and storage adapter;
- release-mode kernel binary.

Alternate ARM64 results are reported but do not replace the reference target.

### Service objectives

Under the standard connected workload, one reference server must sustain at
least 500,000 admitted operation groups per second at 70 percent or less CPU
saturation.

The qualifying profile freezes the durability group-commit policy, including its
maximum batch size and maximum acknowledgment delay. Throughput obtained with a
larger or unbounded batch does not satisfy this objective, and every reported
latency is measured through the durable acknowledgment point.

That throughput profile uses single-member text and attribute groups with median
canonical size at most 256 bytes and p95 size at most 1 KiB; large-payload results
are reported in their own strata and do not inherit this threshold.

At 100,000 active documents and the default mix:

- p50 local admission latency must be at most 2 ms;
- p95 local admission latency must be at most 8 ms;
- p99 local admission latency must be at most 25 ms;
- p99 two-replica synchronization latency on the LAN must be at most 100 ms;
- p99 read-materialization latency for a 100 KiB document must be at most 10 ms.

### Amplification objectives

At the default mix:

- steady-state write amplification must be at most 8x canonical group bytes;
- for documents with at least 4 KiB of visible canonical content, retained
  metadata must be at most 3x visible document bytes after eligible compaction;
- smaller and empty documents report retained metadata as absolute bytes and do
  not use a ratio with a zero or near-zero denominator;
- synchronization retransmission must be at most 1.5x missing canonical bytes on
  a lossless link;
- an idle session must consume at most 64 KiB exclusive memory.

### Recovery objectives

For a 1 TiB retained history with a qualifying snapshot less than ten minutes old,
restart to read availability must complete within 60 seconds on the reference
target.

This objective applies when the post-snapshot suffix contains at most 50 million
groups and at most 32 GiB of canonical group bytes.

Background suffix catch-up may continue after read availability only when reads
report their exact recovered frontier.

No performance shortcut may weaken recovery integrity.

### Peak and skew

Campaigns report average and peak load separately.

The peak profile is 4x average offered load for five minutes.

Hot-document tests route 20 percent of all operations to one document.

Tenant-skew tests route 80 percent of operations to one percent of tenants.

Results must report admission, pending promotion, materialization, sync, and
snapshot queues separately.

### Interpretation

Capacity thresholds apply only to the exact qualified binary, machine, workload,
and environment.

They are independent release gates, not consequences of formal verification.

## Proof engineering measurements

### Runtime

Record cold and warm Verus wall time, solver CPU time, peak memory, per-root
resource use, timeout count, and cache identity.

Report median and p95 across clean repeated runs.

### Stability

Apply contract-preserving implementation refactors and record unrelated proof
breakage.

Change map layout, helper decomposition, and internal index choices without
changing abstract behavior.

Frequent global proof churn is a proof-abstraction defect.

### Maintenance

For sampled changes, report production lines changed, specification lines changed,
proof lines changed, roots invalidated, solver time added, and review effort.

Proof-to-code ratio is descriptive and not an assurance target.

### Agent performance

Measure agent success at constructing and repairing meaningful proofs without
adding trust, weakening contracts, or disconnecting composition.

Score first-pass success, iterations, invalid repair attempts, and independently
reviewed semantic fidelity.

Agent success cannot substitute for deterministic verification.

### Sensitivity

Run the frozen negative mutation catalog against every release candidate.

Report mutation detection by proof root and supplementary fixture.

An undetected soundness-violating mutant blocks release and requires coverage
repair.

## Open questions

### Summary compression

Can range summaries remain compact for adversarially sparse replica counters
without probabilistic false claims of presence?

Any probabilistic hint must be conservative and cannot establish authority.

### Tombstone horizon

What retention policy best balances offline edit acceptance, audit requirements,
and bounded storage?

The PRODUCT configured-horizon semantics remain fixed while tuning the default.

### Large-document indexing

Which persistent sequence index minimizes update amplification while preserving
the pure canonical materialization contract?

Candidate indexes include balanced trees, chunked ropes, and immutable runs.

### Encrypted relay

Can blind relay preserve useful missing-set exchange without exposing document
membership or traffic shape beyond the deployment threat model?

No answer may weaken kernel authorization.

### Partial materialization

Can bounded viewport reads refine the same canonical full document without
forcing full materialization?

The proof must connect every partial result to the full abstract view.

### Human conflict quality

How often do deterministic outcomes require corrective user edits under real
collaborative workloads?

This is an empirical usability question, not a convergence or correctness claim.

## Required deliverables

The repository must provide:

- the sealed workload generator;
- deterministic convergence explorer;
- malformed-peer corpus and mutator;
- real-adapter crash harness;
- synchronization network simulator;
- snapshot equivalence runner;
- schema compatibility corpus;
- capacity runner;
- proof-metrics collector;
- machine-readable report schemas;
- content-addressed example reports;
- one command that runs each release-blocking empirical gate.

Until a required runner, corpus, schema, or qualifying environment exists, its
empirical gate remains planned and cannot be reported as passing.

The verifier and empirical runners must produce separate receipts.

Neither receipt may claim authority belonging to the other.
