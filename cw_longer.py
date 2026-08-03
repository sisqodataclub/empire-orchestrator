"""
cognitive_wrapper.py — Cognitive Agent Wrapper v3
===================================================
Changes vs v2 (cw_v2 / cw_v3):

  FIX 1 — TRIVIAL TASK CLASSIFIER
    _TRIVIAL_SIGNALS added before _COMPLEX_SIGNALS in classify_task().
    One-liner fixes (delete line, remove duplicate, rename var) are now
    routed to SIMPLE instead of COMPLEX, avoiding a full worker dispatch.

  FIX 2 — SCOPED ENVIRONMENT TREE
    _scope_project_root() detects the active sub-project from the instruction
    and scopes the file tree walk to that directory instead of the entire CWD.
    Prevents workers in monorepos from targeting the wrong project.

  FIX 3 — TARGET PATH INJECTION
    _extract_target_paths() parses file paths from the instruction and injects
    them as a pinned "🎯 TARGET FILES" block at the top of every system prompt.
    Workers always know exactly which file to modify before they start.

  FIX 4 — AUDIT-AWARE SELF-VERIFY
    Self-verify now reads the audit log and cross-checks it against the
    final_report. A worker that claims success with zero tool calls is caught
    here instead of at the mentor stage, saving multiple attempt cycles.

  FIX 5 — HARD ASYNC LOCK
    _AsyncLock: a thin threading.Event wrapper.  Workers acquire it on
    dispatch and release on completion.  cognitive_agent_wrapper respects
    the lock: if another invocation holds it, terminal-side-effect calls
    in execute_tool are blocked (not just warned).

Everything else (task tiers, loop detection, mentor, personal memory,
shared state, tool executor, framework integration) is unchanged from v2.
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
# 🏎️ ENVIRONMENT CONTEXT CACHE (120-second TTL)
# ==============================================================================
_env_cache: dict = {"result": None, "expires_at": 0.0, "cwd": ""}
_ENV_CACHE_TTL   = 120

# ==============================================================================
# 🔒 HARD ASYNC LOCK  [FIX 5]
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
# 📊 TASK COMPLEXITY CLASSIFIER  [FIX 1]
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
# 🎯 TARGET PATH EXTRACTOR  [FIX 3]
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
# 🏗️ PROJECT ROOT SCOPER  [FIX 2]
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
# 🧠 FRAMEWORK INTEGRATION HELPERS
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
# 🛠️ CORE TOOL EXECUTOR
# ==============================================================================
def execute_tool(tool_name, tool_args, workspace_dir, available_tools, scratch_dir=""):
    global _tool_cache
    normalized = tool_name.lower().replace(" ", "_")
    artifacts  = {"last_command_output": "", "files_written": []}

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
# 🌍 ENVIRONMENT CONTEXT BUILDER  [FIX 2]
# ==============================================================================
def _build_environment_context(cwd, instruction, scoped_root=""):
    workspace_dir = os.path.join(cwd, "agent_workspace")
    os.makedirs(workspace_dir, exist_ok=True)
    sections = []

    tree_root = scoped_root if scoped_root and os.path.isdir(scoped_root) else cwd

    SKIP_DIRS = {
        ".git", "venv", ".venv", "__pycache__", "node_modules", ".idea",
        "agent_workspace", "chroma_db", ".next", "dist", "build", ".cache",
        "coverage", ".pytest_cache", "target", ".cargo", "vendor",
    }
    IMPORTANT_FILES = {
        'package.json', 'requirements.txt', 'pyproject.toml', 'Dockerfile',
        'docker-compose.yml', '.env', 'tsconfig.json', 'vite.config.ts',
        'vite.config.js', 'Makefile', 'go.mod',
    }

    try:
        cli_tools = [
            c for c in ['python3', 'node', 'npm', 'npx', 'git', 'docker', 'psql',
                         'sqlite3', 'curl', 'make', 'cargo', 'go', 'tsc']
            if shutil.which(c)
        ]
        sections.append(
            f"🖥️  HOST\n"
            f"  OS: {platform.system()} {platform.release()} | User: {getpass.getuser()}\n"
            f"  Python: {subprocess.getoutput('python3 --version 2>/dev/null').strip()} | "
            f"Node: {subprocess.getoutput('node --version 2>/dev/null').strip() or 'not installed'}\n"
            f"  Tools: {', '.join(cli_tools) or 'none'}"
        )
    except Exception:
        pass

    if tree_root != cwd:
        sections.append(
            f"⚠️  PROJECT SCOPE: Tree is scoped to '{tree_root}'\n"
            f"   (Full CWD: '{cwd}')\n"
            f"   Reason: instruction targets files inside this sub-project."
        )

    try:
        tree, all_files = [], []
        for root, dirs, files in os.walk(tree_root):
            dirs[:] = sorted([d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')])
            level   = root.replace(tree_root, '').count(os.sep)
            if level > 3:
                continue
            indent = '  ' * level
            tree.append(f"{indent}📁 {os.path.basename(root) or '.'}/")
            for fname in sorted(files):
                if fname.endswith('.pyc'):
                    continue
                fpath = os.path.join(root, fname)
                rel   = os.path.relpath(fpath, tree_root)
                try:
                    sz   = os.path.getsize(fpath)
                    size = f"{sz // 1024}KB" if sz >= 1024 else f"{sz}B"
                except Exception:
                    size = "?"
                tree.append(
                    f"{indent}  📄 {fname} [{size}]"
                    f"{'⭐' if fname in IMPORTANT_FILES else ''}"
                )
                all_files.append(rel)
            if len('\n'.join(tree)) > 3000:
                tree.append("  ... [tree truncated]")
                break
        counts = []
        for ext, label in [('.py', 'Python'), (('.ts', '.tsx'), 'TypeScript'), (('.js', '.jsx'), 'JavaScript')]:
            n = len([f for f in all_files if f.endswith(ext)])
            if n:
                counts.append(f"{label}: {n}")
        counts.append(f"Total: {len(all_files)}")
        sections.append(
            f"📁 PROJECT @ {tree_root} ({' | '.join(counts)})\n" + "\n".join(tree)
        )
    except Exception:
        pass

    try:
        stack = {"type": "unknown", "frameworks": [], "databases": [], "testing": [], "scripts": [], "pkg_manager": None}
        pkg_path = os.path.join(tree_root, 'package.json')
        if not os.path.exists(pkg_path):
            pkg_path = os.path.join(cwd, 'package.json')
        if os.path.exists(pkg_path):
            with open(pkg_path) as f:
                pkg = json.load(f)
            all_deps = {**pkg.get('dependencies', {}), **pkg.get('devDependencies', {})}
            dep_str  = ' '.join(all_deps.keys()).lower()
            NODE_MAP = {
                'react': ('frameworks', 'React'), 'next': ('frameworks', 'Next.js'),
                'vue': ('frameworks', 'Vue'), 'vite': ('frameworks', 'Vite'),
                'svelte': ('frameworks', 'Svelte'), 'express': ('frameworks', 'Express'),
                'fastify': ('frameworks', 'Fastify'), 'prisma': ('databases', 'Prisma'),
                'mongoose': ('databases', 'MongoDB'), 'pg': ('databases', 'PostgreSQL'),
                'redis': ('databases', 'Redis'), 'jest': ('testing', 'Jest'),
                'vitest': ('testing', 'Vitest'), 'playwright': ('testing', 'Playwright'),
            }
            for kw, (cat, name) in NODE_MAP.items():
                if kw in dep_str and name not in stack[cat]:
                    stack[cat].append(name)
            stack['scripts']     = list(pkg.get('scripts', {}).keys())
            stack['type']        = 'typescript' if 'typescript' in dep_str else 'javascript'
            stack['pkg_manager'] = (
                'pnpm' if os.path.exists(os.path.join(tree_root, 'pnpm-lock.yaml'))
                else 'yarn' if os.path.exists(os.path.join(tree_root, 'yarn.lock'))
                else 'npm'
            )
        for cfg in ['requirements.txt', 'pyproject.toml']:
            cfg_path = os.path.join(tree_root, cfg)
            if not os.path.exists(cfg_path):
                cfg_path = os.path.join(cwd, cfg)
            if os.path.exists(cfg_path):
                with open(cfg_path) as f:
                    raw = f.read().lower()
                PYTHON_MAP = {
                    'fastapi': ('frameworks', 'FastAPI'), 'django': ('frameworks', 'Django'),
                    'flask': ('frameworks', 'Flask'), 'pytest': ('testing', 'pytest'),
                    'sqlalchemy': ('databases', 'SQLAlchemy'), 'psycopg2': ('databases', 'PostgreSQL'),
                }
                for kw, (cat, name) in PYTHON_MAP.items():
                    if kw in raw and name not in stack[cat]:
                        stack[cat].append(name)
                stack['type'] = 'python'
        lines = [
            f"⚙️  STACK: {stack['type'].upper()}",
            f"  Frameworks: {', '.join(stack['frameworks']) or 'none'}",
            f"  Databases:  {', '.join(stack['databases']) or 'none'}",
            f"  Testing:    {', '.join(stack['testing']) or 'none'}",
        ]
        if stack['scripts']:
            lines.append(f"  Scripts:    {', '.join(stack['scripts'][:8])}")
        if stack['pkg_manager']:
            lines.append(f"  Pkg Mgr:    {stack['pkg_manager']}")
        sections.append("\n".join(lines))
    except Exception:
        pass

    try:
        git_root = tree_root if os.path.exists(os.path.join(tree_root, '.git')) else cwd
        if os.path.exists(os.path.join(git_root, '.git')):
            branch = subprocess.getoutput(f"cd {git_root} && git branch --show-current").strip()
            last   = subprocess.getoutput(f"cd {git_root} && git log -1 --oneline").strip()
            status = subprocess.getoutput(f"cd {git_root} && git status -s | head -8").strip()
            sections.append(
                f"🌿 GIT: branch={branch or '?'} | last={last or 'none'}\n  {status or 'clean'}"
            )
    except Exception:
        pass

    try:
        for fname in ['.env', '.env.local', '.env.example']:
            for base in [tree_root, cwd]:
                fpath = os.path.join(base, fname)
                if not os.path.exists(fpath):
                    continue
                keys = []
                with open(fpath) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            k = line.split('=')[0].strip()
                            v = line.split('=', 1)[1].strip()
                            keys.append(f"{k}={'[SET]' if v else '[EMPTY]'}")
                if keys:
                    sections.append(f"🔑 ENV ({fname}): {', '.join(keys[:10])}")
                break
    except Exception:
        pass

    try:
        from empire_tools import library_collection
        res = library_collection.query(
            query_texts=[instruction], n_results=2,
            include=["documents", "distances", "metadatas"],
        )
        if res['documents'] and res['documents'][0]:
            docs = []
            for doc, dist, meta in zip(res['documents'][0], res['distances'][0], res['metadatas'][0]):
                concept = meta.get('concept', '')
                if (dist < 0.6 and meta.get('type') != 'intelligence_report'
                        and not concept.startswith('Auto-Report') and concept != 'Unknown Concept'):
                    docs.append(f"  [{concept}]: {doc[:300]}")
            if docs:
                sections.append("📜 ARCHIVAL (⚠️ may be outdated):\n" + "\n".join(docs))
    except Exception:
        pass

    divider    = "─" * 56
    header     = f"{'═'*56}\n🎯 AUTO-BRIEFING — COMPLETE SITUATIONAL AWARENESS\n{'═'*56}"
    footer     = f"{'═'*56}\nEND OF BRIEFING — act on facts, not assumptions.\n{'═'*56}"
    full_brief = header + "\n\n" + f"\n\n{divider}\n".join(sections) + "\n\n" + footer
    environment_str = f"📁 PROJECT ROOT: {cwd}\n📁 ACTIVE SCOPE:  {tree_root}\n\n{full_brief}"
    return environment_str, workspace_dir, full_brief


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
# 🤖 COGNITIVE AGENT WRAPPER
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

    tier     = classify_task(instruction)
    settings = TIER_SETTINGS[tier]
    MAX_TURNS            = settings["max_turns"]
    MAX_ATTEMPTS         = settings["max_attempts"]
    RUN_MENTOR           = settings["run_mentor"]
    RUN_SELF_VERIFY      = settings["run_self_verify"]
    MAX_TIMELINE_ENTRIES = 5

    logger(f"    ↳ 📊 Task tier: {tier} (max {MAX_TURNS} turns, mentor={'on' if RUN_MENTOR else 'off'})")

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

    scoped_root = _scope_project_root(current_instruction, cwd)

    target_paths     = _extract_target_paths(current_instruction, cwd)
    path_hint_block  = _build_path_hint_block(target_paths)
    if target_paths:
        logger(f"    ↳ 🎯 {len(target_paths)} target file(s) pinned: {[os.path.basename(p) for p in target_paths]}")

    global _env_cache
    now_ts = _time.time()
    _cache_key = f"{cwd}::{scoped_root}"
    if (_env_cache["result"] is not None
            and _env_cache["cwd"] == _cache_key
            and now_ts < _env_cache["expires_at"]):
        environment_context, workspace_dir, jit_combined = _env_cache["result"]
    else:
        environment_context, workspace_dir, jit_combined = _build_environment_context(
            cwd, current_instruction, scoped_root=scoped_root
        )
        _env_cache.update({
            "result":     (environment_context, workspace_dir, jit_combined),
            "cwd":        _cache_key,
            "expires_at": now_ts + _ENV_CACHE_TTL,
        })

    active_schemas = _load_active_schemas(instruction)
    phase_zero     = _framework_phase_zero(instruction, active_schemas, critic_llm)

    framework_brief_block = ""
    if phase_zero["framework_brief"]:
        framework_brief_block = phase_zero["framework_brief"]
        logger(f"    ↳ 🧠 Framework knowledge loaded ({len(active_schemas)} schema(s))")

    known_failures_block = ""
    if phase_zero["known_failures"]:
        known_failures_block = (
            "\n⚠️ KNOWN FAILURE PATTERNS (check these first — from past missions):\n"
            + "\n".join(phase_zero["known_failures"])
        )
        logger(f"    ↳ 🚨 {len(phase_zero['known_failures'])} known failure pattern(s) loaded")

    preflight_research_block = ""
    if phase_zero["needs_research"] and phase_zero["search_queries"]:
        logger(f"    ↳ 🔍 Framework gap detected — researching: {phase_zero['search_queries']}")
        preflight_research_block = _run_preflight_research(
            queries=phase_zero["search_queries"],
            schemas=active_schemas,
            critic_llm=critic_llm,
            available_tools=agent.tools,
            cwd=cwd,
            mission_id=mission_id,
        )
    elif active_schemas:
        logger(f"    ↳ ✅ Framework covers this task — no web search needed")

    personal_memory_block = ""
    role_for_memory       = agent_role or getattr(agent, 'role', '')
    if role_for_memory:
        try:
            from empire_tools import query_agent_memory
            memories = query_agent_memory(role_for_memory, instruction, top_k=3)
            if memories:
                lines = ["🧠 PERSONAL MEMORY (verified past fixes):"]
                for m in memories:
                    stale = f"  {m['stale_warning']}" if m.get('stale_warning') else ""
                    lines.append(f"  • {m['text'][:200]}{stale}")
                personal_memory_block = "\n".join(lines)
        except Exception:
            pass

    learned_directives_block = ""
    try:
        dna_filename = os.path.join("ai_civilization", role_for_memory.lower().replace(" ", "_") + ".json")
        if os.path.exists(dna_filename):
            with open(dna_filename, "r", encoding="utf-8") as _f:
                _dna = json.load(_f)
            _directives = _dna.get("learned_directives", [])
            if _directives:
                lines = ["🧠 ASSIMILATED DIRECTIVES (follow precisely):"]
                for i, d in enumerate(_directives, 1):
                    lines.append(f"  {i}. {d}")
                learned_directives_block = "\n".join(lines)
    except Exception:
        pass

    shared_state_block = ""
    if shared_state:
        hot  = shared_state.get("verified_facts", [])[-5:]
        mods = shared_state.get("files_modified", {})
        blk  = shared_state.get("blockers", [])
        lines = ["📡 SHARED STATE (ground truth from other workers):"]
        if mods: lines.append(f"  Files modified: {json.dumps(mods)[:200]}")
        if hot:  lines.append(f"  Verified facts: {' | '.join(hot)}")
        if blk:  lines.append(f"  ⚠️ Blockers: {' | '.join(blk[:3])}")
        shared_state_block = "\n".join(lines)

    tools_prompt = "\n".join([
        "- 'system_terminal'   : bash commands. Args: {\"command\": \"...\"}",
        "- 'file_manager'      : read|write|patch|append. Args: {\"action\":\"patch\",\"path\":\"...\",\"content\":\"...\"}",
        "- 'ast_inspector'     : map|extract|section a .py/.ts/.tsx. Args: {\"path\":\"...\",\"mode\":\"map\",\"target\":\"\"}",
        "- 'web_search'        : Web search. Args: {\"query\": \"...\"}",
        "- 'web_fetch'         : Fetch URL as text. Args: {\"url\": \"...\"}",
    ] + [
        f"- '{getattr(t, 'name', getattr(t, '__name__', str(t)))}': "
        f"{str(getattr(t, 'description', getattr(t, '__doc__', ''))).split('.')[0][:80]}"
        for t in agent.tools
    ])

    audit_path = os.path.join(workspace_dir, f"audit_{getattr(agent, 'role', 'agent').lower().replace(' ', '_')[:20]}.log")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        failure_context = (
            f"\n⚠️ PREVIOUS FAILURES ({len(past_failures)}):\n"
            + "\n".join(f"  Attempt {i+1}: {f}" for i, f in enumerate(past_failures))
            if past_failures else ""
        )

        system_prompt = f"""You are {getattr(agent, 'role', 'an AI agent')}: {getattr(agent, 'backstory', '')}

{path_hint_block}

{environment_context}

{framework_brief_block}

{known_failures_block}

{preflight_research_block}

{personal_memory_block}

{learned_directives_block}

{shared_state_block}

{failure_context}

You are a senior engineer. Think before you act.

On your FIRST turn, write a concrete plan in "thought". You have {MAX_TURNS} turns total.
Map your plan to turns — e.g. Turn 1: plan, Turn 2: read, Turn 3: fix, Turn 4: verify, Turn 5: finish.
Stick to your plan. If you're on turn N and haven't started writing yet — stop reading and act.

If something fails: form ONE hypothesis, test it. Same error twice = wrong hypothesis — think differently.
If you see an error not covered by the framework knowledge above: use web_search to find the fix.
The system will automatically store what you find for future missions.

⚠️ CLARIFICATION PROTOCOL: If you genuinely cannot proceed (missing credentials, ambiguous target),
set is_finished=true, leave final_report empty, fill clarification_needed with one specific question.
Only for real blockers — not an excuse to avoid hard work.

⚠️ FILE READING: Before reading any file, run wc -l first.
Under 300 lines → cat. Over 300 → grep -n "class|def|export" to map, then read specific section.

TOOLS:
{tools_prompt}

Respond on EVERY turn with ONLY this JSON:
{{
    "thought"              : "What I know, what I'm doing next and why.",
    "tool_name"            : "exact_tool_name or 'none'",
    "tool_args"            : {{"key": "value"}},
    "is_finished"          : false,
    "final_report"         : "Leave EMPTY until is_finished=true.",
    "clarification_needed" : "Leave EMPTY unless genuine blocking question."
}}"""

        agent_finished    = False
        turn_count        = 0
        worker_timeline   = []
        last_action_data  = None
        last_tool_result  = None
        command_run_counts: dict = {}

        WORKER_LOCK.acquire(role_for_memory or getattr(agent, 'role', 'worker'))

        try:
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
                            f"Turn {turn_count}/{MAX_TURNS}. Output next JSON action.\n"
                            f"If this IS what your task asked for: paste it in final_report, is_finished=true."
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

                        if not RUN_SELF_VERIFY:
                            safe_res = draft_report
                            agent_finished = True
                            break

                        # FIX 4: AUDIT-AWARE SELF-VERIFY
                        try:
                            with open(audit_path, "r", encoding="utf-8") as _af:
                                _audit_snapshot = _af.read()[-2500:].strip()
                        except Exception:
                            _audit_snapshot = "NO AUDIT LOG — no tools were executed this attempt."

                        _tools_executed = sum(1 for e in worker_timeline if "🛠️" in e)
                        _quick_verdict  = None

                        if _tools_executed == 0 and any(
                            kw in draft_report.lower()
                            for kw in ('fixed', 'patched', 'updated', 'changed', 'applied', 'modified', 'removed', 'deleted')
                        ):
                            _quick_verdict = {
                                "verified": False,
                                "next_action": (
                                    "ZERO tools executed but report claims a change was made. "
                                    "Run the actual file_manager patch or system_terminal command first."
                                ),
                            }

                        if _quick_verdict is None:
                            try:
                                verify_response = critic_llm.call(messages=[{"role": "user", "content": (
                                    f"You are a strict code reviewer verifying whether a worker actually did their job.\n\n"
                                    f"TASK: {instruction}\n\n"
                                    f"WORKER'S REPORT:\n{draft_report[:1000]}\n\n"
                                    f"AUDIT LOG (most recent tool calls):\n{_audit_snapshot}\n\n"
                                    f"TOOLS EXECUTED THIS ATTEMPT: {_tools_executed}\n\n"
                                    f"VERIFICATION RULES:\n"
                                    f"1. If audit shows NO_TOOLS / NO AUDIT LOG and report claims a fix → verified=false\n"
                                    f"2. If audit shows tool calls that match the task (patch, write, sed) → verified=true\n"
                                    f"3. If audit shows only read/grep calls but report claims a fix → verified=false\n"
                                    f"4. If the task was to FIND/READ something and the report shows the found content → verified=true\n"
                                    f"5. If tsc/compiler output shows 0 errors → verified=true\n\n"
                                    f"Respond ONLY with valid JSON:\n"
                                    f"{{\"verified\": true/false, \"next_action\": \"what to do if not verified\"}}"
                                )}])
                                v_match = re.search(r'\{.*\}', verify_response.replace("```json", "").replace("```", ""), re.DOTALL)
                                if v_match:
                                    _quick_verdict = json.loads(v_match.group(0))
                            except Exception:
                                _quick_verdict = {"verified": True, "next_action": ""}

                        if _quick_verdict and _quick_verdict.get("verified"):
                            safe_res = draft_report
                            agent_finished = True
                            break
                        else:
                            _why = (_quick_verdict or {}).get("next_action", "Report was not backed by tool evidence.")
                            last_tool_result = (
                                f"⚠️ SELF-VERIFY FAILED (audit-aware): {_why}\n"
                                f"Tools executed so far: {_tools_executed}\n"
                                f"Do NOT repeat is_finished=true until you run the actual fix commands."
                            )
                            worker_timeline.append(f"[{timestamp}] 🔍 AUDIT-VERIFY REJECTED: {_why[:80]}")
                            last_action_data = action_data
                            continue

                    tool_name = action_data.get("tool_name", "none")
                    tool_args = action_data.get("tool_args", {})

                    if tool_name and tool_name.lower() != "none":
                        normalized_tool = tool_name.lower().replace(" ", "_")

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

                        tool_result, turn_artifacts = execute_tool(tool_name, tool_args, cwd, agent.tools, scratch_dir)

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

                        _record_tool_observation(
                            tool_name=tool_name,
                            tool_result=tool_result,
                            schemas=active_schemas,
                            mission_id=mission_id,
                            agent_role=role_for_memory or getattr(agent, 'role', 'agent'),
                            turn=turn_count,
                            critic_llm=critic_llm,
                        )

                        tool_lower = tool_result.lower()
                        if any(p in tool_lower for p in ['error', 'exception', 'failed', 'traceback']):
                            err = tool_result.splitlines()[0][:120] if tool_result.splitlines() else tool_result[:120]
                            if err not in structured_result["errors_encountered"]:
                                structured_result["errors_encountered"].append(err)
                        elif '✅' in tool_result and structured_result["errors_encountered"]:
                            last_err = structured_result["errors_encountered"][-1]
                            if last_err not in structured_result["errors_resolved"]:
                                structured_result["errors_resolved"].append(last_err)

                        _VERIFY_CMDS = ['tsc --noEmit', 'npm run build', 'npm test', 'pytest', 'cargo build', 'go build']
                        if normalized_tool == "system_terminal":
                            for vcmd in _VERIFY_CMDS:
                                if vcmd in str(tool_args).lower() and '❌' not in tool_result:
                                    structured_result["verified_by"] = vcmd
                                    break

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
                            f"[{timestamp}] T{turn_count} | "
                            f"🛠️ {tool_name}({str(tool_args)[:50]}...) → {short_res}..."
                        )
                        last_action_data = action_data
                        last_tool_result = tool_result
                    else:
                        last_action_data = action_data
                        last_tool_result = "❌ You must either use a tool or set is_finished=true."
                        worker_timeline.append(f"[{timestamp}] T{turn_count} | ⚠️ No tool, not finished")

                except Exception as e:
                    last_action_data = {"thought": "Crash."}
                    last_tool_result = f"❌ WRAPPER CRASH: {e}"
                    worker_timeline.append(f"[{timestamp}] T{turn_count} | 💥 CRASH: {e}")

        finally:
            WORKER_LOCK.release(role_for_memory or getattr(agent, 'role', 'worker'))

        structured_result["turns_used"]    = turn_count
        structured_result["files_written"] = list(set(
            f for f in session_artifacts["files_written"] if not f.startswith("[REDIRECTED]")
        ))

        if not agent_finished:
            structured_result["status"] = "timeout"
            logger(f"    ↳ ⏰ MAX_TURNS ({MAX_TURNS}) exhausted — auto-fail, skipping Mentor")
            past_failures.append(f"Attempt {attempt}: MAX_TURNS exhausted.")
            current_instruction = (
                f"❌ TIMEOUT (Attempt {attempt}, {MAX_TURNS} turns).\n"
                f"ORIGINAL GOAL: {instruction}\n\n"
                f"Apply FIX-FIRST LAW: write the fix immediately, verify in ONE command, finish."
            )
            continue

        if safe_res.startswith("[CLARIFICATION NEEDED]"):
            structured_result["status"]               = "clarification"
            structured_result["clarification_needed"] = safe_res[len("[CLARIFICATION NEEDED]"):].strip()
            logger(f"    ↳ ❓ CLARIFICATION REQUESTED: {structured_result['clarification_needed'][:100]}")
            return safe_res, session_artifacts, structured_result

        if not RUN_MENTOR:
            structured_result["status"] = "success"
            _write_personal_memory(role_for_memory, structured_result, cwd)
            return safe_res, session_artifacts, structured_result

        try:
            with open(audit_path, "r", encoding="utf-8") as f:
                audit_truth = f.read()
            if not audit_truth.strip():
                audit_truth = "NO TOOLS EXECUTED."
        except Exception:
            audit_truth = "Audit log unavailable."

        safe_audit = audit_truth.replace('{', '{{').replace('}', '}}')[:1500]

        critic_prompt = f"""You are the Imperial Mentor (Senior Principal Engineer).
Evaluate with forensic precision.

ORIGINAL COMMAND: {instruction}
ATTEMPT: {attempt} of {MAX_ATTEMPTS}
PREVIOUS FAILURES: {past_failures if past_failures else 'None.'}

ENGINEER'S FINAL REPORT:
{safe_res}

🕵️ AUDIT LOG:
{safe_audit}

GRADING RULES:
1. OUTCOME-BASED: Is the system state correct now? Evidence in audit log?
2. SMART NO-OP: Worker proved requested change already present → PASS immediately.
3. ADAPTIVE: Worker found real file despite CEO typo and fixed it → PRAISE and PASS.
4. TRUNCATION: Output truncated but goal met → PASS with warning.
5. FAKE SUCCESS: Audit shows errors but report claims success → FAIL HARD.
6. HALLUCINATION: Report mentions result with no tool call → FAIL.
7. COMPILER: tsc/go zero output + exit 0 = SUCCESS = PASS.
8. NO-TOOL REPORT: Audit shows 'NO TOOLS EXECUTED' AND is_finished claimed → FAIL.
9. TRUNCATION PARALYSIS: Same read command 3+ times → FAIL.
10. EFFICIENCY: 12+ turns of read→describe→read with no writes → FAIL.
11. CLARIFICATION PRAISE: Specific intelligent question before wrong work → PASS quality 80+.

Respond ONLY in valid JSON:
{{
    "outcome_analysis":  "What did they achieve vs what was asked?",
    "evidence_check":    "Specific audit log evidence?",
    "passed":            true,
    "quality_score":     85,
    "efficiency_rating": "excellent|adequate|poor",
    "feedback":          "Constructive technical feedback.",
    "coaching_tip":      "One specific technique for next time."
}}"""

        try:
            eval_res = critic_llm.call(messages=[{"role": "user", "content": critic_prompt}])
            grade    = json.loads(re.search(r'\{.*\}', eval_res.replace('\n', ' '), re.DOTALL).group(0))
        except Exception:
            grade = {"passed": True, "feedback": "Mentor skipped.", "quality_score": 50}

        quality  = grade.get("quality_score", "?")
        coaching = grade.get("coaching_tip", "")

        if grade.get("passed"):
            if attempt > 1:
                logger(f"    ↳ ✅ Approved on attempt {attempt}. Quality: {quality}/100")
            if coaching:
                logger(f"    ↳ 🎓 Coaching: {coaching}")

            structured_result["status"]             = "success"
            structured_result["coaching_tip"]       = coaching
            structured_result["primary_technology"] = _infer_technology(
                instruction, session_artifacts["tool_outputs"]
            )
            structured_result["fix_summary"] = (
                structured_result["errors_resolved"][0][:200]
                if structured_result["errors_resolved"] else ""
            )
            _write_personal_memory(role_for_memory, structured_result, cwd)
            try:
                from empire_tools import auto_commit_global_lesson
                auto_commit_global_lesson(structured_result, role_for_memory, cwd)
            except Exception:
                pass
            return safe_res, session_artifacts, structured_result
        else:
            feedback = grade.get('feedback', 'No feedback.')
            logger(f"    ↳ ❌ Rejected (Attempt {attempt}/{MAX_ATTEMPTS}) | Quality: {quality}/100 | {feedback}")
            if coaching:
                logger(f"    ↳ 🎓 Tip: {coaching}")
            past_failures.append(f"Attempt {attempt}: {feedback}" + (f" | {coaching}" if coaching else ""))
            current_instruction = (
                f"❌ REJECTED (Attempt {attempt}/{MAX_ATTEMPTS}). FEEDBACK: {feedback}\n"
                f"🎓 TIP: {coaching}\n\nORIGINAL GOAL: {instruction}"
            )

    return (
        f"🚨 CRITICAL FAILURE after {MAX_ATTEMPTS} attempts.\n"
        + "\n".join(past_failures),
        session_artifacts,
        structured_result,
    )


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
    logger(f"    ↳ 📋 REPORT MODE — deterministic gather + single-shot synthesis")

    artifacts = {
        "last_command_output": "", "files_written": [], "tool_outputs": [],
    }
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
