# ADR-008: Web-Based User Interface (FastAPI + Jinja2 + HTMX)

**Date:** 2026-04-17
**Status:** Proposed
**Supersedes:** ADR-005 (CLI-based man-in-the-loop pattern)

---

## Context

The system is intended to run in the cloud (Azure Container Apps, AWS, or GCP). A CLI-based man-in-the-loop interface (as specified in ADR-005) is unsuitable for a cloud deployment: it requires terminal access to the running container and cannot be used from a browser.

Requirements for the new interface:

- Must be accessible from a browser without terminal access
- Must be implementable in Python only (no TypeScript/React/Vue)
- Must support the MITL review workflow: inspect stage outputs, approve or restart from a named stage
- Must be able to render financial charts (candlestick, trend indicators) inline
- Must work within the existing FastAPI ecosystem used in the sibling projects

## Options evaluated

### Option A: Full SPA framework (React, Vue, Angular)

Requires JavaScript/TypeScript expertise. Out of scope given the constraint of staying within Python.

**Verdict: Rejected.**

### Option B: Streamlit or Gradio

Pure Python, quick to prototype, built-in chart support. However:

- Not designed for production web apps — routing, authentication, and layout control are limited
- Hard to integrate with an existing FastAPI service
- Streamlit's execution model (full re-run on every interaction) does not map cleanly onto the pipeline's step-by-step MITL workflow

**Verdict: Not a good fit for the MITL review use case.**

### Option C: FastAPI + Jinja2 + HTMX

- **FastAPI** serves HTML pages and HTML *fragments* (partial renders)
- **Jinja2** templates handle all HTML generation server-side — no client-side rendering
- **HTMX** is a small JavaScript library included via a single `<script>` tag; it enables dynamic page updates via HTML attributes (`hx-get`, `hx-post`, `hx-swap`) without the developer writing any JavaScript
- Charts are rendered server-side by **Plotly** (interactive HTML `<div>`) or **mplfinance** (PNG/SVG served inline) and swapped into the page by HTMX

**Verdict: Best fit.** Stays entirely within Python, integrates naturally with FastAPI, and supports the fragment-based interaction pattern that HTMX is designed for.

## Decision

Implement the web UI as a **FastAPI + Jinja2 + HTMX** application.

Key structural choices:

- The pipeline orchestrator exposes a FastAPI router (`/pipeline`) with endpoints for starting, resuming, and reviewing runs
- Each MITL checkpoint renders a stage-summary HTML fragment that HTMX loads into the review panel without a full page reload
- The user approves or restarts by clicking a button — HTMX POSTs to the orchestrator, which advances or rewinds the pipeline and returns the next fragment
- Pipeline run state is persisted in MongoDB Atlas as before (no change to the persistence model)
- Charts are rendered on demand by the server: the `/charts/{run_id}/{stage}` endpoint returns a Plotly HTML fragment that HTMX swaps into a `<div>` in the review panel

## Charting

| Library | Output format | Interactivity | Recommendation |
| ------- | ------------- | ------------- | -------------- |
| **Plotly** (`plotly`) | Self-contained HTML `<div>` with embedded JS | Full (zoom, pan, hover, crosshair) | **Preferred** for web |
| **mplfinance** | PNG or SVG rendered to a byte buffer | Static | Fallback for specialised chart types not supported by Plotly |

Plotly charts are generated in the FastAPI route handler via `plotly.graph_objects.Figure.to_html(full_html=False)` and returned as an HTML fragment. HTMX inserts the fragment into the page. No file I/O or base64 encoding required.

mplfinance charts can be used for chart types not available in Plotly (e.g. custom candlestick styles). They are rendered to a PNG byte buffer via `io.BytesIO`, base64-encoded, and embedded as `<img src="data:image/png;base64,...">`.

## Technology additions

| Package | Purpose |
| ------- | ------- |
| `jinja2` | Server-side HTML templating |
| `plotly` | Interactive candlestick and indicator charts |
| `mplfinance` | Static chart fallback (optional) |
| HTMX (CDN) | Dynamic HTML fragment swapping; no install required |

HTMX is loaded from the CDN in the base template and requires no `uv add` step.

## Consequences

- ADR-005's CLI MITL checkpoint protocol is superseded; the `_checkpoint()` logic in the orchestrator is replaced by HTTP endpoints
- The deployment unit expands: the pipeline is no longer a standalone CLI script (`main.py`) but a FastAPI application (`app/main.py`) that also exposes the web UI
- This aligns with the existing `fastapi-azure-container-app` deployment pattern used in sibling projects
- The `execution_dry_run=True` default and all safety constraints from ADR-005 are unchanged — the web UI adds a confirmation step before the execution stage
- A future ADR should address authentication (who can access the web UI in production)
