# Customer Integration Agent Kit

**🔴 Live demo: https://hrutishaa.github.io/customer-integration-agent-kit/** — watch an agent sync a flaky CRM API and a CSV export with retries, schema mapping and idempotent upserts, right in your browser.

[![CI](https://github.com/hrutishaa/customer-integration-agent-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/hrutishaa/customer-integration-agent-kit/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

A Python toolkit for the work a **Forward Deployed Engineer** does on day one at a new customer: connect to their messy systems (REST APIs, webhooks, CRMs, CSV exports, databases), map their schema to yours, and let an LLM agent act on that data safely.

## Why

Most of an LLM integration project isn't the model. It's the plumbing: flaky APIs that return 429s, tokens that must stay out of code, CSV exports with odd columns, and writes that must never run twice. This kit makes that plumbing reusable and tested, so the agent layer stays small.

## Architecture

```mermaid
flowchart LR
    subgraph Customer systems
        A[REST API]
        B[Webhooks]
        C[CRM]
        D[CSV / DB]
    end
    subgraph agentkit
        E[Connectors<br/>retries · auth · idempotency]
        F[Schema mapping<br/>validation]
        G[Tool registry]
        H[LLM agent<br/>tool-calling loop]
        I[Evals + logs]
    end
    A & B & C & D <--> E
    E --> F --> G --> H
    H --> I
```

## What works today (v0.1)

| Piece | Highlights |
|---|---|
| `Connector` | One `read` / `write` / `health_check` interface for every system |
| `RestConnector` | httpx, timeouts, auth token from an env var, exponential backoff + jitter on 429/5xx and network errors, honors `Retry-After`, only retries POST when an `Idempotency-Key` is given |
| `CSVConnector` | Filtered reads, append-safe writes that reject unknown columns, path-traversal guard |
| `WebhookReceiver` | HMAC-SHA256 verified over the raw request bytes, constant-time compare, signed timestamp with a 5-minute window, replay protection that only marks events seen after the handler succeeds, zero-downtime secret rotation |
| `SQLConnector` | SQLite / Postgres via SQLAlchemy Core, DB URL from an env var, table and column names checked against the live schema and every value bound (no SQL injection, even from an LLM), keyset-paginated batch reads, all-or-nothing transactional writes, idempotent upserts, `read_only` mode, single-SELECT `query()` for reports |
| `CRMConnector` + mock Acme CRM | Follows opaque cursors page by page (stops on a repeated cursor or a `max_pages` cap instead of looping forever), incremental `sync()` with an `updated_since` watermark, upserts that search first and PATCH only changed fields, deterministic `Idempotency-Key` on creates so a re-run sync never duplicates, refuses ambiguous matches, and inherits 429/`Retry-After` backoff. `agentkit.mock.AcmeCRM` is a realistic in-process CRM (auth, rate limits, keyset cursors, 409/422 errors) for tests and demos |

| `agentkit.mapping` | Declarative YAML field mappings (rename, cast, default, compute with a `{Field}` template, never `eval`), optional Pydantic validation, per-record error reports instead of a crashed batch, and a `dry_run` that previews creates / updates / unchanged with field-level diffs before anything is written |

## Quickstart

```bash
git clone https://github.com/hrutishaa/customer-integration-agent-kit
cd customer-integration-agent-kit
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

```python
from agentkit import RestConnector, CSVConnector

# token is read from the CRM_API_TOKEN env var, never hard-coded
with RestConnector("https://api.example-crm.com", token_env="CRM_API_TOKEN", data_key="data") as crm:
    open_tickets = list(crm.read("/tickets", status="open"))

CSVConnector("./exports").write("open_tickets", open_tickets)
```

```python
from agentkit import WebhookReceiver, WebhookError

# secrets come from an env var; "new,old" lets you rotate without downtime
rx = WebhookReceiver(secret_env="CRM_WEBHOOK_SECRETS")
rx.on("ticket.created", lambda event: print("new ticket", event["data"]["id"]))

def endpoint(raw_body: bytes, headers: dict) -> int:  # plug into Flask, FastAPI, etc.
    try:
        return rx.handle(raw_body, headers).status   # duplicates also return 200
    except WebhookError as err:
        return err.status                            # 401 bad signature, 400 bad payload
```

```python
from agentkit import SQLConnector

# e.g. postgresql+psycopg://user:pass@replica/crm, read from the env, never hard-coded
with SQLConnector(url_env="CUSTOMER_DB_URL", read_only=True) as db:
    print(db.schema("accounts"))                       # describe the table to an agent
    gold = list(db.read("accounts", tier="gold"))      # streamed in 500-row batches
    churn = db.query("SELECT region, COUNT(*) AS n FROM accounts WHERE churned = :c GROUP BY region", c=True)

# nightly sync: re-running it never duplicates rows
SQLConnector("sqlite:///warehouse.db").write("accounts", gold, upsert_on="id")
```

```python
from agentkit import CRMConnector

# cursor pagination + rate-limit backoff handled for you; token from the env
with CRMConnector("https://api.acme-crm.com", token_env="ACME_TOKEN", page_size=100) as crm:
    result = crm.sync("contacts", since=saved_watermark)    # only what changed
    save_watermark(result.watermark)                         # resume point for next run
    crm.write("contacts", cleaned_contacts, upsert_on="email")  # safe to re-run
```

```yaml
# examples/contacts.yaml: the customer's column names live in config, not code
fields:
  email:  {from: "Email Address", transform: [strip, lower], required: true}
  name:   {compute: "{First Name} {Last Name}", transform: [collapse_spaces]}
  amount: {from: "Deal Value", cast: float, default: 0}
```

```python
from agentkit import load_mapping

mapper = load_mapping("examples/contacts.yaml")
print(mapper.dry_run(csv_rows, key="email", existing=crm_contacts).report())
# would create 41, update 3, leave 120, reject 2
#   ~ ada@example.com: stage: 'lead' -> 'won'
#   ! record 7, field 'amount': cannot cast 'lots' to float
good = mapper.apply(csv_rows)          # .records to write, .errors is the fix list
crm.write("contacts", good.records, upsert_on="email")
```

## Roadmap

Next: a tool-calling agent, an idempotency store, evals, a sample customer demo, and Docker. See [ROADMAP.md](ROADMAP.md).

## License

MIT © 2026 Hrutishaa Nanduti
