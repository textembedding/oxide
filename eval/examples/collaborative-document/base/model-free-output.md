<!-- oxide-roadmap-schema:1 -->
```toml
schema = 1
title = "Roadmap"
status = "ready"
specification_root = "eval/examples/collaborative-document/base/specs"

[[global_invariants]]
id = "oxide-verification-policy"
statement = "Production logic has meaningful contracts, component refinement, complete coverage, and exact-tree composition; trusted effects remain narrow and policy-free."
sources = []

[[global_invariants]]
id = "durable-acknowledgment"
statement = "Every acknowledged operation group remains durable and discoverable after restart under declared storage assumptions."
sources = [{ path = "eval/examples/collaborative-document/base/specs/PRODUCT.md", anchor = "Global invariants", requirement = "Every acknowledged operation group remains durable and discoverable after a successful restart under the declared storage assumptions." }]

[[global_invariants]]
id = "convergent-authority"
statement = "Equal admitted operation sets and schema catalogs have one canonical materialization."
sources = [{ path = "eval/examples/collaborative-document/base/specs/PRODUCT.md", anchor = "Strong convergence", requirement = "Any two qualified replicas with identical admitted-group sets and identical schema catalogs return identical canonical materialized bytes." }]

[[stages]]
id = "formal-foundations"
outcome = "Canonical identities, bounded codecs, causal relations, and the abstract document state have executable definitions and representative proofs."
included_scope = ["Identifier and canonical-byte views", "Version-vector algebra", "Abstract document and operation-group state", "Representative counter and recovery transitions"]
excluded_scope = ["User-visible editing operations", "Network synchronization", "Capacity qualification"]
dependencies = []
source_specifications = [
  { path = "eval/examples/collaborative-document/base/specs/PRODUCT.md", anchor = "Atomicity", requirement = "An admitted group publishes all members or none." },
  { path = "eval/examples/collaborative-document/base/specs/PRODUCT.md", anchor = "Global invariants", requirement = "Every acknowledged operation group remains durable and discoverable after a successful restart under the declared storage assumptions." },
  { path = "eval/examples/collaborative-document/base/specs/DEVELOPMENT.md", anchor = "Storage adapter", requirement = "It must not decide authorization, causal readiness, conflict winners, compaction eligibility, or recovery compatibility." },
]
applicable_global_invariants = ["oxide-verification-policy", "durable-acknowledgment"]
implementation_goals = ["Implement canonical bounded types, operation groups, version vectors, and the public abstract state without effect policy leakage."]
verification_goals = ["Use Verus to prove codec round trips, identifier injectivity, vector partial-order laws, atomic group publication, and one-winner guarded counter allocation."]
readiness = "ready"

[[stages]]
id = "authorization-and-admission"
outcome = "Authenticated operation groups are admitted atomically under causal capability and schema rules with stable failure behavior."
included_scope = ["Admission precedence", "Capability grants and revocations", "Replica-key rotation", "Unknown-schema quarantine", "Malformed-peer non-interference"]
excluded_scope = ["Materialized conflict selection", "Reconnect transport", "Empirical capacity"]
dependencies = ["formal-foundations"]
source_specifications = [
  { path = "eval/examples/collaborative-document/base/specs/PRODUCT.md", anchor = "Revoke", requirement = "An operation concurrent with a revocation is authorized by its own causal state and remains valid if the grant was then active." },
  { path = "eval/examples/collaborative-document/base/specs/PRODUCT.md", anchor = "Unknown-schema admission", requirement = "A group using an unknown schema remains quarantined as `unsupported_schema` and does not enter authoritative document history." },
  { path = "eval/examples/collaborative-document/base/specs/PRODUCT.md", anchor = "Global invariants", requirement = "Unknown or malformed peer input cannot mutate authoritative state." },
]
applicable_global_invariants = ["oxide-verification-policy", "durable-acknowledgment"]
implementation_goals = ["Implement admission, capability resolution, key rotation, quarantine, and bounded rejection paths together with their contracts."]
verification_goals = ["Prove authorization is evaluated in the referenced causal state, rejected input stutters, quarantine lacks authority, and malformed peers cannot cross document boundaries."]
readiness = "ready"

[[stages]]
id = "concurrent-insertion-precedence"
outcome = "Concurrent same-gap insertions follow descending canonical group-dot order and then ascending member-local scalar offset."
included_scope = ["Canonical concurrent same-gap insertion precedence"]
excluded_scope = ["Other document materialization", "Peer exchange", "Snapshots"]
dependencies = ["formal-foundations", "authorization-and-admission"]
source_specifications = [
  { path = "eval/examples/collaborative-document/base/specs/PRODUCT.md", anchor = "Concurrent text insertion", requirement = "Concurrent insertions into the same stable gap are ordered by descending canonical group-dot order and then ascending member-local scalar offset." },
]
applicable_global_invariants = ["oxide-verification-policy", "convergent-authority"]
implementation_goals = ["Implement the source-defined concurrent insertion precedence."]
verification_goals = ["Prove the total same-gap insertion order follows the canonical group-dot and scalar-offset keys."]
readiness = "ready"

[[stages]]
id = "convergent-document-core"
outcome = "Text, tree, move, deletion, and attribute operations materialize to one canonical document for every equal admitted set."
included_scope = ["Stable text gaps", "Tree insertion and tombstones", "Acyclic concurrent moves", "Attribute conflicts", "Canonical materialization"]
excluded_scope = ["Peer exchange", "Snapshots", "Rendering"]
dependencies = ["formal-foundations", "authorization-and-admission", "concurrent-insertion-precedence"]
source_specifications = [
  { path = "eval/examples/collaborative-document/base/specs/PRODUCT.md", anchor = "Strong convergence", requirement = "Any two qualified replicas with identical admitted-group sets and identical schema catalogs return identical canonical materialized bytes." },
]
applicable_global_invariants = ["oxide-verification-policy", "convergent-authority"]
implementation_goals = ["Implement pure and incremental sequence, tombstone, move, and attribute algorithms with one canonical materializer."]
verification_goals = ["Prove deletion monotonicity, move-selection termination and acyclicity, attribute maximality, and kernel-wide materialization convergence."]
readiness = "ready"

[[stages]]
id = "offline-synchronization"
outcome = "Authorized replicas exchange missing groups safely, tolerate duplicate and reordered frames, and converge after quiescence under explicit fairness assumptions."
included_scope = ["Knowledge summaries", "Missing-range selection", "Bounded frames", "Durable cursors", "Backpressure", "Schema re-evaluation"]
excluded_scope = ["Claiming network fairness as a theorem", "Snapshot compaction", "Encrypted blind relay"]
dependencies = ["authorization-and-admission", "convergent-document-core"]
source_specifications = [
  { path = "eval/examples/collaborative-document/base/specs/PRODUCT.md", anchor = "Reconnect completion", requirement = "If two authorized replicas remain connected, exchange fair delivery, possess compatible schemas, and stop creating new operations, synchronization eventually leaves them with equal admitted-group sets." },
]
applicable_global_invariants = ["oxide-verification-policy", "durable-acknowledgment", "convergent-authority"]
implementation_goals = ["Implement conservative summaries, retransmission-safe cursors, ordinary frame admission, and bounded backpressure without transport authority."]
verification_goals = ["Use Verus to prove finite-trace synchronization safety, no false present claims, cursor monotonicity, duplicate idempotence, and conditional local progress while keeping fair delivery explicit."]
readiness = "ready"

[[stages]]
id = "snapshot-compaction-recovery"
outcome = "Closed-frontier snapshots restore to replay-equivalent state, and compaction preserves every retained public observation across crashes."
included_scope = ["Snapshot codec", "Closed-frontier validation", "Prepare-validate-publish restore", "Retention witnesses", "Crash recovery"]
excluded_scope = ["Capacity targets", "Unbounded offline retention", "Storage policy inside adapters"]
dependencies = ["convergent-document-core", "offline-synchronization"]
source_specifications = [
  { path = "eval/examples/collaborative-document/base/specs/PRODUCT.md", anchor = "Restore", requirement = "Restore then replays retained groups above the frontier through the same abstract transitions used by normal admission." },
]
applicable_global_invariants = ["oxide-verification-policy", "durable-acknowledgment", "convergent-authority"]
implementation_goals = ["Implement snapshot generation, suffix replay, compaction eligibility, durable generations, and newest-valid-image recovery."]
verification_goals = ["Prove snapshot replay equivalence, frontier closure, compaction observational preservation, recovery prefix integrity, and idempotent repeated recovery under the declared adapter premises."]
readiness = "ready"

[[stages]]
id = "public-composition"
outcome = "The complete production API and exact trusted boundary refine the approved collaborative-document behavior on the prospective authoritative tree."
included_scope = ["Public API wiring", "Coverage manifest", "Trusted adapter qualification", "Negative mutations", "Exact-tree composition"]
excluded_scope = ["Performance acceptance", "Editor UI", "Semantic search"]
dependencies = ["authorization-and-admission", "offline-synchronization", "snapshot-compaction-recovery"]
source_specifications = [
  { path = "eval/examples/collaborative-document/base/specs/DEVELOPMENT.md", anchor = "Composition theorem", requirement = "The exact production public API refines the PRODUCT abstract transitions for the exact prospective authoritative tree." },
]
applicable_global_invariants = ["oxide-verification-policy", "durable-acknowledgment", "convergent-authority"]
implementation_goals = ["Connect every public entry point, verified component, schema, and trusted adapter to the exact-tree release gate."]
verification_goals = ["Run the complete Verus composition theorem and deterministic cheat, coverage, mutation, and real-adapter gates against the exact prospective tree."]
readiness = "ready"

[[stages]]
id = "empirical-qualification"
outcome = "The exact formally qualified binary meets its independent convergence, isolation, crash, throughput, latency, amplification, and recovery objectives."
included_scope = ["Fault campaigns", "Capacity workload", "Recovery-time objective", "Proof-engineering measurements"]
excluded_scope = ["Generalizing finite measurements into proofs", "Changing product semantics to meet a benchmark"]
dependencies = ["public-composition"]
source_specifications = [
  { path = "eval/examples/collaborative-document/base/specs/RESEARCH.md", anchor = "Service objectives", requirement = "Under the standard connected workload, one reference server must sustain at least 500,000 admitted operation groups per second at 70 percent or less CPU saturation." },
  { path = "eval/examples/collaborative-document/base/specs/RESEARCH.md", anchor = "Recovery objectives", requirement = "For a 1 TiB retained history with a qualifying snapshot less than ten minutes old, restart to read availability must complete within 60 seconds on the reference target." },
]
applicable_global_invariants = ["oxide-verification-policy", "durable-acknowledgment", "convergent-authority"]
implementation_goals = ["Build sealed workload, crash, malformed-peer, synchronization, snapshot, capacity, and proof-metrics runners with machine-readable reports."]
verification_goals = ["Deterministically validate sealed campaign identities and report schemas, enforce that runners have no direct authority path, and treat all capacity and fault outcomes as empirical evidence rather than proof."]
readiness = "ready"
```
