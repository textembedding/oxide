# Non-Goals

The clean-room Python prototype will not implement:

- cgroup authority
- Bubblewrap qualification
- mount namespace proofs
- runtime closure hashing
- canonical runtime inventories
- self-hashed process documents
- process-action journals
- process authority systems
- artifact retirement state machines
- materialized-object generations
- verifier brokers
- controller replacement proofs
- recursive replay validation
- formal process-ownership receipts
- production security isolation
- distributed execution
- remote worker support
- automatic continuous task-graph expansion
- reasoning-trace capture
- terminal multiplexing
- a production-quality Python kernel

A worker crash, normal exit, cancellation, or timeout is represented as a
task-attempt outcome or an expired lease. The prototype does not model every
operating-system action that led to it.
