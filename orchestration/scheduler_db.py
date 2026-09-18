import os, sqlite3
from datetime import datetime, timedelta
from typing import List, Dict, Optional

class SchedulerDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True) if os.path.dirname(db_path) else None
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()
        self._migrate_schema()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                title TEXT NOT NULL,
                description TEXT,
                due_at TEXT,
                recurrence TEXT,
                depends_on_task_id INTEGER,
                status TEXT DEFAULT 'PENDING',
                task_type TEXT DEFAULT 'CEO_WAKE',
                script_code TEXT,
                script_path TEXT,
                agent_role TEXT,
                agent_instruction TEXT,
                thread_id TEXT,
                attempt_count INTEGER DEFAULT 0,
                max_attempts INTEGER DEFAULT 3,
                priority INTEGER DEFAULT 5,
                last_error TEXT,
                next_attempt_at TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY(project_id) REFERENCES projects(id),
                FOREIGN KEY(depends_on_task_id) REFERENCES scheduled_tasks(id)
            );

            CREATE TABLE IF NOT EXISTS task_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                mission TEXT,
                result TEXT,
                status TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_status ON scheduled_tasks(status);
            CREATE INDEX IF NOT EXISTS idx_task_results_created ON task_results(created_at);
        """)
        self.conn.commit()

    def _migrate_schema(self):
        """Add missing columns to scheduled_tasks if they don't exist."""
        cols = [row["name"] for row in self.conn.execute("PRAGMA table_info(scheduled_tasks)").fetchall()]
        desired_columns = {
            "task_type": "TEXT DEFAULT 'CEO_WAKE'",
            "script_code": "TEXT",
            "script_path": "TEXT",
            "agent_role": "TEXT",
            "agent_instruction": "TEXT",
            "thread_id": "TEXT",
            "attempt_count": "INTEGER DEFAULT 0",
            "max_attempts": "INTEGER DEFAULT 3",
            "priority": "INTEGER DEFAULT 5",
            "last_error": "TEXT",
            "next_attempt_at": "TEXT",
        }
        for col_name, col_def in desired_columns.items():
            if col_name not in cols:
                self.conn.execute(f"ALTER TABLE scheduled_tasks ADD COLUMN {col_name} {col_def}")
        self.conn.commit()

    # ---------- Projects ----------
    def add_project(self, name: str, description: str = "") -> int:
        created = datetime.now().isoformat()
        cur = self.conn.execute(
            "INSERT INTO projects (name, description, created_at) VALUES (?, ?, ?)",
            (name, description, created)
        )
        self.conn.commit()
        return cur.lastrowid

    def get_project(self, project_id: int) -> Optional[Dict]:
        row = self.conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return dict(row) if row else None

    def list_projects(self) -> List[Dict]:
        rows = self.conn.execute("SELECT * FROM projects ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # ---------- Tasks ----------
    def add_task(self, title: str,
                 due_at: Optional[str] = None,
                 recurrence: Optional[str] = None,
                 project_id: Optional[int] = None,
                 depends_on_task_id: Optional[int] = None,
                 description: str = "",
                 task_type: str = "CEO_WAKE",
                 script_code: Optional[str] = None,
                 script_path: Optional[str] = None,
                 agent_role: Optional[str] = None,
                 agent_instruction: Optional[str] = None,
                 thread_id: Optional[str] = None,
                 priority: int = 5,
                 max_attempts: int = 3) -> int:
        created = datetime.now().isoformat()
        status = 'WAITING' if depends_on_task_id else 'PENDING'
        cur = self.conn.execute(
            """INSERT INTO scheduled_tasks
               (project_id, title, description, due_at, recurrence,
                depends_on_task_id, status, task_type, script_code, script_path,
                agent_role, agent_instruction, thread_id,
                priority, max_attempts, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (project_id, title, description, due_at, recurrence,
             depends_on_task_id, status, task_type, script_code, script_path,
             agent_role, agent_instruction, thread_id,
             priority, max_attempts, created)
        )
        self.conn.commit()
        return cur.lastrowid

    def get_task(self, task_id: int) -> Optional[Dict]:
        row = self.conn.execute("SELECT * FROM scheduled_tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, status: Optional[str] = None, project_id: Optional[int] = None) -> List[Dict]:
        query = "SELECT * FROM scheduled_tasks WHERE 1=1"
        params = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        query += " ORDER BY priority ASC, id"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def claim_next_task(self, now: str) -> Optional[Dict]:
        rows = self.conn.execute(
            """
            SELECT * FROM scheduled_tasks
            WHERE status='PENDING'
              AND (due_at IS NULL OR due_at <= ?)
            ORDER BY priority ASC, due_at ASC, id ASC
            LIMIT 1
            """,
            (now,)
        ).fetchall()
        if not rows:
            return None
        task = dict(rows[0])
        cur = self.conn.execute(
            "UPDATE scheduled_tasks SET status='IN_PROGRESS' WHERE id=? AND status='PENDING'",
            (task['id'],)
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_task(task['id'])

    def mark_task_in_progress(self, task_id: int):
        self.conn.execute(
            "UPDATE scheduled_tasks SET status='IN_PROGRESS' WHERE id=?",
            (task_id,)
        )
        self.conn.commit()

    def mark_task_completed(self, task_id: int) -> List[Dict]:
        task = self.get_task(task_id)
        if not task:
            return []
        self.conn.execute(
            "UPDATE scheduled_tasks SET status='COMPLETED', completed_at=? WHERE id=?",
            (datetime.now().isoformat(), task_id)
        )
        self.conn.commit()

        if task.get('recurrence'):
            self._schedule_next_recurrence(task)

        dependents = self.conn.execute(
            "SELECT * FROM scheduled_tasks WHERE depends_on_task_id=? AND status='WAITING'",
            (task_id,)
        ).fetchall()
        for dep in dependents:
            self.conn.execute(
                "UPDATE scheduled_tasks SET status='PENDING' WHERE id=?",
                (dep['id'],)
            )
        self.conn.commit()

        if task.get('thread_id') and task.get('task_type') != 'NOTIFY_USER':
            self.add_task(
                title=f"Notify user about completed task #{task_id}",
                description=task.get('title',''),
                due_at=datetime.now().isoformat(),
                task_type="NOTIFY_USER",
                thread_id=task.get('thread_id'),
                agent_instruction=f"Reply to user about task #{task_id}",
                priority=1,
                max_attempts=1
            )

        return [dict(d) for d in dependents]

    def mark_task_failed(self, task_id: int, reason: str = ""):
        task = self.get_task(task_id)
        if not task:
            return
        attempt = task.get('attempt_count', 0) + 1
        max_attempts = task.get('max_attempts', 3)

        if attempt < max_attempts:
            backoff_seconds = 30 * (2 ** (attempt - 1))
            retry_at = (datetime.now() + timedelta(seconds=backoff_seconds)).isoformat()
            self.conn.execute(
                """
                UPDATE scheduled_tasks
                SET status='PENDING', attempt_count=?, last_error=?, next_attempt_at=?, due_at=?
                WHERE id=?
                """,
                (attempt, reason, retry_at, retry_at, task_id)
            )
            self.conn.commit()
        else:
            self.conn.execute(
                "UPDATE scheduled_tasks SET status='FAILED', attempt_count=?, last_error=? WHERE id=?",
                (attempt, reason, task_id)
            )
            self.conn.commit()
            dependents = self.conn.execute(
                "SELECT id FROM scheduled_tasks WHERE depends_on_task_id=? AND status='WAITING'",
                (task_id,)
            ).fetchall()
            for dep in dependents:
                self.conn.execute(
                    "UPDATE scheduled_tasks SET status='CANCELLED', last_error=? WHERE id=?",
                    (f"Predecessor task #{task_id} failed", dep['id'])
                )
            self.conn.commit()

    def cancel_task(self, task_id: int):
        self.conn.execute(
            "UPDATE scheduled_tasks SET status='CANCELLED' WHERE id=?",
            (task_id,)
        )
        self.conn.commit()

    # ---------- Task Results ----------
    def add_task_result(self, task_id: Optional[int], mission: str, result: str, status: str = "COMPLETED"):
        created = datetime.now().isoformat()
        self.conn.execute(
            "INSERT INTO task_results (task_id, mission, result, status, created_at) VALUES (?,?,?,?,?)",
            (task_id, mission, result, status, created)
        )
        self.conn.commit()

    def get_recent_task_results(self, limit: int = 5) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT * FROM task_results ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d['citation_token'] = f"task_{d['id']}"
            results.append(d)
        return results

    # ---------- Recurrence Helper ----------
    def _schedule_next_recurrence(self, task: Dict):
        recurrence = (task.get('recurrence') or '').lower()
        if not recurrence:
            return
        now = datetime.now()
        if 'daily' in recurrence:
            next_due = now + timedelta(days=1)
        elif 'weekly' in recurrence:
            next_due = now + timedelta(weeks=1)
        elif 'monthly' in recurrence:
            next_due = now + timedelta(days=30)
        else:
            return

        self.add_task(
            title=task['title'],
            description=task.get('description',''),
            due_at=next_due.isoformat(),
            recurrence=task.get('recurrence'),
            project_id=task.get('project_id'),
            depends_on_task_id=None,
            task_type=task.get('task_type','CEO_WAKE'),
            script_code=task.get('script_code'),
            script_path=task.get('script_path'),
            agent_role=task.get('agent_role'),
            agent_instruction=task.get('agent_instruction'),
            thread_id=task.get('thread_id'),
            priority=task.get('priority', 5),
            max_attempts=task.get('max_attempts', 3)
        )
