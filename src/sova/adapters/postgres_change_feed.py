"""Change events and collector watermarks in PostgreSQL (INV-08, §23.3).

Collectors append change events and then advance their environment's
watermark. Events are append-only and idempotent by ID; replaying the same ID
with different content is a conflict. A watermark belongs to one collector and
never moves backwards or past the database clock. A late event (observed
before an existing watermark) is still recorded: it can only widen impact.
Same runtime-role rules as the lifecycle repository: non-owner, NOBYPASSRLS,
tenant set by trusted callers.
"""

from contextlib import contextmanager

from sova.domain.change_impact import ChangeEvent, ChangeKind, FeedWatermark
from sova.domain.contracts import ContractError, digest, identifier
from sova.ports.plans import RepositoryConflict

MAX_EVENTS = 4096

_COLUMNS = ("event_id,application_id,kind,environment_id,subject,artifact_digest,"
            "observed_at,cause_plan_digest,cause_step_id")


def _row(event):
    cause = event.cause or (None, None)
    return (event.event_id, event.application_id, str(event.kind), event.environment_id,
            event.subject, event.digest, event.observed_at, cause[0], cause[1])


class PostgresChangeFeed:
    def __init__(self, connect, *, tenant_id):
        self.connect = connect
        self.tenant_id = identifier(tenant_id, "tenant_id")

    @contextmanager
    def transaction(self):
        with self.connect() as conn:
            with conn.transaction():
                privileged = conn.execute("""SELECT rolsuper OR rolbypassrls OR EXISTS (
                    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='sova_control' AND c.relowner=pg_roles.oid)
                    FROM pg_roles WHERE rolname=current_user""").fetchone()
                if not privileged or privileged[0]:
                    raise ContractError(
                        "Change feed runtime role must not own or bypass row security"
                    )
                conn.execute(
                    "SELECT set_config('sova.tenant_id',%s,true)", (self.tenant_id,)
                )
                yield conn

    def record(self, event, *, collector_id):
        if not isinstance(event, ChangeEvent) or event.tenant_id != self.tenant_id:
            raise ContractError("Typed change event for this tenant required")
        identifier(collector_id, "collector_id")
        with self.transaction() as conn:
            inserted = conn.execute(
                f"""INSERT INTO sova_control.change_events
                (tenant_id,{_COLUMNS},collector_id)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id,event_id) DO NOTHING RETURNING event_id""",
                (self.tenant_id, *_row(event), collector_id),
            ).fetchone()
            if inserted:
                return True
            stored = conn.execute(
                f"""SELECT {_COLUMNS},collector_id FROM sova_control.change_events
                WHERE tenant_id=%s AND event_id=%s""",
                (self.tenant_id, event.event_id),
            ).fetchone()
            if stored is None or tuple(stored) != (*_row(event), collector_id):
                raise RepositoryConflict("Change event ID reused with different content")
            return False

    def advance(self, environment_id, *, collector_id, observed_through):
        """Claim that every change up to ``observed_through`` has been recorded."""
        mark = FeedWatermark(environment_id, observed_through)
        identifier(collector_id, "collector_id")
        with self.transaction() as conn:
            if conn.execute("SELECT %s > clock_timestamp()",
                            (mark.observed_through,)).fetchone()[0]:
                raise ContractError("Watermark cannot be ahead of the database clock")
            row = conn.execute(
                """INSERT INTO sova_control.change_feed_watermarks
                (tenant_id,environment_id,collector_id,observed_through)
                VALUES(%s,%s,%s,%s)
                ON CONFLICT (tenant_id,environment_id) DO UPDATE
                SET observed_through=EXCLUDED.observed_through, updated_at=clock_timestamp()
                WHERE change_feed_watermarks.collector_id=EXCLUDED.collector_id
                  AND change_feed_watermarks.observed_through<=EXCLUDED.observed_through
                RETURNING observed_through""",
                (self.tenant_id, mark.environment_id, collector_id, mark.observed_through),
            ).fetchone()
            if row is None:
                raise RepositoryConflict(
                    "Watermark belongs to another collector or would move backwards"
                )
            return FeedWatermark(mark.environment_id, row[0])

    def events(self, *, application_id, environments, since):
        identifier(application_id)
        if type(environments) is not tuple or not environments:
            raise ContractError("Environments to read changes for required")
        for environment in environments:
            identifier(environment, "environment_id")
        with self.transaction() as conn:
            rows = conn.execute(
                f"""SELECT {_COLUMNS} FROM sova_control.change_events
                WHERE tenant_id=%s AND observed_at>=%s
                  AND (application_id=%s OR environment_id IS NULL
                       OR environment_id=ANY(%s)
                       OR kind IN ('manifest','qualification','environment_profile',
                                   'verifier','adapter'))
                ORDER BY observed_at,event_id LIMIT %s""",
                (self.tenant_id, since, application_id, list(environments),
                 MAX_EVENTS + 1),
            ).fetchall()
        if len(rows) > MAX_EVENTS:
            raise ContractError("Change history exceeds the assessment bound")
        return tuple(
            ChangeEvent(event_id, self.tenant_id, app, ChangeKind(kind), env, subject,
                        artifact, observed, None if plan is None else (plan, step))
            for event_id, app, kind, env, subject, artifact, observed, plan, step in rows
        )

    def record_reaffirmation(self, *, application_id, plan_digest, compiled_digest,
                             snapshot_digest, observed_at, actor_id):
        identifier(application_id)
        identifier(actor_id, "actor_id")
        for value in (plan_digest, compiled_digest, snapshot_digest):
            digest(value)
        with self.transaction() as conn:
            if conn.execute("SELECT %s > clock_timestamp()", (observed_at,)).fetchone()[0]:
                raise ContractError("Reaffirming snapshot cannot be ahead of the database clock")
            conn.execute(
                """INSERT INTO sova_control.plan_reaffirmations
                (tenant_id,application_id,plan_digest,compiled_digest,snapshot_digest,
                 observed_at,actor_id) VALUES(%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id,plan_digest,snapshot_digest) DO NOTHING""",
                (self.tenant_id, application_id, plan_digest, compiled_digest,
                 snapshot_digest, observed_at, actor_id),
            )

    def reaffirmed_at(self, plan_digest):
        digest(plan_digest)
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT max(observed_at) FROM sova_control.plan_reaffirmations
                WHERE tenant_id=%s AND plan_digest=%s""",
                (self.tenant_id, plan_digest),
            ).fetchone()
        return row[0]

    def watermarks(self, environments):
        if type(environments) is not tuple:
            raise ContractError("Environment tuple required")
        with self.transaction() as conn:
            rows = conn.execute(
                """SELECT environment_id,observed_through
                FROM sova_control.change_feed_watermarks
                WHERE tenant_id=%s AND environment_id=ANY(%s)""",
                (self.tenant_id, list(environments)),
            ).fetchall()
        return tuple(FeedWatermark(env, through) for env, through in rows)
