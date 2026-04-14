"""merge heads

Revision ID: eadb1eb64495
Revises: 20260413_0006, 20260414_0006
Create Date: 2026-04-14 15:17:33.867574

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'eadb1eb64495'
down_revision: Union[str, None] = ('20260413_0006', '20260414_0006')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
