from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from companion_gateway.project.models import (
    ConflictCandidate,
    DecisionVersion,
    ProjectContextPackage,
)


class ProjectMemoryRepository:
    """SQLite persistence for project context, decision versions, and conflicts."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS project_contexts (
                    project_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS project_versions (
                    project_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (project_id, decision_id, version)
                );

                CREATE TABLE IF NOT EXISTS project_conflicts (
                    candidate_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL
                );
                """
            )

    def save_context(self, package: ProjectContextPackage) -> ProjectContextPackage:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO project_contexts(project_id, payload_json)
                VALUES (?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    payload_json = excluded.payload_json
                """,
                (package.project_id, package.model_dump_json()),
            )
        return package

    def get_context(self, project_id: str) -> ProjectContextPackage | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM project_contexts WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return (
            ProjectContextPackage.model_validate_json(row["payload_json"])
            if row is not None
            else None
        )

    def save_version(
        self,
        project_id: str,
        version: DecisionVersion,
    ) -> DecisionVersion:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO project_versions(
                    project_id, decision_id, version, payload_json
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id, decision_id, version) DO UPDATE SET
                    payload_json = excluded.payload_json
                """,
                (
                    project_id,
                    version.decision_id,
                    version.version,
                    version.model_dump_json(),
                ),
            )
        return version

    def list_versions(
        self,
        project_id: str,
        decision_id: str,
    ) -> list[DecisionVersion]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM project_versions
                WHERE project_id = ? AND decision_id = ?
                ORDER BY version
                """,
                (project_id, decision_id),
            ).fetchall()
        return [DecisionVersion.model_validate_json(row["payload_json"]) for row in rows]

    def save_conflict(self, candidate: ConflictCandidate) -> ConflictCandidate:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO project_conflicts(candidate_id, payload_json)
                VALUES (?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    payload_json = excluded.payload_json
                """,
                (candidate.candidate_id, candidate.model_dump_json()),
            )
        return candidate

    def get_conflict(self, candidate_id: str) -> ConflictCandidate | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM project_conflicts WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return (
            ConflictCandidate.model_validate_json(row["payload_json"])
            if row is not None
            else None
        )

    def commit_conflict_review(
        self,
        *,
        reviewed_candidate: ConflictCandidate,
        expected_active_decision_text: str | None = None,
        updated_context: ProjectContextPackage | None = None,
        previous_version: DecisionVersion | None = None,
        new_version: DecisionVersion | None = None,
    ) -> Literal["committed", "not_found", "already_reviewed", "stale"]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            conflict_row = connection.execute(
                "SELECT payload_json FROM project_conflicts WHERE candidate_id = ?",
                (reviewed_candidate.candidate_id,),
            ).fetchone()
            if conflict_row is None:
                return "not_found"
            stored_candidate = ConflictCandidate.model_validate_json(
                conflict_row["payload_json"]
            )
            if stored_candidate.status.value != "proposed":
                return "already_reviewed"

            review_updates_decision = updated_context is not None
            if review_updates_decision:
                if (
                    expected_active_decision_text is None
                    or previous_version is None
                    or new_version is None
                ):
                    raise ValueError("accepted review requires complete decision state")
                context_row = connection.execute(
                    "SELECT payload_json FROM project_contexts WHERE project_id = ?",
                    (reviewed_candidate.project_id,),
                ).fetchone()
                if context_row is None:
                    return "not_found"
                stored_context = ProjectContextPackage.model_validate_json(
                    context_row["payload_json"]
                )
                stored_decision = next(
                    (
                        item
                        for item in stored_context.active_decisions
                        if item.decision_id == reviewed_candidate.decision_id
                    ),
                    None,
                )
                if (
                    stored_decision is None
                    or stored_decision.decision_text != expected_active_decision_text
                ):
                    return "stale"
                connection.execute(
                    """
                    INSERT INTO project_contexts(project_id, payload_json)
                    VALUES (?, ?)
                    ON CONFLICT(project_id) DO UPDATE SET
                        payload_json = excluded.payload_json
                    """,
                    (updated_context.project_id, updated_context.model_dump_json()),
                )
                for version in (previous_version, new_version):
                    connection.execute(
                        """
                        INSERT INTO project_versions(
                            project_id, decision_id, version, payload_json
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(project_id, decision_id, version) DO UPDATE SET
                            payload_json = excluded.payload_json
                        """,
                        (
                            reviewed_candidate.project_id,
                            version.decision_id,
                            version.version,
                            version.model_dump_json(),
                        ),
                    )

            connection.execute(
                """
                UPDATE project_conflicts
                SET payload_json = ?
                WHERE candidate_id = ?
                """,
                (
                    reviewed_candidate.model_dump_json(),
                    reviewed_candidate.candidate_id,
                ),
            )
        return "committed"
