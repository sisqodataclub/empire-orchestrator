# orchestration/mcp_manager.py
import os
import atexit
import asyncio
import importlib.util
from typing import List

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


def _extract_tools(adapter):
    if hasattr(adapter, "tools"):
        return list(adapter.tools)
    if hasattr(adapter, "get_tools"):
        return list(adapter.get_tools())
    try:
        return list(adapter)
    except TypeError:
        return []


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
    global _mcp_tools_cache, _initialized, _active_adapters

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
                all_tools.extend(tools)
                _active_adapters.append(adapter)
                print(f"✅ Connected to MCP: {server_params.args[1]}", flush=True)
            else:
                print(f"⚠️ No tools found from MCP server {server_params.args[1]}", flush=True)

        except Exception as e:
            print(f"❌ Failed to connect to MCP server ({server_params.args[1]}): {e}", flush=True)

    loop.close()

    _mcp_tools_cache = all_tools
    _initialized = True
    return all_tools


@atexit.register
def cleanup_mcp_servers():
    global _active_adapters, _mcp_tools_cache, _initialized
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
    _initialized = False
