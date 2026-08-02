# Acceptance Criteria

The harness prototype is viable when all of the following are demonstrated:

1. Two concurrent workers cannot both claim the same task.
2. A worker cannot submit with an incorrect, stale, or superseded claim token.
3. A valid submission persists across journal and controller restart.
4. A vanished local worker is observed, fenced, and replaced immediately; an
   optional explicit lease still expires for an unobservable worker.
5. Dependencies prevent downstream tasks from becoming runnable too early.
6. Worker-proposed follow-ups are stored but never executed automatically.
7. The controller runs task-specific checks before accepting and merging.
8. A three-task toy stage completes end to end.
9. The toy stage later runs against the Rust backend without harness changes.
10. One real Stage 0 task completes through the same claim, submit, verify,
    and merge path.

Passing these criteria completes the Python prototype. It then freezes except
for defects that violate this contract. Production behavior belongs in Rust.
