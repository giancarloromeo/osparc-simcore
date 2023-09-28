"""add checksum to database

Revision ID: 5649397a81bf
Revises: e3334cced752
Create Date: 2023-09-20 08:39:58.776281+00:00

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "5649397a81bf"
down_revision = "e3334cced752"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column(
        "file_meta_data",
        sa.Column(
            "sha256_checksum",
            sa.String(),
            server_default=sa.text("NULL"),
            nullable=True,
        ),
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("file_meta_data", "sha256_checksum")
    # ### end Alembic commands ###