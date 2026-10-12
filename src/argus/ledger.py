"""SQLite ledger: the harness's source of truth for coverage and completion.

Invariants:
    * Monotonic stage machine — a run never moves backward.
    * Exact-set accounting — completion is counted from rows, never claimed.
    * ``resolution_key`` uniqueness — a concern cannot be terminally resolved
      twice with different payloads.
    * Lease-based claiming — resumable adjudication without double work.
    * ``terminal_stage`` on concerns + attrition queries: every concern's
      death is attributed to a stage and a reason, so recall loss is
      observable instead of invisible.
    * Duplicate concerns from a divergence worker are demoted to a logged
      ``duplicate_concern_skipped`` event instead of failing the whole unit —
      a model repeating a question is noise to record, not fatal.
    * Structural diagnosis is recorded per section block in one transaction
      that ends with a ``section_done`` marker, so a block is either fully
      diagnosed or absent and resume skips exactly the finished blocks.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
import contextlib
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
from typing import Any

from argus.core import LedgerError, stable_id

TERMINAL_CONCERN_STATES = {
    "solved_by_paper",
    "solved_by_reference_or_consensus",
    "promoted_final",
    "invalid_or_duplicate",
}

RUN_STAGES = (
    "created",
    "extracted",
    "diverging",
    "diverged",
    "resolving",
    "resolved",
    "diagnosing",
    "diagnosed",
    "composing",
    "complete",
)

OUTLINE_DIAGNOSIS_KINDS = ("item", "pair", "sentence", "concern", "section_done")
STRUCTURE_DIAGNOSIS_KINDS = ("section", "issue", "structure_done")
REVISION_PLAN_NAME = "revision-plan.md"


@dataclass(frozen=True, slots=True)
class Completion:
    """Physical completion accounting for one run."""

    complete: bool
    run_id: str
    stage: str
    open_units: int
    uncovered_claims: int
    unresolved_concerns: int
    undiagnosed_sections: int
    next_action: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "run_id": self.run_id,
            "stage": self.stage,
            "open_units": self.open_units,
            "uncovered_claims": self.uncovered_claims,
            "unresolved_concerns": self.unresolved_concerns,
            "undiagnosed_sections": self.undiagnosed_sections,
            "next_action": self.next_action,
        }


class Ledger:
    """One SQLite database per run output directory."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    package_path TEXT NOT NULL,
                    output_path TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    final_review_path TEXT
                );

                CREATE TABLE IF NOT EXISTS units (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    locator TEXT NOT NULL,
                    source_document TEXT NOT NULL,
                    page INTEGER,
                    ordinal INTEGER NOT NULL,
                    content_path TEXT NOT NULL,
                    image_path TEXT,
                    caption TEXT,
                    manifest_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    section_id TEXT,
                    section_title TEXT,
                    tex_file TEXT,
                    line_start INTEGER,
                    line_end INTEGER,
                    UNIQUE(run_id, kind, source_document, page, ordinal)
                );

                CREATE TABLE IF NOT EXISTS claims (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    unit_id TEXT NOT NULL REFERENCES units(id) ON DELETE CASCADE,
                    sentence_ordinal INTEGER NOT NULL,
                    claim_ordinal INTEGER NOT NULL,
                    sentence_text TEXT NOT NULL,
                    claim_text TEXT NOT NULL,
                    coverage_status TEXT NOT NULL,
                    not_applicable_reason TEXT,
                    UNIQUE(unit_id, sentence_ordinal, claim_ordinal)
                );

                CREATE TABLE IF NOT EXISTS concerns (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    unit_id TEXT NOT NULL REFERENCES units(id) ON DELETE CASCADE,
                    claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
                    category TEXT NOT NULL,
                    question TEXT NOT NULL,
                    locator TEXT NOT NULL,
                    status TEXT NOT NULL,
                    evidence TEXT,
                    source TEXT,
                    materiality TEXT,
                    terminal_stage TEXT,
                    lease_owner TEXT,
                    lease_until REAL,
                    resolution_key TEXT UNIQUE,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS diagnoses (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    section_id TEXT,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(run_id, kind, subject_id)
                );

                CREATE INDEX IF NOT EXISTS concerns_run_status ON concerns(run_id, status);
                CREATE INDEX IF NOT EXISTS claims_run_coverage ON claims(run_id, coverage_status);
                """
            )

    def event(
        self,
        db: sqlite3.Connection,
        run_id: str,
        object_type: str,
        object_id: str,
        event: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        db.execute(
            "INSERT INTO events(run_id, object_type, object_id, event, details, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                object_type,
                object_id,
                event,
                json.dumps(details or {}, sort_keys=True),
                time.time(),
            ),
        )

    def note(self, run_id: str, event: str, details: dict[str, Any] | None = None) -> None:
        """Append a run-scoped event outside any other transaction."""
        with self.connect() as db:
            self.event(db, run_id, "run", run_id, event, details)

    # -- runs -------------------------------------------------------------

    def create_run(self, run_id: str, package_path: Path, output_path: Path) -> None:
        now = time.time()
        with self.connect() as db:
            existing = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if existing:
                if (
                    Path(existing["package_path"]).resolve() != package_path.resolve()
                    or Path(existing["output_path"]).resolve() != output_path.resolve()
                ):
                    raise LedgerError(f"run id {run_id} already belongs to another package")
                return
            db.execute(
                "INSERT INTO runs(id, package_path, output_path, stage, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'created', 'active', ?, ?)",
                (run_id, str(package_path.resolve()), str(output_path.resolve()), now, now),
            )
            self.event(db, run_id, "run", run_id, "created")

    def set_stage(self, run_id: str, stage: str) -> None:
        if stage not in RUN_STAGES:
            raise LedgerError(f"unknown run stage: {stage}")
        with self.connect() as db:
            current = db.execute("SELECT stage FROM runs WHERE id = ?", (run_id,)).fetchone()
            if current is None:
                raise KeyError(run_id)
            if RUN_STAGES.index(stage) < RUN_STAGES.index(current["stage"]):
                raise LedgerError(f"cannot move run backward from {current['stage']} to {stage}")
            status = "complete" if stage == "complete" else "active"
            db.execute(
                "UPDATE runs SET stage = ?, status = ?, updated_at = ? WHERE id = ?",
                (stage, status, time.time(), run_id),
            )
            self.event(db, run_id, "run", run_id, "stage_changed", {"stage": stage})

    def run(self, run_id: str) -> dict[str, Any]:
        rows = self.rows("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not rows:
            raise KeyError(run_id)
        return rows[0]

    # -- units ------------------------------------------------------------

    def register_unit(self, record: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO units(
                    id, run_id, kind, locator, source_document, page, ordinal,
                    content_path, image_path, caption, manifest_path, status,
                    section_id, section_title, tex_file, line_start, line_end
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    record["id"],
                    record["run_id"],
                    record["kind"],
                    record["locator"],
                    record["source_document"],
                    record.get("page"),
                    record["ordinal"],
                    record["content_path"],
                    record.get("image_path"),
                    record.get("caption"),
                    record["manifest_path"],
                    record.get("section_id"),
                    record.get("section_title"),
                    record.get("tex_file"),
                    record.get("line_start"),
                    record.get("line_end"),
                ),
            )
            self.event(db, record["run_id"], "unit", record["id"], "registered")

    def set_unit_status(self, unit_id: str, status: str) -> None:
        if status not in {"pending", "processing", "complete", "failed"}:
            raise LedgerError(f"unknown unit status: {status}")
        with self.connect() as db:
            row = db.execute("SELECT run_id FROM units WHERE id = ?", (unit_id,)).fetchone()
            if row is None:
                raise KeyError(unit_id)
            db.execute("UPDATE units SET status = ? WHERE id = ?", (status, unit_id))
            self.event(db, row["run_id"], "unit", unit_id, "status_changed", {"status": status})

    def list_units(self, run_id: str) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM units WHERE run_id = ? ORDER BY ordinal", (run_id,))

    # -- divergence -------------------------------------------------------

    def record_divergence(
        self, unit_id: str, sentences: list[str], items: list[dict[str, Any]]
    ) -> None:
        """Atomically replace one unit's complete divergence result.

        A duplicate (claim, category, question) triple is skipped with a
        logged event instead of raising — a model repeating itself is noise
        to record, not a reason to discard the whole unit's work.
        """
        by_ordinal = {item["sentence_ordinal"]: item for item in items}
        with self.connect() as db:
            unit = db.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()
            if unit is None:
                raise KeyError(unit_id)
            run_id = unit["run_id"]
            db.execute("DELETE FROM concerns WHERE unit_id = ?", (unit_id,))
            db.execute("DELETE FROM claims WHERE unit_id = ?", (unit_id,))
            duplicates = 0
            for sentence_ordinal, sentence in enumerate(sentences, 1):
                item = by_ordinal[sentence_ordinal]
                claims = item["claims"]
                if not claims:
                    claim_id = stable_id("clm", unit_id, sentence_ordinal, 1, sentence)
                    db.execute(
                        """
                        INSERT INTO claims(
                            id, run_id, unit_id, sentence_ordinal, claim_ordinal,
                            sentence_text, claim_text, coverage_status, not_applicable_reason
                        ) VALUES (?, ?, ?, ?, 1, ?, ?, 'not_applicable', ?)
                        """,
                        (
                            claim_id,
                            run_id,
                            unit_id,
                            sentence_ordinal,
                            sentence,
                            sentence,
                            item["not_applicable_reason"],
                        ),
                    )
                    continue
                for claim_ordinal, claim in enumerate(claims, 1):
                    claim_id = stable_id(
                        "clm", unit_id, sentence_ordinal, claim_ordinal, claim["claim_text"]
                    )
                    db.execute(
                        """
                        INSERT INTO claims(
                            id, run_id, unit_id, sentence_ordinal, claim_ordinal,
                            sentence_text, claim_text, coverage_status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'questioned')
                        """,
                        (
                            claim_id,
                            run_id,
                            unit_id,
                            sentence_ordinal,
                            claim_ordinal,
                            sentence,
                            claim["claim_text"],
                        ),
                    )
                    for question in claim["questions"]:
                        concern_id = stable_id(
                            "con", claim_id, question["category"], question["question"]
                        )
                        now = time.time()
                        inserted = db.execute(
                            """
                            INSERT INTO concerns(
                                id, run_id, unit_id, claim_id, category, question, locator,
                                status, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                            ON CONFLICT(id) DO NOTHING
                            """,
                            (
                                concern_id,
                                run_id,
                                unit_id,
                                claim_id,
                                question["category"],
                                question["question"],
                                unit["locator"],
                                now,
                                now,
                            ),
                        ).rowcount
                        if inserted == 0:
                            duplicates += 1
                            self.event(
                                db,
                                run_id,
                                "concern",
                                concern_id,
                                "duplicate_concern_skipped",
                                {"claim_id": claim_id, "category": question["category"]},
                            )
            db.execute("UPDATE units SET status = 'complete' WHERE id = ?", (unit_id,))
            self.event(
                db,
                run_id,
                "unit",
                unit_id,
                "divergence_replaced",
                {
                    "sentences": len(sentences),
                    "claims": sum(max(1, len(item["claims"])) for item in items),
                    "duplicate_questions_skipped": duplicates,
                },
            )

    # -- adjudication -----------------------------------------------------

    def claim_next(
        self, run_id: str, owner: str, lease_seconds: int = 300
    ) -> dict[str, Any] | None:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT * FROM concerns
                WHERE run_id = ? AND (
                    status = 'open' OR
                    (status = 'adjudicating' AND lease_until IS NOT NULL AND lease_until < ?)
                )
                ORDER BY created_at, id LIMIT 1
                """,
                (run_id, now),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE concerns SET status = 'adjudicating', lease_owner = ?, lease_until = ?, "
                "updated_at = ? WHERE id = ?",
                (owner, now + lease_seconds, now, row["id"]),
            )
            self.event(db, run_id, "concern", row["id"], "claimed", {"owner": owner})
            result = dict(row)
            result.update(status="adjudicating", lease_owner=owner, lease_until=now + lease_seconds)
            return result

    def claim_specific(
        self, concern_id: str, owner: str, lease_seconds: int = 300
    ) -> dict[str, Any]:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM concerns WHERE id = ?", (concern_id,)).fetchone()
            if row is None:
                raise KeyError(concern_id)
            if row["status"] in TERMINAL_CONCERN_STATES:
                return dict(row)
            can_claim = (
                row["status"] == "open"
                or (row["status"] == "adjudicating" and row["lease_owner"] == owner)
                or (
                    row["status"] == "adjudicating"
                    and row["lease_until"] is not None
                    and row["lease_until"] < now
                )
            )
            if not can_claim:
                raise LedgerError(f"concern {concern_id} is leased by {row['lease_owner']}")
            db.execute(
                "UPDATE concerns SET status = 'adjudicating', lease_owner = ?, lease_until = ?, "
                "updated_at = ? WHERE id = ?",
                (owner, now + lease_seconds, now, concern_id),
            )
            self.event(db, row["run_id"], "concern", concern_id, "claimed", {"owner": owner})
            result = dict(row)
            result.update(status="adjudicating", lease_owner=owner, lease_until=now + lease_seconds)
            return result

    def resolve_concern(
        self,
        concern_id: str,
        owner: str,
        status: str,
        evidence: str,
        source: str,
        materiality: str,
        resolution_key: str,
        terminal_stage: str = "resolve",
    ) -> None:
        if status not in TERMINAL_CONCERN_STATES:
            raise LedgerError(f"non-terminal concern status: {status}")
        if not evidence.strip():
            raise LedgerError("resolution evidence is required")
        with self.connect() as db:
            row = db.execute("SELECT * FROM concerns WHERE id = ?", (concern_id,)).fetchone()
            if row is None:
                raise KeyError(concern_id)
            if row["status"] in TERMINAL_CONCERN_STATES:
                if (
                    row["resolution_key"] == resolution_key
                    and row["status"] == status
                    and row["evidence"] == evidence
                    and row["source"] == source
                    and row["materiality"] == materiality
                ):
                    return
                raise LedgerError(f"concern {concern_id} already resolved with a different payload")
            if row["status"] != "adjudicating" or row["lease_owner"] != owner:
                raise LedgerError(f"owner {owner} does not hold concern {concern_id}")
            try:
                db.execute(
                    """
                    UPDATE concerns SET status = ?, evidence = ?, source = ?, materiality = ?,
                        terminal_stage = ?, resolution_key = ?, lease_owner = NULL,
                        lease_until = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        evidence,
                        source,
                        materiality,
                        terminal_stage,
                        resolution_key,
                        time.time(),
                        concern_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerError(f"resolution key already used: {resolution_key}") from exc
            self.event(
                db,
                row["run_id"],
                "concern",
                concern_id,
                "resolved",
                {"status": status, "terminal_stage": terminal_stage},
            )

    def discard_promoted_concern(
        self, concern_id: str, reason: str, canonical_id: str | None
    ) -> None:
        if not reason.strip():
            raise LedgerError("global audit discard reason is required")
        source = (
            f"global final-concern audit; canonical concern {canonical_id}"
            if canonical_id
            else "global final-concern audit; no canonical concern retained"
        )
        resolution_key = stable_id("audit", concern_id, reason, canonical_id or "none")
        with self.connect() as db:
            row = db.execute("SELECT * FROM concerns WHERE id = ?", (concern_id,)).fetchone()
            if row is None:
                raise KeyError(concern_id)
            if row["status"] != "promoted_final":
                raise LedgerError(
                    f"global audit can discard only promoted_final concerns: "
                    f"{concern_id} is {row['status']}"
                )
            db.execute(
                """
                UPDATE concerns SET status = 'invalid_or_duplicate', evidence = ?, source = ?,
                    terminal_stage = 'audit', resolution_key = ?, lease_owner = NULL,
                    lease_until = NULL, updated_at = ?
                WHERE id = ?
                """,
                (reason, source, resolution_key, time.time(), concern_id),
            )
            self.event(
                db,
                row["run_id"],
                "concern",
                concern_id,
                "audit_discarded",
                {"canonical_id": canonical_id, "reason": reason},
            )

    # -- diagnosis --------------------------------------------------------

    def _insert_diagnosis(
        self,
        db: sqlite3.Connection,
        run_id: str,
        kind: str,
        subject_id: str,
        section_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        db.execute(
            "INSERT INTO diagnoses(id, run_id, kind, subject_id, section_id, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                stable_id("dgn", run_id, kind, subject_id),
                run_id,
                kind,
                subject_id,
                section_id,
                json.dumps(payload, sort_keys=True, ensure_ascii=False),
                time.time(),
            ),
        )

    def record_section_diagnosis(
        self, run_id: str, section_id: str, result: dict[str, Any]
    ) -> None:
        """Atomically replace one section block's outline diagnosis.

        Rows: one ``item`` per unit, ``pair`` per adjacent pair, ``sentence``
        per (unit, ordinal), ``concern`` per final concern, then the
        ``section_done`` marker that resume and completion count.
        """
        with self.connect() as db:
            db.execute(
                "DELETE FROM diagnoses WHERE run_id = ? AND section_id = ?", (run_id, section_id)
            )
            for item in result["items"]:
                self._insert_diagnosis(db, run_id, "item", item["unit_id"], section_id, item)
            for pair in result["pairs"]:
                subject = f"{pair['from_id']}>{pair['to_id']}"
                self._insert_diagnosis(db, run_id, "pair", subject, section_id, pair)
            for sentence in result["sentences"]:
                subject = f"{sentence['unit_id']}#{sentence['sentence_ordinal']}"
                self._insert_diagnosis(db, run_id, "sentence", subject, section_id, sentence)
            for concern in result["concerns"]:
                self._insert_diagnosis(
                    db, run_id, "concern", concern["concern_id"], section_id, concern
                )
            self._insert_diagnosis(db, run_id, "section_done", section_id, section_id, {})
            self.event(
                db,
                run_id,
                "section",
                section_id,
                "section_diagnosed",
                {
                    "items": len(result["items"]),
                    "pairs": len(result["pairs"]),
                    "sentences": len(result["sentences"]),
                    "concerns": len(result["concerns"]),
                },
            )

    def record_structure_diagnosis(self, run_id: str, result: dict[str, Any]) -> None:
        """Atomically replace the run's section-level structure diagnosis."""
        with self.connect() as db:
            db.execute(
                "DELETE FROM diagnoses WHERE run_id = ? AND kind IN (?, ?, ?)",
                (run_id, *STRUCTURE_DIAGNOSIS_KINDS),
            )
            for section in result["sections"]:
                self._insert_diagnosis(db, run_id, "section", section["section_id"], None, section)
            for issue in result["issues"]:
                self._insert_diagnosis(db, run_id, "issue", issue["issue_id"], None, issue)
            self._insert_diagnosis(db, run_id, "structure_done", run_id, None, {})
            self.event(
                db,
                run_id,
                "run",
                run_id,
                "structure_diagnosed",
                {"sections": len(result["sections"]), "issues": len(result["issues"])},
            )

    def diagnoses(self, run_id: str, kind: str) -> list[dict[str, Any]]:
        """Diagnosis payloads of one kind, each with its subject and section."""
        return [
            {
                **json.loads(row["payload"]),
                "_subject_id": row["subject_id"],
                "_section_id": row["section_id"],
            }
            for row in self.rows(
                "SELECT subject_id, section_id, payload FROM diagnoses "
                "WHERE run_id = ? AND kind = ? ORDER BY created_at, subject_id",
                (run_id, kind),
            )
        ]

    def diagnosed_sections(self, run_id: str) -> set[str]:
        return {
            row["subject_id"]
            for row in self.rows(
                "SELECT subject_id FROM diagnoses WHERE run_id = ? AND kind = 'section_done'",
                (run_id,),
            )
        }

    def structure_diagnosed(self, run_id: str) -> bool:
        return bool(
            self.rows(
                "SELECT 1 FROM diagnoses WHERE run_id = ? AND kind = 'structure_done'", (run_id,)
            )
        )

    # -- queries ----------------------------------------------------------

    def rows(self, query: str, parameters: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(query, tuple(parameters)).fetchall()]

    def list_claims(self, run_id: str) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM claims WHERE run_id = ? ORDER BY unit_id, sentence_ordinal, claim_ordinal",
            (run_id,),
        )

    def list_concerns(self, run_id: str) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM concerns WHERE run_id = ? ORDER BY created_at, id", (run_id,)
        )

    # -- attrition --------------------------------------------------------

    def attrition(self, run_id: str) -> dict[str, Any]:
        """Where concerns died and why — the recall-observability core.

        Returns creation counts, terminal status distribution split by the
        stage that killed each concern, category-level survival, and the
        full discard-reason list for the audit stage.
        """
        concerns = self.list_concerns(run_id)
        by_status: dict[str, int] = {}
        by_terminal_stage: dict[str, dict[str, int]] = {}
        by_category: dict[str, dict[str, int]] = {}
        for concern in concerns:
            status = concern["status"]
            by_status[status] = by_status.get(status, 0) + 1
            stage = concern["terminal_stage"] or "pending"
            by_terminal_stage.setdefault(stage, {})
            by_terminal_stage[stage][status] = by_terminal_stage[stage].get(status, 0) + 1
            category = concern["category"]
            bucket = by_category.setdefault(category, {"created": 0, "surviving": 0})
            bucket["created"] += 1
            if status == "promoted_final":
                bucket["surviving"] += 1
        audit_discards = [
            {
                "concern_id": row["object_id"],
                **json.loads(row["details"]),
            }
            for row in self.rows(
                "SELECT object_id, details FROM events "
                "WHERE run_id = ? AND event = 'audit_discarded' ORDER BY sequence",
                (run_id,),
            )
        ]
        duplicates = self.rows(
            "SELECT COUNT(*) AS n FROM events WHERE run_id = ? AND event = 'duplicate_concern_skipped'",
            (run_id,),
        )[0]["n"]
        return {
            "run_id": run_id,
            "created": len(concerns),
            "surviving_final": by_status.get("promoted_final", 0),
            "by_status": by_status,
            "by_terminal_stage": by_terminal_stage,
            "by_category": by_category,
            "audit_discards": audit_discards,
            "duplicate_questions_skipped": duplicates,
        }

    # -- completion -------------------------------------------------------

    def completion(self, run_id: str) -> Completion:
        run = self.run(run_id)
        final_review_path = Path(run["final_review_path"]) if run["final_review_path"] else None
        final_review_exists = bool(
            final_review_path
            and final_review_path.is_file()
            and final_review_path.stat().st_size > 0
            and final_review_path.with_name("review.json").is_file()
        )
        with self.connect() as db:
            open_units = db.execute(
                "SELECT COUNT(*) FROM units WHERE run_id = ? AND status != 'complete'", (run_id,)
            ).fetchone()[0]
            uncovered = db.execute(
                "SELECT COUNT(*) FROM claims WHERE run_id = ? AND coverage_status = 'uncovered'",
                (run_id,),
            ).fetchone()[0]
            uncovered += db.execute(
                """
                SELECT COUNT(*) FROM units u
                WHERE u.run_id = ? AND u.status = 'complete'
                  AND NOT EXISTS (SELECT 1 FROM claims cl WHERE cl.unit_id = u.id)
                """,
                (run_id,),
            ).fetchone()[0]
            unresolved = db.execute(
                "SELECT COUNT(*) FROM concerns WHERE run_id = ? AND status NOT IN (?, ?, ?, ?)",
                (run_id, *sorted(TERMINAL_CONCERN_STATES)),
            ).fetchone()[0]
            # Section blocks exist only for tex packages; PDF units carry no
            # section_id, so a PDF run has zero blocks and nothing to diagnose.
            blocks = db.execute(
                "SELECT COUNT(DISTINCT section_id) FROM units "
                "WHERE run_id = ? AND section_id IS NOT NULL",
                (run_id,),
            ).fetchone()[0]
            undiagnosed = db.execute(
                """
                SELECT COUNT(DISTINCT u.section_id) FROM units u
                WHERE u.run_id = ? AND u.section_id IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM diagnoses d
                    WHERE d.run_id = u.run_id AND d.kind = 'section_done'
                      AND d.subject_id = u.section_id
                  )
                """,
                (run_id,),
            ).fetchone()[0]
            structure_done = db.execute(
                "SELECT COUNT(*) FROM diagnoses WHERE run_id = ? AND kind = 'structure_done'",
                (run_id,),
            ).fetchone()[0]
        if blocks and not structure_done:
            undiagnosed += 1
        revision_plan = Path(run["output_path"]) / REVISION_PLAN_NAME
        revision_plan_exists = not blocks or (
            revision_plan.is_file() and revision_plan.stat().st_size > 0
        )
        if open_units:
            next_action = "resume divergence for pending or failed units"
        elif uncovered:
            next_action = (
                "create a question or justified not-applicable decision for each uncovered claim"
            )
        elif unresolved:
            next_action = "resume evidence-based concern adjudication"
        elif undiagnosed and RUN_STAGES.index(run["stage"]) >= RUN_STAGES.index("resolved"):
            next_action = "resume structural diagnosis"
        elif run["stage"] != "complete":
            next_action = f"resume pipeline stage {run['stage']}"
        else:
            next_action = "none"
        return Completion(
            complete=(
                run["stage"] == "complete"
                and open_units == 0
                and uncovered == 0
                and unresolved == 0
                and undiagnosed == 0
                and final_review_exists
                and revision_plan_exists
            ),
            run_id=run_id,
            stage=run["stage"],
            open_units=open_units,
            uncovered_claims=uncovered,
            unresolved_concerns=unresolved,
            undiagnosed_sections=undiagnosed,
            next_action=next_action,
        )

    def finalize(self, run_id: str, final_review_path: Path) -> Completion:
        if not final_review_path.is_file() or final_review_path.stat().st_size == 0:
            raise LedgerError("final review file is missing or empty")
        before = self.completion(run_id)
        if (
            before.open_units
            or before.uncovered_claims
            or before.unresolved_concerns
            or before.undiagnosed_sections
        ):
            raise LedgerError(json.dumps(before.as_dict(), sort_keys=True))
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET final_review_path = ?, stage = 'complete', status = 'complete', "
                "updated_at = ? WHERE id = ?",
                (str(final_review_path.resolve()), time.time(), run_id),
            )
            self.event(db, run_id, "run", run_id, "finalized", {"path": str(final_review_path)})
        return self.completion(run_id)
