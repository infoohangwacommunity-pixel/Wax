# WAX Operations

## Deployment

```bash
# Install
pip install -e .

# Run migrations
alembic upgrade head

# Start
uvicorn wax.runtime.asgi:app --host 0.0.0.0 --port $PORT
```

## Health checks

- `GET /healthz` — liveness (process up)
- `GET /readyz` — readiness (DB + intelligence + WhatsApp)

## Recovery

The runtime survives restarts:
- Durable work items are claimed by the work runner on startup
- Orphaned executions are recovered (`recover_orphans()`)
- Pending deliveries are retried by the maintenance loop
- Memory is persisted in the database

## Retention

- Signal ledger: pruned by age + row bound (configurable)
- Audit ledger: opt-in retention (age + row bound)
- Conversations: active → idle → archived (lifecycle hygiene)
- Memory: explicit forget or TTL expiry
