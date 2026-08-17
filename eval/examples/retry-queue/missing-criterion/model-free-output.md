<!-- oxide-roadmap-schema:1 -->
```toml
schema = 1
title = "Roadmap"
status = "ready"
specification_root = "eval/examples/retry-queue/missing-criterion/specs"
[[global_invariants]]
id = "oxide-verification-policy"
statement = "Production logic has meaningful contracts, component refinement, complete coverage, and exact-tree composition; trusted effects remain narrow and policy-free."
sources = []

[[stages]]
id = "queue-core"
outcome = "Jobs can be claimed in FIFO order with exclusive ownership."
included_scope = ["FIFO dequeue", "Exclusive unacknowledged ownership"]
excluded_scope = ["Crash recovery", "Retry policy"]
dependencies = []
source_specifications = [
  { path = "eval/examples/retry-queue/missing-criterion/specs/PRODUCT.md", anchor = "Queue operations", requirement = "Jobs are dequeued in first-in, first-out order." },
  { path = "eval/examples/retry-queue/missing-criterion/specs/PRODUCT.md", anchor = "Queue operations", requirement = "At most one worker owns an unacknowledged job at a time." },
]
applicable_global_invariants = ["oxide-verification-policy"]
implementation_goals = ["Implement queue ordering and exclusive ownership transitions."]
verification_goals = ["Use Verus to prove FIFO selection and single-owner refinement."]
readiness = "ready"

[[stages]]
id = "queue-recovery"
outcome = "Crashed work becomes claimable again."
included_scope = ["Crash recovery"]
excluded_scope = ["Unspecified retry policy"]
dependencies = ["queue-core"]
source_specifications = [
  { path = "eval/examples/retry-queue/missing-criterion/specs/PRODUCT.md", anchor = "Recovery", requirement = "After a worker crash, its unacknowledged job becomes available again during recovery." },
]
applicable_global_invariants = ["oxide-verification-policy"]
implementation_goals = ["Implement recovery of unacknowledged work."]
verification_goals = ["Prove recovery preserves exclusive ownership."]
readiness = "ready"

[[stages]]
id = "retry-policy"
outcome = "Define a bounded retry policy and terminal failed-job disposition."
included_scope = ["Retry semantics requiring user clarification"]
excluded_scope = []
dependencies = ["queue-core"]
source_specifications = [
  { path = "eval/examples/retry-queue/missing-criterion/specs/PRODUCT.md", anchor = "Retry policy", requirement = "Failed jobs are retried, but the maximum attempt count and terminal disposition are not specified." },
]
applicable_global_invariants = ["oxide-verification-policy"]
implementation_goals = ["Obtain the missing maximum attempt count and terminal disposition before implementation."]
verification_goals = ["Define a non-vacuous retry contract after the missing semantics are approved."]
readiness = "blocked"
```
