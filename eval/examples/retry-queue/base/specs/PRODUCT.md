# Recoverable job queue

## Queue operations

Jobs are dequeued in first-in, first-out order.

At most one worker owns an unacknowledged job at a time.

## Recovery

After a worker crash, its unacknowledged job becomes available again during recovery.

## Retry policy

A failed job is attempted at most three times and is then moved to a dead-letter queue.

