"""content_assets: add wp_source_url (original WordPress URL provenance)

Adds ``wp_source_url`` to permanently record the original
collegemoneymethod.com URL an asset was migrated from. The ``link`` column
cannot serve this purpose: the WP migration scripts repoint it to S3
(migrate_wordpress_media.py, migrate_wp_content_files_to_s3.py) or null it
out (migrate_wp_assets_to_tiptap.py), and it also holds genuinely external
URLs. Only crawlable post/page HTML URLs qualify — wp-content file-download
URLs are excluded. Seeds rows whose ``link`` still points at an old-site page;
the rest are recovered by scripts/migrate_wordpress/backfill_wp_source_urls.py.

Revision ID: 0082
Revises: 0081
Create Date: 2026-07-19
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0082"
down_revision: Union[str, None] = "0081"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("content_assets", sa.Column("wp_source_url", sa.Text(), nullable=True))
    # Seed from rows whose link was never repointed away from the old site.
    op.execute(
        """
        UPDATE content_assets
        SET wp_source_url = link
        WHERE wp_source_url IS NULL
          AND link ~* '^https?://(www\\.)?collegemoneymethod\\.com/'
          AND link NOT ILIKE '%/wp-content/%'
        """
    )


def downgrade() -> None:
    op.drop_column("content_assets", "wp_source_url")
