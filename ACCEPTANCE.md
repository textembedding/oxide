# Acceptance Criteria

The disposable harness is ready when:

1. `journal.py` is a workflow-agnostic append/search kernel and the only module
   importing SQLite;
2. an automated boundary test rejects PR, review, verification, merge,
   generation, dependency, task, or lifecycle concepts in that kernel;
3. all swarm semantics are replayed from generic records by `workflow.py`;
4. MCP lists exactly `journal_add` and `journal_search`;
5. deterministic journal order makes only the first of concurrent competing
   claims effective;
6. every task uses its own PR branch and no integration branch is configured;
7. a PR requires checkpoint, handoff, exact branch/base/head, author
   self-verification, and three internal reviews by default;
8. authors cannot review their own candidate, reviewers are distinct, and a
   challenge invalidates the candidate generation's remaining review work;
9. a changed candidate creates a new generation and repeats the full configured
   review count;
10. only a fully approved generation exposes merge work, and the launcher
    verifies the prospective merge tree before merging it directly to the
    target branch;
11. dependencies become ready immediately after their prerequisite PRs merge;
12. claims have no timer and a replacement stable slot recovers owned work
    through `journal_search`;
13. seven workers can traverse the complete real Stage 0 graph in a model-free
    workflow simulation;
14. stream highlighting, safe indentation, compact queue, pause, resume, reset,
    and all native macOS commands work;
15. the end-to-end model-free run performs authoring, three reviews, merge
    authorization, prospective verification, three merge commits, final gate,
    completion, and reset against a real Git checkout.

Run the complete model-free suite with:

```bash
./swarmctl verify
```

The Python journal kernel freezes once these criteria pass. New application
semantics belong in the workflow layer; the production Rust kernel replaces
only the same generic two-operation store.
