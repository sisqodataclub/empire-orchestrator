# orchestration/turn_ledger.py
"""
Per-turn record of every tool call an agent made, classified by what
kind of action it actually was.

The reply contract (orchestration/reply_contract.py) validates the
CEO's SEND_REPLY claim against this ledger.

One TurnLedger per received message — created in run_agent_turn()
right after `empire_tools.set_log_context(...)`, not per LLM
iteration. That way a claim spanning multiple tool calls in the same
turn (e.g. `send_message` then `read_agent_log`) validates correctly
against a single ledger.

Classification
──────────────
Most tools map to a single ActionClass. Two are arg-sensitive and
must be inspected:

  • file_manager(action="read")    → READ
    file_manager(action="write" | "patch" | "append") → WRITE

  • system_terminal(command="ls …") → READ
    anything else                    → EXECUTE

  • send_message(to="worker_…")    → DELEGATE
    send_message(to="user_…")      → OTHER (writing to a user thread
                                     is not delegation)

Maintenance
───────────
When you onboard a new tool, add it to _STATIC. If you forget, the
tool classifies as OTHER, and any CEO claim of READ/WRITE/EXECUTE
after using it will be rejected. The rejection is verbose — it prints
the ledger — so the failure mode is obvious, not silent.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ActionClass(str, Enum):
    READ     = "read"
    WRITE    = "write"
    EXECUTE  = "execute"
    DELEGATE = "delegate"
    QUERY    = "query"
    OTHER    = "other"


# ── Classification table ─────────────────────────────────────────────
#
# Keys are the exact registry keys produced by gm.py:
#     getattr(tool, "name", "").lower().replace(" ", "_")
# So `@tool("File Manager")` → key "file_manager", NOT "manage_file".
#
# file_manager, system_terminal, and send_message are handled
# arg-sensitively in classify() and are omitted here.
_STATIC: dict[str, ActionClass] = {
    # ── EmpireTools (from @tool display names) ─────────────────────
    "ast_inspector":            ActionClass.READ,
    "list_directory":           ActionClass.READ,
    "internet_search":          ActionClass.QUERY,
    "scrape_webpage":           ActionClass.READ,      # fetches remote content
    "harvest_documentation":    ActionClass.WRITE,     # writes to ChromaDB
    "read_gmail":               ActionClass.READ,
    "send_gmail":               ActionClass.EXECUTE,   # external side effect
    "spawn_specialist":         ActionClass.WRITE,     # writes DNA file
    "consult_mission_history":  ActionClass.QUERY,
    "commit_to_global_library": ActionClass.WRITE,
    "search_empire_library":    ActionClass.QUERY,
    "query_official_docs":      ActionClass.QUERY,
    "invalidate_memory":        ActionClass.WRITE,
    "consult_overlord":         ActionClass.OTHER,     # blocking human ask
    "harvest_jobs":             ActionClass.QUERY,
    "describe_tool":            ActionClass.OTHER,     # introspection only

    # ── Attached staticmethod tools ────────────────────────────────
    "read_inbox":               ActionClass.READ,
    "get_new_inbox_messages":   ActionClass.READ,
    "send_user_message":        ActionClass.OTHER,     # writes to user thread
    "ask_user":                 ActionClass.OTHER,     # writes to user thread
    "execute_repl":             ActionClass.EXECUTE,
    "set_secret":               ActionClass.WRITE,
    "get_secret":               ActionClass.READ,
    "list_secret_keys":         ActionClass.READ,
    "delete_secret":            ActionClass.WRITE,
    "add_project":              ActionClass.WRITE,
    "list_projects":            ActionClass.READ,
    "add_task":                 ActionClass.WRITE,
    "list_tasks":               ActionClass.READ,
    "complete_task":            ActionClass.WRITE,
    "cancel_task":              ActionClass.WRITE,
    "list_empire_tools":        ActionClass.OTHER,
    "system_status":            ActionClass.READ,
    "list_agents":              ActionClass.READ,
    "think":                    ActionClass.OTHER,     # scratch reasoning

    # ── Messenger ──────────────────────────────────────────────────
    # send_message handled arg-sensitively in classify().
    "read_agent_log":           ActionClass.READ,

    # ── GitHub MCP ─────────────────────────────────────────────────
    # Add tools as you inspect them via list_empire_tools(). An
    # unlisted MCP tool falls through to OTHER, and a CEO claim of
    # READ/WRITE/EXECUTE after using it will be rejected loudly —
    # which is the correct maintenance signal.
    "get_file_contents":        ActionClass.READ,
    "list_commits":             ActionClass.READ,
    "get_issue":                ActionClass.READ,
    "list_issues":              ActionClass.READ,
    "get_pull_request":         ActionClass.READ,
    "list_pull_requests":       ActionClass.READ,
    "search_repositories":      ActionClass.QUERY,
    "search_code":              ActionClass.QUERY,
    "search_issues":            ActionClass.QUERY,
    "create_or_update_file":    ActionClass.WRITE,
    "create_pull_request":      ActionClass.WRITE,
    "create_issue":             ActionClass.WRITE,
    "create_branch":            ActionClass.WRITE,
    "fork_repository":          ActionClass.WRITE,
    "merge_pull_request":       ActionClass.WRITE,
    "update_issue":             ActionClass.WRITE,
    "add_issue_comment":        ActionClass.WRITE,
    "create_repository":        ActionClass.WRITE,
    "push_files":               ActionClass.WRITE,
}


# Shell commands that only observe — treating these as EXECUTE would
# force the CEO to claim "execute" for what is really a read.
_READ_SHELL_PREFIXES = (
    "cat ", "ls ", "head ", "tail ", "grep ", "find ",
    "wc ", "git log", "git show", "git diff", "git status",
    "pwd", "env", "which ", "whoami",
)

# Actions on the file_manager tool that mutate. Everything else
# (there is only "read") is treated as a read.
_WRITE_FILE_ACTIONS = frozenset({"write", "patch", "append", "delete", "move"})


def _normalise(tool_name: str) -> str:
    """Registry keys are lowercase with spaces → underscores."""
    return (tool_name or "").strip().lower().replace(" ", "_")


def classify(tool_name: str, args: dict[str, Any]) -> ActionClass:
    """
    Return the ActionClass for a given tool call.

    Most tools fall through to _STATIC. Three are arg-sensitive:
    file_manager, system_terminal, send_message.
    """
    key = _normalise(tool_name)

    # ── file_manager — read vs write ───────────────────────────────
    if key == "file_manager":
        action = str(args.get("action", "")).strip().lower()
        if action in _WRITE_FILE_ACTIONS:
            return ActionClass.WRITE
        # Default to READ — unknown action will fail at execution
        # anyway, and READ is the safe classification for the ledger.
        return ActionClass.READ

    # ── system_terminal — read-only shell vs execute ───────────────
    if key == "system_terminal":
        cmd = str(args.get("command", "")).strip().lower()
        if any(cmd.startswith(p) for p in _READ_SHELL_PREFIXES):
            return ActionClass.READ
        return ActionClass.EXECUTE

    # ── send_message — delegation vs writing to a user thread ──────
    if key == "send_message":
        to = str(args.get("to", ""))
        if to.startswith("user_"):
            # CEO messaging a user thread directly. Not delegation.
            # Not a work action either — the reply contract doesn't
            # validate this path (see agent_loop SEND_REPLY branch).
            return ActionClass.OTHER
        return ActionClass.DELEGATE

    # ── Everything else ────────────────────────────────────────────
    return _STATIC.get(key, ActionClass.OTHER)


# ── Records ──────────────────────────────────────────────────────────
@dataclass
class ToolCallRecord:
    """One row in the turn ledger."""
    call_id:      int
    tool_name:    str
    action_class: ActionClass
    args:         dict
    ok:           bool


# ── Ledger ───────────────────────────────────────────────────────────
@dataclass
class TurnLedger:
    """
    Records every tool call this turn. Created once per received
    message by run_agent_turn.
    """
    records: list[ToolCallRecord] = field(default_factory=list)
    _next_id: int = 1

    def record(
        self,
        tool_name: str,
        args: dict | None,
        ok: bool,
    ) -> ToolCallRecord:
        """
        Add a call. `ok` means "the tool ran without a validation or
        exception error" — not "the result was good". A read that
        returns 'file not found' still records ok=True; the CEO can
        honestly claim ['read'] after it.
        """
        safe_args = args if isinstance(args, dict) else {}
        rec = ToolCallRecord(
            call_id=self._next_id,
            tool_name=tool_name,
            action_class=classify(tool_name, safe_args),
            args=safe_args,
            ok=ok,
        )
        self._next_id += 1
        self.records.append(rec)
        return rec

    # ── Queries used by the validator ──────────────────────────────
    def classes_present(self) -> set[ActionClass]:
        """Every ActionClass for which at least one successful call exists."""
        return {r.action_class for r in self.records if r.ok}

    def by_id(self) -> dict[int, ToolCallRecord]:
        return {r.call_id: r for r in self.records}

    def did_direct_work(self) -> bool:
        """
        True if the turn includes a read, write, execute, or query.
        Delegation alone does not count — see did_delegate().
        """
        return bool(
            self.classes_present()
            & {
                ActionClass.READ,
                ActionClass.WRITE,
                ActionClass.EXECUTE,
                ActionClass.QUERY,
            }
        )

    def did_delegate(self) -> bool:
        return ActionClass.DELEGATE in self.classes_present()

    def is_empty(self) -> bool:
        return not self.records

    def summary(self) -> str:
        """
        Human-readable dump included in rejection messages so the CEO
        can see exactly what it did.
        """
        if not self.records:
            return "  (no tool calls this turn)"
        return "\n".join(
            f"  [#{r.call_id}] {r.tool_name} → {r.action_class.value} "
            f"({'ok' if r.ok else 'FAILED'})"
            for r in self.records
        )
