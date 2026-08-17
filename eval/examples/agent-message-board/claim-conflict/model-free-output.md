<!-- oxide-roadmap-schema:1 -->
```toml
schema = 1
title = "Roadmap"
status = "ready"
specification_root = "eval/examples/agent-message-board/claim-conflict/specs"
[[global_invariants]]
id = "oxide-verification-policy"
statement = "Production logic has meaningful contracts, component refinement, complete coverage, and exact-tree composition; trusted effects remain narrow and policy-free."
sources = []

[[stages]]
id = "formal-board-foundation"
outcome = "Canonical record types and the public abstract board state establish the semantic foundation for every production component."
included_scope = ["Bounded canonical types", "Immutable RecordId and BoardSeq views", "Public BoardState model", "State invariants and abstract transitions"]
excluded_scope = ["External effects", "Publication execution", "Empirical capacity"]
dependencies = []
source_specifications = [
  { path = "eval/examples/agent-message-board/claim-conflict/specs/PRODUCT.md", anchor = "3.4 Record", requirement = "No operation edits, replaces, or deletes an admitted record." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/DEVELOPMENT.md", anchor = "3.1 Board state", requirement = "The public abstract state is `BoardState`." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/DEVELOPMENT.md", anchor = "3.3 State invariants", requirement = "Every RecordId maps to exactly one canonical record." },
]
applicable_global_invariants = ["oxide-verification-policy"]
implementation_goals = ["Implement bounded schemas, canonical encoding, identifiers, and a representation-independent abstract board model.", "Connect each foundational production path to stable mathematical views and manifest coverage."]
verification_goals = ["Use Verus to prove encoding round trips, finite bounds, immutable identity, sequence-map consistency, constructive initial state, and preservation of the foundational BoardState invariants."]
readiness = "ready"

[[stages]]
id = "trusted-effects-and-capabilities"
outcome = "Narrow effect contracts and verified capability policy provide qualified inputs without hiding board authority."
included_scope = ["Storage, cryptography, clock, network, and semantic adapter contracts", "Capability parsing and narrowing", "Batch authorization", "Cross-board isolation"]
excluded_scope = ["Atomic publication", "Semantic relevance claims"]
dependencies = ["formal-board-foundation"]
source_specifications = [
  { path = "eval/examples/agent-message-board/claim-conflict/specs/PRODUCT.md", anchor = "10.2 Capability evaluation", requirement = "Every record in a batch must satisfy the capability independently." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/DEVELOPMENT.md", anchor = "13.1 Storage transaction adapter", requirement = "It provides guarded conditional insert or update with at most one successful contender per guard instance." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/DEVELOPMENT.md", anchor = "5.1 Verified policy input", requirement = "The crypto adapter does not decide whether an action is authorized." },
]
applicable_global_invariants = ["oxide-verification-policy"]
implementation_goals = ["Implement verified capability policy and minimal adapter ports whose observations cannot decide product policy.", "Classify and bind every trusted assumption before dependent logic is admitted."]
verification_goals = ["Use Verus to prove capability narrowing, batch authorization, label monotonicity, and cross-board noninterference; qualify guarded storage exclusivity and other trusted premises with real-adapter races and fault fixtures."]
readiness = "ready"

[[stages]]
id = "atomic-global-publication"
outcome = "Authorized atomic batches become immutable records in one globally agreed chronology with exact idempotent recovery."
included_scope = ["Pure publication preparation", "Idempotency arbitration", "BoardSeq interval reservation", "Atomic durable commit", "Multi-topic single-record visibility", "Linearizable read cuts"]
excluded_scope = ["Coordination projections", "Subscriptions", "Physical range movement"]
dependencies = ["formal-board-foundation", "trusted-effects-and-capabilities"]
source_specifications = [
  { path = "eval/examples/agent-message-board/claim-conflict/specs/PRODUCT.md", anchor = "5.3 Atomic visibility", requirement = "The batch either admits every record or admits none." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/PRODUCT.md", anchor = "6.1 Linearizable cut", requirement = "Concurrent publications may be ordered either way, but every observer agrees on the chosen BoardSeq order." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/PRODUCT.md", anchor = "5.5 Idempotency", requirement = "Retrying the same bound key with the same canonical request returns the exact RecordIds and BoardSeq values without admitting another record." },
]
applicable_global_invariants = ["oxide-verification-policy"]
implementation_goals = ["Implement the publication validator, idempotency binding, checked sequence allocation, durable transaction plan, and exact outcome recovery.", "Expose fixed-cut history and RecordId reads over the same chronology."]
verification_goals = ["Use Verus to prove non-overlapping sequence authority, all-or-none visibility, backward causal links, retry equivalence, authorization preservation, and refinement of every publication result to one abstract transition."]
readiness = "ready"

[[stages]]
id = "typed-coordination"
outcome = "Typed claims, decisions, corrections, blockers, results, and handoffs converge to deterministic authority while retaining all evidence."
included_scope = ["Coordination schema validation", "Claim and Decision arbitration", "Correction and Retraction projection", "Handoff and Result closure", "Session fencing"]
excluded_scope = ["Wall-clock claim expiry", "Truth inference from prose"]
dependencies = ["atomic-global-publication"]
source_specifications = [
  { path = "eval/examples/agent-message-board/claim-conflict/specs/PRODUCT.md", anchor = "4.4 Claim", requirement = "For one claim key and generation, the valid Claim with the lowest BoardSeq is the winner." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/CLAIM-CONFLICT.md", anchor = "Winner selection", requirement = "When two valid Claims compete for one claim key and generation, the lexicographically smallest requested owner principal is authoritative, even when its BoardSeq is greater." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/PRODUCT.md", anchor = "7.4 Crash and replacement", requirement = "A replacement cannot become authoritative while the old session remains unfenced." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/DEVELOPMENT.md", anchor = "7.6 Projection replay", requirement = "Replaying the same canonical record sequence produces the same coordination projection." },
]
applicable_global_invariants = ["oxide-verification-policy"]
implementation_goals = ["Implement pure typed coordination validation and projection over immutable records.", "Implement exact predecessor tokens and session fences for safe ownership replacement."]
verification_goals = ["Resolve and approve the contradictory BoardSeq-versus-principal claim winner semantics before generating an authoritative coordination contract; then use Verus to prove the approved unique-winner rule, generation closure, fencing, access-label preservation, and deterministic replay."]
readiness = "blocked"

[[stages]]
id = "durable-delivery"
outcome = "Machine subscriptions deliver matching records chronologically through monotonic durable cursors without durable inbox copies."
included_scope = ["Normalized subscription predicates", "Cut-bound delivery enumeration", "Cursor acknowledgement guards", "Backpressure", "Derived fan-out rebuild"]
excluded_scope = ["Exactly-once network delivery"]
dependencies = ["atomic-global-publication", "trusted-effects-and-capabilities"]
source_specifications = [
  { path = "eval/examples/agent-message-board/claim-conflict/specs/PRODUCT.md", anchor = "8.3 Cursor acknowledgement", requirement = "Acknowledgement advances a cursor monotonically." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/PRODUCT.md", anchor = "8.4 Deduplicated fan-out", requirement = "The service stores one logical record regardless of subscriber count." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/DEVELOPMENT.md", anchor = "10.4 Recovery", requirement = "After restart, enumeration begins after the durable cursor and may redeliver unacknowledged records." },
]
applicable_global_invariants = ["oxide-verification-policy"]
implementation_goals = ["Implement predicate compilation, bounded delivery, guarded cursor advancement, and rebuildable shared fan-out indexes."]
verification_goals = ["Use Verus to prove predicate equivalence, BoardSeq delivery order, per-subscription deduplication, cursor monotonicity, stale-token rejection, backpressure bounds, and restart redelivery semantics."]
readiness = "ready"

[[stages]]
id = "bounded-context-recovery"
outcome = "Fresh agents recover bounded exact, lexical, semantic, causal, and unresolved-work context without granting retrieval hints authority."
included_scope = ["Exact and lexical candidate indexes", "Semantic adapter integration", "Causal closure", "Bounded context selector", "Resolved and unresolved-work views", "Iterative query frontier"]
excluded_scope = ["Semantic relevance as a theorem", "Exhaustive one-shot retrieval"]
dependencies = ["atomic-global-publication", "trusted-effects-and-capabilities", "typed-coordination"]
source_specifications = [
  { path = "eval/examples/agent-message-board/claim-conflict/specs/PRODUCT.md", anchor = "9.2 Authority boundary", requirement = "Semantic similarity never creates a causal link, claim, decision, correction, authorization, or task dependency." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/PRODUCT.md", anchor = "9.3 Context selection", requirement = "The final selected union is returned strictly by ascending BoardSeq." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/DEVELOPMENT.md", anchor = "9.4 Selector proof", requirement = "It does not claim semantic relevance or exhaustive retrieval." },
]
applicable_global_invariants = ["oxide-verification-policy"]
implementation_goals = ["Implement rebuildable candidate indexes, exact candidate validation, bounded selection, causal frontier reporting, and coordination-derived unresolved-work views."]
verification_goals = ["Use Verus to prove eligibility, authorization, exact-anchor floor, deduplication, finite budgets, causal-frontier accounting, chronological output, and the inability of semantic scores or prompt content to alter authority."]
readiness = "ready"

[[stages]]
id = "live-shard-movement"
outcome = "The board scales across physical ranges whose live movement is observationally equivalent to one unsharded chronology."
included_scope = ["Versioned routing map", "Unique write tokens", "Shadow copy", "Guarded cutover", "Overlap reads", "Movement crash recovery"]
excluded_scope = ["Topology-dependent product semantics"]
dependencies = ["atomic-global-publication", "trusted-effects-and-capabilities"]
source_specifications = [
  { path = "eval/examples/agent-message-board/claim-conflict/specs/PRODUCT.md", anchor = "11.3 Live shard movement", requirement = "No authorized retained record may be omitted or duplicated because movement is active." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/DEVELOPMENT.md", anchor = "11.3 Cutover", requirement = "Exactly one contender can replace write authority for that guard instance." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/DEVELOPMENT.md", anchor = "11.4 Read overlap", requirement = "The movement proof shows the merged result equals a read from one unsharded abstract board at the same cut." },
]
applicable_global_invariants = ["oxide-verification-policy"]
implementation_goals = ["Implement routing authority, idempotent range copy, range commitments, exact cutover recovery, and source/destination read merge."]
verification_goals = ["Use Verus to prove one write authority, stale-source rejection, copy identity, no omission or duplication at fixed cuts, pre/post-cutover crash safety, and unsharded observational equivalence."]
readiness = "ready"

[[stages]]
id = "checkpoint-retention-recovery"
outcome = "Cut-consistent checkpoints, safe compaction, and fail-closed replay recover the same retained public board after crashes."
included_scope = ["Checkpoint capture and validation", "Retention classes", "Compaction eligibility", "Genesis and suffix replay", "Integrity failure", "Derived index rebuild"]
excluded_scope = ["Silent corruption repair", "Retention promises absent from configuration"]
dependencies = ["typed-coordination", "durable-delivery", "bounded-context-recovery", "live-shard-movement"]
source_specifications = [
  { path = "eval/examples/agent-message-board/claim-conflict/specs/PRODUCT.md", anchor = "12.1 Checkpoint", requirement = "Restoring a checkpoint followed by replay above its cut produces the same logical state as full replay." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/PRODUCT.md", anchor = "13.2 Recovery result", requirement = "Recovery either produces one state observationally equivalent to pre-crash acknowledged history or fails closed before serving traffic." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/DEVELOPMENT.md", anchor = "12.3 Compaction planner", requirement = "It never marks an ineligible record compactable." },
]
applicable_global_invariants = ["oxide-verification-policy"]
implementation_goals = ["Implement canonical checkpoints, verified compaction planning, authoritative replay, corruption detection, and post-replay derived-index rebuild."]
verification_goals = ["Use Verus to prove common-cut checkpoint validity, compaction safety, genesis versus checkpoint-plus-suffix equivalence, preservation of claims and cursors, and fail-closed corruption behavior."]
readiness = "ready"

[[stages]]
id = "whole-program-assurance"
outcome = "Every public operation refines the abstract board through a closed production and trusted-effect boundary on the exact prospective tree."
included_scope = ["Public handler composition", "Coverage manifest", "Cheat rejection", "Exact-tree proof", "Real-adapter boundary fixtures", "Proof sensitivity"]
excluded_scope = ["Empirical throughput and semantic relevance"]
dependencies = ["typed-coordination", "durable-delivery", "bounded-context-recovery", "live-shard-movement", "checkpoint-retention-recovery"]
source_specifications = [
  { path = "eval/examples/agent-message-board/claim-conflict/specs/DEVELOPMENT.md", anchor = "14.3 Composition theorem", requirement = "The whole-program theorem connects every public handler to one abstract transition or observation over `BoardState`." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/DEVELOPMENT.md", anchor = "15.3 Exact-tree gate", requirement = "Authoritative merge requires the complete composition proof on the exact prospective production tree." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/DEVELOPMENT.md", anchor = "18.2 Real integration boundary", requirement = "Atomic publication, idempotency race, sequence race, cursor race, routing cutover, checkpoint durability, and crash recovery fixtures run against the declared real storage adapter." },
]
applicable_global_invariants = ["oxide-verification-policy"]
implementation_goals = ["Close every public API path through verified production components and narrow trusted adapters.", "Bind exact-tree proof, deterministic policy, real-boundary fixtures, and mutation sensitivity into immutable evidence."]
verification_goals = ["Run every component Verus root, deterministic cheat checks, named rejecting mutants, and the complete composition theorem against the exact prospective production tree with complete coverage and trusted-boundary identity."]
readiness = "ready"

[[stages]]
id = "capacity-and-agent-utility"
outcome = "The exact verified binary meets the declared high-scale capacity, fault, recovery, and bounded agent-context objectives on the reference deployment."
included_scope = ["Sustainable throughput", "Publication and fan-out latency", "Storage amplification", "Fault campaigns", "Shard movement", "Retrieval quality", "Fresh-agent utility", "Proof runtime and stability"]
excluded_scope = ["Generalizing finite measurements into logical theorems"]
dependencies = ["whole-program-assurance"]
source_specifications = [
  { path = "eval/examples/agent-message-board/claim-conflict/specs/RESEARCH.md", anchor = "6.1 Sustainable throughput", requirement = "The release target is 5,000,000 admitted records per second across the reference deployment." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/RESEARCH.md", anchor = "9.5 Agent utility", requirement = "The study measures answer accuracy, follow-up query count, input tokens, wall time, and unsupported assertions." },
  { path = "eval/examples/agent-message-board/claim-conflict/specs/RESEARCH.md", anchor = "16. Release disposition", requirement = "An empirical pass cannot waive a logical failure." },
]
applicable_global_invariants = ["oxide-verification-policy"]
implementation_goals = ["Execute preregistered reference-deployment capacity, contention, fault, retrieval, movement, retention, isolation, and proof-engineering campaigns against the exact verified binary."]
verification_goals = ["Preserve the complete Verus and deterministic-policy pass while separately requiring reproducible empirical evidence for throughput, latency, recovery, storage, and agent utility; reject either gate independently when its criteria fail."]
readiness = "ready"
```
