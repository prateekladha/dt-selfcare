"""db-init (Helm pre-install/upgrade hook): create the least-privilege application schema user.

Runs with the ADB ADMIN password (only this Job sees it); services connect as SELFCARE, which can
only manage objects in its own schema.
"""
from __future__ import annotations

import os
import re

import oracledb


def main() -> None:
    user = os.environ.get("APP_DB_USER", "SELFCARE")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,29}", user):
        raise SystemExit("invalid schema user name")
    app_pw = os.environ["APP_DB_PASSWORD"]
    with oracledb.connect(user="ADMIN", password=os.environ["DB_ADMIN_PASSWORD"], dsn=os.environ["APP_DB_DSN"]) as cx:
        cur = cx.cursor()
        cur.execute("SELECT COUNT(*) FROM dba_users WHERE username = :u", u=user)
        pw_sql = app_pw.replace('"', "")
        if cur.fetchone()[0] == 0:
            cur.execute(f'CREATE USER {user} IDENTIFIED BY "{pw_sql}" QUOTA 5G ON DATA')
            print(f"created user {user}")
        else:
            try:  # keep Vault the source of truth (rotation); same password again is a no-op
                cur.execute(f'ALTER USER {user} IDENTIFIED BY "{pw_sql}"')
            except oracledb.DatabaseError as e:
                if "ORA-28007" not in str(e):
                    raise
        cur.execute(f"GRANT CREATE SESSION, CREATE TABLE, CREATE SEQUENCE, CREATE VIEW TO {user}")
        print("db-init done")


if __name__ == "__main__":
    main()
