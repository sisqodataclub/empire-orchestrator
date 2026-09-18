#
from crewai.tools import tool
from orchestration.scheduler_db import SchedulerDB

_current_scheduler_db = None

def set_scheduler_db(db):
    global _current_scheduler_db
    _current_scheduler_db = db

@tool("Add Project")
def add_project(name: str, description: str = ""):
    """Create a new project for grouping related tasks."""
    if _current_scheduler_db:
        project_id = _current_scheduler_db.add_project(name, description)
        return f"Project '{name}' created with ID {project_id}."
    return "Error: Scheduler DB not initialized."

@tool("List Projects")
def list_projects():
    """List all projects."""
    if _current_scheduler_db:
        projects = _current_scheduler_db.list_projects()
        if not projects:
            return "No projects found."
        return "\n".join([f"#{p['id']} {p['name']}" for p in projects])
    return "Error: Scheduler DB not initialized."

@tool("Add Task")
def add_task(
    title: str,
    due_at: str = None,
    recurrence: str = None,
    project_id: int = None,
    depends_on_task_id: int = None,
    description: str = "",
    task_type: str = "CEO_WAKE",
    script_code: str = None,
    script_path: str = None
):
    """
    Add a task to the scheduler.
    - task_type can be 'CEO_WAKE' (default) or 'SCRIPT'.
    - For 'SCRIPT', provide either script_code (Python code) or script_path (file path).
    """
    if _current_scheduler_db:
        task_id = _current_scheduler_db.add_task(
            title=title,
            due_at=due_at,
            recurrence=recurrence,
            project_id=project_id,
            depends_on_task_id=depends_on_task_id,
            description=description,
            task_type=task_type,
            script_code=script_code,
            script_path=script_path
        )
        return f"Task '{title}' added with ID {task_id}."
    return "Error: Scheduler DB not initialized."

@tool("List Tasks")
def list_tasks(status: str = None, project_id: int = None):
    """List tasks, optionally filtered by status and project."""
    if _current_scheduler_db:
        tasks = _current_scheduler_db.list_tasks(status, project_id)
        if not tasks:
            return "No tasks found."
        return "\n".join(
            [f"#{t['id']} [{t['status']}] {t['title']} (project {t.get('project_id')})" for t in tasks]
        )
    return "Error: Scheduler DB not initialized."

@tool("Complete Task")
def complete_task(task_id: int):
    """Mark a task as completed and trigger dependent tasks."""
    if _current_scheduler_db:
        dependents = _current_scheduler_db.mark_task_completed(task_id)
        msg = f"Task #{task_id} completed."
        if dependents:
            msg += f" Triggered {len(dependents)} dependent tasks: " + ", ".join([d['title'] for d in dependents])
        return msg
    return "Error: Scheduler DB not initialized."

@tool("Cancel Task")
def cancel_task(task_id: int):
    """Cancel a scheduled task."""
    if _current_scheduler_db:
        _current_scheduler_db.cancel_task(task_id)
        return f"Task #{task_id} cancelled."
    return "Error: Scheduler DB not initialized."
