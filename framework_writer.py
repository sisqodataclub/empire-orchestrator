"""
framework_writer.py
====================
Handles all writes to the mental framework schemas:
  - record_observation()    : mid-mission micro-updates per worker turn
  - record_failure()        : log an error signature + fix into Layer 4
  - record_structural()     : add/update a structural fact in Layer 2
  - record_intuition()      : add/reinforce a fast intuition in Layer 1
  - record_decision_rule()  : add/reinforce a decision rule in Layer 3
  - record_research()       : store structured web research in Layer 7
  - evolve_framework()      : post-mission additive evolution (NOT overwrite)
  - build_from_research()   : Systems Analyst output → bootstrap a new schema

Design principles:
  - DOMAIN-AGNOSTIC. Adapts to Software, Accounting, Scientific, or Legal environments.
  - ADDITIVE, not replacement. Facts are reinforced or contradicted.
  - RESEARCH PERSISTS. Web research results are structured by LLM and stored.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from typing import Any

from mental_framework import (
    _empty_schema,
    _error_hash,
    _decay_confidence,
    load_framework,
    save_framework,
    FRAMEWORK_DIR,
    MIN_SURFACE_CONFIDENCE,
)


# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _fact_hash(text: str) -> str:
    return hashlib.md5(text.strip().lower().encode()).hexdigest()[:10]


def _provenance_entry(
    mission_id: str,
    agent: str,
    layer: str,
    fact_hash: str,
    action: str,
    mission_score: float = 0.0,
) -> dict:
    return {
        "mission_id":    mission_id,
        "agent":         agent,
        "layer":         layer,
        "fact_hash":     fact_hash,
        "action":        action,
        "timestamp":     datetime.now().isoformat(),
        "mission_score": round(mission_score, 3),
    }


def _reinforce_confidence(existing: float, new_evidence_strength: float) -> float:
    """Bayesian-style confidence update: pull existing toward 1.0."""
    updated = existing + (1.0 - existing) * new_evidence_strength * 0.35
    return round(min(0.97, updated), 4)


def _contradict_confidence(existing: float, contradiction_strength: float) -> float:
    """Penalise an existing fact when contradicting evidence arrives."""
    updated = existing * (1.0 - contradiction_strength * 0.4)
    return round(max(0.05, updated), 4)


def _research_similarity(entry: dict, query_text: str) -> float:
    """Return word-overlap similarity between a research entry and a query."""
    STOP = {"the", "a", "an", "is", "in", "at", "of", "to", "and", "or",
            "for", "with", "on", "was", "be", "are", "has", "not"}

    entry_text = " ".join([
        entry.get("problem_summary", ""),
        entry.get("key_insight", ""),
        entry.get("search_query", ""),
        " ".join(entry.get("applies_to", [])),
    ])
    entry_words = {
        w for w in re.findall(r"[a-zA-Z_]{4,}", entry_text.lower())
        if w not in STOP
    }
    query_words = {
        w for w in re.findall(r"[a-zA-Z_]{4,}", query_text.lower())
        if w not in STOP
    }
    if not entry_words or not query_words:
        return 0.0
    return len(entry_words & query_words) / max(len(entry_words | query_words), 1)


# ──────────────────────────────────────────────────────────────────────────────
# LAYER 1 — FAST INTUITIONS
# ──────────────────────────────────────────────────────────────────────────────

def record_intuition(
    schema: dict,
    text: str,
    mission_id: str = "unknown",
    agent: str = "CEO",
    confidence: float = 0.65,
) -> dict:
    fh         = _fact_hash(text)
    text_words = set(re.findall(r"[a-z]{4,}", text.lower()))

    for existing in schema["fast_intuitions"]:
        ex_words = set(re.findall(r"[a-z]{4,}", existing["text"].lower()))
        overlap  = len(text_words & ex_words) / max(len(text_words | ex_words), 1)
        if overlap > 0.8:
            existing["confidence"] = _reinforce_confidence(existing["confidence"], confidence)
            if mission_id not in existing.get("source_missions", []):
                existing.setdefault("source_missions", []).append(mission_id)
            return schema

    schema["fast_intuitions"].append({
        "text":            text[:200],
        "confidence":      confidence,
        "source_missions": [mission_id],
    })
    schema["provenance_log"].append(
        _provenance_entry(mission_id, agent, "fast_intuitions", fh, "added")
    )
    return schema


# ──────────────────────────────────────────────────────────────────────────────
# LAYER 2 — STRUCTURAL KNOWLEDGE
# ──────────────────────────────────────────────────────────────────────────────

def record_structural(
    schema: dict,
    key: str,
    value: str,
    kind: str = "pattern",
    mission_id: str = "unknown",
    agent: str = "CEO",
    confidence: float = 0.7,
) -> dict:
    fh  = _fact_hash(key + value[:40])
    now = datetime.now().isoformat()

    existing = next(
        (s for s in schema["structural_knowledge"]
         if s["key"].lower() == key.lower()),
        None,
    )

    if existing:
        existing["confidence"]   = _reinforce_confidence(existing["confidence"], confidence)
        existing["last_verified"] = now
        if mission_id not in existing.get("source_missions", []):
            existing.setdefault("source_missions", []).append(mission_id)
    else:
        schema["structural_knowledge"].append({
            "key":             key,
            "value":           value[:300],
            "kind":            kind,
            "confidence":      confidence,
            "source_missions": [mission_id],
            "last_verified":   now,
        })
        schema["provenance_log"].append(
            _provenance_entry(mission_id, agent, "structural_knowledge", fh, "added")
        )

    return schema


# ──────────────────────────────────────────────────────────────────────────────
# LAYER 3 — DECISION RULES
# ──────────────────────────────────────────────────────────────────────────────

def record_decision_rule(
    schema: dict,
    condition: str,
    action: str,
    priority: int = 5,
    mission_id: str = "unknown",
    agent: str = "CEO",
    confidence: float = 0.65,
) -> dict:
    fh = _fact_hash(condition + action[:30])

    existing = next(
        (r for r in schema["decision_rules"]
         if r["condition"].lower()[:40] == condition.lower()[:40]),
        None,
    )

    if existing:
        existing["confidence"] = _reinforce_confidence(existing["confidence"], confidence)
        existing["priority"]   = max(existing["priority"], priority)
        if mission_id not in existing.get("source_missions", []):
            existing.setdefault("source_missions", []).append(mission_id)
    else:
        schema["decision_rules"].append({
            "condition":       condition[:200],
            "action":          action[:200],
            "priority":        priority,
            "confidence":      confidence,
            "source_missions": [mission_id],
        })
        schema["provenance_log"].append(
            _provenance_entry(mission_id, agent, "decision_rules", fh, "added")
        )

    return schema


# ──────────────────────────────────────────────────────────────────────────────
# LAYER 4 — FAILURE CATALOG
# ──────────────────────────────────────────────────────────────────────────────

def record_failure(
    schema: dict,
    error_text: str,
    root_cause: str,
    fix: str,
    mission_id: str = "unknown",
    agent: str = "CEO",
    confidence: float = 0.75,
) -> dict:
    if not error_text.strip():
        return schema

    eh  = _error_hash(error_text)
    now = datetime.now().isoformat()

    existing = next(
        (e for e in schema["failure_catalog"] if e["error_hash"] == eh),
        None,
    )

    if existing:
        existing["occurrences"] = existing.get("occurrences", 1) + 1
        existing["confidence"]  = _reinforce_confidence(existing["confidence"], confidence)
        existing["last_seen"]   = now
        if fix.strip():
            existing["fix"] = fix[:300]
        if mission_id not in existing.get("source_missions", []):
            existing.setdefault("source_missions", []).append(mission_id)
    else:
        schema["failure_catalog"].append({
            "error_signature": error_text[:200],
            "error_hash":      eh,
            "root_cause":      root_cause[:200],
            "fix":             fix[:300],
            "confidence":      confidence,
            "occurrences":     1,
            "last_seen":       now,
            "source_missions": [mission_id],
        })
        schema["provenance_log"].append(
            _provenance_entry(mission_id, agent, "failure_catalog", eh, "added")
        )

    return schema


# ──────────────────────────────────────────────────────────────────────────────
# LAYER 7 — RESEARCH KNOWLEDGE
# ──────────────────────────────────────────────────────────────────────────────

def record_research(
    schema: dict,
    research_entry: dict,
    mission_id: str = "unknown",
    agent: str = "CEO",
) -> dict:
    schema.setdefault("research_knowledge", [])

    if not research_entry.get("problem_summary"):
        return schema

    confidence = float(research_entry.get("confidence", 0.7))
    if confidence < 0.3:
        return schema

    query_text = (
        research_entry.get("problem_summary", "") + " "
        + research_entry.get("key_insight", "")
    )
    for existing in schema["research_knowledge"]:
        sim = _research_similarity(existing, query_text)
        if sim > 0.60:
            existing["confidence"] = _reinforce_confidence(existing["confidence"], confidence)
            if confidence > existing.get("confidence", 0):
                if research_entry.get("fix_approaches"):
                    existing["fix_approaches"] = research_entry["fix_approaches"]
                if research_entry.get("diagnosis_steps"):
                    existing["diagnosis_steps"] = research_entry["diagnosis_steps"]
            if mission_id not in existing.get("source_missions", []):
                existing.setdefault("source_missions", []).append(mission_id)
            return schema

    entry = {
        "problem_summary":        research_entry.get("problem_summary", "")[:200],
        "likely_causes":          research_entry.get("likely_causes", [])[:5],
        "diagnosis_steps":        research_entry.get("diagnosis_steps", [])[:5],
        "fix_approaches":         research_entry.get("fix_approaches", [])[:3],
        "key_insight":            research_entry.get("key_insight", "")[:200],
        "applies_to":             research_entry.get("applies_to", [])[:5],
        "confidence":             confidence,
        "search_query":           research_entry.get("search_query", "")[:150],
        "researched_on_mission":  mission_id,
        "source_missions":        [mission_id],
        "timestamp":              datetime.now().isoformat(),
    }
    schema["research_knowledge"].append(entry)

    if len(schema["research_knowledge"]) > 50:
        schema["research_knowledge"] = sorted(
            schema["research_knowledge"],
            key=lambda x: x.get("confidence", 0),
            reverse=True
        )[:50]

    fix_approaches = research_entry.get("fix_approaches", [])
    if fix_approaches:
        best_fix = fix_approaches[0]
        record_failure(
            schema,
            error_text=research_entry.get("problem_summary", "")[:150],
            root_cause=(
                research_entry["likely_causes"][0]
                if research_entry.get("likely_causes")
                else "See research entry"
            )[:150],
            fix=best_fix.get("fix", "")[:250],
            mission_id=mission_id,
            agent=agent,
            confidence=confidence * 0.9,
        )

    for approach in fix_approaches[:1]:
        condition = approach.get("condition", "")
        fix       = approach.get("fix", "")
        if condition and fix and len(condition) > 10 and len(fix) > 10:
            record_decision_rule(
                schema,
                condition=f"IF {condition}",
                action=f"THEN {fix}",
                priority=6,
                mission_id=mission_id,
                agent=agent,
                confidence=confidence * 0.85,
            )

    schema["provenance_log"].append(
        _provenance_entry(
            mission_id, agent, "research_knowledge",
            _fact_hash(research_entry.get("problem_summary", "")),
            "research_added"
        )
    )

    return schema


# ──────────────────────────────────────────────────────────────────────────────
# GENERAL OBSERVATION ROUTER
# ──────────────────────────────────────────────────────────────────────────────

def record_observation(
    schema: dict,
    observation_text: str,
    mission_id: str,
    agent: str,
    turn: int,
) -> dict:
    """
    General-purpose mid-mission observation logger.
    DOMAIN-AGNOSTIC: Detects broad organizational, scientific, financial, and technical markers.
    """
    text = observation_text.strip()
    if not text or len(text) < 20:
        return schema

    lower = text.lower()

    # ── Error/failure signals (Broadened for Cross-Industry) ───────────────
    ERROR_SIGNALS = [
        # Software/Tech
        "error:", "exception:", "failed:", "traceback", "❌", "command not found", "segfault",
        # Finance/Accounting
        "discrepancy", "unbalanced", "audit failed", "reconciliation error", "overdraft", "invalid ledger",
        # Science/Data
        "anomaly detected", "outlier", "non-convergent", "p-value rejected", "data missing", "divergence",
        # General Business
        "rejected", "unauthorized", "compliance violation"
    ]
    has_error = any(sig in lower for sig in ERROR_SIGNALS)

    if has_error:
        lines    = text.splitlines()
        err_line = next(
            (l for l in lines if any(sig in l.lower() for sig in ERROR_SIGNALS)),
            text[:100]
        )
        fix_hint = ""
        for l in lines:
            if any(kw in l.lower() for kw in ["fix:", "solution:", "try:", "hint:", "suggestion:", "adjust:"]):
                fix_hint = l.strip()[:200]
                break

        record_failure(
            schema,
            error_text=err_line[:150],
            root_cause="Auto-detected from agent output — see mission history",
            fix=fix_hint or "See mission history for resolution",
            mission_id=mission_id,
            agent=agent,
            confidence=0.55,
        )
        return schema

    # ── Structural fact signals (Documents, Data, APIs) ─────────────────────
    # Matches generic data files (code, spreadsheets, PDFs, scientific data)
    doc_match = re.search(
        r"(/[\w./\-]+\.(?:py|js|ts|json|csv|xlsx|docx|pdf|sql|xml|pdb|fasta|log|md))",
        text, re.IGNORECASE
    )
    # Matches web endpoints or APIs
    api_match  = re.search(r"(https?://[\w./\-]+(?:api|graphql|v1|v2|localhost)[\w./\-]*)", text, re.IGNORECASE)
    env_match  = re.search(r"([A-Z_]{4,})\s*=\s*(\S{3,})", text)
    
    if doc_match and os.path.exists(doc_match.group(1)):
        record_structural(
            schema, "verified_document", doc_match.group(1),
            kind="document_path", mission_id=mission_id, agent=agent, confidence=0.75
        )
    if api_match:
        record_structural(
            schema, "active_endpoint", api_match.group(1),
            kind="api_endpoint", mission_id=mission_id, agent=agent, confidence=0.82
        )
    if env_match:
        record_structural(
            schema, f"env_var:{env_match.group(1)}", env_match.group(1),
            kind="environment_variable", mission_id=mission_id, agent=agent, confidence=0.65
        )

    # ── Success/verification signals (Cross-Industry) ───────────────────────
    SUCCESS_SIGNALS = [
        "✅", "verified", "passed", "success", "confirmed",
        "zero errors", "build complete", # Tech
        "balanced", "reconciled", "audit clear", "approved", # Finance
        "converged", "validated", "statistically significant" # Science
    ]
    if any(sig in lower for sig in SUCCESS_SIGNALS):
        record_intuition(
            schema, f"Turn {turn}: {text[:120]}",
            mission_id=mission_id, agent=agent, confidence=0.70
        )

    return schema


# ──────────────────────────────────────────────────────────────────────────────
# POST-MISSION EVOLUTION
# ──────────────────────────────────────────────────────────────────────────────

def evolve_framework(
    schema: dict,
    mission_id: str,
    mission_text: str,
    conversation_history: list[dict],
    master_plan: list[str],
    turn_count: int,
    qa_failures: int,
    director_llm: Any,
) -> dict:
    """
    Post-mission additive evolution. Dynamically adapts to the industry/domain.
    """
    mission_score = max(
        0.0,
        min(1.0, 1.0 - (turn_count * 0.012) - (qa_failures * 0.04))
    )

    _mine_successful_fixes(schema, conversation_history, mission_id, mission_score)

    history_digest = []
    for item in conversation_history[-30:]:
        agent  = item.get("agent", "?")
        result = str(item.get("result", ""))[:200]
        history_digest.append(f"{agent}: {result}")

    extraction_prompt = f"""
You are a Principal Knowledge Engineer optimizing an AI workforce in the industry domain: '{schema['domain']}'.
Analyse this completed AI mission and extract structured knowledge to permanently update the corporate mental framework.

MISSION: {mission_text[:300]}
TURNS USED: {turn_count}
QA FAILURES: {qa_failures}
MASTER PLAN:
{json.dumps(master_plan[:8], indent=2)}

MISSION HISTORY (last 30 turns):
{chr(10).join(history_digest)}

CRITICAL INSTRUCTION: Adapt your extraction strictly to the '{schema['domain']}' domain.
- If this is Accounting, extract ledger rules, reconciliation steps, and tax compliance errors.
- If this is Science, extract experimental parameters, data cleaning conventions, and statistical anomalies.
- If this is Software, extract architectural patterns, build errors, and dependencies.

Return ONLY valid JSON with these fields. Use specific real-world values from the history above.
Return ONLY the JSON object, no markdown fences.

{{
  "new_fast_intuitions": ["string — reusable domain pattern insight"],
  "new_structural_facts": [
    {{"key": "string", "value": "string", "kind": "path|api_endpoint|document|metric|financial_code|scientific_parameter|convention|pattern"}}
  ],
  "new_decision_rules": [
    {{"condition": "IF ...", "action": "THEN ...", "priority": 1-10}}
  ],
  "new_failures": [
    {{"error_signature": "exact anomaly/error pattern", "root_cause": "why it happens", "fix": "business/technical resolution"}}
  ],
  "meta_update": {{
    "difficulty": "low|medium|high|expert",
    "completeness_threshold_for_architecture": 50-90,
    "trust_notes": ["string"]
  }}
}}
"""

    extracted: dict = {}
    try:
        raw       = director_llm.call(messages=[{"role": "user", "content": extraction_prompt}])
        raw       = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()
        extracted = json.loads(raw)
    except Exception:
        pass

    now = datetime.now().isoformat()

    for text in extracted.get("new_fast_intuitions", [])[:6]:
        schema = record_intuition(schema, text, mission_id, "EvolutionEngine", mission_score)

    for sf in extracted.get("new_structural_facts", [])[:8]:
        schema = record_structural(
            schema, sf.get("key", ""), sf.get("value", ""),
            kind=sf.get("kind", "pattern"), mission_id=mission_id,
            agent="EvolutionEngine", confidence=mission_score * 0.85,
        )

    for dr in extracted.get("new_decision_rules", [])[:5]:
        schema = record_decision_rule(
            schema, dr.get("condition", ""), dr.get("action", ""),
            priority=int(dr.get("priority", 5)), mission_id=mission_id,
            agent="EvolutionEngine", confidence=mission_score * 0.80,
        )

    for fe in extracted.get("new_failures", [])[:5]:
        schema = record_failure(
            schema, fe.get("error_signature", ""), fe.get("root_cause", ""),
            fe.get("fix", ""), mission_id=mission_id,
            agent="EvolutionEngine", confidence=mission_score * 0.85,
        )

    meta_upd = extracted.get("meta_update", {})
    if meta_upd.get("difficulty"):
        schema["meta_knowledge"]["difficulty"] = meta_upd["difficulty"]
    if meta_upd.get("completeness_threshold_for_architecture"):
        existing_thresh = schema["meta_knowledge"].get("completeness_threshold_for_architecture", 75)
        new_thresh = int(meta_upd["completeness_threshold_for_architecture"])
        schema["meta_knowledge"]["completeness_threshold_for_architecture"] = int(existing_thresh * 0.6 + new_thresh * 0.4)
    for note in meta_upd.get("trust_notes", [])[:2]:
        notes = schema["meta_knowledge"].setdefault("trust_notes", [])
        if note not in notes:
            notes.append(note)

    old_conf = schema["meta_knowledge"].get("confidence", 0.5)
    schema["meta_knowledge"]["confidence"] = round(old_conf * 0.7 + mission_score * 0.3, 4)

    schema["provenance_log"].append({
        "mission_id":    mission_id,
        "agent":         "EvolutionEngine",
        "layer":         "all",
        "fact_hash":     hashlib.md5(mission_text.encode()).hexdigest()[:8],
        "action":        "evolved",
        "timestamp":     now,
        "mission_score": round(mission_score, 3),
        "facts_added": {
            "intuitions":  len(extracted.get("new_fast_intuitions", [])),
            "structural":  len(extracted.get("new_structural_facts", [])),
            "rules":       len(extracted.get("new_decision_rules", [])),
            "failures":    len(extracted.get("new_failures", [])),
        },
    })

    if len(schema["provenance_log"]) > 200:
        schema["provenance_log"] = schema["provenance_log"][-200:]

    save_framework(schema)
    return schema


def _mine_successful_fixes(
    schema: dict,
    conversation_history: list[dict],
    mission_id: str,
    mission_score: float,
) -> None:
    """
    Industry-agnostic scanner: Finds instances where an Action resulted in an Error, 
    followed by a Correction Action that resulted in Success.
    """
    SUCCESS_MARKERS = [
        "zero errors", "✅", "build complete", "reconciled", "balanced", 
        "audit passed", "converged", "validated", "success"
    ]
    ERROR_MARKERS = [
        "error:", "failed:", "exception:", "✗", "❌", "discrepancy", 
        "unbalanced", "anomaly", "divergence"
    ]

    prev_had_error  = False
    prev_error_text = ""
    prev_fix_cmd    = ""

    for item in conversation_history:
        step   = str(item.get("step", ""))
        result = str(item.get("result", "")).lower()
        cmd    = str(item.get("instruction_text", ""))

        if "VISION" in step or "FILE-WRITE" in step or "DELEGATE" in step:
            prev_fix_cmd = cmd

        if any(m in result for m in ERROR_MARKERS):
            prev_had_error  = True
            for line in result.splitlines():
                if any(m in line for m in ERROR_MARKERS):
                    prev_error_text = line.strip()[:150]
                    break
        elif prev_had_error and any(m in result for m in SUCCESS_MARKERS):
            if prev_fix_cmd and prev_error_text:
                record_failure(
                    schema,
                    error_text=prev_error_text,
                    root_cause="Auto-mined from successful task recovery",
                    fix=prev_fix_cmd[:250],
                    mission_id=mission_id,
                    agent="EvolutionEngine",
                    confidence=min(0.92, mission_score * 1.1),
                )
            prev_had_error  = False
            prev_error_text = ""
            prev_fix_cmd    = ""


# ──────────────────────────────────────────────────────────────────────────────
# BOOTSTRAP FROM RESEARCH
# ──────────────────────────────────────────────────────────────────────────────

def build_from_research(
    domain: str,
    research_text: str,
    mission_id: str,
    agent: str = "Systems Analyst",
    director_llm: Any = None,
) -> dict:
    """
    Given raw research text, bootstrap or enrich a framework schema.
    Domain-Agnostic mapping applied dynamically by the LLM.
    """
    schema = load_framework(domain)

    if director_llm is not None:
        bootstrap_prompt = f"""
You are a Principal Knowledge Engineer structuring a mental framework for the industry domain '{domain}'.
Convert this raw research into a highly structured JSON schema for AI workers.

CRITICAL INSTRUCTION: Tailor the extracted entities strictly to the '{domain}' domain.
If this is Finance, look for tax codes, ledger rules, and reconciliation logic.
If this is Data Science, look for statistical thresholds, pipeline architectures, and cleaning rules.

RAW RESEARCH:
{research_text[:6000]}

Return ONLY valid JSON (no markdown fences) with these fields:

{{
  "fast_intuitions": [
    "string — core domain principles"
  ],
  "structural_knowledge": [
    {{"key": "string", "value": "string", "kind": "path|api_endpoint|document|metric|financial_code|scientific_parameter|convention|pattern"}}
  ],
  "decision_rules": [
    {{"condition": "IF ...", "action": "THEN ...", "priority": 1-10}}
  ],
  "failure_catalog": [
    {{"error_signature": "string", "root_cause": "string", "fix": "string"}}
  ],
  "research_knowledge": [
    {{
      "problem_summary": "string",
      "likely_causes": ["string"],
      "diagnosis_steps": ["exact procedure or check"],
      "fix_approaches": [{{"condition": "when X", "fix": "action taken", "verify": "how to confirm"}}],
      "key_insight": "string",
      "applies_to": ["technology or business context"],
      "confidence": 0.7
    }}
  ],
  "meta_knowledge": {{
    "difficulty": "low|medium|high|expert",
    "completeness_threshold_for_architecture": 50-90,
    "required_credentials": ["string"],
    "common_ambiguities": ["string"],
    "trust_notes": ["string"]
  }}
}}
"""
        try:
            raw       = director_llm.call(messages=[{"role": "user", "content": bootstrap_prompt}])
            raw       = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()
            extracted = json.loads(raw)

            for text in extracted.get("fast_intuitions", [])[:8]:
                schema = record_intuition(schema, text, mission_id, agent, 0.65)
            for sf in extracted.get("structural_knowledge", [])[:10]:
                schema = record_structural(
                    schema, sf["key"], sf["value"],
                    sf.get("kind", "pattern"), mission_id, agent, 0.65
                )
            for dr in extracted.get("decision_rules", [])[:6]:
                schema = record_decision_rule(
                    schema, dr["condition"], dr["action"],
                    dr.get("priority", 5), mission_id, agent, 0.65
                )
            for fe in extracted.get("failure_catalog", [])[:5]:
                schema = record_failure(
                    schema, fe["error_signature"],
                    fe["root_cause"], fe["fix"],
                    mission_id, agent, 0.65
                )
            for re_entry in extracted.get("research_knowledge", [])[:5]:
                re_entry["search_query"] = re_entry.get("search_query", f"bootstrap:{domain}")
                schema = record_research(schema, re_entry, mission_id, agent)

            meta = extracted.get("meta_knowledge", {})
            schema["meta_knowledge"].update({
                k: v for k, v in meta.items()
                if k in (
                    "difficulty", "completeness_threshold_for_architecture",
                    "required_credentials", "common_ambiguities", "trust_notes"
                )
            })

        except Exception:
            pass

    if not schema["fast_intuitions"]:
        bullets = re.findall(r"^[-*]\s+(.+)$", research_text, re.MULTILINE)
        for b in bullets[:8]:
            schema = record_intuition(schema, b.strip(), mission_id, agent, 0.55)

    schema["provenance_log"].append({
        "mission_id":    mission_id,
        "agent":         agent,
        "layer":         "all",
        "fact_hash":     hashlib.md5(research_text[:200].encode()).hexdigest()[:8],
        "action":        "bootstrapped_from_research",
        "timestamp":     datetime.now().isoformat(),
        "mission_score": 0.6,
    })

    save_framework(schema)
    return schema
