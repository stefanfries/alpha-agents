# ADR-002: In-Memory Typed Pydantic Models for Inter-Agent Communication

**Date:** 2026-03-29
**Status:** Accepted

---

## Context

Agents need to pass data to each other. The choice of communication mechanism has implications for type safety, testability, and operational complexity.

Options considered:

1. **In-memory Pydantic models** — agents return typed objects directly
2. **Message queue** (e.g. Redis Streams, RabbitMQ) — agents publish/consume messages asynchronously
3. **Database** (e.g. MongoDB) — agents write results to a collection; next agent reads from it

## Decision

Agents communicate via **in-memory Pydantic V2 models** passed directly between pipeline stages.

## Rationale

- Type safety is enforced at the Python level — passing the wrong model to an agent is a compile-time-style error, not a runtime surprise
- Zero infrastructure: no message broker or database needed to run the pipeline
- Pydantic models are trivially serialisable to JSON for logging and audit trails
- For a single-process, sequential pipeline there is no need for the decoupling that a message queue provides
- A message queue would be appropriate if agents needed to run in separate processes or scale independently — that is a future concern, not a current one

## Consequences

- All inter-agent contracts are defined as Pydantic models in `models/signals.py` — changes to contracts are visible in code review
- If agents are later distributed across services, the Pydantic models can be serialised to JSON and sent over the wire with minimal changes
- There is no built-in persistence of intermediate results; the orchestrator is responsible for logging each stage's output if an audit trail is needed
