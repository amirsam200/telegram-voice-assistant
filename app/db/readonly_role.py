"""Create or update the dashboard's read-only database role (run by the migrate service).

    python -m app.db.readonly_role

Does nothing when DASHBOARD_DB_PASSWORD is empty. The role may log in and SELECT from every table
(now and in future migrations) and nothing else.
"""

import re
import sys

from psycopg import sql
from sqlalchemy import Engine, text

from app.config import get_settings

ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def ensure_readonly_role(engine: Engine, role: str, password: str) -> None:
    if not ROLE_NAME.match(role):
        raise ValueError(f"invalid role name {role!r}")
    with engine.begin() as conn:
        raw = conn.connection.driver_connection  # psycopg connection, for safe identifier quoting
        database = conn.scalar(text("SELECT current_database()"))
        owner = conn.scalar(text("SELECT current_user"))
        exists = conn.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role})
        verb = "ALTER" if exists else "CREATE"
        statements = [
            sql.SQL(verb + " ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD {}")
            .format(sql.Identifier(role), sql.Literal(password)),
            sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(sql.Identifier(role)),
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), sql.Identifier(role)),
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)),
            sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(sql.Identifier(role)),
            sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT ON TABLES TO {}")
            .format(sql.Identifier(owner), sql.Identifier(role)),
        ]
        cursor = raw.cursor()
        for statement in statements:
            cursor.execute(statement)


def main() -> int:
    s = get_settings()
    if not s.dashboard_db_password:
        print("readonly role: DASHBOARD_DB_PASSWORD is empty; skipped")
        return 0
    from app.db.session import engine

    ensure_readonly_role(engine, s.dashboard_db_user, s.dashboard_db_password)
    print(f"readonly role: {s.dashboard_db_user} ready (SELECT only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
