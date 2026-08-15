# Concurrent Editing Override

## Concurrent insertion precedence

This document applies to the same offline collaborative document engine and is
normative for concurrent text insertion.

> When two **authorized** inserts target the same stable gap and are concurrent,
> the insert with the lexicographically smaller canonical group dot must appear
> first.

The rule applies before member-local scalar offsets are considered.

No exception or compatibility rule reconciles this requirement with a different
ordering rule elsewhere in the specification corpus.
