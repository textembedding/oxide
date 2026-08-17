<!-- oxide-roadmap-schema:1 -->
```toml
schema = 1
title = "Roadmap"
status = "ready"
specification_root = "eval/examples/private-notes/base/specs"
[[global_invariants]]
id = "oxide-verification-policy"
statement = "Production logic has meaningful contracts, component refinement, complete coverage, and exact-tree composition; trusted effects remain narrow and policy-free."
sources = []

[[stages]]
id = "note-core"
outcome = "Accounts can create notes and search titles exactly."
included_scope = ["Create notes", "Creation-ordered exact title search"]
excluded_scope = ["Body access", "Semantic retrieval"]
dependencies = []
source_specifications = [
  { path = "eval/examples/private-notes/base/specs/PRODUCT.md", anchor = "Core operations", requirement = "An authenticated account can create a note with a title and body." },
  { path = "eval/examples/private-notes/base/specs/PRODUCT.md", anchor = "Core operations", requirement = "Exact title search returns matching notes in creation order." },
]
applicable_global_invariants = ["oxide-verification-policy"]
implementation_goals = ["Implement note creation and ordered exact title search."]
verification_goals = ["Use Verus to prove creation-order refinement."]
readiness = "ready"

[[stages]]
id = "body-access-control"
outcome = "Only the owning account can read a note body."
included_scope = ["Owning-account body access"]
excluded_scope = []
dependencies = ["note-core"]
source_specifications = [
  { path = "eval/examples/private-notes/base/specs/PRODUCT.md", anchor = "Access control", requirement = "Only the owning account can read a note body." },
]
applicable_global_invariants = ["oxide-verification-policy"]
implementation_goals = ["Implement the owning-account body-access rule."]
verification_goals = ["Use Verus to prove only the owning account can read a note body."]
readiness = "ready"

[[stages]]
id = "semantic-retrieval"
outcome = "Add semantic retrieval after relevance and privacy criteria are specified."
included_scope = ["Semantic full-text retrieval"]
excluded_scope = []
dependencies = ["note-core", "body-access-control"]
source_specifications = [
  { path = "eval/examples/private-notes/base/specs/PRODUCT.md", anchor = "Future retrieval", requirement = "Semantic full-text retrieval is deferred until its relevance and privacy criteria are specified." },
]
applicable_global_invariants = ["oxide-verification-policy"]
implementation_goals = ["Define the deferred semantic retrieval behavior."]
verification_goals = ["Prove semantic retrieval refines the approved privacy contract before implementation is admitted."]
readiness = "deferred"
```
