<!-- oxide-roadmap-schema:1 -->
```toml
schema = 1
title = "Roadmap"
status = "ready"
specification_root = "eval/examples/private-notes/base/specs"
global_invariants = []

[[stages]]
id = "private-note-core"
outcome = "Owners can create private notes and search their titles exactly."
included_scope = ["Create notes", "Creation-ordered exact title search", "Owner-only bodies"]
excluded_scope = ["Semantic full-text retrieval"]
dependencies = []
source_specifications = [
  { path = "eval/examples/private-notes/base/specs/PRODUCT.md", anchor = "Core operations", requirement = "An authenticated account can create a note with a title and body." },
  { path = "eval/examples/private-notes/base/specs/PRODUCT.md", anchor = "Core operations", requirement = "Exact title search returns matching notes in creation order." },
  { path = "eval/examples/private-notes/base/specs/PRODUCT.md", anchor = "Access control", requirement = "Only the owning account can read a note body." },
]
applicable_global_invariants = []
implementation_goals = ["Implement note creation, ordered exact search, and owner checks."]
verification_goals = ["Use Verus to prove owner isolation and creation-order refinement."]
readiness = "ready"

[[stages]]
id = "semantic-retrieval"
outcome = "Add privacy-preserving semantic retrieval after its criteria are specified."
included_scope = ["Semantic full-text retrieval"]
excluded_scope = []
dependencies = ["private-note-core"]
source_specifications = [
  { path = "eval/examples/private-notes/base/specs/PRODUCT.md", anchor = "Future retrieval", requirement = "Semantic full-text retrieval is deferred until its relevance and privacy criteria are specified." },
]
applicable_global_invariants = []
implementation_goals = ["Define the deferred semantic retrieval behavior."]
verification_goals = ["Prove semantic retrieval refines the approved privacy contract before implementation is admitted."]
readiness = "deferred"
```

