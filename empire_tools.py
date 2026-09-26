import os
import re
import json
import hashlib
import threading
import subprocess
import chromadb
import numpy as np
import requests
import time
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from pydantic import BaseModel, field_validator
from crewai.tools import tool
from rich import print as rprint
from bs4 import BeautifulSoup

# 🧠 ADVANCED RAG & CRAWL4AI IMPORTS
from rank_bm25 import BM25Okapi
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

# ─── ADDED FOR RAG EMBEDDING ──────────────────────────────────────────────
from sentence_transformers import SentenceTransformer

# ==============================================================================
# 0. HELPER FUNCTIONS
# ==============================================================================
# (pure_duckduckgo_scrape moved to tools/internet_search_tool.py)


# ══════════════════════════════════════════════════════════════════════
# 0.1  LOG CONTEXT — which agent is calling, for per-agent log routing
# ══════════════════════════════════════════════════════════════════════
_log_ctx = threading.local()


def set_log_context(agent_name: str) -> None:
    """Set the calling agent for tool-log routing. Called by agent_loop."""
    _log_ctx.agent_name = agent_name


def get_log_context() -> Optional[str]:
    return getattr(_log_ctx, "agent_name", None)


def clear_log_context() -> None:
    """Optional: clear the context at the end of a turn."""
    if hasattr(_log_ctx, "agent_name"):
        del _log_ctx.agent_name


def log_agent_action(tool_name: str, action_details: str) -> None:
    """
    Log a tool call.
    (unchanged — see original for full docstring)
    """
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    agent_name = get_log_context()

    if agent_name:
        try:
            from orchestration import agents as _agents
            log_dir = _agents.logs_dir(agent_name)
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(log_dir, "tools.log")
            detail_one_line = action_details.replace("\n", " ").strip()[:200]
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{ts}  detail      {tool_name}: {detail_one_line}\n")
        except Exception:
            pass
        return

    try:
        os.makedirs("agent_workspace", exist_ok=True)
        with open("agent_workspace/imperial_audit.log", "a", encoding="utf-8") as f:
            f.write(f"[{ts}] 🛠️ {tool_name}:\n{action_details}\n{'-'*40}\n")
    except Exception:
        pass


# ==============================================================================
# 0.5  SCRATCH DIR — sandboxed per-mission temp file zone
# ==============================================================================
SCRATCH_DIR = os.path.abspath(os.path.join("ai_civilization", "scratch"))
os.makedirs(SCRATCH_DIR, exist_ok=True)


# ==============================================================================
# 1. DATABASE & AI MODEL INITIALIZATION
# ==============================================================================
chroma_client = chromadb.PersistentClient(path="./ai_civilization/chroma_db")

# ─── RAG EMBEDDING FUNCTION ────────────────────────────────────────────────
_embedding_model = None

def _get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
    return _embedding_model


class EmbeddingFunction:
    def name(self) -> str:
        return "all-MiniLM-L6-v2"

    def __call__(self, input):
        model = _get_embedding_model()
        if isinstance(input, str):
            input = [input]
        return model.encode(input, convert_to_numpy=True).tolist()

    def embed_query(self, input):
        model = _get_embedding_model()
        if isinstance(input, str):
            input = [input]
        return model.encode(input, convert_to_numpy=True).tolist()

    def embed_documents(self, input):
        model = _get_embedding_model()
        if isinstance(input, str):
            input = [input]
        return model.encode(input, convert_to_numpy=True).tolist()


ef = EmbeddingFunction()


def _safe_get_or_create_collection(name: str):
    try:
        return chroma_client.get_or_create_collection(
            name=name,
            embedding_function=ef
        )
    except ValueError as e:
        if "embedding function conflict" in str(e).lower():
            chroma_client.delete_collection(name)
            return chroma_client.create_collection(
                name=name,
                embedding_function=ef
            )
        else:
            raise


library_collection = _safe_get_or_create_collection("empire_library")
logs_collection    = _safe_get_or_create_collection("current_mission_logs")
docs_collection    = _safe_get_or_create_collection("empire_docs")


# ==============================================================================
# 1.5  PYDANTIC MEMORY SCHEMAS
# ==============================================================================

class LessonEntry(BaseModel):
    """Schema for a lesson committed to the shared global library (Tier 2)."""
    technology:  str
    error:       str
    fix:         str
    verified_by: Optional[str] = None
    agent_role:  Optional[str] = None

    @field_validator('technology', 'error', 'fix')
    @classmethod
    def not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError('Field cannot be empty')
        return v[:500]


class AgentMemoryEntry(BaseModel):
    """Schema for a personal memory entry committed to per-agent ChromaDB (Tier 1)."""
    entry_type:  str = "fix"
    technology:  str
    error:       str
    fix:         str
    file_pattern: Optional[str] = None
    verified_by:  Optional[str] = None

    @field_validator('entry_type')
    @classmethod
    def valid_type(cls, v: str) -> str:
        return v if v in ("fix", "pattern", "warning") else "fix"

    @field_validator('technology', 'error', 'fix')
    @classmethod
    def not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError('Field cannot be empty')
        return v[:500]


def _log_failed_commit(raw: dict, error: str) -> None:
    log_path = os.path.join("ai_civilization", "failed_commits.jsonl")
    os.makedirs("ai_civilization", exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": datetime.now().isoformat(),
            "error": error,
            "raw": raw
        }) + "\n")


# ==============================================================================
# 1.6  ENV VERSION EXTRACTION
# ==============================================================================

def _extract_env_versions(cwd: str) -> dict:
    versions: dict = {}

    pkg_path = os.path.join(cwd, "package.json")
    if os.path.exists(pkg_path):
        try:
            with open(pkg_path, encoding="utf-8") as f:
                pkg = json.load(f)
            all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            for name, ver in all_deps.items():
                m = re.match(r'[\^~>=]?(\d+)', str(ver).strip())
                if m:
                    safe = re.sub(r'[^a-z0-9_]', '_', name.lower())[:20]
                    versions[f"env_{safe}_major"] = int(m.group(1))
        except Exception:
            pass

    req_path = os.path.join(cwd, "requirements.txt")
    if os.path.exists(req_path):
        try:
            with open(req_path, encoding="utf-8") as f:
                for line in f:
                    m = re.match(r'^([a-zA-Z0-9_-]+)[>=<!~^]+(\d+)', line.strip())
                    if m:
                        safe = re.sub(r'[^a-z0-9_]', '_', m.group(1).lower())[:20]
                        versions[f"env_{safe}_major"] = int(m.group(2))
        except Exception:
            pass

    return versions


# ==============================================================================
# 1.7  PER-AGENT PERSONAL CHROMADB HELPERS (Tier 1 memory)
# ==============================================================================

def _agent_collection_name(role: str) -> str:
    slug = re.sub(r'[^a-z0-9_]', '_', role.lower().strip())[:25].strip('_')
    return f"agent_{slug}"


def get_agent_collection(role: str):
    try:
        name = _agent_collection_name(role)
        return chroma_client.get_or_create_collection(
            name=name,
            embedding_function=ef
        )
    except Exception:
        return None


def query_agent_memory(role: str, query: str, top_k: int = 3) -> list:
    results = []
    try:
        col = get_agent_collection(role)
        if col is None:
            return results
        count = col.count()
        if count == 0:
            return results

        res = col.query(
            query_texts=[query],
            n_results=min(top_k, count),
            include=["documents", "metadatas", "distances"]
        )
        if not res['documents'] or not res['documents'][0]:
            return results

        cwd = os.getcwd()
        current_versions = _extract_env_versions(cwd)
        now = datetime.now()

        for doc, dist, meta in zip(
            res['documents'][0], res['distances'][0], res['metadatas'][0]
        ):
            if dist >= 0.65:
                continue
            if meta.get('trust_score', 1.0) < 0.4:
                continue

            entry = {
                "text":        doc,
                "technology":  meta.get('technology', '?'),
                "verified_by": meta.get('verified_by', None),
                "memory_id":   meta.get('memory_id', ''),
                "stale_warning": None
            }

            date_str = meta.get('date', '')
            if date_str:
                try:
                    entry_date = datetime.strptime(date_str[:15], "%Y%m%d_%H%M%S")
                    age_days   = (now - entry_date).days
                    if age_days > 90:
                        tech_key = re.sub(r'[^a-z0-9_]', '_', entry["technology"].lower())[:20]
                        mem_ver  = meta.get(f"env_{tech_key}_major")
                        cur_ver  = current_versions.get(f"env_{tech_key}_major")
                        if mem_ver and cur_ver and mem_ver != cur_ver:
                            entry["stale_warning"] = (
                                f"⚠️ STALE ({age_days}d old, "
                                f"{entry['technology']} was v{mem_ver}, now v{cur_ver})"
                            )
                        elif age_days > 90:
                            entry["stale_warning"] = f"⚠️ OLD ({age_days}d) — verify before applying"
                except Exception:
                    pass

            results.append(entry)

    except Exception:
        pass
    return results


def write_agent_memory_async(role: str, entry_dict: dict, cwd: str = "") -> None:
    def _write():
        try:
            validated = AgentMemoryEntry(**entry_dict)
        except Exception as e:
            _log_failed_commit(entry_dict, str(e))
            return
        try:
            col = get_agent_collection(role)
            if col is None:
                return
            versions  = _extract_env_versions(cwd or os.getcwd())
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            memory_id = f"mem_{hashlib.md5((role + validated.technology + validated.error).encode()).hexdigest()[:10]}"

            try:
                col.delete(ids=[memory_id])
            except Exception:
                pass

            metadata = {
                "technology":  validated.technology,
                "entry_type":  validated.entry_type,
                "verified_by": validated.verified_by or "",
                "file_pattern": validated.file_pattern or "",
                "date":        timestamp,
                "trust_score": 0.7,
                "memory_id":   memory_id,
                **versions
            }
            text = (
                f"[{validated.entry_type.upper()}] {validated.technology}\n"
                f"ERROR: {validated.error}\n"
                f"FIX:   {validated.fix}\n"
                + (f"VERIFIED: {validated.verified_by}" if validated.verified_by else "")
            )
            col.add(documents=[text], metadatas=[metadata], ids=[memory_id])
        except Exception:
            pass

    threading.Thread(target=_write, daemon=True).start()


def auto_commit_global_lesson(structured_result: dict, agent_role: str, cwd: str = "") -> None:
    if structured_result.get("status") != "success":
        return
    if not structured_result.get("verified_by"):
        return
    if not structured_result.get("errors_resolved"):
        return

    def _commit():
        for error in structured_result.get("errors_resolved", [])[:2]:
            technology = structured_result.get("primary_technology", "unknown")
            fix_desc   = structured_result.get("fix_summary", error)
            raw = {
                "technology":  technology,
                "error":       error[:300],
                "fix":         fix_desc[:300],
                "verified_by": structured_result.get("verified_by"),
                "agent_role":  agent_role
            }
            try:
                validated = LessonEntry(**raw)
            except Exception as e:
                _log_failed_commit(raw, str(e))
                continue
            try:
                versions  = _extract_env_versions(cwd or os.getcwd())
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                concept   = f"{validated.technology}: {validated.error[:60]}"
                lesson_id = f"lesson_{hashlib.md5(concept.encode()).hexdigest()[:10]}"

                existing = library_collection.query(
                    query_texts=[concept], n_results=1,
                    include=["distances", "ids"]
                )
                if (existing['distances'] and existing['distances'][0] and
                        existing['distances'][0][0] < 0.15):
                    try:
                        library_collection.delete(ids=[existing['ids'][0][0]])
                    except Exception:
                        pass

                text = (
                    f"[AUTO-LESSON] {validated.technology}\n"
                    f"ERROR:  {validated.error}\n"
                    f"FIX:    {validated.fix}\n"
                    f"VERIFIED: {validated.verified_by}\n"
                    f"AGENT: {agent_role}"
                )
                metadata = {
                    "concept":     concept,
                    "type":        "lesson",
                    "agent_role":  agent_role,
                    "date":        timestamp,
                    "trust_score": 0.7,
                    "verified_by": validated.verified_by or "",
                    **versions
                }
                library_collection.add(
                    documents=[text],
                    metadatas=[metadata],
                    ids=[lesson_id]
                )
            except Exception:
                pass


# ==============================================================================
# 2. RERANKER MODEL — lazy-loaded
# ==============================================================================
_reranker_model = None

def _get_reranker():
    global _reranker_model
    if _reranker_model is None:
        from sentence_transformers import CrossEncoder as _CrossEncoder
        print("Loading Reranker Model (first use)...")
        _reranker_model = _CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')
    return _reranker_model


class _LazyReranker:
    def predict(self, pairs):
        return _get_reranker().predict(pairs)

reranker_model = _LazyReranker()


# ==============================================================================
# 3. EMPIRE TOOLS
# ==============================================================================
class EmpireTools:

    # ──────────────────────────────────────────────────────────────────────────
    # 💻 SYSTEM TERMINAL
    # ──────────────────────────────────────────────────────────────────────────
    @tool("System Terminal")
    def execute_terminal(command: str):
        """
        Executes unrestricted shell commands. Always use absolute paths.
        Never use interactive commands. Always use auto-confirm flags: -y, --yes, --force.
        NOTE: Zero output from compilers (tsc, rustc, go) means zero errors — do not panic.
        """
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True,
                text=True, encoding='utf-8', errors='replace'
            )
            stdout_str = result.stdout.strip() if result.stdout else ""
            stderr_str = result.stderr.strip() if result.stderr else ""

            output = stdout_str
            if stderr_str:
                output += (
                    f"\n\n[STDERR/ERRORS]:\n{stderr_str}"
                    if output
                    else f"[STDERR/ERRORS]:\n{stderr_str}"
                )

            if "No such file" in stderr_str:
                output += "\n\n[SYSTEM]: Path error. Use 'List Directory' to verify paths."

            final_output = output.strip() if output.strip() else (
                "✅ ok (no output — compilers: zero output = zero errors)"
            )

            log_agent_action("System Terminal", f"CMD:\n{command}\nRESULT:\n{final_output[:500]}")

            if len(final_output) > 10000:
                return (
                    final_output[:10000] +
                    "\n\n[⚠️ TRUNCATED at 10,000 chars. Use grep/head/tail to target sections.]"
                )
            return final_output

        except Exception as e:
            return f"CRITICAL TOOL ERROR: {str(e)}"

    # ──────────────────────────────────────────────────────────────────────────
    # 📂 FILE MANAGER — read / write / patch / append
    # ──────────────────────────────────────────────────────────────────────────
    @tool("File Manager")
    def manage_file(action: str, path: str, content: str = ""):
        """
        Manages files on disk.
        - 'read'   : Read file (up to 10,000 chars with truncation warning).
        - 'write'  : Overwrite entire file. Use ONLY when replacing the whole file.
        - 'patch'  : Surgical find-and-replace. content must be JSON: {"old": "...", "new": "..."}
        - 'append' : Append content to end of file.
        Always use absolute paths. ASSUME THE PATH GIVEN BY THE CEO IS CORRECT. DO NOT run List Directory first unless absolutely necessary.
        """
        try:
            if action == 'read':
                if not os.path.exists(path):
                    return (
                        f"❌ FILE NOT FOUND: '{path}'.\n"
                        f"Use 'List Directory' to verify the path exists."
                    )
                with open(path, 'r', encoding='utf-8') as f:
                    data = f.read()
                log_agent_action("File Manager", f"READ: {path} ({len(data):,} chars)")
                if len(data) > 10000:
                    return (
                        data[:10000] +
                        f"\n\n[⚠️ TRUNCATED: File is {len(data):,} chars. "
                        f"Use grep or Python ast to extract specific sections.]"
                    )
                return data

            elif action == 'write':
                os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)
                log_agent_action("File Manager", f"WROTE: {path} ({len(content):,} chars)")
                return f"✅ wrote {os.path.basename(path)} ({len(content):,}B)"

            elif action == 'patch':
                if not os.path.exists(path):
                    return f"❌ FILE NOT FOUND: '{path}'"
                try:
                    payload  = json.loads(content) if isinstance(content, str) else content
                    old_text = payload.get('old', '')
                    new_text = payload.get('new', '')
                except (json.JSONDecodeError, AttributeError):
                    return "❌ PATCH: content must be JSON {\"old\":\"...\",\"new\":\"...\"}"
                with open(path, 'r', encoding='utf-8') as f:
                    original = f.read()
                if old_text not in original:
                    return (
                        f"❌ PATCH FAILED: target not found in {os.path.basename(path)}.\n"
                        f"Target preview: {old_text[:120]}"
                    )
                patched = original.replace(old_text, new_text, 1)
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(patched)
                log_agent_action("File Manager", f"PATCHED: {path}")
                return f"✅ patched {os.path.basename(path)} ({len(original):,}→{len(patched):,}B)"

            elif action == 'append':
                os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
                with open(path, 'a', encoding='utf-8') as f:
                    f.write(content)
                log_agent_action("File Manager", f"APPENDED: {path} (+{len(content):,} chars)")
                return f"✅ appended {os.path.basename(path)} (+{len(content):,}B)"

            else:
                return f"❌ unknown action '{action}'. valid: read, write, patch, append."

        except Exception as e:
            return f"❌ FILE MANAGER ERROR: {str(e)}"

    # ──────────────────────────────────────────────────────────────────────────
    # 📁 LIST DIRECTORY
    # ──────────────────────────────────────────────────────────────────────────
    @tool("List Directory")
    def list_directory(path: str):
        """
        Returns a clean indented file tree.
        ⚠️ ANTI-RECONNAISSANCE WARNING: Do NOT use this tool just to 'look around'.
        Assume the file paths given to you in your instructions are correct. ONLY use this if a file read explicitly fails.
        Skips node_modules, __pycache__, .git, venv.
        """
        log_agent_action("List Directory", f"PATH: {path}")
        try:
            if not os.path.exists(path):
                return f"❌ Path not found: '{path}'"

            SKIP_DIRS = {
                'node_modules', '__pycache__', '.git', 'venv',
                '.venv', 'dist', 'build', '.next'
            }
            result = []

            for root, dirs, files in os.walk(path):
                dirs[:] = sorted([d for d in dirs if d not in SKIP_DIRS])
                level   = root.replace(path, '').count(os.sep)
                indent  = '  ' * level
                result.append(f"{indent}📁 {os.path.basename(root) or path}/")
                for fname in sorted(files):
                    fpath = os.path.join(root, fname)
                    try:
                        size     = os.path.getsize(fpath)
                        size_str = f"{size:,} B" if size < 1024 else f"{size//1024:,} KB"
                    except Exception:
                        size_str = "?"
                    result.append(f"{indent}  📄 {fname}  [{size_str}]")

                if len('\n'.join(result)) > 6000:
                    result.append(
                        "\n[⚠️ TREE TRUNCATED: Directory is large. Navigate sub-folders individually.]"
                    )
                    break

            return '\n'.join(result)
        except Exception as e:
            return f"❌ LIST DIRECTORY ERROR: {str(e)}"

    # ──────────────────────────────────────────────────────────────────────────
    # 🌐 INTERNET SEARCH — moved to tools/internet_search_tool.py
    # 🔬 AST INSPECTOR   — moved to tools/ast_inspector_tool.py
    # ──────────────────────────────────────────────────────────────────────────

    # ──────────────────────────────────────────────────────────────────────────
    # 🕷️ SCRAPE WEBPAGE (With requests fallback)
    # ──────────────────────────────────────────────────────────────────────────
    @tool("Scrape Webpage")
    def scrape_webpage(url: str, filename: str = "scraped_page.txt"):
        """
        Renders a webpage and saves clean text to a local file.
        Tries Selenium first (JS-heavy sites), falls back to requests+BeautifulSoup.
        """
        log_agent_action("Scrape Webpage", f"URL: {url} | File: {filename}")

        def _parse_html(html: str) -> str:
            soup = BeautifulSoup(html, 'html.parser')
            for junk in soup(["script", "style", "nav", "footer", "header"]):
                junk.extract()
            return soup.get_text(separator='\n', strip=True)

        text = None

        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
            opts = Options()
            opts.add_argument("--headless=new")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            driver = webdriver.Chrome(service=Service('/usr/bin/chromedriver'), options=opts)
            driver.get(url)
            time.sleep(3)
            html = driver.page_source
            driver.quit()
            text = _parse_html(html)
        except Exception as selenium_err:
            log_agent_action("Scrape Webpage", f"Selenium failed ({selenium_err}). Falling back.")

        if not text:
            try:
                resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
                resp.raise_for_status()
                text = _parse_html(resp.text)
            except Exception as req_err:
                return f"❌ Both Selenium and requests failed. requests error: {req_err}"

        filepath = os.path.join(os.getcwd(), filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(text)
        return f"✅ Saved {len(text):,} chars to '{filepath}'."

    # ──────────────────────────────────────────────────────────────────────────
    # 📥 HARVEST DOCUMENTATION
    # ──────────────────────────────────────────────────────────────────────────
    @tool("Harvest Documentation")
    def harvest_documentation(url: str, topic_name: str):
        """
        Uses Crawl4AI to harvest clean Markdown from a URL and stores it in the
        DOCS collection (separate from lessons) for JIT retrieval via 'Query Official Docs'.
        Re-harvesting a URL updates the existing entry (upsert).
        Large docs are safe here — they never pollute lesson queries.
        """
        log_agent_action("Harvest Documentation", f"URL: {url} | Topic: {topic_name}")

        async def run_crawl():
            async with AsyncWebCrawler(config=BrowserConfig(headless=True)) as crawler:
                res = await crawler.arun(
                    url=url, config=CrawlerRunConfig(word_count_threshold=10)
                )
                return res.markdown if res.success else None

        try:
            content = asyncio.run(run_crawl())
            if not content:
                return "❌ Crawl4AI returned empty content. Page may require authentication."

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            doc_id    = f"doc_{hashlib.md5(url.encode()).hexdigest()[:12]}"

            try:
                docs_collection.delete(ids=[doc_id])
            except Exception:
                pass

            docs_collection.add(
                documents=[content],
                metadatas=[{
                    "concept": topic_name,
                    "type":    "MASTER_DOC",
                    "source":  url,
                    "date":    timestamp
                }],
                ids=[doc_id]
            )
            return (
                f"✅ DOCS INDEXED: '{topic_name}' stored in docs collection "
                f"({len(content):,} chars). Query with 'Query Official Docs'."
            )

        except Exception as e:
            return f"❌ Harvest Error: {e}"

    # ──────────────────────────────────────────────────────────────────────────
    # 🧬 SPAWN SPECIALIST
    # ──────────────────────────────────────────────────────────────────────────
    @tool("Spawn Specialist")
    def spawn_agent(role: str, goal: str, backstory: str):
        """
        Recruits a new specialist agent by saving their DNA to the civilization directory.
        The agent becomes available on the next mission.
        """
        log_agent_action("Spawn Specialist", f"Role: {role}")
        filename = role.lower().replace(" ", "_") + ".json"
        path     = os.path.join(os.getcwd(), "ai_civilization", filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        dna = {
            "role":      role,
            "goal":      goal,
            "backstory": backstory,
            "status":    "ACTIVE",
            "created":   datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dna, f, indent=4)
        return f"⚡ RECRUITED: '{role}' — DNA saved to '{path}'"

    # ──────────────────────────────────────────────────────────────────────────
    # 📖 CONSULT MISSION HISTORY
    # ──────────────────────────────────────────────────────────────────────────
    @tool("Consult Mission History")
    def search_mission_logs(query: str):
        """
        RAG search over logs of the CURRENT active mission only.
        Use before repeating a command to check if it was already tried.
        """
        log_agent_action("Consult Mission History", query)
        try:
            results = logs_collection.query(query_texts=[query], n_results=5)
            if not results['documents'][0]:
                return "No matching records in current mission logs."
            return "-- PAST STEPS --\n" + "\n\n".join(results['documents'][0])
        except Exception as e:
            return f"❌ Mission Log Search Error: {e}"

    # ──────────────────────────────────────────────────────────────────────────
    # 💾 COMMIT TO GLOBAL LIBRARY
    # ──────────────────────────────────────────────────────────────────────────
    @tool("Commit to Global Library")
    def commit_to_library(concept: str, detail: str):
        """
        Saves a permanent technical lesson to the Empire Brain for FUTURE missions.
        Use structured format: 'technology | error | fix' for best retrieval.
        Deduplicates similar concepts. Only call this after you have VERIFIED the fix works.
        The lesson is validated against a strict schema — vague entries are rejected silently.
        """
        log_agent_action("Commit to Global Library", f"Concept: {concept}")

        parts = [p.strip() for p in detail.split('|')]
        if len(parts) >= 3:
            raw = {"technology": parts[0], "error": parts[1], "fix": '|'.join(parts[2:])}
        elif len(parts) == 2:
            raw = {"technology": concept, "error": parts[0], "fix": parts[1]}
        else:
            raw = {"technology": concept, "error": "general", "fix": detail}

        try:
            validated = LessonEntry(**raw)
        except Exception as e:
            _log_failed_commit(raw, str(e))
            return f"⚠️ Lesson not saved — schema validation failed: {e}. Use format: 'technology | error description | fix description'"

        try:
            cwd      = os.getcwd()
            versions = _extract_env_versions(cwd)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            concept_key = f"{validated.technology}: {validated.error[:60]}"
            lesson_id   = f"lesson_{hashlib.md5(concept_key.encode()).hexdigest()[:10]}"

            existing = library_collection.query(
                query_texts=[concept_key], n_results=1,
                include=["distances", "ids"]
            )
            if (existing['distances'] and existing['distances'][0] and
                    existing['distances'][0][0] < 0.15):
                try:
                    library_collection.delete(ids=[existing['ids'][0][0]])
                except Exception:
                    pass
                log_agent_action("Commit to Global Library", f"UPDATED existing: {concept_key}")

            text = (
                f"[LESSON] {validated.technology}\n"
                f"ERROR:  {validated.error}\n"
                f"FIX:    {validated.fix}\n"
                + (f"VERIFIED: {validated.verified_by}" if validated.verified_by else "")
            )
            metadata = {
                "concept":     concept_key,
                "type":        "lesson",
                "date":        timestamp,
                "trust_score": 0.7,
                "verified_by": validated.verified_by or "",
                **versions
            }
            library_collection.add(
                documents=[text],
                metadatas=[metadata],
                ids=[lesson_id]
            )
            return f"📚 LESSON SECURED: '{concept_key}' (trust=0.7, versions pinned: {list(versions.keys())[:4]})"

        except Exception as e:
            return f"❌ Library Commit Error: {e}"

    # ──────────────────────────────────────────────────────────────────────────
    # 🔍 SEARCH EMPIRE LIBRARY
    # ──────────────────────────────────────────────────────────────────────────
    @tool("Search Empire Library")
    def search_library(query: str):
        """
        ADVANCED HYBRID SEARCH: Vector Search + BM25 keyword search merged and
        reranked with CrossEncoder for maximum precision.
        Only returns lessons (type='lesson'). Excludes docs and mission reports.
        Results include trust scores and staleness warnings.
        Use for historical technical lessons and institutional knowledge.
        """
        log_agent_action("Search Empire Library", query)
        try:
            all_data = library_collection.get(include=["documents", "metadatas", "ids"])
            if not all_data['documents']:
                return "📚 Library is empty. Use 'Commit to Global Library' to populate it."

            filtered_docs, filtered_metas = [], []
            for doc, meta in zip(all_data['documents'], all_data['metadatas']):
                if meta.get('type') in ('intelligence_report', 'MASTER_DOC', 'documentation'):
                    continue
                if meta.get('trust_score', 1.0) < 0.4:
                    continue
                filtered_docs.append(doc)
                filtered_metas.append(meta)

            if not filtered_docs:
                return "No trusted lessons found in library yet."

            vec_res   = library_collection.query(
                query_texts=[query], n_results=min(10, len(filtered_docs))
            )
            vec_docs  = vec_res['documents'][0]
            vec_metas = vec_res['metadatas'][0]
            vec_pairs = [
                (d, m) for d, m in zip(vec_docs, vec_metas)
                if m.get('type') not in ('intelligence_report', 'MASTER_DOC', 'documentation')
                and m.get('trust_score', 1.0) >= 0.4
            ]

            tokenized = [doc.lower().split() for doc in filtered_docs]
            bm25      = BM25Okapi(tokenized)
            scores    = bm25.get_scores(query.lower().split())
            top_idx   = np.argsort(scores)[::-1][:10]
            bm25_pairs = [(filtered_docs[i], filtered_metas[i]) for i in top_idx]

            seen, combined = set(), []
            for doc, meta in vec_pairs + bm25_pairs:
                key = doc[:100]
                if key not in seen:
                    seen.add(key)
                    combined.append((doc, meta))

            if not combined:
                return "No relevant results found."

            pairs  = [[query, doc] for doc, _ in combined]
            scores = reranker_model.predict(pairs)
            ranked = sorted(
                zip([d for d, _ in combined], [m for _, m in combined], scores),
                key=lambda x: x[2], reverse=True
            )

            cwd              = os.getcwd()
            current_versions = _extract_env_versions(cwd)
            now              = datetime.now()

            output = f"🔍 HYBRID SEARCH: '{query}'\n{'='*50}\n\n"
            for i, (doc, meta, score) in enumerate(ranked[:3], 1):
                concept   = meta.get('concept', 'Unknown')
                date      = meta.get('date', '?')
                trust     = meta.get('trust_score', '?')
                stale_msg = ""

                if date and date != '?':
                    try:
                        entry_date = datetime.strptime(date[:15], "%Y%m%d_%H%M%S")
                        age_days   = (now - entry_date).days
                        if age_days > 90:
                            for vk, vv in current_versions.items():
                                mem_vv = meta.get(vk)
                                if mem_vv and mem_vv != vv:
                                    tech = vk.replace('env_','').replace('_major','')
                                    stale_msg = f"\n  ⚠️ STALE: {tech} was v{mem_vv}, now v{vv} — verify before applying"
                                    break
                            if not stale_msg:
                                stale_msg = f"\n  ⚠️ OLD ({age_days}d) — verify still valid"
                    except Exception:
                        pass

                output += f"📌 [{i}] {concept} (Score: {score:.3f} | trust={trust} | {date}){stale_msg}\n"
                output += f"{doc[:600]}{'...' if len(doc) > 600 else ''}\n"
                output += "-" * 40 + "\n"

            return output

        except Exception as e:
            return f"❌ Hybrid Search Failed: {e}"

    # ──────────────────────────────────────────────────────────────────────────
    # 📚 QUERY OFFICIAL DOCS
    # ──────────────────────────────────────────────────────────────────────────
    @tool("Query Official Docs")
    def query_docs(search_query: str):
        """
        Searches the pre-indexed documentation database (separate from lessons library).
        Contains pages harvested via 'Harvest Documentation' — API syntax, config refs, examples.
        Use BEFORE Internet Search for any framework or library question.
        Much faster than live search, returns structured docs capped at 800 chars per result.
        """
        log_agent_action("Query Official Docs", search_query)
        try:
            count = docs_collection.count()
            if count == 0:
                return (
                    f"📚 No docs indexed yet for: '{search_query}'.\n"
                    f"Use 'Harvest Documentation' to index a URL first, "
                    f"then 'Internet Search' for live results."
                )

            results = docs_collection.query(
                query_texts=[search_query], n_results=min(3, count),
                include=["documents", "metadatas", "distances"]
            )

            if not results['documents'] or not results['documents'][0]:
                return (
                    f"❌ No docs found for: '{search_query}'.\n"
                    f"Try 'Internet Search' or 'Harvest Documentation' to add it first."
                )

            output = f"📚 OFFICIAL DOCS: '{search_query}'\n{'='*40}\n\n"
            for i in range(len(results['documents'][0])):
                meta     = results['metadatas'][0][i]
                doc      = results['documents'][0][i]
                distance = results['distances'][0][i]
                output  += f"📌 SOURCE:    {meta.get('concept', 'Documentation')}\n"
                output  += f"🔗 URL:       {meta.get('source', 'Local Index')}\n"
                output  += f"📊 Relevance: {1 - distance:.2%}\n"
                output  += f"{doc[:800]}{'...' if len(doc) > 800 else ''}\n"
                output  += "-" * 40 + "\n"

            return output

        except Exception as e:
            return f"❌ Doc Query Failed: {e}"

    # ──────────────────────────────────────────────────────────────────────────
    # 🚫 INVALIDATE MEMORY
    # ──────────────────────────────────────────────────────────────────────────
    @tool("Invalidate Memory")
    def invalidate_memory(memory_id: str, reason: str = ""):
        """
        Mark a retrieved memory as untrustworthy after it failed in the current context.
        Use when a personal or global memory was applied and made things worse.
        memory_id is shown in Search Empire Library results.
        This prevents future agents from being misled by the same bad memory.
        """
        log_agent_action("Invalidate Memory", f"ID: {memory_id} | Reason: {reason}")
        invalidated = 0
        try:
            for col in [library_collection, logs_collection]:
                try:
                    res = col.get(ids=[memory_id], include=["documents", "metadatas"])
                    if res['documents']:
                        doc  = res['documents'][0]
                        meta = res['metadatas'][0]
                        meta['trust_score'] = 0.0
                        meta['invalidated_reason'] = reason[:200]
                        col.delete(ids=[memory_id])
                        col.add(documents=[doc], metadatas=[meta], ids=[memory_id])
                        invalidated += 1
                except Exception:
                    pass
        except Exception as e:
            return f"❌ Invalidate Error: {e}"

        if invalidated:
            return f"🚫 Memory '{memory_id}' marked untrusted (trust=0.0). Reason: {reason}"
        return f"⚠️ Memory ID '{memory_id}' not found in library. Check the ID from search results."

    # ──────────────────────────────────────────────────────────────────────────
    # 🆘 CONSULT OVERLORD
    # ──────────────────────────────────────────────────────────────────────────
    @tool("Consult Overlord")
    def consult_overlord(question: str, error_details: str):
        """
        CRITICAL ESCALATION: Pauses this agent and asks the Human Architect for guidance.
        Writes the question to a shared file that the UI polls. Waits up to 5 minutes.
        Only use when genuinely blocked with no other options.
        """
        log_agent_action("Consult Overlord", question)

        from rich.console import Console
        from rich.panel import Panel
        console = Console()
        console.print(Panel(
            f"[bold red]❓ QUESTION:[/bold red] {question}\n\n"
            f"[dim]📄 ERROR:[/dim] {error_details}",
            title="🚨 AGENT ESCALATION — OVERLORD INPUT REQUIRED",
            border_style="red"
        ))

        question_file = "agent_workspace/pending_question.json"
        os.makedirs("agent_workspace", exist_ok=True)
        with open(question_file, "w", encoding="utf-8") as f:
            json.dump({
                "question":  question,
                "error":     error_details,
                "answered":  False,
                "answer":    "",
                "timestamp": datetime.now().isoformat()
            }, f, indent=4)

        start_time = time.time()
        while time.time() - start_time < 300:
            time.sleep(3)
            try:
                with open(question_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("answered"):
                    answer = data.get("answer", "").strip()
                    with open(question_file, "w") as f:
                        json.dump({"answered": False}, f)
                    return (
                        f"THE OVERLORD COMMANDS: {answer}"
                        if answer
                        else "Overlord acknowledged but gave no instruction. Proceed with best judgment."
                    )
            except Exception:
                pass

        return (
            "⏰ Overlord did not respond within 5 minutes. "
            "Proceeding autonomously. Document this decision in the mission log."
        )

    # ──────────────────────────────────────────────────────────────────────────
    # 🕵️ THE HEADHUNTER (JobSpy Integration)
    # ──────────────────────────────────────────────────────────────────────────
    @tool("Harvest Jobs")
    def harvest_jobs(search_term: str, location: str, limit: int = 5, sites: list = None):
        """
        Scrapes job listings from Indeed, LinkedIn, Glassdoor, and ZipRecruiter.
        Returns highly structured data including Job URL, Company, and Description.
        Args:
          search_term: The job title or keyword (e.g., "Python Developer").
          location: City, State, or Country (e.g., "Manchester, UK").
          limit: Max number of jobs to fetch total (keep under 10 for context window).
          sites: List of sites. Defaults to ["indeed", "linkedin"].
        """
        log_agent_action("Harvest Jobs", f"Role: {search_term} | Loc: {location} | Limit: {limit}")

        try:
            from jobspy import scrape_jobs
            import pandas as pd
        except ImportError:
            return "❌ Missing dependencies. Ask the Overlord to run: pip install python-jobspy pandas"

        if not sites:
            sites = ["indeed", "linkedin"]

        try:
            jobs_df = scrape_jobs(
                site_name=sites,
                search_term=search_term,
                location=location,
                results_wanted=limit,
                country_dict_name="UK" if "UK" in location.upper() or "UNITED KINGDOM" in location.upper() else "USA",
                hours_old=72,
            )

            if jobs_df.empty:
                return f"📭 No jobs found for '{search_term}' in '{location}'."

            jobs_data = jobs_df.to_dict(orient="records")

            output = []
            for i, job in enumerate(jobs_data, 1):
                title = job.get('title', 'Unknown Title')
                company = job.get('company', 'Unknown Company')
                site = job.get('site', 'Unknown')
                url = job.get('job_url', 'No URL')
                desc = str(job.get('description', ''))[:500]

                output.append(
                    f"🏢 [{site.upper()}] {title} @ {company}\n"
                    f"🔗 URL: {url}\n"
                    f"📄 DESC: {desc}...\n"
                    f"{'-'*50}"
                )

            return f"✅ HARVEST COMPLETE ({len(jobs_data)} jobs found):\n\n" + "\n".join(output)

        except Exception as e:
            return f"❌ Job Harvest Error: {e}"


##################################################################################
    # ──────────────────────────────────────────────────────────────────────────
    # 🔎 DESCRIBE TOOL — read the docs for any tool by name
    # ──────────────────────────────────────────────────────────────────────────
    @tool("Describe Tool")
    def describe_tool(tool_name: str):
        """
        Returns the full documentation for any tool in the empire — description,
        argument signature, and an example call if the docstring has one.

        Use this when list_empire_tools() shows a tool name you don't recognize.
        Read the docs before calling the tool so you use its arguments correctly.

        Args:
          tool_name: The exact name shown by list_empire_tools (e.g.
                     "search_repositories", "add_task", "harvest_jobs").
        """
        import inspect as _inspect

        log_agent_action("Describe Tool", tool_name)

        key = (tool_name or "").strip().lower().replace(" ", "_")
        if not key:
            return "❌ describe_tool requires a tool name. Call list_empire_tools first."

        tool = None
        try:
            import gm
            tool = gm.TOOL_REGISTRY.get(key)
        except Exception:
            pass

        if tool is None:
            try:
                from tools.list_tools import list_empire_tools as _let  # noqa: F401
                return (
                    f"❌ No tool named '{tool_name}'.\n"
                    f"Call list_empire_tools to see every available tool name."
                )
            except Exception:
                return f"❌ Could not resolve tool '{tool_name}'."

        name = getattr(tool, "name", key)
        desc = (getattr(tool, "description", "") or "(no description)").strip()

        sig = ""
        fn = getattr(tool, "func", None) or getattr(tool, "run", None) or tool
        try:
            sig = str(_inspect.signature(fn))
        except (TypeError, ValueError):
            sig = "(signature unavailable)"

        examples = ""
        if "EXAMPLES:" in desc:
            examples = "\n" + desc[desc.index("EXAMPLES:"):]

        return (
            f"Tool: {name}\n"
            f"Key:  {key}\n"
            f"Signature: {key}{sig}\n"
            f"\n{desc}"
        )


# ==============================================================================
# BACKWARD-COMPAT ast_inspector WRAPPER
#
# The old version was `EmpireTools.inspect_code` plus a module-level
# `ast_inspector(...)` wrapper. Both moved to tools/ast_inspector_tool.py.
# This module re-exports a plain callable under the same name so any code
# doing `from empire_tools import ast_inspector` keeps working.
#
# The @tool-decorated version is attached to EmpireTools below so the
# registry loader still picks it up.
# ==============================================================================
from tools.ast_inspector_tool import ast_inspector as _ast_inspector_tool
EmpireTools.inspect_code = staticmethod(_ast_inspector_tool)


def ast_inspector(path: str, mode: str = "map", target: str = ""):
    """Backward-compat plain callable wrapper around the AST Inspector tool."""
    return _ast_inspector_tool.run(path=path, mode=mode, target=target)


# ==============================================================================
# INBOX TOOLS
# ==============================================================================
from tools.inbox_tools import (
    read_inbox,
    get_new_inbox_messages,
    send_user_message,
    ask_user,
    set_inbox_db,
)

EmpireTools.read_inbox = staticmethod(read_inbox)
EmpireTools.get_new_inbox_messages = staticmethod(get_new_inbox_messages)
EmpireTools.send_user_message = staticmethod(send_user_message)
EmpireTools.ask_user = staticmethod(ask_user)

set_inbox_db = set_inbox_db


# ==============================================================================
# REPL TOOL
# ==============================================================================
from tools.repl_tool import execute_repl

EmpireTools.execute_repl = staticmethod(execute_repl)


# ==============================================================================
# SECRET TOOLS
# ==============================================================================
from tools.secret_tools import (
    set_secret,
    get_secret,
    list_secret_keys,
    delete_secret,
    set_secrets_manager,
)

EmpireTools.set_secret = staticmethod(set_secret)
EmpireTools.get_secret = staticmethod(get_secret)
EmpireTools.list_secret_keys = staticmethod(list_secret_keys)
EmpireTools.delete_secret = staticmethod(delete_secret)

set_secrets_manager = set_secrets_manager


# ==============================================================================
# SCHEDULER TOOLS
# ==============================================================================
from tools.scheduler_tools import (
    add_project,
    list_projects,
    add_task,
    list_tasks,
    complete_task,
    cancel_task,
    set_scheduler_db,
)

EmpireTools.add_project = staticmethod(add_project)
EmpireTools.list_projects = staticmethod(list_projects)
EmpireTools.add_task = staticmethod(add_task)
EmpireTools.list_tasks = staticmethod(list_tasks)
EmpireTools.complete_task = staticmethod(complete_task)
EmpireTools.cancel_task = staticmethod(cancel_task)

set_scheduler_db = set_scheduler_db


# ==============================================================================
# LIST EMPIRE TOOLS
# ==============================================================================
from tools.list_tools import list_empire_tools

EmpireTools.list_empire_tools = staticmethod(list_empire_tools)


# ==============================================================================
# GMAIL TOOLS
# ==============================================================================
from tools.gmail_tools import (
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

EmpireTools.read_latest_emails   = staticmethod(read_latest_emails)
EmpireTools.read_email           = staticmethod(read_email)
EmpireTools.search_emails        = staticmethod(search_emails)
EmpireTools.send_email           = staticmethod(send_email)
EmpireTools.reply_to_email       = staticmethod(reply_to_email)
EmpireTools.forward_email        = staticmethod(forward_email)
EmpireTools.list_gmail_folders   = staticmethod(list_gmail_folders)
EmpireTools.mark_email_read      = staticmethod(mark_email_read)
EmpireTools.mark_email_unread    = staticmethod(mark_email_unread)
EmpireTools.move_email_to_folder = staticmethod(move_email_to_folder)
EmpireTools.download_attachments = staticmethod(download_attachments)
EmpireTools.delete_email         = staticmethod(delete_email)
EmpireTools.unread_count         = staticmethod(unread_count)


# ==============================================================================
# INTERNET SEARCH — moved to tools/internet_search_tool.py
# ==============================================================================
from tools.internet_search_tool import internet_search

EmpireTools.internet_search = staticmethod(internet_search)


# ==============================================================================
# CONTAINER LOG TOOLS — read-only view of other containers' logs.
#
# The host's /var/lib/docker/containers directory is bind-mounted at
# /host_containers (read-only). The tools walk it, parse Docker's json-file
# logs, and expose:
#
#   list_containers      — enumerate accessible containers by name
#   read_container_logs  — fetch recent lines for one container, filterable
#   scan_for_errors      — sweep every container for error-looking lines
#   container_health     — quick health table across all containers
#
# Deliberately read-only: no docker socket, no exec, no restart.
# ==============================================================================
from tools.container_logs_tool import (
    list_containers,
    read_container_logs,
    scan_for_errors,
    container_health,
)

EmpireTools.list_containers     = staticmethod(list_containers)
EmpireTools.read_container_logs = staticmethod(read_container_logs)
EmpireTools.scan_for_errors     = staticmethod(scan_for_errors)
EmpireTools.container_health    = staticmethod(container_health)


# ==============================================================================
# DEPLOY LOG TOOLS — read-only view of deployment logs.
# ==============================================================================
from tools.deploy_logs_tool import (
    list_deploy_logs,
    read_deploy_log,
    scan_deploy_failures,
)

EmpireTools.list_deploy_logs     = staticmethod(list_deploy_logs)
EmpireTools.read_deploy_log      = staticmethod(read_deploy_log)
EmpireTools.scan_deploy_failures = staticmethod(scan_deploy_failures)




# ==============================================================================
# DYNAMIC TOOLS — agent-authored, staged, activated at runtime.
# ==============================================================================
from tools.dynamic_tools_tool import (
    propose_tool,
    list_pending_tools,
    read_pending_tool,
    activate_tool,
    deactivate_tool,
    list_dynamic_tools,
    reject_pending_tool,
)

EmpireTools.propose_tool        = staticmethod(propose_tool)
EmpireTools.list_pending_tools  = staticmethod(list_pending_tools)
EmpireTools.read_pending_tool   = staticmethod(read_pending_tool)
EmpireTools.activate_tool       = staticmethod(activate_tool)
EmpireTools.deactivate_tool     = staticmethod(deactivate_tool)
EmpireTools.list_dynamic_tools  = staticmethod(list_dynamic_tools)
EmpireTools.reject_pending_tool = staticmethod(reject_pending_tool)





# ==============================================================================
# SYSTEM OBSERVABILITY TOOLS
# ==============================================================================
from tools.system_observability_tools import (
    system_status,
    list_agents,
    think,
)

EmpireTools.system_status = staticmethod(system_status)
EmpireTools.list_agents   = staticmethod(list_agents)
EmpireTools.think         = staticmethod(think)
