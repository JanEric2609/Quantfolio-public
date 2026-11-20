"""Clear conviction_calibrated values that are the raw score relabelled.

Without enough resolved history the calibrator stored the raw conviction as
``conviction_calibrated``. On prod none of the 204 predictions had resolved,
so every stored value was such a copy: scoring would have labelled their
raw-score Brier values "calibrated", and the advisor UI showed "0.65 -> 0.65"
instead of "uncalibrated". The calibrator now leaves the column NULL; rows
whose value equals the raw conviction are cleared to match. A fitted Platt
value equal to the raw score to the last bit is not a realistic case.

The downgrade is a no-op: the cleared values carried no information.

Revision ID: 0117_uncalibrated_is_null
Revises: 0116_listing_currencies
"""
from alembic import op
import sqlalchemy as sa

revision = "0117_uncalibrated_is_null"
down_revision = "0116_listing_currencies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("discovery_prediction"):
        return
    bind.execute(
        sa.text(
            "UPDATE discovery_prediction SET conviction_calibrated = NULL "
            "WHERE conviction_calibrated IS NOT NULL AND conviction_calibrated = conviction"
        )
    )


def downgrade() -> None:
    pass
