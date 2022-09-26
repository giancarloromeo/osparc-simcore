"""add services specifications

Revision ID: 90ed05df20d7
Revises: 1c84432e5dbb
Create Date: 2022-05-12 16:07:33.288844+00:00

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "90ed05df20d7"
down_revision = "1c84432e5dbb"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "services_specifications",
        sa.Column("service_key", sa.String(), nullable=False),
        sa.Column("service_version", sa.String(), nullable=False),
        sa.Column("gid", sa.BigInteger(), nullable=False),
        sa.Column("sidecar", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["gid"],
            ["groups.gid"],
            name="fk_services_specifications_gid_groups",
            onupdate="CASCADE",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["service_key", "service_version"],
            ["services_meta_data.key", "services_meta_data.version"],
            onupdate="CASCADE",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "service_key", "service_version", "gid", name="services_specifications_pk"
        ),
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table("services_specifications")
    # ### end Alembic commands ###