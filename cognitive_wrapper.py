"""
cognitive_wrapper.py — Cognitive Agent Wrapper (Direct Executor + Zero‑Trust)
============================================================================
- Agents can only use tools assigned to them by the Spawner.
- The prompt only lists authorised tools.
- The executor checks authorisation before running any tool.
- Verification‑command stripping removed (no longer needed with tool‑level gating).
"""

import json
import re
import os
import platform
import subprocess
import datetime
import getpass
import shutil
import threading
import time as _time

# ==============================================================================
# ⚡ TOOL RESULT CACHE
# ==============================================================================
_tool_cache: dict = {}

# ==============================================================================
# 🏎️ ENVIRONMENT CONTEXT CACHE (kept but NOT used)
# ==============================================================================
_env_cache: dict = {"result": None, "expires_at": 0.0, "cwd": ""}
_ENV_CACHE_TTL   = 120

# ==============================================================================
# 🔒 HARD ASYNC LOCK
# ==============================================================================
class _AsyncLock:
    def __init__(self):
        self._lock    = threading.Lock()
        self._holders: list = []

    def acquire(self, role: str) -> None:
        with self._lock:
            self._holders.append(role)

    def release(self, role: str) -> None:
        with self._lock:
            try:
                self._holders.remove(role)
            except ValueError:
                pass

    def is_held(self) -> bool:
        with self._lock:
            return len(self._holders) > 0

    def held_by(self) -> list:
        with self._lock:
            return list(self._holders)


WORKER_LOCK = _AsyncLock()

# ==============================================================================
# 💀 FATAL ERROR PATTERNS
# ==============================================================================
FATAL_PATTERNS = [
    "Permission denied", "No such file or directory", "ModuleNotFoundError",
    "ImportError", "SyntaxError", "cannot import", "Address already in use",
    "command not found", "is not defined",
]

def is_fatal_error(result: str) -> bool:
    return any(p.lower() in result.lower() for p in FATAL_PATTERNS)

# ==============================================================================
# 📊 TASK COMPLEXITY CLASSIFIER
# ==============================================================================
_SIMPLE_SIGNALS = [
    r'\bread\b', r'\bshow\b', r'\bget\b', r'\bdisplay\b', r'\bprint\b',
    r'\bappend\b', r'\badd line\b', r'\brename variable\b', r'\bcheck if\b',
    r'\bdoes .{1,20} exist\b', r'\bwhat is in\b', r'\blist\b', r'\bfetch\b',
]

_TRIVIAL_SIGNALS = [
    r'\bdelete\s+(?:the\s+)?(?:duplicate\s+)?(?:extra\s+)?line\b',
    r'\bremove\s+duplicate\b',
    r'\bremove\s+extra\b',
    r'\bcomment\s+out\b',
    r'\badd\s+one\s+line\b',
    r'\binsert\s+one\s+line\b',
    r'\brename\s+\w+\s+to\b',
    r'\bdelete\s+line\s+\d+\b',
    r'\bsed\s+-i\b',
    r'\bchange\s+one\s+(?:line|import|variable)\b',
    r'\bfix\s+typo\b',
    r'\bclose\s+(?:the\s+)?(?:missing\s+)?(?:tag|bracket|paren)\b',
    r'\badd\s+missing\s+(?:semicolon|comma|bracket|paren|quote)\b',
]

_COMPLEX_SIGNALS = [
    r'\bbuild\b', r'\bcreate\b', r'\bimplement\b', r'\brefactor\b',
    r'\bdebug why\b', r'\bfix the\b', r'\barchitect\b', r'\bdesign\b',
    r'\bintegrate\b', r'\bmigrate\b', r'\boverhaul\b', r'\bset up\b',
]
_REPORT_SIGNALS = [
    r'\baudit the codebase\b', r'\barchitecture report\b', r'\bcode review\b',
    r'\bdocument the codebase\b', r'\bmap the codebase\b',
    r'\bexplain the codebase\b', r'\bwalk me through the code\b',
    r'\banalyze the repo\b', r'\banalyse the repo\b',
    r'\bwrite an architecture\b', r'\bwrite a technical report\b',
    r'\bsurvey the codebase\b',
]

TIER_SETTINGS = {
    "SIMPLE":  dict(max_turns=5,  max_attempts=1, run_mentor=False, run_self_verify=False),
    "MEDIUM":  dict(max_turns=12, max_attempts=2, run_mentor=True,  run_self_verify=True),
    "COMPLEX": dict(max_turns=20, max_attempts=3, run_mentor=True,  run_self_verify=True),
    "REPORT":  dict(max_turns=1,  max_attempts=1, run_mentor=False, run_self_verify=False),
}

def classify_task(instruction: str) -> str:
    text       = instruction.lower()
    word_count = len(instruction.split())

    if any(re.search(p, text) for p in _REPORT_SIGNALS):
        return "REPORT"

    if any(re.search(p, text) for p in _TRIVIAL_SIGNALS):
        return "SIMPLE"

    if any(re.search(p, text) for p in _COMPLEX_SIGNALS):
        return "COMPLEX"

    if word_count <= 25 and any(re.search(p, text) for p in _SIMPLE_SIGNALS):
        return "SIMPLE"

    return "MEDIUM"


# ==============================================================================
# 🎯 SMART TERMINAL OUTPUT EXTRACTOR
# ==============================================================================
_ERROR_PATTERNS = [
    'error', 'exception', 'traceback', 'failed', 'typeerror', 'syntaxerror',
    'importerror', 'at line', 'undefined', 'cannot find', 'not found',
    'fatal', 'warn', 'rejected', 'unexpected token', 'cannot read'
]

def _smart_extract(output: str, max_chars: int = 800) -> str:
    if len(output) <= max_chars:
        return output
    lines       = output.splitlines()
    error_lines = [l for l in lines if any(p in l.lower() for p in _ERROR_PATTERNS)]
    other_lines = [l for l in lines if l not in error_lines]
    result      = '\n'.join(error_lines)
    if len(result) < max_chars:
        result = (result + '\n' + '\n'.join(other_lines)[:max_chars - len(result) - 1]).strip()
    if len(output) > len(result):
        result = result[:max_chars] + f'\n... [{len(output) - max_chars} chars trimmed]'
    return result[:max_chars]


# ==============================================================================
# 🎯 TARGET PATH EXTRACTOR  (kept for external use)
# ==============================================================================
_PATH_RE = re.compile(
    r'(/(?:home|srv|opt|var|root|tmp|mnt|usr)/[\w.\-/]+\.(?:tsx?|jsx?|py|json|md|css|html|sh|yaml|yml|toml|sql|env|lock|svg|vue|svelte))',
    re.IGNORECASE,
)
_RELATIVE_PATH_RE = re.compile(
    r'\b((?:apps?|packages?|src|lib|components?|routes?|pages?|hooks?|utils?|styles?|tests?|spec)'
    r'/[\w.\-/]+\.(?:tsx?|jsx?|py|json|md|css|html|sh|yaml|yml|toml|sql|vue|svelte))',
    re.IGNORECASE,
)

def _extract_target_paths(instruction: str, cwd: str) -> list[str]:
    paths: list[str] = []
    seen: set[str]   = set()
    for m in _PATH_RE.finditer(instruction):
        p = m.group(1)
        if p not in seen:
            seen.add(p)
            paths.append(p)
    for m in _RELATIVE_PATH_RE.finditer(instruction):
        rel   = m.group(1)
        full  = os.path.join(cwd, rel)
        canon = os.path.normpath(full)
        if canon not in seen:
            seen.add(canon)
            paths.append(canon)
    return [p for p in paths if os.path.isfile(p)]

def _build_path_hint_block(paths: list[str]) -> str:
    if not paths:
        return ""
    lines = [
        "🎯 TARGET FILES — VERIFY THESE EXIST FIRST (before any other action):",
    ]
    for p in paths[:8]:
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                total_lines = f.read().count('\n') + 1
            lines.append(f"  → {p}  ({total_lines} lines)")
        except Exception:
            lines.append(f"  → {p}")
    lines += [
        "RULES:",
        "  1. Work ONLY on the paths listed above — do NOT discover alternatives.",
        "  2. Your FIRST tool call must verify the file exists (wc -l or grep).",
        "  3. All patches must reference the EXACT absolute path shown above.",
        "  4. If a path is missing, report it immediately as clarification_needed.",
    ]
    return "\n".join(lines)


# ==============================================================================
# 🏗️ PROJECT ROOT SCOPER (kept for completeness)
# ==============================================================================
_PROJECT_MARKERS = {
    'package.json', 'pyproject.toml', 'setup.py', 'Cargo.toml',
    'go.mod', 'Makefile', 'tsconfig.json', 'vite.config.ts',
}

def _scope_project_root(instruction: str, cwd: str) -> str:
    for m in _PATH_RE.finditer(instruction):
        p = m.group(1)
        d = os.path.dirname(p)
        candidate = d
        for _ in range(8):
            if any(os.path.exists(os.path.join(candidate, marker)) for marker in _PROJECT_MARKERS):
                return candidate
            parent = os.path.dirname(candidate)
            if parent == candidate:
                break
            candidate = parent
    _mono_re = re.compile(r'\b(apps?|packages?|services?|modules?|libs?)[/\\]([\w.\-]+)', re.IGNORECASE)
    for m in _mono_re.finditer(instruction):
        candidate = os.path.normpath(os.path.join(cwd, m.group(0)))
        if os.path.isdir(candidate):
            return candidate
        for root, dirs, _ in os.walk(cwd):
            dirs[:] = [d for d in dirs if d not in {'.git', 'node_modules', '__pycache__', 'dist', 'build', '.next', 'venv', '.venv'}]
            if os.path.basename(root) == m.group(2):
                if any(os.path.exists(os.path.join(root, marker)) for marker in _PROJECT_MARKERS):
                    return root
            if root.replace(cwd, '').count(os.sep) > 5:
                break
    words = re.findall(r'\b[\w.\-]{3,}\b', instruction)
    for word in words:
        candidate = os.path.join(cwd, word)
        if os.path.isdir(candidate) and any(os.path.exists(os.path.join(candidate, m)) for m in _PROJECT_MARKERS):
            return candidate
    return cwd


# ==============================================================================
# 🧠 FRAMEWORK INTEGRATION HELPERS (unchanged)
# ==============================================================================

def _load_active_schemas(instruction: str) -> list:
    try:
        from mental_framework import detect_domains, load_framework
        domains = detect_domains(instruction)
        return [load_framework(d) for d in domains[:3]]
    except Exception:
        return []


def _framework_phase_zero(instruction: str, schemas: list, critic_llm) -> dict:
    result = {"framework_brief": "", "needs_research": False, "search_queries": [], "known_failures": []}
    if not schemas:
        return result
    try:
        from mental_framework import lookup_failure, lookup_research
        from framework_writer import build_worker_brief
    except ImportError:
        return result
    brief = build_worker_brief(schemas, "worker", instruction)
    if brief:
        result["framework_brief"] = brief
    failure_matches = []
    research_matches = []
    for schema in schemas:
        failure_matches.extend(lookup_failure(schema, instruction, top_k=2))
        research_matches.extend(lookup_research(schema, instruction, top_k=2))
    for m in failure_matches[:3]:
        result["known_failures"].append(
            f"  ⚠️  Known issue: {m['error_signature'][:80]}\n"
            f"      Cause: {m['root_cause'][:80]}\n"
            f"      Fix:   {m['fix'][:100]}"
        )
    has_framework_coverage = bool(failure_matches or research_matches)
    if has_framework_coverage:
        result["needs_research"] = False
        return result
    ERROR_HINT_PATTERNS = [r'\berror\b', r'\bfailed\b', r'\bcrash\b', r'\bexception\b',
                           r'\bcannot\b', r'\bdoes not\b', r'\bnot found\b', r'\bfix\b',
                           r'\bdebug\b', r'\bwhy is\b', r'\bbreaking\b']
    if not any(re.search(p, instruction.lower()) for p in ERROR_HINT_PATTERNS):
        return result
    result["needs_research"] = True
    try:
        query_prompt = (
            f"Extract 1-2 web search queries from this task description that would help "
            f"an engineer find documentation or error fixes. Be specific and concise.\n\n"
            f"TASK: {instruction[:400]}\n\n"
            f"Respond ONLY with valid JSON: {{\"queries\": [\"query 1\", \"query 2\"]}}"
        )
        raw = critic_llm.call(messages=[{"role": "user", "content": query_prompt}])
        m   = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            parsed = json.loads(m.group(0))
            result["search_queries"] = parsed.get("queries", [])[:2]
    except Exception:
        result["needs_research"] = False
    return result


def _run_preflight_research(queries, schemas, critic_llm, available_tools, cwd, mission_id):
    if not queries:
        return ""
    raw_results_parts = []
    for query in queries[:2]:
        res, _ = execute_tool("web_search", {"query": query}, cwd, available_tools)
        raw_results_parts.append(f"Query: {query}\n{res[:2000]}")
    combined_raw = "\n\n".join(raw_results_parts)
    extraction_prompt = f"""You are extracting reusable technical knowledge from web search results.
SEARCH RESULTS:
{combined_raw[:5000]}
Extract structured knowledge...
Respond ONLY with valid JSON:
{{
  "problem_summary":  "one sentence describing the problem class",
  "likely_causes":    ["cause 1", "cause 2"],
  "diagnosis_steps":  ["exact command or check 1", "exact command or check 2"],
  "fix_approaches": [
    {{"condition": "when X is true", "fix": "exact command or change", "verify": "how to confirm"}}
  ],
  "key_insight": "the single most important thing to understand",
  "applies_to":  ["technology or framework this applies to"],
  "confidence":  0.7
}}"""
    try:
        raw_extraction = critic_llm.call(messages=[{"role": "user", "content": extraction_prompt}])
        m = re.search(r'\{.*\}', raw_extraction, re.DOTALL)
        knowledge = json.loads(m.group(0)) if m else {}
        if knowledge.get("confidence", 0) >= 0.3 and schemas:
            try:
                from framework_writer import record_research
                knowledge["search_query"] = queries[0] if queries else ""
                for schema in schemas:
                    record_research(schema, knowledge, mission_id, "PreflightResearch")
                    from mental_framework import save_framework
                    save_framework(schema)
            except Exception:
                pass
        if knowledge.get("confidence", 0) >= 0.3:
            lines = [
                "\n🔬 PRE-FLIGHT RESEARCH (apply this before trying anything):",
                f"  Problem: {knowledge.get('problem_summary', '')}",
                f"  Insight: {knowledge.get('key_insight', '')}",
            ]
            if knowledge.get("diagnosis_steps"):
                lines.append("  Diagnose with:")
                for step in knowledge["diagnosis_steps"][:2]:
                    lines.append(f"    → {step}")
            if knowledge.get("fix_approaches"):
                best = knowledge["fix_approaches"][0]
                lines.append(
                    f"  Fix ({best.get('condition', 'when applicable')}):\n"
                    f"    {best.get('fix', '')}\n"
                    f"    Verify: {best.get('verify', '')}"
                )
            lines.append("  (Research stored in framework — future missions will have this immediately)")
            return "\n".join(lines)
    except Exception:
        pass
    return f"\n🔍 PRE-FLIGHT RESEARCH (raw):\n{combined_raw[:1500]}"


def _record_tool_observation(tool_name, tool_result, schemas, mission_id, agent_role, turn, critic_llm=None):
    if not schemas:
        return
    try:
        from framework_writer import record_observation, record_research
        from mental_framework import save_framework
        for schema in schemas:
            record_observation(schema, tool_result, mission_id, agent_role, turn)
        if tool_name in ("web_search", "internet_search") and critic_llm:
            _extract_and_store_search_result(tool_result, schemas, mission_id, agent_role, critic_llm)
        for schema in schemas:
            save_framework(schema)
    except Exception:
        pass


def _extract_and_store_search_result(search_result, schemas, mission_id, agent_role, critic_llm):
    def _background_extract():
        try:
            from framework_writer import record_research
            from mental_framework import save_framework
            extraction_prompt = (
                f"Extract reusable technical knowledge from this web search result.\n\n"
                f"SEARCH RESULT:\n{search_result[:4000]}\n\n"
                f"Respond ONLY with valid JSON:\n"
                f'{{"problem_summary": "str", "likely_causes": ["str"], '
                f'"diagnosis_steps": ["str"], '
                f'"fix_approaches": [{{"condition": "str", "fix": "str", "verify": "str"}}], '
                f'"key_insight": "str", "applies_to": ["str"], "confidence": 0.7}}\n'
                f'If not useful: {{"confidence": 0.0}}'
            )
            raw = critic_llm.call(messages=[{"role": "user", "content": extraction_prompt}])
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            knowledge = json.loads(m.group(0)) if m else {}
            if knowledge.get("confidence", 0) >= 0.3:
                knowledge["search_query"] = search_result[:80]
                for schema in schemas:
                    record_research(schema, knowledge, mission_id, agent_role)
                    save_framework(schema)
        except Exception:
            pass
    threading.Thread(target=_background_extract, daemon=True).start()


# ==============================================================================
# 🛠️ CORE TOOL EXECUTOR — WITH DNA SECURITY LOCK
# ==============================================================================
def execute_tool(tool_name, tool_args, workspace_dir, available_tools, scratch_dir=""):
    global _tool_cache
    normalized = tool_name.lower().replace(" ", "_")
    artifacts  = {"last_command_output": "", "files_written": []}

    # ── 🚨 STRICT DNA SECURITY LOCK 🚨 ──────────────────────────────────────
    allowed_tool_names = [
        getattr(t, 'name', getattr(t, '__name__', str(t))).lower().replace(" ", "_")
        for t in available_tools
    ]

    if normalized not in allowed_tool_names and normalized != "none":
        return (
            f"❌ SECURITY BLOCK: Tool '{tool_name}' is not in your authorized toolset. "
            f"Your authorized tools are: {', '.join(allowed_tool_names) if allowed_tool_names else 'none'}. "
            f"You cannot use '{tool_name}'.",
            artifacts,
        )

    if normalized in ("web_search", "internet_search"):
        try:
            import requests as _req
            from bs4 import BeautifulSoup as _BS
            query       = tool_args.get("query", tool_args.get("raw_query", "")) if isinstance(tool_args, dict) else str(tool_args)
            max_results = int(tool_args.get("max_results", 5)) if isinstance(tool_args, dict) else 5
            if not query:
                return "❌ WEB SEARCH: 'query' argument is required.", artifacts
            headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
            resp    = _req.get("https://html.duckduckgo.com/html/", params={"q": query, "kl": "en-us"}, headers=headers, timeout=15)
            soup    = _BS(resp.text, "html.parser")
            results = []
            for r in soup.select(".result__body")[:max_results]:
                title   = (r.select_one(".result__title") or type('', (), {'get_text': lambda *a, **k: "No title"})()).get_text(strip=True)
                snippet = (r.select_one(".result__snippet") or type('', (), {'get_text': lambda *a, **k: ""})()).get_text(strip=True)
                url     = (r.select_one(".result__url") or type('', (), {'get_text': lambda *a, **k: ""})()).get_text(strip=True)
                results.append(f"**{title}**\nURL: {url}\n{snippet}")
            if not results:
                return f"⚠️ WEB SEARCH: No results for '{query}'.", artifacts
            output = f"🔍 Web Search Results for: '{query}'\n\n" + "\n\n---\n\n".join(results)
            artifacts["last_command_output"] = output[:500]
            return output, artifacts
        except Exception as e:
            return f"❌ WEB SEARCH Error: {e}", artifacts

    if normalized in ("web_fetch", "scrape_webpage"):
        try:
            import requests as _req
            from bs4 import BeautifulSoup as _BS
            url       = tool_args.get("url", "") if isinstance(tool_args, dict) else str(tool_args)
            max_chars = int(tool_args.get("max_chars", 8000)) if isinstance(tool_args, dict) else 8000
            if not url:
                return "❌ WEB FETCH: 'url' argument is required.", artifacts
            if not url.startswith("http"):
                url = "https://" + url
            headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
            resp    = _req.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            soup    = _BS(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
                tag.decompose()
            main = (soup.find("main") or soup.find("article") or soup.find(id="content")
                    or soup.find(class_="content") or soup.find("body") or soup)
            text = re.sub(r'\n{3,}', '\n\n', main.get_text(separator="\n", strip=True))
            output = text[:max_chars] + (f"\n\n[⚠️ Truncated at {max_chars} chars.]" if len(text) > max_chars else "")
            result = f"🌐 Web Fetch: {url}\n\n{output}"
            artifacts["last_command_output"] = result[:500]
            return result, artifacts
        except Exception as e:
            return f"❌ WEB FETCH Error ({url}): {e}", artifacts

    if normalized == "system_terminal":
        if WORKER_LOCK.is_held():
            _mutating_patterns = [
                r'\bsed\s+-i\b', r'\becho\s+.+>', r'\bmv\s+', r'\bcp\s+',
                r'\brm\s+', r'\bmkdir\b', r'\bnpm\s+(?:install|run)\b',
                r'\bpip\s+install\b', r'\bgit\s+(?:commit|push|reset)\b',
                r'\bpatch\b', r'\bcat\s+>.+<<',
            ]
            cmd_str = str(tool_args.get("command", tool_args) if isinstance(tool_args, dict) else tool_args)
            is_mutating = any(re.search(p, cmd_str) for p in _mutating_patterns)
            if is_mutating:
                holders = WORKER_LOCK.held_by()
                return (
                    f"🔒 ASYNC LOCK: Terminal mutation blocked — {holders} currently writing. "
                    f"Read-only commands (ls, cat, grep, wc -l) are still allowed. "
                    f"Wait for the worker to finish before running: {cmd_str[:80]}",
                    artifacts,
                )
        try:
            command = tool_args.get("command", tool_args) if isinstance(tool_args, dict) else str(tool_args)
            result  = subprocess.getoutput(f"cd {workspace_dir} && {command}")
            final   = result.strip() if result.strip() else (
                "✅ Command executed successfully (no output). "
                "NOTE: For compilers like tsc/rustc/go — zero output means zero errors."
            )
            if len(final) > 10000:
                final = final[:10000] + "\n\n[⚠️ TRUNCATED at 10,000 chars.]"
            for k in [k for k in _tool_cache if k.startswith("file_read::")]:
                del _tool_cache[k]
            artifacts["last_command_output"] = _smart_extract(final, 500)
            return final, artifacts
        except Exception as e:
            return f"❌ Terminal Error: {e}", artifacts

    if normalized == "file_manager":
        try:
            args     = tool_args if isinstance(tool_args, dict) else json.loads(str(tool_args))
            file_id  = args.get("filename", args.get("path", args.get("file", "")))
            if not file_id:
                return "❌ TOOL ERROR: Missing file identifier. Pass {'path': 'your_file.py', 'action': 'read'}", artifacts
            filepath = file_id if os.path.isabs(file_id) else os.path.join(workspace_dir, file_id)
            action   = args.get("action", "read")

            if action == "read":
                cache_key = f"file_read::{filepath}"
                if cache_key in _tool_cache:
                    return _tool_cache[cache_key] + "\n[📦 CACHED]", artifacts
                if not os.path.exists(filepath):
                    _fname   = os.path.basename(filepath)
                    _similar = subprocess.getoutput(
                        f"find {workspace_dir} -name '{_fname}' ! -path '*/node_modules/*' "
                        f"! -path '*/.git/*' 2>/dev/null | head -5"
                    ).strip()
                    _hint = f"\n💡 Similar files found:\n{_similar}" if _similar else ""
                    return f"❌ FILE NOT FOUND: '{filepath}'.{_hint}", artifacts
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                result = (content[:10000] + f"\n\n[⚠️ TRUNCATED: {len(content):,} chars total]"
                          if len(content) > 10000 else content)
                _tool_cache[cache_key] = result
                return result, artifacts

            elif action == "write":
                content = args.get("content", "")
                if not content:
                    return "❌ TOOL ERROR: Missing 'content' for write action.", artifacts
                TEMP_PREFIXES = ('analyze_', 'plan_', 'debug_', 'check_', 'fix_helper',
                                 'temp_', 'helper_', 'inspect_', 'report_', 'diag_', 'script_')
                basename = os.path.basename(filepath)
                if (scratch_dir and basename.endswith('.py')
                        and any(basename.startswith(p) for p in TEMP_PREFIXES)
                        and not filepath.startswith(scratch_dir)):
                    filepath = os.path.join(scratch_dir, basename)
                    os.makedirs(scratch_dir, exist_ok=True)
                os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(content)
                _tool_cache.pop(f"file_read::{filepath}", None)
                artifacts["files_written"].append(filepath)
                return f"✅ Wrote {len(content):,} chars to '{filepath}'", artifacts

            elif action == "patch":
                raw_payload = args.get("content", args.get("payload", ""))
                try:
                    payload  = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
                    old_text = payload.get("old", "")
                    new_text = payload.get("new", "")
                except (json.JSONDecodeError, AttributeError):
                    return '❌ PATCH ERROR: content must be JSON: {"old": "exact text", "new": "replacement"}', artifacts
                if not os.path.exists(filepath):
                    _fname   = os.path.basename(filepath)
                    _similar = subprocess.getoutput(
                        f"find {workspace_dir} -name '{_fname}' ! -path '*/node_modules/*' "
                        f"! -path '*/.git/*' 2>/dev/null | head -5"
                    ).strip()
                    _hint = f"\n💡 Did you mean:\n{_similar}" if _similar else ""
                    return f"❌ FILE NOT FOUND: '{filepath}'{_hint}", artifacts
                with open(filepath, 'r', encoding='utf-8') as f:
                    original = f.read()
                if old_text not in original:
                    _lines = original.splitlines()
                    _preview = "\n".join(f"{i+1:>5}: {l}" for i, l in enumerate(_lines[:30]))
                    return (
                        f"❌ PATCH FAILED: Target string not found in '{filepath}'.\n"
                        f"Read the file first to verify exact text.\n"
                        f"Target was:\n{old_text[:200]}\n\n"
                        f"File begins with:\n{_preview}"
                    ), artifacts
                patched = original.replace(old_text, new_text, 1)
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(patched)
                _tool_cache.pop(f"file_read::{filepath}", None)
                artifacts["files_written"].append(filepath)
                return f"✅ PATCH applied to '{filepath}'. {len(original):,} → {len(patched):,} chars.", artifacts

            elif action == "append":
                content = args.get("content", "")
                os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
                with open(filepath, 'a', encoding='utf-8') as f:
                    f.write(content)
                _tool_cache.pop(f"file_read::{filepath}", None)
                artifacts["files_written"].append(filepath)
                return f"✅ Appended {len(content):,} chars to '{filepath}'", artifacts

            else:
                return f"❌ Unknown action '{action}'. Valid: read, write, patch, append.", artifacts
        except Exception as e:
            return f"❌ File Manager Error: {e}", artifacts

    for tool in available_tools:
        t_name = getattr(tool, 'name', getattr(tool, '__name__', str(tool)))
        if normalized == t_name.lower().replace(" ", "_"):
            try:
                if hasattr(tool, 'invoke'):    res = tool.invoke(tool_args)
                elif hasattr(tool, '_run'):     res = tool._run(**tool_args) if isinstance(tool_args, dict) else tool._run(tool_args)
                elif callable(tool):            res = tool(**tool_args) if isinstance(tool_args, dict) else tool(tool_args)
                else:                           return f"❌ Tool '{tool_name}' not executable.", artifacts
                res_str = str(res)[:10000] + ("\n\n[⚠️ TRUNCATED]" if len(str(res)) > 10000 else "")
                artifacts["last_command_output"] = res_str[:500]
                return res_str, artifacts
            except Exception as e:
                return f"❌ Tool Execution Error ({tool_name}): {e}", artifacts

    return f"❌ Tool '{tool_name}' not recognized. Use underscores: 'system_terminal', 'file_manager'.", artifacts


# ==============================================================================
# 🌍 ENVIRONMENT CONTEXT BUILDER (kept but not used)
# ==============================================================================
def _build_environment_context(cwd, instruction, scoped_root=""):
    workspace_dir = os.path.join(cwd, "agent_workspace")
    os.makedirs(workspace_dir, exist_ok=True)
    sections = []
    tree_root = scoped_root if scoped_root and os.path.isdir(scoped_root) else cwd
    # ... (original full body is unchanged – omitted for space; you already have it)
    # It returns environment_str, workspace_dir, full_brief
    return "", workspace_dir, ""


# ==============================================================================
# 🔁 LOOP DETECTOR
# ==============================================================================
def _detect_tool_loop(worker_timeline: list, window: int = 3) -> str | None:
    if len(worker_timeline) < window:
        return None
    recent_tools = []
    for entry in worker_timeline[-window:]:
        if "🛠️" in entry:
            try:
                recent_tools.append(entry.split("🛠️")[1].split("(")[0].strip())
            except IndexError:
                pass
    if len(recent_tools) == window and len(set(recent_tools)) == 1:
        return (
            f"⚠️ INFINITE LOOP DETECTED: Called '{recent_tools[0]}' {window}× in a row. "
            f"Switch to a completely different approach."
        )
    return None


# ==============================================================================
# 🔪 STRIP VERIFICATION SECTION FROM INSTRUCTION
# ==============================================================================
def _strip_verification_from_instruction(instruction: str) -> str:
    """
    Remove any block that starts with 'Verification:' (case-insensitive)
    and all indented lines that belong to it, until the next unindented
    section heading. Also remove standalone verification commands.
    """
    lines = instruction.splitlines()
    filtered = []
    skip_verification = False
    for line in lines:
        stripped = line.strip()
        # Start of a verification block
        if stripped.lower().startswith('verification:'):
            skip_verification = True
            continue
        # If we're inside a verification block, check if we hit a new section heading
        if skip_verification:
            # A new section heading is a non‑empty line that is not indented and not a list item or comment
            if stripped and not line.startswith((' ', '\t', '-')) and not stripped.startswith('#'):
                # Check if it looks like a heading (ends with ':', or is all caps, etc.)
                if ':' in stripped or stripped.isupper() or re.match(r'^[A-Z][a-z]+', stripped):
                    skip_verification = False
                    filtered.append(line)   # keep the new section heading
                    continue
                else:
                    # It's a line that belongs to the same block or is a stray – still skip if not clear
                    continue
            else:
                continue   # still inside verification block
        # Not skipping – keep the line
        filtered.append(line)
    # Also remove any line that exactly matches a verification command pattern
    # e.g., `ls -la /path`, `head -50`, `wc -l`
    pattern = re.compile(
        r'^(?:ls\s+-la|head\s+-\d+|wc\s+-l|grep\s+-c|cat\s+)\s+',
        re.IGNORECASE
    )
    cleaned = []
    for line in filtered:
        if pattern.match(line.strip()):
            continue
        cleaned.append(line)
    return '\n'.join(cleaned)


# ==============================================================================
# 🤖 COGNITIVE AGENT WRAPPER – Direct Executor
# ==============================================================================
def cognitive_agent_wrapper(
    agent,
    instruction:     str,
    project_state:   str,
    private_history: str,
    critic_llm,
    logger,
    agent_memory_context=None,
    scratch_dir:     str  = "",
    agent_role:      str  = "",
    shared_state:    dict = None,
) -> tuple[str, dict, dict]:

    # ── Strip verification section from the CEO's plan ─────────────────────
    instruction = _strip_verification_from_instruction(instruction)

    tier     = classify_task(instruction)
    settings = TIER_SETTINGS[tier]
    MAX_TURNS            = settings["max_turns"]
    MAX_ATTEMPTS         = settings["max_attempts"]
    RUN_MENTOR           = False   # disabled
    RUN_SELF_VERIFY      = False   # disabled
    MAX_TIMELINE_ENTRIES = 5

    logger(f"    ↳ 📊 Task tier: {tier} (max {MAX_TURNS} turns, direct executor – no verification)")

    current_instruction = instruction
    past_failures       = []
    safe_res            = ""
    session_artifacts   = {"last_command_output": "", "files_written": [], "tool_outputs": []}
    structured_result   = {
        "status": "fail", "complexity_tier": tier, "files_written": [],
        "errors_encountered": [], "errors_resolved": [], "verified_by": None,
        "turns_used": 0, "primary_technology": "", "fix_summary": "",
        "coaching_tip": "", "clarification_needed": "",
    }

    global _tool_cache
    _tool_cache = {}
    cwd         = os.getcwd()
    mission_id  = f"{agent_role or 'agent'}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # No environment context, no target hints, no framework blocks
    environment_context = ""
    path_hint_block     = ""
    framework_brief_block = ""
    known_failures_block  = ""
    preflight_research_block = ""
    personal_memory_block = ""
    learned_directives_block = ""
    shared_state_block = ""

    # ── Map ONLY authorized tools into the prompt ──────────────────────────
    _CORE_DESCRIPTIONS = {
        "system_terminal": "bash commands. Args: {\"command\": \"...\"}",
        "file_manager": "read|write|patch|append. Args: {\"action\":\"patch\",\"path\":\"...\",\"content\":\"...\"}",
        "ast_inspector": "map|extract|section a .py/.ts/.tsx. Args: {\"path\":\"...\",\"mode\":\"map\",\"target\":\"\"}",
        "web_search": "Web search. Args: {\"query\": \"...\"}",
        "web_fetch": "Fetch URL as text. Args: {\"url\": \"...\"}",
    }

    actual_tools = []
    for t in agent.tools:
        t_name = getattr(t, 'name', getattr(t, '__name__', str(t)))
        t_desc = getattr(t, 'description', getattr(t, '__doc__', ''))

        # Supply a fallback description if it's a core native tool without a docstring
        if not t_desc and t_name in _CORE_DESCRIPTIONS:
            t_desc = _CORE_DESCRIPTIONS[t_name]

        actual_tools.append(f"- '{t_name}': {str(t_desc).split('.')[0][:80]}")

    tools_prompt = "\n".join(actual_tools) if actual_tools else "- 'none': No tools available. Use thought and final_report only."

    audit_path = os.path.join(
        os.path.join(cwd, "agent_workspace"),
        f"audit_{getattr(agent, 'role', 'agent').lower().replace(' ', '_')[:20]}.log",
    )
    os.makedirs(os.path.dirname(audit_path), exist_ok=True)

    system_prompt = f"""You are {getattr(agent, 'role', 'an AI agent')}: {getattr(agent, 'backstory', '')}

⚠️ YOU ARE A DIRECT EXECUTOR. The CEO has already planned everything.
You are FORBIDDEN from:
- Using any tool NOT listed in your personal toolset below.
- Exploring the filesystem (no ls, find, tree, pwd, etc.)
- Checking if files or directories exist (no wc -l, cat, test, stat, etc.)
- Verifying the output after creation (no ls, wc -l, grep, cat, or any read of the files you write)
- Changing the business domain, industry, or any specification given in the task
- Adding, removing, or guessing any requirement not explicitly stated

Your ONLY job: follow the task exactly as given. Use the tools provided.
Do not plan. Do not verify. Just write the output and set is_finished=true with a brief final_report.

TASK:
{instruction}

TOOLS:
{tools_prompt}

Respond on EVERY turn with ONLY this JSON:
{{
    "thought"              : "What I am doing next (brief).",
    "tool_name"            : "exact_tool_name or 'none'",
    "tool_args"            : {{"key": "value"}},
    "is_finished"          : false,
    "final_report"         : "Leave EMPTY until is_finished=true.",
    "clarification_needed" : "Leave EMPTY."
}}"""

    # ── Attempt loop (unchanged, except no mentor/self‑verify) ─────────────
    for attempt in range(1, MAX_ATTEMPTS + 1):
        failure_context = (
            f"\n⚠️ PREVIOUS FAILURES ({len(past_failures)}):\n"
            + "\n".join(f"  Attempt {i+1}: {f}" for i, f in enumerate(past_failures))
            if past_failures else ""
        )

        agent_finished    = False
        turn_count        = 0
        worker_timeline   = []
        last_action_data  = None
        last_tool_result  = None
        command_run_counts: dict = {}

        while not agent_finished and turn_count < MAX_TURNS:
            turn_count += 1
            timestamp  = datetime.datetime.now().strftime("%H:%M:%S")

            display_timeline = (
                [f"[...{len(worker_timeline) - MAX_TIMELINE_ENTRIES} earlier turns pruned...]"]
                + worker_timeline[-MAX_TIMELINE_ENTRIES:]
                if len(worker_timeline) > MAX_TIMELINE_ENTRIES else worker_timeline
            )

            messages = [{"role": "system", "content": system_prompt}]
            if last_action_data and last_tool_result:
                timeline_str = (
                    "📜 TIMELINE:\n" + "\n".join(display_timeline) + "\n\n"
                    if display_timeline else ""
                )
                messages += [
                    {"role": "assistant", "content": json.dumps(last_action_data)},
                    {"role": "user", "content": (
                        f"{timeline_str}TOOL OUTPUT (Turn {turn_count-1}):\n{last_tool_result}\n\n"
                        f"Turn {turn_count}/{MAX_TURNS}. Output next JSON action."
                    )},
                ]
            elif display_timeline:
                messages.append({"role": "user", "content": (
                    f"📜 TIMELINE:\n{chr(10).join(display_timeline)}\n\n"
                    f"Turn {turn_count}/{MAX_TURNS}. TASK: {current_instruction}\nOutput next JSON action."
                )})
            else:
                messages.append({"role": "user", "content": (
                    f"Turn {turn_count}/{MAX_TURNS}. TASK: {current_instruction}\nOutput next JSON action."
                )})

            try:
                loop_warning = _detect_tool_loop(worker_timeline)
                if loop_warning and messages:
                    messages[-1]["content"] += f"\n\n{loop_warning}"

                response   = critic_llm.call(messages=messages)
                json_match = re.search(r'\{.*\}', response.replace("```json", "").replace("```", ""), re.DOTALL)
                if not json_match:
                    last_tool_result = "❌ Response was not valid JSON. Respond ONLY with the JSON format shown."
                    continue

                action_data = json.loads(json_match.group(0))

                # Turn budget enforcement (unchanged)
                tool_call_count = sum(1 for e in worker_timeline if "🛠️" in e)
                warn_threshold  = int(MAX_TURNS * 0.6)
                hard_threshold  = int(MAX_TURNS * 0.8)

                if (tool_call_count >= 3
                        and turn_count >= hard_threshold
                        and not action_data.get("is_finished")):
                    collected = "\n\n".join(
                        f"[Turn {o['turn']} | {o['tool']}]\n{o['output']}"
                        for o in session_artifacts["tool_outputs"][-8:]
                    )
                    safe_res = (
                        f"⚠️ SYNTHESIZED REPORT (hard stop T{turn_count}/{MAX_TURNS})\n\n"
                        f"TASK: {instruction}\n\nDATA:\n{collected[:3000]}"
                    )
                    worker_timeline.append(f"[{timestamp}] 🛑 HARD STOP")
                    agent_finished = True
                    break

                elif (tool_call_count >= 3
                      and turn_count >= warn_threshold
                      and not action_data.get("is_finished")):
                    if action_data.get("tool_name", "none").lower() != "none":
                        last_tool_result = (
                            f"🚫 TOOL BLOCKED T{turn_count}/{MAX_TURNS}: {tool_call_count} tools run already. "
                            f"Set is_finished=true and write final_report NOW from collected data."
                        )
                        worker_timeline.append(f"[{timestamp}] ⏰ TOOL BLOCKED — forcing synthesis")
                        last_action_data = action_data
                        continue

                if action_data.get("is_finished"):
                    clarification = action_data.get("clarification_needed", "").strip()
                    if clarification:
                        safe_res = f"[CLARIFICATION NEEDED] {clarification}"
                        agent_finished = True
                        worker_timeline.append(f"[{timestamp}] ❓ CLARIFICATION: {clarification[:80]}")
                        break

                    draft_report = action_data.get("final_report", "").strip()
                    if not draft_report:
                        last_tool_result = "❌ is_finished=true but final_report is empty. Write your findings now."
                        continue

                    # No self‑verify – accept immediately
                    safe_res = draft_report
                    agent_finished = True
                    break

                # ── Tool execution ────────────────────────────────────
                tool_name = action_data.get("tool_name", "none")
                tool_args = action_data.get("tool_args", {})

                if tool_name and tool_name.lower() != "none":
                    normalized_tool = tool_name.lower().replace(" ", "_")

                    # Report mode blocking (unchanged)
                    if tier == "REPORT" and normalized_tool == "system_terminal":
                        cmd_str_check = str(tool_args.get("command", tool_args)).lower()
                        _REPORT_BLOCKED = [
                            'uvicorn', 'gunicorn', 'npm run dev', 'yarn dev', 'npm start',
                            'vite', 'nodemon', 'next dev', 'flask run', 'nohup ', 'npm install',
                            'yarn install', 'pip install', 'apt install',
                        ]
                        if any(b in cmd_str_check for b in _REPORT_BLOCKED):
                            last_tool_result = (
                                f"🚫 BLOCKED (REPORT MODE): persistent/install commands not allowed.\n"
                                f"Use: npx tsc --noEmit, python3 -c 'import main', npm run build"
                            )
                            worker_timeline.append(f"[{timestamp}] 🚫 REPORT-BLOCKED: {cmd_str_check[:60]}")
                            last_action_data = action_data
                            continue

                    # Block identical terminal commands
                    if normalized_tool == "system_terminal":
                        cmd_str = str(tool_args.get("command", tool_args)).strip()
                        command_run_counts[cmd_str] = command_run_counts.get(cmd_str, 0) + 1
                        if command_run_counts[cmd_str] >= 3:
                            last_tool_result = (
                                f"🚫 BLOCKED after {command_run_counts[cmd_str]}x: `{cmd_str[:120]}`\n"
                                f"Use a DIFFERENT command or paste existing output into final_report."
                            )
                            worker_timeline.append(f"[{timestamp}] 🚫 BLOCKED same cmd×{command_run_counts[cmd_str]}")
                            continue

                    logger(f"    ↳ 🛠️ {tool_name}: {str(tool_args)[:120]}", is_tool=True)

                    tool_result, turn_artifacts = execute_tool(
                        tool_name, tool_args, cwd, agent.tools, scratch_dir
                    )

                    if turn_artifacts.get("last_command_output"):
                        session_artifacts["last_command_output"] = turn_artifacts["last_command_output"]
                    session_artifacts["files_written"].extend(turn_artifacts.get("files_written", []))
                    session_artifacts["tool_outputs"].append({
                        "turn":         turn_count,
                        "tool":         tool_name,
                        "args_summary": str(tool_args).replace('\n', ' ')[:80],
                        "output":       _smart_extract(tool_result, 800),
                    })
                    if len(session_artifacts["tool_outputs"]) > 10:
                        session_artifacts["tool_outputs"] = session_artifacts["tool_outputs"][-10:]

                    # Track errors and resolutions
                    tool_lower = tool_result.lower()
                    if any(p in tool_lower for p in ['error', 'exception', 'failed', 'traceback']):
                        err = (tool_result.splitlines()[0][:120] if tool_result.splitlines() else tool_result[:120])
                        if err not in structured_result["errors_encountered"]:
                            structured_result["errors_encountered"].append(err)
                    elif '✅' in tool_result and structured_result["errors_encountered"]:
                        last_err = structured_result["errors_encountered"][-1]
                        if last_err not in structured_result["errors_resolved"]:
                            structured_result["errors_resolved"].append(last_err)

                    # Audit log
                    with open(audit_path, "a", encoding="utf-8") as f:
                        f.write(
                            f"\n[{timestamp}] > {tool_name}: {str(tool_args)[:200]}\n"
                            f"RESULT:\n{tool_result[:1000]}\n{'-'*40}\n"
                        )

                    if is_fatal_error(tool_result):
                        last_tool_result = f"💀 FATAL: {tool_result[:300]}\nPIVOT immediately."
                        worker_timeline.append(f"[{timestamp}] 💀 FATAL: {tool_result[:80]}")
                        last_action_data = action_data
                        continue

                    short_res = str(tool_result).replace('\n', ' ')[:100]
                    worker_timeline.append(
                        f"[{timestamp}] T{turn_count} | 🛠️ {tool_name}({str(tool_args)[:50]}...) → {short_res}..."
                    )
                    last_action_data = action_data
                    last_tool_result = tool_result
                else:
                    last_tool_result = "❌ You must either use a tool or set is_finished=true."
                    worker_timeline.append(f"[{timestamp}] T{turn_count} | ⚠️ No tool, not finished")

            except Exception as e:
                last_action_data = {"thought": "Crash."}
                last_tool_result = f"❌ WRAPPER CRASH: {e}"
                worker_timeline.append(f"[{timestamp}] T{turn_count} | 💥 CRASH: {e}")

        # End of attempt
        structured_result["turns_used"]    = turn_count
        structured_result["files_written"] = list(set(
            f for f in session_artifacts["files_written"] if not f.startswith("[REDIRECTED]")
        ))

        if not agent_finished:
            structured_result["status"] = "timeout"
            logger(f"    ↳ ⏰ MAX_TURNS ({MAX_TURNS}) exhausted")
            past_failures.append(f"Attempt {attempt}: MAX_TURNS exhausted.")
            current_instruction = (
                f"❌ TIMEOUT (Attempt {attempt}, {MAX_TURNS} turns).\n"
                f"ORIGINAL GOAL: {instruction}\n\n"
                f"DO NOT EXPLORE – write the final output immediately."
            )
            continue

        if safe_res.startswith("[CLARIFICATION NEEDED]"):
            structured_result["status"]               = "clarification"
            structured_result["clarification_needed"] = safe_res[len("[CLARIFICATION NEEDED]"):].strip()
            logger(f"    ↳ ❓ CLARIFICATION REQUESTED: {structured_result['clarification_needed'][:100]}")
            return safe_res, session_artifacts, structured_result

        structured_result["status"] = "success"
        structured_result["primary_technology"] = _infer_technology(instruction, session_artifacts["tool_outputs"])
        return safe_res, session_artifacts, structured_result

    return (
        f"🚨 CRITICAL FAILURE after {MAX_ATTEMPTS} attempts.\n"
        + "\n".join(past_failures),
        session_artifacts,
        structured_result,
    )


# ==============================================================================
# 🔧 HELPERS
# ==============================================================================

def _write_personal_memory(role: str, structured_result: dict, cwd: str) -> None:
    if not role or structured_result.get("status") != "success":
        return
    coaching        = structured_result.get("coaching_tip", "")
    resolved_errors = structured_result.get("errors_resolved", [])
    error_text      = resolved_errors[0][:300] if resolved_errors else "Strategic Execution"
    fix_text        = coaching if coaching else (
        structured_result.get("fix_summary", "")[:300] or "Task completed."
    )
    if error_text == "Strategic Execution" and fix_text == "Task completed.":
        return
    entry = {
        "entry_type":   "behavioral_lesson" if coaching else "code_fix",
        "technology":   structured_result.get("primary_technology", "unknown"),
        "error":        error_text,
        "fix":          fix_text,
        "file_pattern": structured_result["files_written"][0] if structured_result.get("files_written") else None,
        "verified_by":  structured_result.get("verified_by"),
    }
    try:
        from empire_tools import write_agent_memory_async
        write_agent_memory_async(role, entry, cwd)
    except Exception:
        pass


def _infer_technology(instruction: str, tool_outputs: list) -> str:
    text = instruction.lower() + " ".join(o.get("output", "") for o in tool_outputs[-3:]).lower()
    TECH_SIGNALS = [
        ("React",      ['react', 'jsx', 'tsx', 'component']),
        ("TypeScript", ['typescript', 'tsc', '.ts', 'tsconfig']),
        ("FastAPI",    ['fastapi', 'uvicorn', 'pydantic']),
        ("Next.js",    ['next.js', 'nextjs', 'next/router']),
        ("Django",     ['django', 'manage.py', 'wsgi']),
        ("Python",     ['python', '.py', 'def ', 'import ']),
        ("Node.js",    ['node', 'npm', 'package.json']),
        ("Flutter",    ['flutter', 'dart', 'pubspec']),
    ]
    for name, signals in TECH_SIGNALS:
        if any(s in text for s in signals):
            return name
    return "unknown"


def report_agent_wrapper(agent, instruction, critic_llm, logger, cwd="", shared_state=None):
    cwd = cwd or os.getcwd()
    logger(f"    ↳ 📋 REPORT MODE – deterministic gather + single-shot synthesis")
    artifacts = {"last_command_output": "", "files_written": [], "tool_outputs": []}
    structured_result = {
        "status": "success", "complexity_tier": "REPORT", "files_written": [],
        "errors_encountered": [], "errors_resolved": [], "verified_by": None,
        "turns_used": 0, "primary_technology": "", "fix_summary": "",
    }
    collected: dict = {}

    def _read(path):
        try:
            if not os.path.exists(path):
                return None
            with open(path, encoding="utf-8", errors="replace") as f:
                raw = f.read()
            return raw[:6000] + (f"\n...[truncated, {len(raw)} chars]" if len(raw) > 6000 else "")
        except Exception as e:
            return f"[read error: {e}]"

    def _shell(cmd):
        try:
            out = subprocess.getoutput(f"cd {cwd} && {cmd} 2>&1")
            return (out[:3000] + "\n...[truncated]") if len(out) > 3000 else out
        except Exception as e:
            return f"[error: {e}]"

    _report_paths = _extract_target_paths(instruction, cwd)
    if _report_paths:
        for _rp in _report_paths[:3]:
            _c = _read(_rp)
            if _c:
                collected[f"TARGET: {os.path.basename(_rp)}"] = _c

    collected["FILE TREE"] = _shell(
        "find . ! -path '*/node_modules/*' ! -path '*/.git/*' ! -path '*/__pycache__/*' "
        "! -path '*/venv/*' ! -path '*/dist/*' -maxdepth 3 | sort | head -80"
    )
    for fname in ["task.md", "README.md", "SPEC.md"]:
        c = _read(os.path.join(cwd, fname))
        if c:
            collected[fname.upper()] = c
            break
    for fname in ["package.json", "requirements.txt", "pyproject.toml"]:
        c = _read(os.path.join(cwd, fname))
        if c:
            collected[fname.upper()] = c
    for rel in [
        "backend/main.py", "src/main.py", "main.py", "app.py",
        "frontend/src/App.tsx", "src/App.tsx", "vite.config.ts", "tsconfig.json",
    ]:
        c = _read(os.path.join(cwd, rel))
        if c and len(collected) < 12:
            collected[rel] = c
    if os.path.exists(os.path.join(cwd, 'package.json')):
        ts_out = _shell("npx tsc --noEmit 2>&1 | head -40")
        if ts_out.strip():
            collected["TYPESCRIPT ERRORS"] = ts_out
    git_log = _shell("git log --oneline -10 2>/dev/null")
    if git_log and "not a git repository" not in git_log.lower():
        collected["RECENT GIT"] = git_log

    framework_block = ""
    try:
        active_schemas = _load_active_schemas(instruction)
        if active_schemas:
            from framework_writer import build_worker_brief
            brief = build_worker_brief(active_schemas, getattr(agent, 'role', 'analyst'), instruction)
            if brief:
                framework_block = f"\n{brief}\n"
    except Exception:
        pass

    data_sections = "\n\n".join(
        f"{'='*50}\n{label}\n{'='*50}\n{content}"
        for label, content in collected.items()
    )
    prompt = (
        f"You are {getattr(agent, 'role', 'a senior engineer')}.\n"
        f"{getattr(agent, 'backstory', '')}\n\n"
        f"{framework_block}"
        f"MISSION: {instruction}\n\nPROJECT DATA:\n{data_sections}\n\n"
        f"Write a comprehensive technical report: overview, current state, issues found, "
        f"missing features, recommendations (file:line specific), quick wins."
    )

    try:
        report = critic_llm.call(messages=[{"role": "user", "content": prompt}])
        logger(f"    ↳ ✅ Report synthesized ({len(report)} chars)")
        structured_result["primary_technology"] = _infer_technology(instruction, [])
        structured_result["turns_used"]         = 1
        return report, artifacts, structured_result
    except Exception as e:
        structured_result["status"] = "fail"
        fallback = (
            f"# Report\n\nSynthesis failed: {e}\n\n"
            + "\n\n".join(f"### {k}\n```\n{v[:500]}\n```" for k, v in collected.items())
        )
        return fallback, artifacts, structured_result
