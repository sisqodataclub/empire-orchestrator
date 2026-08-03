"""
mental_framework.py
====================
Replaces the flat .md CoT playbook system with a living, layered mental
framework schema — structured the way a human expert actually holds domain
knowledge: fast intuitions, structural facts, decision rules, failure
catalogs, research knowledge, meta-knowledge, and a provenance log that
enables additive (non-destructive) evolution across missions.

Seven-layer schema (stored as JSON, one file per domain):

  Layer 1 — fast_intuitions      : bullet-set of instant pattern recognition
  Layer 2 — structural_knowledge : typed facts (paths, env vars, ports, APIs, codes)
  Layer 3 — decision_rules       : ordered if/then heuristics
  Layer 4 — failure_catalog      : error-signature/anomaly → root-cause + fix
  Layer 5 — meta_knowledge       : domain difficulty, confidence thresholds
  Layer 6 — provenance_log       : per-fact source, mission-id, confidence
  Layer 7 — research_knowledge   : structured knowledge extracted from web research
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from typing import Any


# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

FRAMEWORK_DIR       = os.path.abspath(os.path.join("ai_civilization", "mental_frameworks"))
LEGACY_PLAYBOOK_DIR = os.path.abspath(os.path.join("ai_civilization", "cot_playbooks"))

# Confidence decay: facts older than this many days start losing weight
CONFIDENCE_HALF_LIFE_DAYS = 60

# Minimum confidence to surface a fact to the CEO prompt
MIN_SURFACE_CONFIDENCE = 0.35

# How many items to show per layer in the CEO prompt (keeps token cost low)
MAX_LAYER_1_ITEMS = 6
MAX_LAYER_2_ITEMS = 8
MAX_LAYER_3_ITEMS = 5
MAX_LAYER_4_ITEMS = 4
MAX_LAYER_5_ITEMS = 4
MAX_RESEARCH_ITEMS = 3   # Research entries shown per domain in worker brief


# ──────────────────────────────────────────────────────────────────────────────
# SCHEMA SKELETON
# ──────────────────────────────────────────────────────────────────────────────

def _empty_schema(domain: str) -> dict:
    """Return a blank seven-layer schema for a new domain."""
    return {
        "domain":         domain,
        "schema_version": 3,
        "created_at":     datetime.now().isoformat(),
        "last_updated":   datetime.now().isoformat(),

        "fast_intuitions": [],
        "structural_knowledge": [],
        "decision_rules": [],
        "failure_catalog": [],
        "meta_knowledge": {
            "difficulty":                             "unknown",
            "completeness_threshold_for_architecture": 75,
            "common_ambiguities":                     [],
            "required_credentials":                   [],
            "trust_notes":                            [],
            "confidence":                             0.5,
        },
        "provenance_log": [],
        "research_knowledge": [],
    }


# ──────────────────────────────────────────────────────────────────────────────
# PERSISTENCE
# ──────────────────────────────────────────────────────────────────────────────

def _schema_path(domain: str) -> str:
    os.makedirs(FRAMEWORK_DIR, exist_ok=True)
    slug = re.sub(r"[^\w]", "_", domain.lower().strip())
    return os.path.join(FRAMEWORK_DIR, f"{slug}_framework.json")


def load_framework(domain: str) -> dict:
    """Load the schema for *domain*, creating an empty one if absent."""
    path = _schema_path(domain)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("schema_version", 1) < 2:
                data = _migrate_v1(data, domain)
            if data.get("schema_version", 2) < 3:
                data = _migrate_v2(data, domain)
            return data
        except Exception:
            pass
    return _empty_schema(domain)


def save_framework(schema: dict) -> None:
    """Persist the schema to disk atomically."""
    schema["last_updated"] = datetime.now().isoformat()
    path = _schema_path(schema["domain"])
    tmp  = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(schema, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def _migrate_v1(old: dict, domain: str) -> dict:
    new = _empty_schema(domain)
    raw = old.get("raw_markdown") or old.get("content", "")
    if raw:
        new["fast_intuitions"].append({
            "text":            f"[Migrated from v1] {raw[:400]}",
            "confidence":      0.5,
            "source_missions": ["migration"],
        })
    new["schema_version"] = 2
    return new


def _migrate_v2(old: dict, domain: str) -> dict:
    old.setdefault("research_knowledge", [])
    old["schema_version"] = 3
    return old


# ──────────────────────────────────────────────────────────────────────────────
# CONFIDENCE DECAY
# ──────────────────────────────────────────────────────────────────────────────

def _decay_confidence(base: float, last_verified_iso: str | None) -> float:
    if not last_verified_iso:
        return base
    try:
        last       = datetime.fromisoformat(last_verified_iso)
        age_days   = (datetime.now() - last).days
        half_lives = age_days / CONFIDENCE_HALF_LIFE_DAYS
        return max(MIN_SURFACE_CONFIDENCE - 0.01, base * (0.5 ** half_lives))
    except Exception:
        return base


# ──────────────────────────────────────────────────────────────────────────────
# DOMAIN DETECTION (Agnostic & Manifest-Driven)
# ──────────────────────────────────────────────────────────────────────────────

def detect_domains(mission: str, cwd: str = ".") -> list[str]:
    """
    Return a ranked list of domain slugs.
    1. Highest Priority: The domain_manifest.md in the current working directory.
    2. Fallback: Semantic matching against existing framework files.
    """
    scores: dict[str, int] = {}
    
    # 1. Manifest-Driven Detection (The Constitution)
    manifest_path = os.path.join(cwd, "domain_manifest.md")
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                content = f.read().lower()
                # Simple extraction of the first bolded/headed term or general theme
                if "domain:" in content:
                    extracted = re.search(r"domain:\s*([a-z0-9_]+)", content)
                    if extracted:
                        primary_domain = extracted.group(1).strip()
                        scores[primary_domain] = 100 # Guaranteed selection
        except Exception:
            pass

    # 2. Semantic matching against existing historical frameworks
    mission_lower = mission.lower()
    os.makedirs(FRAMEWORK_DIR, exist_ok=True)
    for fname in os.listdir(FRAMEWORK_DIR):
        if not fname.endswith("_framework.json"):
            continue
        slug     = fname.replace("_framework.json", "")
        readable = slug.replace("_", " ")
        if readable in mission_lower or slug in mission_lower:
            scores[slug] = scores.get(slug, 0) + 10

    if os.path.isdir(LEGACY_PLAYBOOK_DIR):
        for fname in os.listdir(LEGACY_PLAYBOOK_DIR):
            if not fname.endswith("_cot.md"):
                continue
            slug     = fname.replace("_cot.md", "")
            readable = slug.replace("_", " ")
            if any(w in mission_lower for w in readable.split() if len(w) > 3):
                scores[slug] = scores.get(slug, 0) + 3

    # If no domains are detected, default to a generic 'enterprise' domain
    if not scores:
        return ["enterprise_operations"]

    return sorted(scores, key=lambda d: scores[d], reverse=True)


# ──────────────────────────────────────────────────────────────────────────────
# LEGACY MIGRATION
# ──────────────────────────────────────────────────────────────────────────────

def migrate_legacy_playbook(slug: str) -> dict | None:
    md_path = os.path.join(LEGACY_PLAYBOOK_DIR, f"{slug}_cot.md")
    if not os.path.exists(md_path):
        return None
    try:
        with open(md_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return None

    schema = _empty_schema(slug)
    score_match = re.search(r"Score:\s*(\d+)", content)
    if score_match:
        schema["meta_knowledge"]["confidence"] = int(score_match.group(1)) / 100

    bullets = re.findall(r"^[-*]\s+(.+)$", content, re.MULTILINE)
    for b in bullets[:MAX_LAYER_1_ITEMS * 2]:
        schema["fast_intuitions"].append({
            "text":            b.strip(),
            "confidence":      0.6,
            "source_missions": ["legacy_migration"],
        })

    sections = re.split(r"^#+\s+", content, flags=re.MULTILINE)
    for sec in sections[1:]:
        lines   = sec.strip().splitlines()
        if not lines:
            continue
        heading = lines[0].strip()
        body    = "\n".join(lines[1:]).strip()[:300]
        if body:
            schema["structural_knowledge"].append({
                "key":             heading,
                "value":           body,
                "kind":            "pattern",
                "confidence":      0.55,
                "source_missions": ["legacy_migration"],
                "last_verified":   datetime.now().isoformat(),
            })

    schema["provenance_log"].append({
        "mission_id":    "legacy_migration",
        "agent":         "MigrationBot",
        "layer":         "all",
        "fact_hash":     hashlib.md5(content.encode()).hexdigest()[:8],
        "action":        "migrated_from_md",
        "timestamp":     datetime.now().isoformat(),
        "mission_score": schema["meta_knowledge"]["confidence"],
    })

    save_framework(schema)
    return schema


# ──────────────────────────────────────────────────────────────────────────────
# FAILURE CATALOG
# ──────────────────────────────────────────────────────────────────────────────

def _error_hash(error_text: str) -> str:
    normalised = re.sub(r"(line\s+\d+|0x[0-9a-f]+|\d{4,})", "N", error_text.lower())
    normalised = re.sub(r"\s+", " ", normalised).strip()[:200]
    return hashlib.md5(normalised.encode()).hexdigest()[:10]


def lookup_failure(schema: dict, error_text: str, top_k: int = 2) -> list[dict]:
    STOP = {"the", "a", "an", "is", "in", "at", "of", "to", "and", "or",
            "for", "with", "on", "was", "be", "are", "has", "not", "error"}
    err_words = {
        w for w in re.findall(r"[a-zA-Z_]{3,}", error_text.lower())
        if w not in STOP
    }

    candidates = []
    for entry in schema.get("failure_catalog", []):
        if _decay_confidence(entry["confidence"], entry.get("last_seen")) < MIN_SURFACE_CONFIDENCE:
            continue
        sig_words = {
            w for w in re.findall(r"[a-zA-Z_]{3,}", entry["error_signature"].lower())
            if w not in STOP
        }
        overlap = len(err_words & sig_words) / max(len(err_words | sig_words), 1)
        if overlap > 0.15:
            candidates.append((overlap, entry))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in candidates[:top_k]]


def lookup_research(schema: dict, query_text: str, top_k: int = 2) -> list[dict]:
    STOP = {"the", "a", "an", "is", "in", "at", "of", "to", "and", "or",
            "for", "with", "on", "was", "be", "are", "has", "not"}

    query_words = {
        w for w in re.findall(r"[a-zA-Z_]{4,}", query_text.lower())
        if w not in STOP
    }

    if not query_words:
        return []

    candidates = []
    for entry in schema.get("research_knowledge", []):
        entry_text = " ".join([
            entry.get("problem_summary", ""),
            entry.get("key_insight", ""),
            entry.get("search_query", ""),
            " ".join(entry.get("applies_to", [])),
            " ".join(entry.get("likely_causes", [])),
        ])
        entry_words = {
            w for w in re.findall(r"[a-zA-Z_]{4,}", entry_text.lower())
            if w not in STOP
        }
        if not entry_words:
            continue

        overlap = len(query_words & entry_words) / max(len(query_words | entry_words), 1)
        if overlap > 0.10:
            candidates.append((overlap, entry))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in candidates[:top_k]]


# ──────────────────────────────────────────────────────────────────────────────
# PROMPT RENDERING
# ──────────────────────────────────────────────────────────────────────────────

def render_framework_block(domains: list[str], mission: str = "") -> tuple[str, list[str]]:
    log_lines: list[str] = []
    blocks:    list[str] = []

    for domain in domains:
        schema = load_framework(domain)
        if not schema["fast_intuitions"] and not schema["structural_knowledge"]:
            migrated = migrate_legacy_playbook(domain)
            if migrated:
                schema = migrated
                log_lines.append(
                    f"[bold cyan]🔄 Migrated legacy playbook → framework: {domain}[/bold cyan]"
                )

        has_content = (
            schema["fast_intuitions"]
            or schema["structural_knowledge"]
            or schema["research_knowledge"]
            or schema["failure_catalog"]
        )

        if not has_content:
            log_lines.append(
                f"[yellow]🆕 No framework found for domain '{domain}' — analyst research needed[/yellow]"
            )
            blocks.append(
                f"🧠 MENTAL FRAMEWORK: {domain.upper()}\n"
                f"  Status: MISSING\n"
                f"  Action: Dispatch Principal Systems Analyst to research the environment. "
                f"Tell them to read documents and define the business logic/patterns."
            )
            continue

        log_lines.append(f"[bold cyan]🧠 Mental framework loaded: {domain}[/bold cyan]")
        parts = [f"🧠 MENTAL FRAMEWORK: {domain.upper()}"]

        intuitions = sorted(
            [i for i in schema["fast_intuitions"]
             if _decay_confidence(i["confidence"], None) >= MIN_SURFACE_CONFIDENCE],
            key=lambda x: x["confidence"], reverse=True
        )
        if intuitions:
            parts.append("  ⚡ Fast intuitions (activate immediately):")
            for i in intuitions[:MAX_LAYER_1_ITEMS]:
                parts.append(f"    • [{int(i['confidence']*100)}%] {i['text']}")

        structural = sorted(
            [s for s in schema["structural_knowledge"]
             if _decay_confidence(s["confidence"], s.get("last_verified")) >= MIN_SURFACE_CONFIDENCE],
            key=lambda x: x["confidence"], reverse=True
        )
        if structural:
            parts.append("  📐 Structural knowledge:")
            for s in structural[:MAX_LAYER_2_ITEMS]:
                parts.append(f"    • [{s['kind']}] {s['key']}: {s['value'][:120]}")

        rules = sorted(
            [r for r in schema["decision_rules"] if r["confidence"] >= MIN_SURFACE_CONFIDENCE],
            key=lambda x: (-x["priority"], -x["confidence"])
        )
        if rules:
            parts.append("  🔀 Decision rules:")
            for r in rules[:MAX_LAYER_3_ITEMS]:
                parts.append(f"    • IF {r['condition']} → {r['action']}")

        failures = sorted(
            [e for e in schema["failure_catalog"]
             if _decay_confidence(e["confidence"], e.get("last_seen")) >= MIN_SURFACE_CONFIDENCE],
            key=lambda x: (-x["occurrences"], -x["confidence"])
        )
        if failures:
            parts.append("  🚨 Known failure/anomaly patterns (check these first when blocked):")
            for e in failures[:MAX_LAYER_4_ITEMS]:
                parts.append(
                    f"    • ANOMALY: {e['error_signature'][:80]}\n"
                    f"      CAUSE:   {e['root_cause'][:80]}\n"
                    f"      FIX:     {e['fix'][:100]}"
                )

        research = schema.get("research_knowledge", [])
        if research:
            research_sorted = sorted(
                [r for r in research if r.get("confidence", 0) >= MIN_SURFACE_CONFIDENCE],
                key=lambda x: x.get("confidence", 0), reverse=True
            )
            if research_sorted:
                parts.append("  🔬 Research knowledge (verified — do NOT re-search these):")
                for r in research_sorted[:MAX_RESEARCH_ITEMS]:
                    parts.append(f"    • {r.get('problem_summary', '')[:100]}")
                    if r.get("key_insight"):
                        parts.append(f"      Insight: {r['key_insight'][:100]}")
                    if r.get("fix_approaches"):
                        best = r["fix_approaches"][0]
                        parts.append(f"      Fix: {best.get('fix', '')[:100]}")

        meta      = schema.get("meta_knowledge", {})
        threshold = meta.get("completeness_threshold_for_architecture", 75)
        difficulty = meta.get("difficulty", "unknown")
        creds      = meta.get("required_credentials", [])
        trust      = meta.get("trust_notes", [])
        parts.append(
            f"  📊 Meta: difficulty={difficulty} | "
            f"arch-threshold={threshold}% | "
            + (f"creds={creds} | " if creds else "")
            + (f"notes: {'; '.join(trust[:2])}" if trust else "")
        )

        blocks.append("\n".join(parts))

    if not blocks:
        return "", log_lines

    header = (
        "╔══════════════════════════════════════════════════════════════════════╗\n"
        "║  🧠 MENTAL FRAMEWORKS ACTIVATED — living domain knowledge            ║\n"
        "║  Layer 4: check known anomalies BEFORE web search.                   ║\n"
        "║  Layer 7: check research knowledge BEFORE searching the web.         ║\n"
        "║  Only search if NEITHER layer has a match.                           ║\n"
        "╚══════════════════════════════════════════════════════════════════════╝"
    )
    footer = (
        "\n📝 FRAMEWORK DUTY: frameworks grow mid-mission automatically.\n"
        "  Web research results are extracted and stored in Layer 7 permanently.\n"
        "  On FINISHED: evolve_framework() is called to add new structural facts."
    )

    return header + "\n\n" + "\n\n".join(blocks) + footer, log_lines


# ──────────────────────────────────────────────────────────────────────────────
# WORKER BRIEF
# ──────────────────────────────────────────────────────────────────────────────

def build_worker_brief(schemas: list[dict], worker_role: str, instruction: str) -> str:
    if not schemas:
        return ""

    STOP = {"the", "a", "an", "is", "in", "at", "of", "to", "and", "or",
            "for", "with", "on", "was", "be", "are", "has", "not", "error",
            "this", "that", "from", "your", "will", "have", "been"}

    inst_lower = instruction.lower()
    task_words = {
        w for w in re.findall(r"[a-zA-Z_]{4,}", inst_lower)
        if w not in STOP
    }

    brief_parts: list[str] = []

    for schema in schemas:
        domain = schema.get("domain", "unknown")
        domain_parts: list[str] = []

        relevant_failures = []
        for entry in schema.get("failure_catalog", []):
            if _decay_confidence(entry["confidence"], entry.get("last_seen")) < MIN_SURFACE_CONFIDENCE:
                continue
            sig_lower  = entry["error_signature"].lower()
            root_lower = entry["root_cause"].lower()
            entry_words = {
                w for w in re.findall(r"[a-zA-Z_]{4,}", sig_lower + " " + root_lower)
                if w not in STOP
            }
            overlap = len(task_words & entry_words) / max(len(task_words | entry_words), 1)
            if overlap > 0.12:
                relevant_failures.append((overlap, entry))

        if relevant_failures:
            relevant_failures.sort(key=lambda x: x[0], reverse=True)
            domain_parts.append(f"  [{domain}] Known anomalies/failures:")
            for _, e in relevant_failures[:2]:
                domain_parts.append(
                    f"    ⚠️  Anomaly: {e['error_signature'][:70]}\n"
                    f"        Cause:   {e['root_cause'][:70]}\n"
                    f"        Fix:     {e['fix'][:100]}"
                )

        relevant_structural = []
        for s in schema.get("structural_knowledge", []):
            if _decay_confidence(s["confidence"], s.get("last_verified")) < MIN_SURFACE_CONFIDENCE:
                continue
            s_words = {
                w for w in re.findall(r"[a-zA-Z_]{4,}", s["key"].lower() + " " + s["value"].lower())
                if w not in STOP
            }
            overlap = len(task_words & s_words) / max(len(task_words | s_words), 1)
            if overlap > 0.12:
                relevant_structural.append((overlap, s))

        if relevant_structural:
            relevant_structural.sort(key=lambda x: x[0], reverse=True)
            domain_parts.append(f"  [{domain}] Verified facts:")
            for _, s in relevant_structural[:3]:
                domain_parts.append(f"    ✓ {s['key']}: {s['value'][:100]}")

        relevant_research = lookup_research(schema, instruction, top_k=2)
        if relevant_research:
            domain_parts.append(f"  [{domain}] Past research (already verified — apply directly):")
            for entry in relevant_research:
                domain_parts.append(
                    f"    🔬 {entry.get('problem_summary', '')[:80]}"
                )
                if entry.get("key_insight"):
                    domain_parts.append(
                        f"        Insight: {entry['key_insight'][:100]}"
                    )
                if entry.get("diagnosis_steps"):
                    domain_parts.append("        Diagnose:")
                    for step in entry["diagnosis_steps"][:2]:
                        domain_parts.append(f"          → {step[:100]}")
                if entry.get("fix_approaches"):
                    best = entry["fix_approaches"][0]
                    domain_parts.append(
                        f"        Fix ({best.get('condition', 'when applicable')[:50]}):\n"
                        f"          {best.get('fix', '')[:100]}\n"
                        f"          Verify: {best.get('verify', '')[:80]}"
                    )

        if domain_parts:
            brief_parts.extend(domain_parts)

    if not brief_parts:
        return ""

    return (
        "📋 DOMAIN KNOWLEDGE BRIEF — verified patterns from past missions and research:\n"
        + "\n".join(brief_parts)
        + "\n→ Apply the most relevant pattern above before trying anything else.\n"
        + "→ If none match your situation, research it and the system will store what you learn.\n"
    )


# ──────────────────────────────────────────────────────────────────────────────
# FRAMEWORK QUERY
# ──────────────────────────────────────────────────────────────────────────────

def query_on_blocker(schemas: list[dict], blocker_text: str) -> str:
    results: list[str] = []

    for schema in schemas:
        domain = schema.get("domain", "?")

        matches = lookup_failure(schema, blocker_text, top_k=2)
        for m in matches:
            results.append(
                f"[{domain}] PAST ANOMALY MATCH (×{m['occurrences']}):\n"
                f"  Sig:   {m['error_signature'][:100]}\n"
                f"  Cause: {m['root_cause'][:100]}\n"
                f"  Fix:   {m['fix'][:120]}"
            )

        research_matches = lookup_research(schema, blocker_text, top_k=2)
        for r in research_matches:
            fix_str = ""
            if r.get("fix_approaches"):
                fix_str = r["fix_approaches"][0].get("fix", "")[:120]
            results.append(
                f"[{domain}] PAST RESEARCH MATCH:\n"
                f"  Problem: {r.get('problem_summary', '')[:100]}\n"
                f"  Insight: {r.get('key_insight', '')[:100]}\n"
                f"  Fix:     {fix_str}"
            )

    if not results:
        return ""

    return (
        "🔍 FRAMEWORK BLOCKER LOOKUP — check these before searching the web:\n"
        + "\n".join(results)
        + "\n→ If these match your blocker, apply the fix directly.\n"
        + "→ Only run a web search if NONE of these match."
    )
