# tools/__init__.py
#
# Central exports for the tools package. Any module that does
# `from tools import X` gets these names.
#
# Note on name uniqueness:
#   Every name exported here becomes a potential EmpireTools method.
#   Duplicate names would silently clobber each other in the registry,
#   so each tool has exactly one home.
#
#   • Inbox read/write → tools/inbox_tools.py
#   • System awareness → tools/system_observability_tools.py
#   • Secrets, scheduler, REPL → their own modules

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
    list_agents,
    think,
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
    # Tool discovery
    "list_empire_tools",
    # System awareness
    "system_status",
    "list_agents",
    "think",
    "set_observability_context",
]
