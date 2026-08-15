# Durable counter

## Updates

A client may add a signed 64-bit delta to a named counter.

An update that would overflow the signed 64-bit range is rejected without changing the counter.

## Reads

A read returns the current value of the named counter.

## Durability

Every acknowledged update remains visible after process restart.

## HTTP adapter

An HTTP adapter is deferred until the counter state machine is implemented and proved.

