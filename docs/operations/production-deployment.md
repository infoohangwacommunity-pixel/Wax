# WAX Production Deployment Guide

**Status:** IMPLEMENTED
**Last updated:** 2026-09-13

This guide covers deploying WAX to production on Railway (or any container host).

## Prerequisites

1. A GitHub repository with the WAX codebase pushed to `main`.
2. A Railway account (or alternative container host: Fly.io, Render, AWS ECS).
3. A PostgreSQL database (Railway's PostgreSQL add-on, or external).
4. A Meta Developer account with a WhatsApp Business app (for the WhatsApp interface).
5. An OpenAI API key (or another LLM provider's key — see ADR-0001).

## Configuration

All configuration is via environment variables (prefix `WAX_`). See `.env.example` for the full list.

### Required in production

| Variable | Description | Example |
|---|---|---|
| `WAX_ENV` | Must be `production` | `production` |
| `WAX_SECRET_KEY` | Master secret; ≥32 chars; generate with `python -c "import secrets; print(secrets.token_urlsafe(64))"` | (random) |
| `WAX_DATABASE_URL` | PostgreSQL URL with asyncpg driver | `postgresql+asyncpg://user:pass@host:5432/wax` |
| `WAX_WHATSAPP_ACCESS_TOKEN` | From Meta Developer dashboard | (token) |
| `WAX_WHATSAPP_PHONE_NUMBER_ID` | From Meta Developer dashboard | (number) |
| `WAX_WHATSAPP_APP_SECRET` | App secret for webhook signature verification | (secret) |
| `WAX_WHATSAPP_VERIFY_TOKEN` | Arbitrary string you set; Meta echoes during webhook setup | `wax-verify-xyz` |
| `WAX_LLM_DEFAULT_PROVIDER` | `openai` for production | `openai` |
| `WAX_OPENAI_API_KEY` | OpenAI API key | `sk-...` |

### Optional

| Variable | Default | Description |
|---|---|---|
| `WAX_LOG_LEVEL` | `INFO` | One of: DEBUG, INFO, WARNING, ERROR, CRITICAL |
| `WAX_LOG_FORMAT` | `console` | `console` (dev) or `json` (production) |
| `WAX_HOST` | `127.0.0.1` | Bind address (use `0.0.0.0` in containers) |
| `WAX_PORT` | `8000` | Bind port (Railway injects `$PORT`) |

## Deployment on Railway

### 1. Create a Railway project

1. Go to [railway.app](https://railway.app) and sign in.
2. Click "New Project" → "Deploy from GitHub repo".
3. Select your WAX repository.
4. Railway reads `railway.toml` and builds automatically.

### 2. Add a PostgreSQL database

1. In the Railway project, click "New" → "Database" → "PostgreSQL".
2. Railway creates the database and provides a `DATABASE_URL` variable.
3. In your WAX service settings, set `WAX_DATABASE_URL` to `postgresql+asyncpg://...` (replace `postgresql://` with `postgresql+asyncpg://`).

### 3. Set environment variables

In Railway's service settings → Variables, add:
- `WAX_ENV=production`
- `WAX_SECRET_KEY=<generate with: python -c "import secrets; print(secrets.token_urlsafe(64))">`
- `WAX_DATABASE_URL=postgresql+asyncpg://...`
- `WAX_WHATSAPP_ACCESS_TOKEN=<from Meta>`
- `WAX_WHATSAPP_PHONE_NUMBER_ID=<from Meta>`
- `WAX_WHATSAPP_APP_SECRET=<from Meta>`
- `WAX_WHATSAPP_VERIFY_TOKEN=<a string you choose>`
- `WAX_LLM_DEFAULT_PROVIDER=openai`
- `WAX_OPENAI_API_KEY=sk-...`

### 4. Configure the WhatsApp webhook

1. In Meta Developer dashboard → your app → WhatsApp → Configuration:
   - Callback URL: `https://<your-railway-domain>/webhooks/whatsapp`
   - Verify Token: the value you set as `WAX_WHATSAPP_VERIFY_TOKEN`
2. Click "Verify and Save" — Meta sends a GET request; WAX echoes the challenge.
3. Subscribe to `messages` and `message_status` webhook fields.

### 5. Deploy

Push to `main`:
```bash
git push origin main
```

Railway auto-deploys. The deploy runs:
1. `pip install uv && uv pip install --system -e .` (install dependencies)
2. `alembic upgrade head` (apply migrations)
3. `uvicorn wax.runtime.asgi:app --host 0.0.0.0 --port $PORT` (start server)

Health check at `/healthz` confirms the process is up.

## Deployment validation

After deploy, verify:

```bash
# Liveness
curl https://<domain>/healthz
# → {"status":"ok","version":"0.1.0"}

# Readiness (all deps up)
curl https://<domain>/readyz
# → {"status":"ok","checks":{"database":"ok","intelligence":"ok","whatsapp":"ok"}}

# Migration status
curl https://<domain>/migrations/status
# → {"current_revision":"...","head_revision":"...","up_to_date":true}

# Config validation
curl https://<domain>/config/validate
# → {"env":"production","validation_passed":true,"production_ready":true,...}
```

## Observability

### Logs

WAX uses `structlog` for structured JSON logs in production. Railway captures stdout automatically.

Key events to monitor:
- `wax.runtime.starting` — process started
- `wax.runtime.stopping` — graceful shutdown
- `bridge.duplicate` — idempotency hit (normal under Meta retries)
- `bridge.process.error` — runtime bridge failure
- `whatsapp.message.sent` — outbound message sent
- `whatsapp.webhook.invalid_signature` — security event
- `authority.denied` — authorization denied
- `abuse.detected` — abuse signal
- `rate_limited` — rate limit triggered
- `cost.token_cap_exceeded` — cost cap hit

### Metrics

`GET /metrics` returns the in-memory metrics snapshot (counters, gauges, histograms). For Prometheus scraping, add a scrape config:

```yaml
scrape_configs:
  - job_name: 'wax'
    scrape_interval: 15s
    metrics_path: /metrics
    static_configs:
      - targets: ['<your-domain>']
```

### Audit trail

Every authorization decision, capability invocation, and agency decision is recorded in the `audit_events` table. Query for forensic analysis:

```sql
SELECT created_at, actor_kind, event_kind, outcome, payload
FROM audit_events
WHERE actor_principal_id = '<principal_id>'
ORDER BY created_at DESC
LIMIT 100;
```

## Migrations

### Apply migrations

```bash
WAX_DATABASE_URL=postgresql+asyncpg://... \
WAX_SECRET_KEY=... \
alembic upgrade head
```

### Rollback

```bash
alembic downgrade -1   # rollback one revision
alembic downgrade -3   # rollback three revisions
```

### Inspect status

```bash
alembic current         # current revision in DB
alembic history         # all revisions
```

## Failure recovery

### Dead-letter entries

When operations exhaust retries, they land in `dead_letter_entries`. Inspect:

```sql
SELECT created_at, kind, error_type, error_message, attempts, payload
FROM dead_letter_entries
WHERE reprocessed = false
ORDER BY created_at DESC;
```

After fixing the underlying issue, mark entries reprocessed:

```python
from wax.reliability.dead_letter import DeadLetterRepository
async with db_session() as session:
    repo = DeadLetterRepository(session)
    await repo.mark_reprocessed("<entry_id>")
    await session.commit()
```

### Circuit breaker reset

If a circuit breaker is stuck open (e.g. after a long outage):

```python
from wax.reliability.circuit_breaker import CircuitBreaker
breaker = CircuitBreaker(name="openai_api")
breaker.reset()
```

## Security checklist

Before going live:

- [ ] `WAX_SECRET_KEY` is set to a strong random value (≥64 chars recommended)
- [ ] `WAX_ENV=production` (triggers strict validation)
- [ ] `WAX_DATABASE_URL` is PostgreSQL (not SQLite)
- [ ] `WAX_LOG_FORMAT=json` (machine-readable)
- [ ] `WAX_WHATSAPP_APP_SECRET` is set (webhook signature verification)
- [ ] `WAX_WHATSAPP_VERIFY_TOKEN` is a non-default string
- [ ] `WAX_OPENAI_API_KEY` is set (or another LLM provider)
- [ ] Health check endpoints respond (`/healthz`, `/readyz`)
- [ ] `/config/validate` reports `validation_passed: true`
- [ ] `/migrations/status` reports `up_to_date: true`
- [ ] CI passes on the latest commit
- [ ] The Railway deployment is healthy (no restart loops)

## Backup

### Database backup

PostgreSQL on Railway supports automated backups. For manual backup:

```bash
pg_dump $WAX_DATABASE_URL > wax_backup_$(date +%Y%m%d).sql
```

### Restore

```bash
psql $WAX_DATABASE_URL < wax_backup_YYYYMMDD.sql
```

## Scaling

For higher throughput:

1. Increase Railway service instances (horizontal scaling).
2. Each instance is stateless (state lives in PostgreSQL).
3. The idempotency table (`processed_messages`) ensures duplicate webhooks don't double-execute across instances.
4. Use a connection pooler (PgBouncer) if database connections become the bottleneck.

## Troubleshooting

### Webhook signature verification fails

- Verify `WAX_WHATSAPP_APP_SECRET` matches the app secret in Meta Developer dashboard.
- Verify the raw request body is being passed to `verify_webhook_signature()` (not parsed JSON).

### Database migration fails

- Check `/migrations/status` for the current revision.
- Try `alembic downgrade -1` then `alembic upgrade head`.
- If a migration is partially applied, manually fix the schema + update `alembic_version` table.

### WhatsApp messages not arriving

- Verify webhook subscription in Meta Developer dashboard.
- Check `/healthz` and `/readyz` are 200.
- Check logs for `whatsapp.webhook.invalid_signature`.
- Verify `WAX_WHATSAPP_APP_SECRET` is correct.

### LLM calls failing

- Verify `WAX_LLM_DEFAULT_PROVIDER` and the corresponding API key.
- Check circuit breaker state (if open, the dependency is failing).
- Check `/metrics` for the `llm_latency_ms` histogram.
