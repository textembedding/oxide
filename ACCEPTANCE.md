# Acceptance Criteria

The disposable harness is ready when:

1. the source tree contains only the thin CLI, generic worker, two-tool MCP
   facade, YAML codec, and one swappable journal prototype;
2. only the journal prototype imports SQLite or implements task transitions;
3. MCP lists exactly `journal_add` and `journal_search`;
4. two concurrent workers cannot both claim one task through `journal_add`;
5. checkpoint, handoff, exact commit, and `verified: true` are required before
   self-completion;
6. dependencies become ready immediately after their prerequisites complete;
7. claims have no timer and a replacement stable slot recovers them through
   `journal_search`;
8. seven concurrent workers can claim seven distinct tasks and traverse the
   complete real Stage 0 graph;
9. stream highlighting, safe indentation, the compact queue, pause, resume,
   reset, and all native macOS commands work;
10. an end-to-end model-free run can launch workers, complete a workload, stop,
    resume, and reset;
11. one authorized paid run exposes visible calls to both journal tools and
    completes a real task through worker-owned Git integration.

Run the model-free suite with:

```bash
./swarmctl verify
```

The Python prototype freezes after these criteria pass. Production journal
semantics belong in the Rust kernel behind the same two tools.
