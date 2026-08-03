# helpers.py
import os
import re
import subprocess
from mental_framework import detect_domains, render_framework_block


def _preload_file_context(mission: str) -> tuple[str, list[str]]:
    PATH_RE = re.compile(
        r'(/(?:[\w.\-]+/)*[\w.\-]+\.'
        r'(?:py|ts|tsx|js|jsx|json|md|txt|yaml|yml|toml|sh|css|html|sql))'
    )

    found_paths = []
    clean_mission = mission

    for match in PATH_RE.finditer(mission):
        path = match.group(1)
        if os.path.isfile(path):
            found_paths.append(path)
            clean_mission = clean_mission.replace(path, "").strip()

    EXT_LANG = {
        '.py': 'python', '.ts': 'typescript', '.tsx': 'tsx',
        '.js': 'javascript', '.jsx': 'jsx', '.json': 'json',
        '.md': 'markdown', '.sh': 'bash', '.css': 'css',
        '.html': 'html', '.sql': 'sql', '.yaml': 'yaml', '.yml': 'yaml',
    }

    file_blocks = []
    log_lines   = []

    # ── Mental Framework Auto-Injection ──────────────────────────────────────
    detected_domains = detect_domains(mission)
    if detected_domains:
        framework_block, fw_logs = render_framework_block(detected_domains, mission)
        if framework_block:
            file_blocks.insert(0, framework_block)
        log_lines.extend(fw_logs)

    # ── Legacy CoT Playbook fallback ─────────────────────────────────────────
    PLAYBOOK_DIR = os.path.abspath(os.path.join("ai_civilization", "cot_playbooks"))
    os.makedirs(PLAYBOOK_DIR, exist_ok=True)
    mission_lower = mission.lower()
    injected_playbooks = []
    already_covered = set(detected_domains)
    try:
        for fname in os.listdir(PLAYBOOK_DIR):
            if not fname.endswith(".md"):
                continue
            slug = fname.replace("_cot.md", "")
            if slug in already_covered:
                continue
            keyword = fname.replace("_cot.md", "").replace("_", " ").lower()
            if any(word in mission_lower for word in keyword.split() if len(word) > 3):
                pb_path = os.path.join(PLAYBOOK_DIR, fname)
                try:
                    with open(pb_path, encoding="utf-8", errors="replace") as f:
                        pb_content = f.read()
                    file_blocks.append(
                        f"📖 COT PLAYBOOK (legacy) ({fname}):\n"
                        f"```markdown\n{pb_content[:8000]}\n```"
                    )
                    log_lines.append(
                        f"[bold yellow]📖 Legacy playbook injected: {fname}[/bold yellow]"
                    )
                    injected_playbooks.append(fname)
                except Exception:
                    pass
    except Exception:
        pass

    for path in found_paths:
        ext     = os.path.splitext(path)[1].lower()
        lang    = EXT_LANG.get(ext, '')
        name    = os.path.basename(path)
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                raw = f.read()
            total_lines = raw.count('\n') + 1
            content = raw[:25000]
            truncated = len(raw) > 25000
            if truncated:
                content += f"\n... [{len(raw) - 25000} chars truncated — use ceo_ast_vision for deeper reads]"
            file_blocks.append(
                f"TARGET FILE ({name}) — {path} ({total_lines} lines):\n```{lang}\n{content}\n```"
            )
            status = f"({total_lines} lines{', truncated' if truncated else ''})"
            log_lines.append(
                f"[bold green]🪄 Pre-fetched {name} {status} directly into mission context[/bold green]"
            )
        except Exception as e:
            file_blocks.append(f"TARGET FILE ({name}): [could not read: {e}]")
            log_lines.append(f"[yellow]⚠️ Pre-fetch failed for {name}: {e}[/yellow]")

    if not found_paths and not injected_playbooks and not detected_domains:
        return mission, []

    parts = [clean_mission]
    if detected_domains:
        parts.append(
            f"⚡ MENTAL FRAMEWORKS ACTIVE: Domain knowledge loaded for: "
            f"{', '.join(detected_domains)}. "
            f"Read the framework blocks below — they encode expert intuition, "
            f"known failure patterns, and structural facts from past missions. "
            f"Use Layer 4 (failure catalog) FIRST when you hit a blocker."
        )
    if injected_playbooks:
        parts.append(
            f"📖 LEGACY PLAYBOOKS: {', '.join(injected_playbooks)}."
        )
    if found_paths:
        parts.append(
            "⚠️ FILE CONTEXT PRE-LOADED: The target file(s) are already embedded below. "
            "Do NOT command any worker to read or cat these files — the code is already here. "
            "Read the code below, form your plan, then command the worker to make the specific change."
        )
    parts.extend(file_blocks)
    enriched = "\n\n".join(parts)
    return enriched, log_lines


def _build_post_execution_report(cmd: str, raw_output: str, cwd: str) -> str:
    """
    After every CEO terminal command, automatically verify what actually happened.
    Tech-agnostic: checks file writes, install results, and server starts.
    """
    report_lines = []
    cmd_lower = cmd.lower().strip()

    # ── 1. HEREDOC / FILE WRITE DETECTION ────────────────────────────────────
    _heredoc_match = re.search(
        r"cat\s*>\s*([^\s<]+)\s*<<\s*['\"]?EOF['\"]?", cmd, re.IGNORECASE
    )
    if _heredoc_match:
        target_file = _heredoc_match.group(1).strip()
        if not os.path.isabs(target_file):
            _cd_match = re.match(r"cd\s+(\S+)\s*&&", cmd)
            if _cd_match:
                target_file = os.path.join(_cd_match.group(1), target_file)
            else:
                target_file = os.path.join(cwd, target_file)
        try:
            if os.path.exists(target_file):
                with open(target_file, encoding='utf-8', errors='replace') as f:
                    content = f.read()
                lines = content.splitlines()
                line_count = len(lines)
                _has_eof_junk = any(l.strip().startswith('EOF') for l in lines[-5:])
                tail_preview = "\n".join(f"  {i+1:>4}: {l}" for i, l in enumerate(lines[-5:], line_count - min(5, line_count)))
                report_lines.append(
                    f"\n╔══ FILE WRITTEN: {target_file} ══"
                    f"\n║  Lines: {line_count}"
                    f"\n║  Last 5 lines:\n{tail_preview}"
                )
                if _has_eof_junk:
                    report_lines.append(
                        f"║  ⚠️  EOF JUNK DETECTED — fix: sed -i '/^EOF/d' {target_file}"
                    )
                else:
                    report_lines.append(f"║  ✅ Clean.")
                report_lines.append("╚" + "═" * 50)
            else:
                report_lines.append(f"\n⚠️  FILE NOT CREATED: {target_file}")
        except Exception as e:
            report_lines.append(f"\n⚠️  Could not verify: {e}")

        # Scan for EOF junk in all source files after any heredoc write
        try:
            _src_root = cwd
            _cd_m = re.match(r"cd\s+(\S+)\s*&&", cmd)
            if _cd_m:
                _src_root = _cd_m.group(1)
            _infected = []
            for _dirpath, _dirnames, _fnames in os.walk(_src_root):
                _dirnames[:] = [d for d in _dirnames if d not in ('node_modules', '.git', 'dist', 'build', '__pycache__', '.venv')]
                for _fname in _fnames:
                    _fpath = os.path.join(_dirpath, _fname)
                    try:
                        with open(_fpath, encoding='utf-8', errors='replace') as _fh:
                            _flines = _fh.readlines()
                        for _fl in _flines[-10:]:
                            if _fl.strip().startswith('EOF'):
                                _infected.append(_fpath)
                                break
                    except Exception:
                        pass
            if _infected:
                report_lines.append(
                    f"\n🚨 EOF JUNK FOUND IN {len(_infected)} FILE(S):"
                )
                for _inf in _infected:
                    report_lines.append(f"   → sed -i '/^EOF/d' {_inf}")
        except Exception:
            pass

    # ── 2. BUILD / TEST OUTPUT ANALYSIS ──────────────────────────────────────
    elif any(kw in cmd_lower for kw in ['build', 'test', 'check', 'lint', 'compile', 'typecheck']):
        out = raw_output.strip()
        if not out or out == "[Command executed silently with no output]":
            report_lines.append(
                "\n╔══ BUILD/TEST RESULT ══"
                "\n║  ✅ Zero output — completed cleanly."
                "\n╚" + "═" * 50
            )
        else:
            error_lines = [l for l in out.splitlines() if any(
                kw in l.lower() for kw in ('error', 'failed', 'exception', 'cannot find', 'does not exist')
            )]
            if error_lines:
                report_lines.append(
                    f"\n╔══ BUILD/TEST RESULT: {len(error_lines)} ISSUE(S) ══"
                    f"\n║  First issue: {error_lines[0][:120]}"
                    f"\n║  → Fix these before declaring FINISHED."
                    "\n╚" + "═" * 50
                )
            else:
                report_lines.append(
                    "\n╔══ BUILD/TEST RESULT ══"
                    "\n║  ✅ No errors found. Safe to declare FINISHED."
                    "\n╚" + "═" * 50
                )

    # ── 3. SED / PATCH CONFIRMATION ──────────────────────────────────────────
    elif 'sed ' in cmd_lower:
        _sed_files = re.findall(r'(?:^|\s)((?:/[\w./\-]+|[\w./\-]+\.[\w]+))', cmd)
        _sed_files = [f for f in _sed_files if '.' in f and not f.startswith('-')][:3]
        if _sed_files:
            confirmations = []
            for sf in _sed_files:
                full_sf = sf if os.path.isabs(sf) else os.path.join(cwd, sf)
                _cd_m = re.match(r"cd\s+(\S+)\s*&&", cmd)
                if _cd_m and not os.path.exists(full_sf):
                    full_sf = os.path.join(_cd_m.group(1), sf)
                if os.path.exists(full_sf):
                    try:
                        with open(full_sf, encoding='utf-8', errors='replace') as f:
                            fc = f.read()
                        confirmations.append(f"  ✅ {sf} exists ({fc.count(chr(10))+1} lines)")
                    except Exception:
                        confirmations.append(f"  ⚠️ {sf} — could not read")
                else:
                    confirmations.append(f"  ⚠️ {sf} — file not found")
            if confirmations:
                report_lines.append(
                    "\n╔══ SED RESULT ══"
                    + "".join(f"\n║ {c}" for c in confirmations)
                    + "\n╚" + "═" * 50
                )

    # ── 4. PACKAGE INSTALL ───────────────────────────────────────────────────
    elif any(kw in cmd_lower for kw in ['npm install', 'npm i ', 'pnpm add', 'pnpm install', 'yarn add', 'pip install', 'pip3 install', 'poetry add']):
        if 'error' in raw_output.lower() or 'failed' in raw_output.lower():
            report_lines.append(
                "\n╔══ INSTALL RESULT: FAILED ══"
                "\n║  ❌ Package install reported errors. Check the error message."
                "\n║  → Do NOT proceed until the install succeeds."
                "\n╚" + "═" * 50
            )
        elif raw_output.strip() and raw_output != "[Command executed silently with no output]":
            report_lines.append(
                "\n╔══ INSTALL RESULT ══"
                "\n║  ✅ Package install completed without errors."
                "\n╚" + "═" * 50
            )

    # ── 5. SERVER START DETECTION ─────────────────────────────────────────────
    elif 'nohup' in cmd_lower and '&' in cmd:
        _log_match = re.search(r'>\s*(/tmp/\S+\.log)', cmd)
        if _log_match:
            log_file = _log_match.group(1)
            report_lines.append(
                f"\n╔══ SERVER START ══"
                f"\n║  Process launched in background."
                f"\n║  → Next turn: run `sleep 3 && tail -20 {log_file}` to confirm startup."
                "\n╚" + "═" * 50
            )

    # ── 6. GENERIC SILENT COMMAND ─────────────────────────────────────────────
    elif raw_output == "[Command executed silently with no output]":
        _silent_ok = any(kw in cmd_lower for kw in ['mkdir', 'cp ', 'mv ', 'chmod', 'chown', 'touch', 'rm '])
        if not _silent_ok:
            report_lines.append(
                "\n⚠️  SILENT OUTPUT: The command produced no output."
                "\n   → Verify with ls or wc -l on the target next turn."
            )

    return "\n".join(report_lines)
