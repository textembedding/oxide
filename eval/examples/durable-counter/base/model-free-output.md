<!-- oxide-roadmap-schema:1 -->
```toml
schema = 1
title = "Roadmap"
status = "ready"
specification_root = "eval/examples/durable-counter/base/specs"
global_invariants = []

[[stages]]
id = "counter-core"
outcome = "A durable checked counter is available through its core API."
included_scope = ["Signed updates", "Overflow rejection", "Current-value reads", "Restart durability"]
excluded_scope = ["HTTP transport"]
dependencies = []
source_specifications = [
  { path = "eval/examples/durable-counter/base/specs/PRODUCT.md", anchor = "Updates", requirement = "A client may add a signed 64-bit delta to a named counter." },
  { path = "eval/examples/durable-counter/base/specs/PRODUCT.md", anchor = "Updates", requirement = "An update that would overflow the signed 64-bit range is rejected without changing the counter." },
  { path = "eval/examples/durable-counter/base/specs/PRODUCT.md", anchor = "Reads", requirement = "A read returns the current value of the named counter." },
  { path = "eval/examples/durable-counter/base/specs/PRODUCT.md", anchor = "Durability", requirement = "Every acknowledged update remains visible after process restart." },
]
applicable_global_invariants = []
implementation_goals = ["Implement checked updates, reads, and durable recovery together."]
verification_goals = ["Use Verus to prove arithmetic safety, state refinement, and restart preservation."]
readiness = "ready"

[[stages]]
id = "http-adapter"
outcome = "Expose the proved counter through HTTP after its protocol is specified."
included_scope = ["HTTP adapter"]
excluded_scope = []
dependencies = ["counter-core"]
source_specifications = [
  { path = "eval/examples/durable-counter/base/specs/PRODUCT.md", anchor = "HTTP adapter", requirement = "An HTTP adapter is deferred until the counter state machine is implemented and proved." },
]
applicable_global_invariants = []
implementation_goals = ["Define and implement the deferred HTTP adapter."]
verification_goals = ["Prove the adapter refines the counter API once its wire contract is approved."]
readiness = "deferred"
```

