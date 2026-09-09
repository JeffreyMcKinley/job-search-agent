# Work History Profile

## How I describe myself
Backend and platform engineer, 9 years, strongest on high-integrity systems
(payments, ledgers, claims) and on the developer platform around them.

## Stories

### The monolith migration (Northwind, 2022-2024)
Inherited a Ruby monolith that took 40 minutes to deploy and broke on every
release. Proposed a strangler-fig migration, got buy-in by shipping the
checkout path first and proving the latency win. 14 services later, deploys
went from weekly to ~40/day. The hard part was not the code, it was the
30 or so people who had to change how they worked.

### The settlement bug (Northwind, 2023)
A double-settlement bug was costing about $18k/month. Traced it to a Kafka
consumer that acked before the DB commit. Fixed with an outbox pattern and
an idempotency key; wrote the reconciliation job that found and clawed back
the historical duplicates.

### Schema contracts (Vertex, 2021)
Nine producer teams kept silently breaking the reporting pipeline. Instead of
policing them, shipped a contract check in CI that failed the producer's
build. Incidents of that class went to zero within a quarter.

## What I want next
Staff-level scope, remote, a team that ships. Prefer to own a domain rather
than float across many.
