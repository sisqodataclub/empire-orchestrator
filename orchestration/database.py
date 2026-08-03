# orchestration/database.py
import sqlite3
import io
import tarfile
import tempfile
import shutil
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

class MissionDB:
    def __init__(self, mission_dir: str):
        self.db_path = Path(mission_dir) / "mission.db"
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS phases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                status TEXT DEFAULT 'PENDING',
                dependencies TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phase_id INTEGER REFERENCES phases(id),
                description TEXT NOT NULL,
                assigned_role TEXT,
                tools_allowed TEXT,
                acceptance_criteria TEXT,
                status TEXT DEFAULT 'PENDING',
                feedback TEXT,
                rework_count INTEGER DEFAULT 0,
                current_iteration INTEGER DEFAULT 0,
                FOREIGN KEY (phase_id) REFERENCES phases(id)
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER REFERENCES tasks(id),
                actor TEXT,
                action TEXT,
                details TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER REFERENCES tasks(id),
                artifact_type TEXT NOT NULL,
                file_path TEXT NOT NULL,
                schema_metadata TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS rework_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER REFERENCES tasks(id),
                iteration INTEGER NOT NULL,
                feedback TEXT NOT NULL,
                reworked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                db_snapshot BLOB,
                file_snapshot_path TEXT
            );
        """)
        self.conn.commit()

    # ── Phase & Task CRUD ────────────────────────
    def add_phase(self, title: str, dependencies: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO phases (title, dependencies) VALUES (?, ?)",
            (title, dependencies)
        )
        self.conn.commit()
        return cur.lastrowid

    def add_task(self, phase_id: int, description: str, assigned_role: str = "",
                 tools_allowed: str = "", acceptance_criteria: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO tasks (phase_id, description, assigned_role, tools_allowed, acceptance_criteria) VALUES (?, ?, ?, ?, ?)",
            (phase_id, description, assigned_role, tools_allowed, acceptance_criteria)
        )
        self.conn.commit()
        return cur.lastrowid

    def update_task_status(self, task_id: int, status: str, feedback: str = ""):
        self.conn.execute(
            "UPDATE tasks SET status = ?, feedback = ? WHERE id = ?",
            (status, feedback, task_id)
        )
        self.conn.execute(
            "INSERT INTO audit_logs (task_id, actor, action, details) VALUES (?, ?, ?, ?)",
            (task_id, "system", status, feedback)
        )
        self.conn.commit()

    def mark_phase_complete_if_all_done(self, phase_id: int) -> bool:
        tasks = self.conn.execute(
            "SELECT status FROM tasks WHERE phase_id = ?", (phase_id,)
        ).fetchall()
        if all(row[0] == "COMPLETED" for row in tasks):
            self.conn.execute(
                "UPDATE phases SET status = 'COMPLETED' WHERE id = ?", (phase_id,)
            )
            self.conn.commit()
            return True
        return False

    def get_phase_status(self, phase_id: int) -> str:
        row = self.conn.execute("SELECT status FROM phases WHERE id = ?", (phase_id,)).fetchone()
        return row[0] if row else "UNKNOWN"

    def get_pending_phase_id(self) -> Optional[int]:
        row = self.conn.execute(
            "SELECT id FROM phases WHERE status = 'PENDING' ORDER BY id LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def render_markdown(self, plan_path: str):
        """Generate plan.md with task IDs for precise referencing."""
        phases = self.conn.execute("SELECT id, title, status FROM phases ORDER BY id").fetchall()
        md = "# 🎯 MISSION PLAN\n\n"
        for pid, title, status in phases:
            icon = "✅" if status == "COMPLETED" else "⏳"
            md += f"## {title} - {icon} [{status}]\n"
            tasks = self.conn.execute(
                "SELECT id, description, status FROM tasks WHERE phase_id = ? ORDER BY id", (pid,)
            ).fetchall()
            for tid, desc, tstatus in tasks:
                checkbox = "[x]" if tstatus == "COMPLETED" else "[ ]"
                md += f"- {checkbox} (Task #{tid}) {desc}\n"
            md += "\n"
        Path(plan_path).write_text(md, encoding="utf-8")

    def handle_plan_mutation(self, mutation: str, phase_title: str, task_description: str,
                             assigned_role: str = "", deliverable_file: str = "",
                             tools_allowed: str = "", task_id: int = None) -> str:
        mutation = mutation.upper()
        if mutation == "ADD_PHASE":
            pid = self.add_phase(phase_title)
            self.add_task(pid, task_description, assigned_role, tools_allowed, deliverable_file)
            return f"Successfully created phase '{phase_title}' with initial task."
        elif mutation == "ADD_TASK":
            cur = self.conn.execute("SELECT id FROM phases WHERE title = ?", (phase_title,))
            row = cur.fetchone()
            if row:
                pid = row[0]
                self.add_task(pid, task_description, assigned_role, tools_allowed, deliverable_file)
                return f"Successfully added task to phase '{phase_title}'."
            else:
                pid = self.add_phase(phase_title)
                self.add_task(pid, task_description, assigned_role, tools_allowed, deliverable_file)
                return f"Phase '{phase_title}' not found; auto-created phase and added task."
        elif mutation == "MARK_TASK_DONE":
            if task_id:
                self.update_task_status(task_id, "COMPLETED")
                return f"Successfully marked task #{task_id} as COMPLETED."
            cur = self.conn.execute(
                "SELECT id FROM tasks WHERE description LIKE ? AND status != 'COMPLETED' ORDER BY id LIMIT 1",
                (f"%{task_description[:40]}%",)
            )
            row = cur.fetchone()
            if row:
                tid = row[0]
                self.update_task_status(tid, "COMPLETED")
                return f"Successfully marked task #{tid} as COMPLETED."
            return "Error: No matching task found. Provide the task_id or a more precise description."
        return f"Unknown mutation type: {mutation}"

    def register_artifact(self, task_id: int, artifact_type: str, file_path: str, schema_metadata: str = ""):
        self.conn.execute(
            "INSERT INTO artifacts (task_id, artifact_type, file_path, schema_metadata) VALUES (?,?,?,?)",
            (task_id, artifact_type, file_path, schema_metadata)
        )
        self.conn.commit()

    def get_artifacts_by_task(self, task_id: int) -> List[Dict]:
        cur = self.conn.execute("SELECT * FROM artifacts WHERE task_id = ?", (task_id,))
        return [dict(row) for row in cur.fetchall()]

    def get_latest_artifact(self, artifact_type: str, phase_id: int = None) -> Optional[Dict]:
        query = "SELECT * FROM artifacts WHERE artifact_type = ? ORDER BY registered_at DESC LIMIT 1"
        cur = self.conn.execute(query, (artifact_type,))
        row = cur.fetchone()
        return dict(row) if row else None

    def record_rework_feedback(self, task_id: int, feedback: str):
        cur = self.conn.execute("SELECT current_iteration FROM tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        iteration = (row[0] if row else 0) + 1
        self.conn.execute(
            "INSERT INTO rework_history (task_id, iteration, feedback) VALUES (?,?,?)",
            (task_id, iteration, feedback)
        )
        self.conn.execute(
            "UPDATE tasks SET status='PENDING', current_iteration=?, feedback=? WHERE id=?",
            (iteration, feedback, task_id)
        )
        self.conn.commit()

    def get_rework_feedback(self, task_id: int) -> str:
        rows = self.conn.execute(
            "SELECT feedback FROM rework_history WHERE task_id=? ORDER BY iteration DESC", (task_id,)
        ).fetchall()
        if not rows:
            return ""
        return "\n".join(f"[Iteration {i+1}]: {f}" for i, (f,) in enumerate(rows))

    def create_checkpoint(self, label: str, scratch_dir: str) -> int:
        db_backup = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        shutil.copyfile(self.db_path, db_backup.name)
        with open(db_backup.name, "rb") as f:
            db_blob = f.read()
        os.unlink(db_backup.name)

        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w:gz") as tar:
            for root, dirs, files in os.walk(scratch_dir):
                for file in files:
                    if file == "mission.db":
                        continue
                    full = os.path.join(root, file)
                    arcname = os.path.relpath(full, scratch_dir)
                    tar.add(full, arcname=arcname)
        tar_bytes = tar_buffer.getvalue()

        self.conn.execute(
            "INSERT INTO checkpoints (label, db_snapshot) VALUES (?, ?)",
            (label, db_blob)
        )
        checkpoint_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        tar_path = os.path.join(scratch_dir, f"checkpoint_{checkpoint_id}.tar.gz")
        with open(tar_path, "wb") as f:
            f.write(tar_bytes)
        self.conn.execute("UPDATE checkpoints SET file_snapshot_path=? WHERE id=?", (tar_path, checkpoint_id))
        self.conn.commit()
        return checkpoint_id

    def rollback_to_checkpoint(self, checkpoint_id: int, scratch_dir: str):
        row = self.conn.execute("SELECT db_snapshot, file_snapshot_path FROM checkpoints WHERE id=?", (checkpoint_id,)).fetchone()
        if not row:
            return False
        db_blob, tar_path = row
        with open(self.db_path, "wb") as f:
            f.write(db_blob)
        if tar_path and os.path.exists(tar_path):
            with tarfile.open(tar_path, "r:gz") as tar:
                tar.extractall(scratch_dir)
        self.conn.close()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        return True

    def get_active_task(self) -> Optional[Dict]:
        cur = self.conn.execute(
            "SELECT id, description, acceptance_criteria, assigned_role, tools_allowed FROM tasks WHERE status='PENDING' ORDER BY id LIMIT 1"
        )
        row = cur.fetchone()
        if row:
            return {
                "id": row[0],
                "description": row[1],
                "acceptance_criteria": row[2],
                "assigned_role": row[3],
                "tools_allowed": row[4]
            }
        return None
