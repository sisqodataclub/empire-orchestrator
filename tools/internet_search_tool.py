# tools/internet_search_tool.py
#
# Internet Search — keyless, package-free.
#
# Primary engine: Tavily's keyless REST endpoint. No API key, no
# signup, no pip package — just an HTTP POST with a special header.
# Built for LLM agents, so it doesn't block cloud IPs.
#
# Fallback engines (free scrapers, in case Tavily rate-limits):
#   1. Marginalia — niche indie-web index, generally accessible
#   2. DuckDuckGo Lite — kept in case DDG ever unblocks
#   3. Mojeek — kept in case Mojeek ever unblocks
#
# Note: as of 2026, Mojeek and public SearXNG instances captcha-block
# datacenter IPs on sight. DDG blocks them outright. So in practice
# Tavily is the only engine that works reliably from a VPS.
#
# Registry key (from the @tool display name "Internet Search"):
#     internet_search
#
# Parameter is `query`. The old name `raw_query` is remapped to
# `query` by agent_loop._TOOL_ARG_ALIASES for backward compat with
# old tool history — see orchestration/agent_loop.py.
import threading
import time

import requests
from bs4 import BeautifulSoup
from crewai.tools import tool


# Rate limiter — one search every 1.5 seconds, process-wide.
_search_lock      = threading.Lock()
_last_search_time = 0.0

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}


def _log(tool_name: str, detail: str) -> None:
    """Lazy log helper — avoids circular import with empire_tools."""
    try:
        from empire_tools import log_agent_action
        log_agent_action(tool_name, detail)
    except Exception:
        pass


# ── Engine 1: Tavily keyless REST ────────────────────────────────────
def _tavily_keyless(query: str) -> list:
    """
    Call Tavily's search endpoint in keyless mode.
    No API key required. The X-Tavily-Access-Mode header is what
    selects keyless auth on the server side.
    """
    r = requests.post(
        "https://api.tavily.com/search",
        headers={
            "Content-Type": "application/json",
            "X-Tavily-Access-Mode": "keyless",
        },
        json={
            "query": query,
            "max_results": 5,
            "search_depth": "basic",
            "include_answer": False,
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()

    out = []
    for it in (data.get("results") or [])[:5]:
        out.append({
            "Title":   it.get("title", "?"),
            "Link":    it.get("url", ""),
            "Snippet": (it.get("content") or "").strip(),
        })
    return out


# ── Engine 2: Marginalia ─────────────────────────────────────────────
def _marginalia(query: str) -> list:
    r = requests.get(
        "https://search.marginalia.nu/search",
        headers=_HEADERS,
        params={"query": query},
        timeout=12,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for a in soup.select("a"):
        href = a.get("href", "")
        title = a.get_text(strip=True)
        if title and href.startswith("http") and len(title) > 8:
            out.append({"Title": title, "Link": href, "Snippet": ""})
        if len(out) >= 5:
            break
    return out


# ── Engine 3: DuckDuckGo Lite (may be IP-blocked) ────────────────────
def _ddg_lite(query: str) -> list:
    r = requests.post(
        "https://lite.duckduckgo.com/lite/",
        headers=_HEADERS,
        data={"q": query},
        timeout=10,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for link in soup.select("a.result-link"):
        href = link.get("href", "")
        title = link.get_text(strip=True)
        snip_el = link.find_next("td", class_="result-snippet")
        snippet = snip_el.get_text(" ", strip=True) if snip_el else ""
        if title and href:
            out.append({"Title": title, "Link": href, "Snippet": snippet})
    return out[:5]


# ── Engine chain ─────────────────────────────────────────────────────
_ENGINES = [
    ("tavily",     _tavily_keyless),
    ("marginalia", _marginalia),
    ("ddg-lite",   _ddg_lite),
]


@tool("Internet Search")
def internet_search(query: str):
    """
    Searches the live internet and returns the top 5 results as
    title / URL / snippet. No API key or account required.

    Use this for any question that needs current information from the
    web — news, sports, prices, releases, docs, anything that isn't
    in a local file and isn't in your training data.

    If the top results don't answer the question, rephrase the query
    and try again with a narrower or different phrasing. Do NOT
    repeat the identical query — it will return the same results.

    If you already know the exact URL of a page, use `scrape_webpage`
    instead — it returns the full page, not just snippets.

    Args:
      query: Plain-English search query. Can be anything — general
             knowledge, sports scores, news, technical docs.
             Examples:
               "Erling Haaland goal September 2026"
               "FastAPI background tasks"
               "current price of bitcoin"

    EXAMPLES:
      internet_search(query="Haaland Manchester City result yesterday")
      internet_search(query="FastAPI background tasks tutorial")
      internet_search(query="FastAPI vs Flask 2026")
    """
    global _last_search_time
    with _search_lock:
        elapsed = time.time() - _last_search_time
        if elapsed < 1.5:
            time.sleep(1.5 - elapsed)
        _last_search_time = time.time()

    _log("Internet Search", query)

    errors = []
    for name, fn in _ENGINES:
        try:
            results = fn(query)
        except requests.exceptions.RequestException as e:
            errors.append(f"{name}: {type(e).__name__}")
            continue
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}")
            continue

        if results:
            _log("Internet Search",
                 f"served by {name} ({len(results)} results)")
            output = f"✅ SEARCH RESULTS for: '{query}'\n\n"
            for i, res in enumerate(results, 1):
                snippet = res.get("Snippet", "")
                if len(snippet) > 400:
                    snippet = snippet[:397] + "..."
                output += (
                    f"{i}. **{res['Title']}**\n"
                    f"🔗 {res['Link']}\n"
                    f"📄 {snippet}\n\n"
                )
            return output

        errors.append(f"{name}: no results")

    return (
        "❌ SEARCH BACKEND UNREACHABLE: all engines failed.\n"
        f"Tried: {', '.join(errors)}.\n"
        "This is a tool failure, not a 'no results' outcome. "
        "Tell the user the search service is unavailable; do not guess."
    )# tools/internet_search_tool.py
#
# Internet Search — keyless, package-free.
#
# Primary engine: Tavily's keyless REST endpoint. No API key, no
# signup, no pip package — just an HTTP POST with a special header.
# Built for LLM agents, so it doesn't block cloud IPs.
#
# Fallback engines (free scrapers, in case Tavily rate-limits):
#   1. Marginalia — niche indie-web index, generally accessible
#   2. DuckDuckGo Lite — kept in case DDG ever unblocks
#   3. Mojeek — kept in case Mojeek ever unblocks
#
# Note: as of 2026, Mojeek and public SearXNG instances captcha-block
# datacenter IPs on sight. DDG blocks them outright. So in practice
# Tavily is the only engine that works reliably from a VPS.
#
# Registry key (from the @tool display name "Internet Search"):
#     internet_search
#
# Parameter is `query`. The old name `raw_query` is remapped to
# `query` by agent_loop._TOOL_ARG_ALIASES for backward compat with
# old tool history — see orchestration/agent_loop.py.
import threading
import time

import requests
from bs4 import BeautifulSoup
from crewai.tools import tool


# Rate limiter — one search every 1.5 seconds, process-wide.
_search_lock      = threading.Lock()
_last_search_time = 0.0

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}


def _log(tool_name: str, detail: str) -> None:
    """Lazy log helper — avoids circular import with empire_tools."""
    try:
        from empire_tools import log_agent_action
        log_agent_action(tool_name, detail)
    except Exception:
        pass


# ── Engine 1: Tavily keyless REST ────────────────────────────────────
def _tavily_keyless(query: str) -> list:
    """
    Call Tavily's search endpoint in keyless mode.
    No API key required. The X-Tavily-Access-Mode header is what
    selects keyless auth on the server side.
    """
    r = requests.post(
        "https://api.tavily.com/search",
        headers={
            "Content-Type": "application/json",
            "X-Tavily-Access-Mode": "keyless",
        },
        json={
            "query": query,
            "max_results": 5,
            "search_depth": "basic",
            "include_answer": False,
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()

    out = []
    for it in (data.get("results") or [])[:5]:
        out.append({
            "Title":   it.get("title", "?"),
            "Link":    it.get("url", ""),
            "Snippet": (it.get("content") or "").strip(),
        })
    return out


# ── Engine 2: Marginalia ─────────────────────────────────────────────
def _marginalia(query: str) -> list:
    r = requests.get(
        "https://search.marginalia.nu/search",
        headers=_HEADERS,
        params={"query": query},
        timeout=12,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for a in soup.select("a"):
        href = a.get("href", "")
        title = a.get_text(strip=True)
        if title and href.startswith("http") and len(title) > 8:
            out.append({"Title": title, "Link": href, "Snippet": ""})
        if len(out) >= 5:
            break
    return out


# ── Engine 3: DuckDuckGo Lite (may be IP-blocked) ────────────────────
def _ddg_lite(query: str) -> list:
    r = requests.post(
        "https://lite.duckduckgo.com/lite/",
        headers=_HEADERS,
        data={"q": query},
        timeout=10,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for link in soup.select("a.result-link"):
        href = link.get("href", "")
        title = link.get_text(strip=True)
        snip_el = link.find_next("td", class_="result-snippet")
        snippet = snip_el.get_text(" ", strip=True) if snip_el else ""
        if title and href:
            out.append({"Title": title, "Link": href, "Snippet": snippet})
    return out[:5]


# ── Engine chain ─────────────────────────────────────────────────────
_ENGINES = [
    ("tavily",     _tavily_keyless),
    ("marginalia", _marginalia),
    ("ddg-lite",   _ddg_lite),
]


@tool("Internet Search")
def internet_search(query: str):
    """
    Searches the live internet and returns the top 5 results as
    title / URL / snippet. No API key or account required.

    Use this for any question that needs current information from the
    web — news, sports, prices, releases, docs, anything that isn't
    in a local file and isn't in your training data.

    If the top results don't answer the question, rephrase the query
    and try again with a narrower or different phrasing. Do NOT
    repeat the identical query — it will return the same results.

    If you already know the exact URL of a page, use `scrape_webpage`
    instead — it returns the full page, not just snippets.

    Args:
      query: Plain-English search query. Can be anything — general
             knowledge, sports scores, news, technical docs.
             Examples:
               "Erling Haaland goal September 2026"
               "FastAPI background tasks"
               "current price of bitcoin"

    EXAMPLES:
      internet_search(query="Haaland Manchester City result yesterday")
      internet_search(query="FastAPI background tasks tutorial")
      internet_search(query="FastAPI vs Flask 2026")
    """
    global _last_search_time
    with _search_lock:
        elapsed = time.time() - _last_search_time
        if elapsed < 1.5:
            time.sleep(1.5 - elapsed)
        _last_search_time = time.time()

    _log("Internet Search", query)

    errors = []
    for name, fn in _ENGINES:
        try:
            results = fn(query)
        except requests.exceptions.RequestException as e:
            errors.append(f"{name}: {type(e).__name__}")
            continue
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}")
            continue

        if results:
            _log("Internet Search",
                 f"served by {name} ({len(results)} results)")
            output = f"✅ SEARCH RESULTS for: '{query}'\n\n"
            for i, res in enumerate(results, 1):
                snippet = res.get("Snippet", "")
                if len(snippet) > 400:
                    snippet = snippet[:397] + "..."
                output += (
                    f"{i}. **{res['Title']}**\n"
                    f"🔗 {res['Link']}\n"
                    f"📄 {snippet}\n\n"
                )
            return output

        errors.append(f"{name}: no results")

    return (
        "❌ SEARCH BACKEND UNREACHABLE: all engines failed.\n"
        f"Tried: {', '.join(errors)}.\n"
        "This is a tool failure, not a 'no results' outcome. "
        "Tell the user the search service is unavailable; do not guess."
    )
