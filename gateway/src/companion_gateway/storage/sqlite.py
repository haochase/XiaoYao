import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from companion_gateway.domain.models import (
    TaskCreate,
    TaskEvent,
    TaskKind,
    TaskRecord,
)
from companion_gateway.domain.agents import AgentDraft, AgentExecution, AgentSpec
from companion_gateway.domain.memory import (
    Memory,
    MemoryCategory,
    PendingMemoryProposal,
)
from companion_gateway.domain.medication import (
    FeishuFallbackStatus,
    MedicationOccurrence,
    MedicationOccurrenceStatus,
    MedicationPlan,
    MedicationPlanCreate,
)
from companion_gateway.domain.vision import VisionObservation
from companion_gateway.domain.tasks import TaskEventType, TaskStatus, transition


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


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

                CREATE TABLE IF NOT EXISTS medication_plans (
                    plan_id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    target_device_id TEXT NOT NULL,
                    reminder_times_json TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    message TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_medication_plans_enabled
                ON medication_plans(enabled, updated_at);

                CREATE TABLE IF NOT EXISTS medication_occurrences (
                    occurrence_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    target_device_id TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    local_time TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    ack_deadline_at TEXT NOT NULL,
                    task_id TEXT,
                    status TEXT NOT NULL,
                    acknowledged_at TEXT,
                    feishu_status TEXT NOT NULL,
                    feishu_message_id TEXT,
                    feishu_error TEXT,
                    created_at TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    UNIQUE(plan_id, local_date, local_time),
                    FOREIGN KEY (plan_id) REFERENCES medication_plans(plan_id)
                );

                CREATE INDEX IF NOT EXISTS idx_medication_occurrences_status_deadline
                ON medication_occurrences(status, ack_deadline_at);

                CREATE TABLE IF NOT EXISTS memories (
                    memory_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    value TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consent_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memories_subject_expiry
                ON memories(subject_id, expires_at);

                CREATE INDEX IF NOT EXISTS idx_memories_subject_category
                ON memories(subject_id, category);

                CREATE TABLE IF NOT EXISTS memory_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    value TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memory_proposals_subject_expiry
                ON memory_proposals(subject_id, expires_at);

                CREATE TABLE IF NOT EXISTS vision_observations (
                    observation_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    storage_key TEXT NOT NULL,
                    UNIQUE(subject_id, turn_id)
                );

                CREATE INDEX IF NOT EXISTS idx_vision_observations_subject_expiry
                ON vision_observations(subject_id, expires_at);

                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    spec_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_agents_owner
                ON agents(owner_id, agent_id);

                CREATE TABLE IF NOT EXISTS agent_drafts (
                    draft_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    source_message_id TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    confirmed_agent_id TEXT,
                    UNIQUE(owner_id, source_message_id),
                    FOREIGN KEY (confirmed_agent_id) REFERENCES agents(agent_id)
                    ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_agent_drafts_owner
                ON agent_drafts(owner_id, draft_id);

                CREATE TABLE IF NOT EXISTS agent_executions (
                    execution_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    trigger_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    output_text TEXT,
                    error TEXT,
                    FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
                    ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_agent_executions_agent_started
                ON agent_executions(agent_id, started_at, execution_id);
                """
            )

    def check(self) -> bool:
        try:
            with self._connect() as connection:
                return connection.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error:
            return False

    def create_draft(self, draft: AgentDraft) -> AgentDraft:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT * FROM agent_drafts
                    WHERE owner_id = ? AND source_message_id = ?
                    """,
                    (draft.owner_id, draft.source_message_id),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    return self._agent_draft_from_row(existing)
                connection.execute(
                    """
                    INSERT INTO agent_drafts (
                        draft_id, owner_id, source_message_id, spec_json, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        draft.draft_id,
                        draft.owner_id,
                        draft.source_message_id,
                        self._agent_spec_to_json(draft.spec),
                        _require_aware(draft.created_at).isoformat(),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return draft

    def get_draft(self, draft_id: str, *, owner_id: str) -> AgentDraft | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM agent_drafts
                WHERE draft_id = ? AND owner_id = ?
                """,
                (draft_id, owner_id),
            ).fetchone()
        return self._agent_draft_from_row(row) if row is not None else None

    def confirm_draft(
        self,
        draft_id: str,
        *,
        owner_id: str,
    ) -> tuple[AgentSpec, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                draft_row = connection.execute(
                    """
                    SELECT * FROM agent_drafts
                    WHERE draft_id = ? AND owner_id = ?
                    """,
                    (draft_id, owner_id),
                ).fetchone()
                if draft_row is None:
                    raise KeyError(draft_id)
                confirmed_agent_id = draft_row["confirmed_agent_id"]
                if confirmed_agent_id is not None:
                    existing = connection.execute(
                        "SELECT * FROM agents WHERE agent_id = ?",
                        (confirmed_agent_id,),
                    ).fetchone()
                    if existing is not None:
                        connection.commit()
                        return self._agent_spec_from_row(existing), False
                    connection.execute(
                        """
                        UPDATE agent_drafts
                        SET confirmed_agent_id = NULL
                        WHERE draft_id = ?
                        """,
                        (draft_id,),
                    )
                draft = self._agent_draft_from_row(draft_row)
                existing = connection.execute(
                    "SELECT * FROM agents WHERE agent_id = ?",
                    (draft.spec.agent_id,),
                ).fetchone()
                if existing is not None:
                    existing_spec = self._agent_spec_from_row(existing)
                    if existing_spec != draft.spec:
                        raise ValueError(
                            "agent_id is already registered with a different specification"
                        )
                    created = False
                else:
                    connection.execute(
                        """
                        INSERT INTO agents (agent_id, owner_id, spec_json)
                        VALUES (?, ?, ?)
                        """,
                        (
                            draft.spec.agent_id,
                            draft.spec.owner_id,
                            self._agent_spec_to_json(draft.spec),
                        ),
                    )
                    created = True
                connection.execute(
                    """
                    UPDATE agent_drafts
                    SET confirmed_agent_id = ?
                    WHERE draft_id = ? AND owner_id = ?
                    """,
                    (draft.spec.agent_id, draft_id, owner_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return draft.spec, created

    def list_agents(self, *, owner_id: str) -> list[AgentSpec]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agents
                WHERE owner_id = ?
                ORDER BY agent_id
                """,
                (owner_id,),
            ).fetchall()
        return [self._agent_spec_from_row(row) for row in rows]

    def get_agent(self, agent_id: str, *, owner_id: str) -> AgentSpec | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM agents
                WHERE agent_id = ? AND owner_id = ?
                """,
                (agent_id, owner_id),
            ).fetchone()
        return self._agent_spec_from_row(row) if row is not None else None

    def update_agent(self, agent: AgentSpec, *, owner_id: str) -> AgentSpec:
        if agent.owner_id != owner_id:
            raise ValueError("agent owner_id must match owner_id")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agents
                SET spec_json = ?
                WHERE agent_id = ? AND owner_id = ?
                """,
                (self._agent_spec_to_json(agent), agent.agent_id, owner_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(agent.agent_id)
        return agent

    def delete_agent(self, agent_id: str, *, owner_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM agents WHERE agent_id = ? AND owner_id = ?",
                (agent_id, owner_id),
            )
        return cursor.rowcount == 1

    def record_execution(self, execution: AgentExecution) -> AgentExecution:
        with self._connect() as connection:
            agent = connection.execute(
                "SELECT agent_id FROM agents WHERE agent_id = ?",
                (execution.agent_id,),
            ).fetchone()
            if agent is None:
                raise KeyError(execution.agent_id)
            connection.execute(
                """
                INSERT INTO agent_executions (
                    execution_id, agent_id, trigger_id, status, started_at,
                    completed_at, output_text, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(execution_id) DO UPDATE SET
                    agent_id = excluded.agent_id,
                    trigger_id = excluded.trigger_id,
                    status = excluded.status,
                    started_at = excluded.started_at,
                    completed_at = excluded.completed_at,
                    output_text = excluded.output_text,
                    error = excluded.error
                """,
                (
                    execution.execution_id,
                    execution.agent_id,
                    execution.trigger_id,
                    execution.status.value,
                    _require_aware(execution.started_at).isoformat(),
                    (
                        _require_aware(execution.completed_at).isoformat()
                        if execution.completed_at is not None
                        else None
                    ),
                    execution.output_text,
                    execution.error,
                ),
            )
        return execution

    def list_executions(
        self,
        agent_id: str,
        *,
        owner_id: str,
    ) -> list[AgentExecution]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT executions.*
                FROM agent_executions AS executions
                INNER JOIN agents AS agents ON agents.agent_id = executions.agent_id
                WHERE executions.agent_id = ? AND agents.owner_id = ?
                ORDER BY executions.started_at, executions.execution_id
                """,
                (agent_id, owner_id),
            ).fetchall()
        return [self._agent_execution_from_row(row) for row in rows]

    @staticmethod
    def _agent_spec_to_json(agent: AgentSpec) -> str:
        return json.dumps(
            agent.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _agent_spec_from_row(row: sqlite3.Row) -> AgentSpec:
        return AgentSpec.model_validate(json.loads(row["spec_json"]))

    @staticmethod
    def _agent_draft_from_row(row: sqlite3.Row) -> AgentDraft:
        return AgentDraft.model_validate(
            {
                "draft_id": row["draft_id"],
                "owner_id": row["owner_id"],
                "source_message_id": row["source_message_id"],
                "spec": json.loads(row["spec_json"]),
                "created_at": row["created_at"],
            }
        )

    @staticmethod
    def _agent_execution_from_row(row: sqlite3.Row) -> AgentExecution:
        return AgentExecution.model_validate(
            {
                "execution_id": row["execution_id"],
                "agent_id": row["agent_id"],
                "trigger_id": row["trigger_id"],
                "status": row["status"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "output_text": row["output_text"],
                "error": row["error"],
            }
        )

    def upsert_memory(self, memory: Memory) -> Memory:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memories (
                    memory_id, subject_id, category, value, source,
                    created_at, expires_at, consent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    subject_id = excluded.subject_id,
                    category = excluded.category,
                    value = excluded.value,
                    source = excluded.source,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at,
                    consent_at = excluded.consent_at
                """,
                (
                    memory.memory_id,
                    memory.subject_id,
                    memory.category.value,
                    memory.value,
                    memory.source,
                    _require_aware(memory.created_at).isoformat(),
                    _require_aware(memory.expires_at).isoformat(),
                    _require_aware(memory.consent_at).isoformat(),
                ),
            )
        return memory

    def get_memory(self, memory_id: str) -> Memory | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
        return self._memory_from_row(row) if row is not None else None

    def list_memories(
        self,
        *,
        subject_id: str,
        query: str | None = None,
        limit: int | None = None,
        now: datetime,
    ) -> list[Memory]:
        current = _require_aware(now).isoformat()
        sql = (
            "SELECT * FROM memories "
            "WHERE subject_id = ? AND expires_at > ?"
        )
        parameters: list[object] = [subject_id, current]
        if query:
            escaped = (
                query.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            sql += " AND value LIKE ? ESCAPE '\\'"
            parameters.append(f"%{escaped}%")
        sql += " ORDER BY created_at, memory_id"
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, tuple(parameters)).fetchall()
        return [self._memory_from_row(row) for row in rows]

    def delete_memory(self, *, subject_id: str, memory_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM memories WHERE memory_id = ? AND subject_id = ?",
                (memory_id, subject_id),
            )
        return cursor.rowcount == 1

    def export_memories(
        self,
        *,
        subject_id: str,
        now: datetime,
    ) -> list[Memory]:
        return self.list_memories(subject_id=subject_id, now=now)

    def purge_expired(self, *, now: datetime) -> int:
        current = _require_aware(now).isoformat()
        with self._connect() as connection:
            memory_cursor = connection.execute(
                "DELETE FROM memories WHERE expires_at <= ?",
                (current,),
            )
            proposal_cursor = connection.execute(
                "DELETE FROM memory_proposals WHERE expires_at <= ?",
                (current,),
            )
        return memory_cursor.rowcount + proposal_cursor.rowcount

    def memory_usage_bytes(self, *, subject_id: str, now: datetime) -> int:
        current = _require_aware(now).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT value FROM memories WHERE subject_id = ? AND expires_at > ?",
                (subject_id, current),
            ).fetchall()
        return sum(len(row["value"].encode("utf-8")) for row in rows)

    def create_vision_observation(
        self,
        observation: VisionObservation,
    ) -> tuple[VisionObservation, bool]:
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM vision_observations WHERE subject_id = ? AND turn_id = ?",
                (observation.subject_id, observation.turn_id),
            ).fetchone()
            if existing is not None:
                return self._vision_observation_from_row(existing), False
            connection.execute(
                """
                INSERT INTO vision_observations (
                    observation_id, subject_id, turn_id, captured_at, expires_at,
                    content_type, byte_size, sha256, storage_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.subject_id,
                    observation.turn_id,
                    _require_aware(observation.captured_at).isoformat(),
                    _require_aware(observation.expires_at).isoformat(),
                    observation.content_type,
                    observation.byte_size,
                    observation.sha256,
                    observation.storage_key,
                ),
            )
        return observation, True

    def get_vision_observation(self, observation_id: str) -> VisionObservation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM vision_observations WHERE observation_id = ?",
                (observation_id,),
            ).fetchone()
        return self._vision_observation_from_row(row) if row is not None else None

    def get_vision_observation_for_turn(
        self,
        *,
        subject_id: str,
        turn_id: str,
        now: datetime,
    ) -> VisionObservation | None:
        current = _require_aware(now).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM vision_observations
                WHERE subject_id = ? AND turn_id = ? AND expires_at > ?
                """,
                (subject_id, turn_id, current),
            ).fetchone()
        return self._vision_observation_from_row(row) if row is not None else None

    def list_vision_observations(
        self,
        *,
        subject_id: str,
        now: datetime,
    ) -> list[VisionObservation]:
        current = _require_aware(now).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM vision_observations
                WHERE subject_id = ? AND expires_at > ?
                ORDER BY captured_at, observation_id
                """,
                (subject_id, current),
            ).fetchall()
        return [self._vision_observation_from_row(row) for row in rows]

    def delete_vision_observation(
        self,
        *,
        subject_id: str,
        observation_id: str,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM vision_observations WHERE observation_id = ? AND subject_id = ?",
                (observation_id, subject_id),
            )
        return cursor.rowcount == 1

    def purge_expired_vision_observations(self, *, now: datetime) -> list[str]:
        current = _require_aware(now).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT storage_key FROM vision_observations WHERE expires_at <= ?",
                (current,),
            ).fetchall()
            connection.execute(
                "DELETE FROM vision_observations WHERE expires_at <= ?",
                (current,),
            )
        return [row["storage_key"] for row in rows]

    def vision_usage_bytes(self, *, subject_id: str, now: datetime) -> int:
        current = _require_aware(now).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(byte_size), 0) AS total
                FROM vision_observations
                WHERE subject_id = ? AND expires_at > ?
                """,
                (subject_id, current),
            ).fetchone()
        return int(row["total"])

    def create_memory_proposal(
        self,
        proposal: PendingMemoryProposal,
    ) -> PendingMemoryProposal:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memory_proposals (
                    proposal_id, subject_id, category, value, source,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    subject_id = excluded.subject_id,
                    category = excluded.category,
                    value = excluded.value,
                    source = excluded.source,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (
                    proposal.proposal_id,
                    proposal.subject_id,
                    proposal.category.value,
                    proposal.value,
                    proposal.source,
                    _require_aware(proposal.created_at).isoformat(),
                    _require_aware(proposal.expires_at).isoformat(),
                ),
            )
        return proposal

    def get_memory_proposal(self, proposal_id: str) -> PendingMemoryProposal | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM memory_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        return (
            self._pending_memory_proposal_from_row(row)
            if row is not None
            else None
        )

    def list_memory_proposals(
        self,
        *,
        subject_id: str,
        now: datetime,
    ) -> list[PendingMemoryProposal]:
        current = _require_aware(now).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM memory_proposals
                WHERE subject_id = ? AND expires_at > ?
                ORDER BY created_at, proposal_id
                """,
                (subject_id, current),
            ).fetchall()
        return [self._pending_memory_proposal_from_row(row) for row in rows]

    def delete_memory_proposal(self, *, subject_id: str, proposal_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM memory_proposals
                WHERE proposal_id = ? AND subject_id = ?
                """,
                (proposal_id, subject_id),
            )
        return cursor.rowcount == 1

    def consume_memory_proposal(
        self,
        *,
        subject_id: str,
        proposal_id: str,
        memory: Memory,
        now: datetime,
    ) -> Memory | None:
        if memory.subject_id != subject_id:
            return None
        current = _require_aware(now).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                proposal = connection.execute(
                    """
                    SELECT proposal_id FROM memory_proposals
                    WHERE proposal_id = ? AND subject_id = ? AND expires_at > ?
                    """,
                    (proposal_id, subject_id, current),
                ).fetchone()
                if proposal is None:
                    connection.rollback()
                    return None
                existing = connection.execute(
                    "SELECT subject_id FROM memories WHERE memory_id = ?",
                    (memory.memory_id,),
                ).fetchone()
                if existing is not None and existing["subject_id"] != subject_id:
                    connection.rollback()
                    return None
                connection.execute(
                    """
                    INSERT INTO memories (
                        memory_id, subject_id, category, value, source,
                        created_at, expires_at, consent_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(memory_id) DO UPDATE SET
                        subject_id = excluded.subject_id,
                        category = excluded.category,
                        value = excluded.value,
                        source = excluded.source,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at,
                        consent_at = excluded.consent_at
                    """,
                    (
                        memory.memory_id,
                        memory.subject_id,
                        memory.category.value,
                        memory.value,
                        memory.source,
                        _require_aware(memory.created_at).isoformat(),
                        _require_aware(memory.expires_at).isoformat(),
                        _require_aware(memory.consent_at).isoformat(),
                    ),
                )
                connection.execute(
                    "DELETE FROM memory_proposals WHERE proposal_id = ?",
                    (proposal_id,),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return memory

    def create_medication_plan(
        self,
        plan: MedicationPlanCreate,
        *,
        plan_id: str,
        occurred_at: datetime,
    ) -> tuple[MedicationPlan, bool]:
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        created_at = occurred_at.astimezone(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM medication_plans WHERE plan_id = ?",
                    (plan_id,),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    return self._medication_plan_from_row(existing), False
                connection.execute(
                    """
                    INSERT INTO medication_plans (
                        plan_id, actor_id, target_device_id, reminder_times_json,
                        timezone, message, enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        plan_id,
                        plan.actor_id,
                        plan.target_device_id,
                        json.dumps(
                            [item.isoformat(timespec="minutes") for item in plan.reminder_times],
                            separators=(",", ":"),
                        ),
                        plan.timezone,
                        plan.message,
                        int(plan.enabled),
                        created_at.isoformat(),
                        created_at.isoformat(),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM medication_plans WHERE plan_id = ?",
                    (plan_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._medication_plan_from_row(row), True

    def get_medication_plan(self, plan_id: str) -> MedicationPlan | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM medication_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        return self._medication_plan_from_row(row) if row is not None else None

    def list_medication_plans(
        self, *, enabled: bool | None = None
    ) -> list[MedicationPlan]:
        query = "SELECT * FROM medication_plans"
        parameters: tuple[object, ...] = ()
        if enabled is not None:
            query += " WHERE enabled = ?"
            parameters = (int(enabled),)
        query += " ORDER BY created_at, plan_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._medication_plan_from_row(row) for row in rows]

    def disable_medication_plan(
        self, plan_id: str, *, occurred_at: datetime
    ) -> MedicationPlan:
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        with self._connect() as connection:
            connection.execute(
                "UPDATE medication_plans SET enabled = 0, updated_at = ? WHERE plan_id = ?",
                (occurred_at.astimezone(UTC).isoformat(), plan_id),
            )
            row = connection.execute(
                "SELECT * FROM medication_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        if row is None:
            raise KeyError(plan_id)
        return self._medication_plan_from_row(row)

    def create_occurrence_if_absent(
        self, occurrence: MedicationOccurrence
    ) -> tuple[MedicationOccurrence, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT * FROM medication_occurrences
                    WHERE plan_id = ? AND local_date = ? AND local_time = ?
                    """,
                    (
                        occurrence.plan_id,
                        occurrence.local_date.isoformat(),
                        occurrence.local_time.isoformat(timespec="minutes"),
                    ),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    return self._medication_occurrence_from_row(existing), False
                connection.execute(
                    """
                    INSERT INTO medication_occurrences (
                        occurrence_id, plan_id, actor_id, target_device_id,
                        local_date, local_time, scheduled_at, ack_deadline_at,
                        task_id, status, acknowledged_at, feishu_status,
                        feishu_message_id, feishu_error, created_at, trace_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        occurrence.occurrence_id,
                        occurrence.plan_id,
                        occurrence.actor_id,
                        occurrence.target_device_id,
                        occurrence.local_date.isoformat(),
                        occurrence.local_time.isoformat(timespec="minutes"),
                        occurrence.scheduled_at.astimezone(UTC).isoformat(),
                        occurrence.ack_deadline_at.astimezone(UTC).isoformat(),
                        occurrence.task_id,
                        occurrence.status.value,
                        (
                            occurrence.acknowledged_at.astimezone(UTC).isoformat()
                            if occurrence.acknowledged_at is not None
                            else None
                        ),
                        occurrence.feishu_status.value,
                        occurrence.feishu_message_id,
                        occurrence.feishu_error,
                        occurrence.created_at.astimezone(UTC).isoformat(),
                        occurrence.trace_id,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return occurrence, True

    def get_medication_occurrence(
        self, occurrence_id: str
    ) -> MedicationOccurrence | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM medication_occurrences WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
        return self._medication_occurrence_from_row(row) if row is not None else None

    def get_medication_occurrence_by_task_id(
        self,
        task_id: str,
    ) -> MedicationOccurrence | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM medication_occurrences WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._medication_occurrence_from_row(row) if row is not None else None

    def list_medication_occurrences(
        self, *, statuses: tuple[MedicationOccurrenceStatus, ...] | None = None
    ) -> list[MedicationOccurrence]:
        query = "SELECT * FROM medication_occurrences"
        parameters: tuple[object, ...] = ()
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            parameters = tuple(item.value for item in statuses)
        query += " ORDER BY scheduled_at, occurrence_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._medication_occurrence_from_row(row) for row in rows]

    def bind_occurrence_task(
        self, occurrence_id: str, *, task_id: str
    ) -> MedicationOccurrence:
        with self._connect() as connection:
            connection.execute(
                "UPDATE medication_occurrences SET task_id = ? WHERE occurrence_id = ?",
                (task_id, occurrence_id),
            )
            row = connection.execute(
                "SELECT * FROM medication_occurrences WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
        if row is None:
            raise KeyError(occurrence_id)
        return self._medication_occurrence_from_row(row)

    def mark_occurrence_delivered(self, occurrence_id: str) -> MedicationOccurrence:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE medication_occurrences
                SET status = CASE WHEN status = ? THEN ? ELSE status END
                WHERE occurrence_id = ?
                """,
                (
                    MedicationOccurrenceStatus.SCHEDULED.value,
                    MedicationOccurrenceStatus.DELIVERED.value,
                    occurrence_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM medication_occurrences WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
        if row is None:
            raise KeyError(occurrence_id)
        return self._medication_occurrence_from_row(row)

    def mark_occurrence_acknowledged(
        self, occurrence_id: str, *, occurred_at: datetime
    ) -> MedicationOccurrence:
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE medication_occurrences
                SET status = ?, acknowledged_at = ?
                WHERE occurrence_id = ?
                """,
                (
                    MedicationOccurrenceStatus.ACKNOWLEDGED.value,
                    occurred_at.astimezone(UTC).isoformat(),
                    occurrence_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM medication_occurrences WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
        if row is None:
            raise KeyError(occurrence_id)
        return self._medication_occurrence_from_row(row)

    def claim_feishu_fallback(self, occurrence_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE medication_occurrences
                SET feishu_status = ?
                WHERE occurrence_id = ? AND feishu_status = ?
                """,
                (
                    FeishuFallbackStatus.SENDING.value,
                    occurrence_id,
                    FeishuFallbackStatus.PENDING.value,
                ),
            )
        return cursor.rowcount == 1

    def complete_feishu_fallback(
        self,
        occurrence_id: str,
        *,
        status: FeishuFallbackStatus,
        message_id: str | None,
        error: str | None,
    ) -> MedicationOccurrence:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM medication_occurrences WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
            if row is None:
                raise KeyError(occurrence_id)
            occurrence_status = row["status"]
            if status is FeishuFallbackStatus.SENT and occurrence_status != (
                MedicationOccurrenceStatus.ACKNOWLEDGED.value
            ):
                occurrence_status = MedicationOccurrenceStatus.ESCALATED.value
            connection.execute(
                """
                UPDATE medication_occurrences
                SET feishu_status = ?, feishu_message_id = ?, feishu_error = ?, status = ?
                WHERE occurrence_id = ?
                """,
                (
                    status.value,
                    message_id,
                    error,
                    occurrence_status,
                    occurrence_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM medication_occurrences WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
        return self._medication_occurrence_from_row(updated)

    @staticmethod
    def _medication_plan_from_row(row: sqlite3.Row) -> MedicationPlan:
        return MedicationPlan.model_validate(
            {
                "plan_id": row["plan_id"],
                "actor_id": row["actor_id"],
                "target_device_id": row["target_device_id"],
                "reminder_times": json.loads(row["reminder_times_json"]),
                "timezone": row["timezone"],
                "message": row["message"],
                "enabled": bool(row["enabled"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    @staticmethod
    def _medication_occurrence_from_row(
        row: sqlite3.Row,
    ) -> MedicationOccurrence:
        return MedicationOccurrence.model_validate(
            {
                "occurrence_id": row["occurrence_id"],
                "plan_id": row["plan_id"],
                "actor_id": row["actor_id"],
                "target_device_id": row["target_device_id"],
                "local_date": row["local_date"],
                "local_time": row["local_time"],
                "scheduled_at": row["scheduled_at"],
                "ack_deadline_at": row["ack_deadline_at"],
                "task_id": row["task_id"],
                "status": row["status"],
                "acknowledged_at": row["acknowledged_at"],
                "feishu_status": row["feishu_status"],
                "feishu_message_id": row["feishu_message_id"],
                "feishu_error": row["feishu_error"],
                "created_at": row["created_at"],
                "trace_id": row["trace_id"],
            }
        )

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

    def get_latest_task(
        self,
        *,
        actor_id: str,
        target_device_id: str,
        kind: TaskKind,
    ) -> TaskRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM tasks
                WHERE actor_id = ? AND target_device_id = ? AND kind = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (actor_id, target_device_id, kind.value),
            ).fetchone()
        return self._task_from_row(row) if row is not None else None

    def list_due_tasks(self, *, now: datetime) -> list[TaskRecord]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks
                WHERE status IN (?, ?)
                ORDER BY scheduled_at, rowid
                """,
                (TaskStatus.SCHEDULED.value, TaskStatus.PENDING_DELIVERY.value),
            ).fetchall()
        current = now.astimezone(UTC)
        due = []
        for row in rows:
            scheduled_at = datetime.fromisoformat(row["scheduled_at"])
            if scheduled_at.astimezone(UTC) <= current:
                due.append(self._task_from_row(row))
        return due

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

    @staticmethod
    def _memory_from_row(row: sqlite3.Row) -> Memory:
        return Memory.model_validate(
            {
                "memory_id": row["memory_id"],
                "subject_id": row["subject_id"],
                "category": MemoryCategory(row["category"]),
                "value": row["value"],
                "source": row["source"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "consent_at": row["consent_at"],
            }
        )

    @staticmethod
    def _pending_memory_proposal_from_row(
        row: sqlite3.Row,
    ) -> PendingMemoryProposal:
        return PendingMemoryProposal.model_validate(
            {
                "proposal_id": row["proposal_id"],
                "subject_id": row["subject_id"],
                "category": MemoryCategory(row["category"]),
                "value": row["value"],
                "source": row["source"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
            }
        )

    @staticmethod
    def _vision_observation_from_row(row: sqlite3.Row) -> VisionObservation:
        return VisionObservation.model_validate(
            {
                "observation_id": row["observation_id"],
                "subject_id": row["subject_id"],
                "turn_id": row["turn_id"],
                "captured_at": row["captured_at"],
                "expires_at": row["expires_at"],
                "content_type": row["content_type"],
                "byte_size": row["byte_size"],
                "sha256": row["sha256"],
                "storage_key": row["storage_key"],
            }
        )
