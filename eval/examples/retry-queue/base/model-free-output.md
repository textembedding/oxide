<!-- oxide-roadmap-schema:1 -->
```toml
schema = 1
title = "Roadmap"
status = "ready"
specification_root = "eval/examples/retry-queue/base/specs"
[[global_invariants]]
id = "oxide-verification-policy"
statement = "Production logic has meaningful contracts, component refinement, complete coverage, and exact-tree composition; trusted effects remain narrow and policy-free."
sources = []

[[stages]]
id = "queue-core"
outcome = "Jobs can be claimed in FIFO order with exclusive ownership."
included_scope = ["FIFO dequeue", "Exclusive unacknowledged ownership"]
excluded_scope = ["Crash recovery", "Retries"]
dependencies = []
source_specifications = [
  { path = "eval/examples/retry-queue/base/specs/PRODUCT.md", anchor = "Queue operations", requirement = "Jobs are dequeued in first-in, first-out order." },
  { path = "eval/examples/retry-queue/base/specs/PRODUCT.md", anchor = "Queue operations", requirement = "At most one worker owns an unacknowledged job at a time." },
]
applicable_global_invariants = ["oxide-verification-policy"]
implementation_goals = ["Implement queue ordering and exclusive ownership transitions."]
verification_goals = ["Use Verus to prove FIFO selection and single-owner refinement."]
readiness = "ready"

[[stages]]
id = "queue-recovery"
outcome = "Crashed work is recovered and failed jobs terminate under the retry policy."
included_scope = ["Crash recovery", "Three-attempt retry limit", "Dead-letter disposition"]
excluded_scope = []
dependencies = ["queue-core"]
source_specifications = [
  { path = "eval/examples/retry-queue/base/specs/PRODUCT.md", anchor = "Recovery", requirement = "After a worker crash, its unacknowledged job becomes available again during recovery." },
  { path = "eval/examples/retry-queue/base/specs/PRODUCT.md", anchor = "Retry policy", requirement = "A failed job is attempted at most three times and is then moved to a dead-letter queue." },
]
applicable_global_invariants = ["oxide-verification-policy"]
implementation_goals = ["Implement recovery and the bounded retry transition."]
verification_goals = ["Prove recovery preserves ownership and retry attempts cannot exceed three."]
readiness = "ready"
```
