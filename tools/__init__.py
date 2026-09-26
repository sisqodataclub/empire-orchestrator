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
#   • Inbox read/write        → tools/inbox_tools.py
#   • System awareness        → tools/system_observability_tools.py
#   • Gmail read/send/manage  → tools/gmail_tools.py
#   • AST inspection          → tools/ast_inspector_tool.py
#   • Internet search         → tools/internet_search_tool.py
#   • Container logs          → tools/container_logs_tool.py
#   • Deploy logs             → tools/deploy_logs_tool.py
#   • Dynamic tool management → tools/dynamic_tools_tool.py
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

from .gmail_tools import (
    read_latest_emails,
    read_email,
    search_emails,
    send_email,
    reply_to_email,
    forward_email,
    list_gmail_folders,
    mark_email_read,
    mark_email_unread,
    move_email_to_folder,
    download_attachments,
    delete_email,
    unread_count,
)

from .ast_inspector_tool import ast_inspector

from .internet_search_tool import internet_search

from .container_logs_tool import (
    list_containers,
    read_container_logs,
    scan_for_errors,
    container_health,
)

from .deploy_logs_tool import (
    list_deploy_logs,
    read_deploy_log,
    scan_deploy_failures,
)

from .dynamic_tools_tool import (
    propose_tool,
    list_pending_tools,
    read_pending_tool,
    activate_tool,
    deactivate_tool,
    list_dynamic_tools,
    reject_pending_tool,
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
    # Gmail
    "read_latest_emails",
    "read_email",
    "search_emails",
    "send_email",
    "reply_to_email",
    "forward_email",
    "list_gmail_folders",
    "mark_email_read",
    "mark_email_unread",
    "move_email_to_folder",
    "download_attachments",
    "delete_email",
    "unread_count",
    # AST + Search
    "ast_inspector",
    "internet_search",
    # Container logs
    "list_containers",
    "read_container_logs",
    "scan_for_errors",
    "container_health",
    # Deploy logs
    "list_deploy_logs",
    "read_deploy_log",
    "scan_deploy_failures",
    # Dynamic tools
    "propose_tool",
    "list_pending_tools",
    "read_pending_tool",
    "activate_tool",
    "deactivate_tool",
    "list_dynamic_tools",
    "reject_pending_tool",
]
