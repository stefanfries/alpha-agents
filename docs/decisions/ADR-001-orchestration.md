# ADR-001: Sequential Pipeline Orchestration

**Date:** 2026-03-29
**Status:** Accepted

---

## Context

The investment workflow has a natural order: you must research securities before you can screen them, screen them before you can size positions, and so on. We need to decide how the five agents coordinate with each other.

Three patterns were considered:

1. **Sequential pipeline** — agents run in a fixed order; each output becomes the next input
2. **Event-driven** — agents react to events published on a message bus
3. **LLM-orchestrated** — a large language model decides which agent to invoke at each step

## Decision

We use a **sequential pipeline** (`orchestrator.Pipeline`).

## Rationale

- The investment process has a fixed logical order; there is no reason to deviate from it at runtime
- A fixed pipeline is easy to reason about, debug, and audit — essential in a financial context where every decision must be explainable
- Event-driven architectures add significant infrastructure complexity (message broker, at-least-once delivery, idempotency) that is not justified at this stage
- LLM orchestration is non-deterministic; an LLM might skip the risk check or reorder steps in ways that are hard to detect, which is unacceptable for trade execution

## Consequences

- Adding a new agent means inserting it into the pipeline — straightforward
- Branching logic (e.g. "only run execution if risk approves") must be handled explicitly inside the orchestrator or inside individual agents
- Parallel execution of independent agents (e.g. running research for all tickers concurrently) is possible within a stage using `asyncio.gather()`, and is not precluded by this decision
