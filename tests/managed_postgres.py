"""A managed PostgreSQL in a cloud (RDS, Cloud SQL) as the source of the
business world; the target, a restore scratch and another tenant stay on the
local server (on premises).

The managed server is administered by its master user, which is not a
superuser there; the writer roles it creates log in with a password.
"""

from uuid import uuid4

from postgres_business import prepare
import psycopg
from psycopg import sql


def managed_business(local, remote, password):
    suffix = uuid4().hex[:10]
    names = {k: f"{k}_{suffix}" for k in ("src", "tgt", "scratch", "other")}
    app, batch = "orders_app_" + suffix, "orders_batch_" + suffix
    servers = {names["src"]: remote, **{names[k]: local for k in ("tgt", "scratch", "other")}}
    for dsn, databases, secret in ((remote, ("src",), password),
                                   (local, ("tgt", "scratch", "other"), None)):
        with psycopg.connect(dsn, autocommit=True) as admin:
            for role in (app, batch):
                admin.execute(sql.SQL("CREATE ROLE {} LOGIN" + (" PASSWORD {}" if secret else ""))
                              .format(sql.Identifier(role),
                                      *([sql.Literal(secret)] if secret else [])))
            for key in databases:
                admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(names[key])))
                if key in ("src", "tgt"):
                    admin.execute(sql.SQL("COMMENT ON DATABASE {} IS 'approved-zone-a'").format(
                        sql.Identifier(names[key])))
            if "other" in databases:
                admin.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(names["other"])))

    def connect(database):
        return psycopg.connect(servers[database], dbname=database)

    def connect_as(database, role):
        extra = {"password": password} if servers[database] is remote else {}
        return psycopg.connect(servers[database], dbname=database, user=role, **extra)

    prepare(names, app, batch, connect, connect_as,
            schema_on=(names["src"], names["tgt"], names["scratch"]))
    return dict(names=names, app=app, batch=batch, connect=connect, connect_as=connect_as,
                servers=servers, password=password)
