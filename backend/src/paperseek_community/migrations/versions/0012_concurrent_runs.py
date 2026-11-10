"""Let one workspace hold several queued or running tasks.

The worker's concurrency limit decides how many run at once; the rest wait in the queue.
"""

from alembic import op
import sqlalchemy as sa

revision = "0012_concurrent_runs"
down_revision = "0011_workspace_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_runs_active_subject", table_name="runs")


def downgrade() -> None:
    op.create_index(
        "uq_runs_active_subject",
        "runs",
        ["owner_subject"],
        unique=True,
        sqlite_where=sa.text(
            "owner_subject IS NOT NULL AND deleted_at IS NULL "
            "AND status IN ('queued', 'running', 'cancelling')"
        ),
    )
