"""capability-invocation idempotency ledger (CV-19 fix)

Revision ID: a8c2e6f0b4d6
Revises: f2b4d6a8c0e2
Create Date: 2026-09-14

`CapabilityInvocationRequest.idempotency_key` was declared in the
contract ("the runtime validates the idempotency key has not been used")
while nothing read it — a false mechanism at the SOLE effect point.

This migration adds the ledger that makes the promise real: a claim row
per (principal_id, capability_name, idempotency_key), claimed BEFORE the
implementation runs. `succeeded` rows carry the recorded response so a
replay returns the FIRST outcome instead of re-executing the effect.
The unique index makes at-most-one-claim a database property.

Crash semantics (honest, at-least-once — same as the durable-work
queue): a claim abandoned mid-execution keeps status `executing` until
its claim lease expires; an identical request may then take the claim
over and re-execute. The ledger is append-only evidence.

downgrade drops exactly the table that was added.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8c2e6f0b4d6"
down_revision: Union[str, None] = "f2b4d6a8c0e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "capability_invocations",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("principal_id", sa.String(length=26), nullable=False),
        sa.Column("capability_name", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=512), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_capability_invocations_principal_capability_key",
        "capability_invocations",
        ["principal_id", "capability_name", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_capability_invocations_claim_expiry",
        "capability_invocations",
        ["status", "claim_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_capability_invocations_claim_expiry", table_name="capability_invocations"
    )
    op.drop_index(
        "uq_capability_invocations_principal_capability_key",
        table_name="capability_invocations",
    )
    op.drop_table("capability_invocations")
