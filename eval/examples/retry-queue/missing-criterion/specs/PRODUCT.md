# Recoverable job queue

## Queue operations

Jobs are dequeued in first-in, first-out order.

At most one worker owns an unacknowledged job at a time.

## Recovery

After a worker crash, its unacknowledged job becomes available again during recovery.

## Retry policy

Failed jobs are retried, but the maximum attempt count and terminal disposition are not specified.

