# tools/__init__.py
# tools/__init__.py

# tools/__init__.py
from .inbox_tools import (
    read_inbox,
    get_new_inbox_messages,
    send_user_message,
    ask_user,
    set_inbox_db,
)
from .repl_tool import execute_repl
from .secret_tools import (
    set_secret,
    get_secret,
    list_secret_keys,
    delete_secret,
    set_secrets_manager,
)
from .scheduler_tools import (
    add_project,
    list_projects,
    add_task,
    list_tasks,
    complete_task,
    cancel_task,
    set_scheduler_db,
)
from .list_tools import list_empire_tools
from .system_observability_tools import (
    system_status,
    inspect_task,
    cancel_task,
    set_observability_context,
)

__all__ = [
    # Inbox
    "read_inbox",
    "get_new_inbox_messages",
    "send_user_message",
    "ask_user",
    "set_inbox_db",
    # REPL
    "execute_repl",
    # Secrets
    "set_secret",
    "get_secret",
    "list_secret_keys",
    "delete_secret",
    "set_secrets_manager",
    # Scheduler
    "add_project",
    "list_projects",
    "add_task",
    "list_tasks",
    "complete_task",
    "cancel_task",
    "set_scheduler_db",
    # Misc
    "list_empire_tools",
    # System observability
    "system_status",
    "inspect_task",
    "cancel_task",
    "set_observability_context",
]
