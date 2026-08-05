import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from companion_gateway.domain.models import TaskCreate, TaskEvent, TaskRecord
from companion_gateway.domain.tasks import TaskEventType, TaskStatus, transition


class SQLiteTaskRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    target_device_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    confirmation_policy TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    trace_id TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    reason TEXT,
                    occurred_at TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                );

                CREATE INDEX IF NOT EXISTS idx_task_events_task_time
                ON task_events(task_id, occurred_at);
                """
            )

    def check(self) -> bool:
        try:
            with self._connect() as connection:
                return connection.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error:
            return False

    def create_task(
        self,
        command: TaskCreate,
        *,
        task_id: str,
        event_id: str,
        trace_id: str,
        occurred_at: datetime,
    ) -> tuple[TaskRecord, bool]:
        created_event = TaskEvent(
            event_id=event_id,
            task_id=task_id,
            type=TaskEventType.CREATED,
            reason="task_created",
            occurred_at=occurred_at,
            trace_id=trace_id,
        )
        created_at = created_event.occurred_at.astimezone(UTC)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM tasks WHERE idempotency_key = ?",
                    (command.idempotency_key,),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    return self._task_from_row(existing), False

                connection.execute(
                    """
                    INSERT INTO tasks (
                        task_id,
                        actor_id,
                        target_device_id,
                        kind,
                        scheduled_at,
                        timezone,
                        payload_json,
                        confirmation_policy,
                        idempotency_key,
                        status,
                        created_at,
                        trace_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        command.actor_id,
                        command.target_device_id,
                        command.kind.value,
                        command.schedule.at.isoformat(),
                        command.schedule.timezone,
                        json.dumps(
                            command.payload.model_dump(mode="json"),
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        command.confirmation_policy.value,
                        command.idempotency_key,
                        TaskStatus.CREATED.value,
                        created_at.isoformat(),
                        trace_id,
                    ),
                )
                self._insert_event(connection, created_event)
                row = connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return self._task_from_row(row), True

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._task_from_row(row) if row is not None else None

    def list_events(self, task_id: str) -> list[TaskEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM task_events
                WHERE task_id = ?
                ORDER BY occurred_at, rowid
                """,
                (task_id,),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def append_event(
        self,
        task_id: str,
        event_type: TaskEventType,
        *,
        event_id: str,
        trace_id: str,
        reason: str | None,
        occurred_at: datetime,
    ) -> TaskEvent:
        event = TaskEvent(
            event_id=event_id,
            task_id=task_id,
            type=event_type,
            reason=reason,
            occurred_at=occurred_at,
            trace_id=trace_id,
        )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT status FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(task_id)

                next_status = transition(TaskStatus(row["status"]), event.type)
                self._insert_event(connection, event)
                connection.execute(
                    "UPDATE tasks SET status = ? WHERE task_id = ?",
                    (next_status.value, task_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return event

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        event: TaskEvent,
    ) -> None:
        connection.execute(
            """
            INSERT INTO task_events (
                event_id,
                task_id,
                type,
                reason,
                occurred_at,
                trace_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.task_id,
                event.type.value,
                event.reason,
                event.occurred_at.astimezone(UTC).isoformat(),
                event.trace_id,
            ),
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord.model_validate(
            {
                "task_id": row["task_id"],
                "actor_id": row["actor_id"],
                "target_device_id": row["target_device_id"],
                "kind": row["kind"],
                "schedule": {
                    "at": row["scheduled_at"],
                    "timezone": row["timezone"],
                },
                "payload": json.loads(row["payload_json"]),
                "confirmation_policy": row["confirmation_policy"],
                "idempotency_key": row["idempotency_key"],
                "status": row["status"],
                "created_at": row["created_at"],
                "trace_id": row["trace_id"],
            }
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> TaskEvent:
        return TaskEvent.model_validate(
            {
                "event_id": row["event_id"],
                "task_id": row["task_id"],
                "type": row["type"],
                "reason": row["reason"],
                "occurred_at": row["occurred_at"],
                "trace_id": row["trace_id"],
            }
        )
