# orchestration/mcp_manager.py
import os
import atexit
import asyncio
import importlib.util
from typing import List, Set

# Check if required packages are available without importing them yet
mcp_spec = importlib.util.find_spec("mcp")
crewai_tools_spec = importlib.util.find_spec("crewai_tools")
MCP_AVAILABLE = mcp_spec is not None and crewai_tools_spec is not None

if MCP_AVAILABLE:
    from mcp import StdioServerParameters
    from crewai_tools import MCPServerAdapter
else:
    StdioServerParameters = None
    MCPServerAdapter = None

_active_adapters: List = []
_mcp_tools_cache: List = []
_initialized = False

# Names of tools that came from an MCP server. Read by list_tools.py to
# annotate the catalog with [MCP], and by ceo_prompter to remind the CEO
# that this system runs MCP.
MCP_TOOL_NAMES: Set[str] = set()


def _normalise(name: str) -> str:
    return (name or "").lower().replace(" ", "_")


def _extract_tools(adapter):
    if hasattr(adapter, "tools"):
        return list(adapter.tools)
    if hasattr(adapter, "get_tools"):
        return list(adapter.get_tools())
    try:
        return list(adapter)
    except TypeError:
        return []


# ── None-stripping wrapper ──────────────────────────────────────────
def _wrap_tool_strip_none(tool):
    """
    Wrap the tool's underlying `_run` so that None-valued kwargs are
    stripped AFTER crewai's Pydantic validation has re-added them.

    Why this is needed:

      agent_loop._run_tool strips None from the LLM's tool_args before
      dispatching. But crewai's Tool.run then calls _validate_kwargs,
      which runs the args schema through Pydantic. Pydantic fills any
      unset Optional field with None. So the None reappears.

      Some MCP servers (GitHub, notably) reject null for optional typed
      args with "expected string, received null". Stripping at the
      outer layer doesn't help because Pydantic re-injects.

      Wrapping the tool's own _run means the strip happens at the last
      possible moment — after validation, right before the MCP session
      call — where it actually sticks.
    """
    original_run = getattr(tool, "_run", None)
    if original_run is None:
        return tool

    def _clean_run(**kwargs):
        cleaned = {k: v for k, v in kwargs.items() if v is not None}
        return original_run(**cleaned)

    try:
        tool._run = _clean_run
    except Exception:
        # Pydantic frozen model or other immutable — fall back to
        # returning the unwrapped tool rather than crashing.
        pass
    return tool


async def _async_connect_adapter(adapter):
    if hasattr(adapter, "connect"):
        if asyncio.iscoroutinefunction(adapter.connect):
            await adapter.connect()
        else:
            adapter.connect()
    elif hasattr(adapter, "initialize"):
        if asyncio.iscoroutinefunction(adapter.initialize):
            await adapter.initialize()
        else:
            adapter.initialize()


def load_mcp_tools(force_reload: bool = False) -> List:
    global _mcp_tools_cache, _initialized, _active_adapters, MCP_TOOL_NAMES

    if not MCP_AVAILABLE:
        print("⚠️ MCP not available (mcp or crewai_tools missing). Skipping MCP tools.", flush=True)
        return []

    if _initialized and not force_reload:
        return _mcp_tools_cache

    if force_reload:
        cleanup_mcp_servers()

    all_tools = []
    servers = []

    github_token = os.getenv("ai_mcp", "")
    if github_token:
        servers.append(
            StdioServerParameters(
                command="npx",
                args=["-y", "@modelcontextprotocol/server-github"],
                env={"GITHUB_PERSONAL_ACCESS_TOKEN": github_token, "PATH": os.getenv("PATH", "")}
            )
        )

    gmail_credentials = os.getenv("GMAIL_MCP_CREDENTIALS", "")
    if gmail_credentials:
        servers.append(
            StdioServerParameters(
                command="npx",
                args=["-y", "@modelcontextprotocol/server-gmail"],
                env={"GMAIL_CREDENTIALS": gmail_credentials, "PATH": os.getenv("PATH", "")}
            )
        )

    drive_credentials = os.getenv("GOOGLE_DRIVE_MCP_CREDENTIALS", "")
    if drive_credentials:
        servers.append(
            StdioServerParameters(
                command="npx",
                args=["-y", "@modelcontextprotocol/server-google-drive"],
                env={"GOOGLE_DRIVE_CREDENTIALS": drive_credentials, "PATH": os.getenv("PATH", "")}
            )
        )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for server_params in servers:
        try:
            adapter = MCPServerAdapter(server_params)
            loop.run_until_complete(_async_connect_adapter(adapter))

            tools = _extract_tools(adapter)
            if tools:
                # Wrap every MCP tool so None-valued optional args are
                # stripped at the last moment, after crewai's Pydantic
                # validation has re-injected them.
                tools = [_wrap_tool_strip_none(t) for t in tools]
                all_tools.extend(tools)
                _active_adapters.append(adapter)

                # Record the names so list_tools can tag them [MCP].
                for t in tools:
                    name = getattr(t, "name", None)
                    if name:
                        MCP_TOOL_NAMES.add(_normalise(name))

                print(
                    f"✅ Connected to MCP: {server_params.args[1]} "
                    f"({len(tools)} tools)",
                    flush=True,
                )
            else:
                print(f"⚠️ No tools found from MCP server {server_params.args[1]}", flush=True)

        except Exception as e:
            print(f"❌ Failed to connect to MCP server ({server_params.args[1]}): {e}", flush=True)

    loop.close()

    _mcp_tools_cache = all_tools
    _initialized = True
    return all_tools


def is_mcp_tool(tool_name: str) -> bool:
    """True if `tool_name` came from an MCP server."""
    return _normalise(tool_name) in MCP_TOOL_NAMES


def get_mcp_tool_names() -> Set[str]:
    """Return the set of MCP-provided tool names (normalised)."""
    return set(MCP_TOOL_NAMES)


@atexit.register
def cleanup_mcp_servers():
    global _active_adapters, _mcp_tools_cache, _initialized, MCP_TOOL_NAMES
    for adapter in _active_adapters:
        try:
            if hasattr(adapter, "close"):
                if asyncio.iscoroutinefunction(adapter.close):
                    asyncio.run(adapter.close())
                else:
                    adapter.close()
            elif hasattr(adapter, "shutdown"):
                if asyncio.iscoroutinefunction(adapter.shutdown):
                    asyncio.run(adapter.shutdown())
                else:
                    adapter.shutdown()
        except Exception as e:
            print(f"⚠️ Error shutting down MCP adapter: {e}", flush=True)

    _active_adapters.clear()
    _mcp_tools_cache.clear()
    MCP_TOOL_NAMES.clear()
    _initialized = False
