# Roadmap

Each milestone ships with tests. Checked items are on `main`.

## M1: Connectors
- [x] `Connector` base interface (read / write / health_check / context manager)
- [x] `RestConnector`: httpx, timeouts, env-var auth, exponential backoff on 429/5xx, `Retry-After`, idempotency keys for POST
- [x] `CSVConnector`: read with filters, append-safe writes, path-traversal guard
- [x] `WebhookReceiver`: HMAC signature verification over the raw body, replay protection
- [ ] `SQLConnector` (SQLite / Postgres via SQLAlchemy)
- [ ] CRM-style connector with cursor pagination (mock "Acme CRM")

## M2: Schema mapping
- [ ] Declarative YAML field mappings (rename, cast, default, compute)
- [ ] Pydantic validation and per-record error reports
- [ ] Mapping dry-run / diff mode

## M3: Agent
- [ ] Expose connectors as LLM tools (JSON schema auto-generated)
- [ ] Tool-calling loop with pluggable model provider (OpenAI-compatible / local)
- [ ] Guardrails: read-only mode, write confirmation, max tool calls

## M4: Reliability
- [ ] Idempotency store (SQLite) for dedup of webhook/agent writes
- [ ] Structured JSON logging with request IDs
- [ ] Circuit breaker per connector

## M5: Evals
- [ ] Golden task set for the sample scenario
- [ ] Metrics: tool-call accuracy, mapping accuracy, task success
- [ ] Eval run in CI with a mock model

## M6: Ship it
- [ ] Sample customer scenario demo (support tickets + CRM + CSV export)
- [ ] Dockerfile + docker-compose with a mock customer API
- [ ] Docs: "deploying at a new customer" playbook
