"""Apply ``litmus/db/schema.sql`` to the configured Postgres database.

Usage:
    python scripts/bootstrap_db.py
    python scripts/bootstrap_db.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from litmus.config import get_settings  # noqa: E402

SCHEMA_PATH = REPO_ROOT / "litmus" / "db" / "schema.sql"


async def apply_schema(dsn: str, dry_run: bool = False) -> int:
    """Run every statement in schema.sql against the database."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    if dry_run:
        print(sql)
        return 0

    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()
    print(f"schema applied from {SCHEMA_PATH}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply the Litmus schema to Postgres.")
    parser.add_argument("--dry-run", action="store_true", help="print the schema and exit")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.has_database:
        print("DATABASE_URL is not configured - copy .env.example to .env and fill it in.")
        return 2
    return asyncio.run(apply_schema(settings.postgres_dsn(), dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
