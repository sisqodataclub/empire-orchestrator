# tools/ast_inspector_tool.py
#
# AST Inspector tool — understand source files by code analysis, not
# raw text dumping.
#
# Extracted from empire_tools.py. Body is unchanged; only the `self`
# parameter and the direct dependency on empire_tools.log_agent_action
# changed.
#
# Registry key (from the @tool display name "AST Inspector"):
#     ast_inspector
#
# Supports:
#   • Python (.py)                        — full AST analysis
#   • TypeScript / JS (.ts .tsx .js .jsx) — regex-based
#   • Fallback for other file types       — section reads only.
import os
from crewai.tools import tool


def _log(tool_name: str, detail: str) -> None:
    """Lazy log helper — avoids circular import with empire_tools."""
    try:
        from empire_tools import log_agent_action
        log_agent_action(tool_name, detail)
    except Exception:
        pass


@tool("AST Inspector")
def ast_inspector(path: str, mode: str = "map", target: str = ""):
    """
    Understands source files using code analysis — NOT raw text dumping.
    Replaces `cat file.py` for large files. Use this first, then extract only what you need.

    MODES:
      'map'     — Full structural skeleton: all classes, functions, fields, line numbers.
                  Returns ~400 tokens instead of 8,000 for a large file. USE THIS FIRST.
      'extract' — Pull a specific class or function by name (exact lines, no noise).
                  Use after 'map' tells you the line range.
      'fields'  — List all Pydantic/SQLAlchemy fields and their types for a class.
                  Instantly answers "what columns does ProfileDB have?" in ~100 tokens.
      'imports' — List all imports and what they bring in. Diagnoses ModuleNotFoundError fast.
      'section' — Read lines start_line..end_line (use line numbers from 'map' output).
                  More precise than sed -n, works for any language.

    ARGS:
      path   — Absolute path to the file.
      mode   — One of: map | extract | fields | imports | section
      target — For extract/fields: class or function name (e.g. "ProfileDB").
               For section: "start_line,end_line" (e.g. "45,80").

    EXAMPLES:
      map     : ast_inspector('/app/backend/models.py', 'map')
      extract : ast_inspector('/app/backend/models.py', 'extract', 'ProfileDB')
      fields  : ast_inspector('/app/backend/models.py', 'fields',  'ProfileCreate')
      imports : ast_inspector('/app/backend/main.py',   'imports')
      section : ast_inspector('/app/backend/models.py', 'section', '45,80')

    Supports: Python (.py), TypeScript (.ts, .tsx), JavaScript (.js, .jsx).
    Falls back to section-reading for unknown file types.
    """
    _log("AST Inspector", f"mode={mode} target={target!r} path={path}")

    if not os.path.exists(path):
        return f"❌ FILE NOT FOUND: '{path}'. Use 'List Directory' to verify the path."

    ext = os.path.splitext(path)[1].lower()

    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            source = f.read()
            lines  = source.splitlines()
    except Exception as e:
        return f"❌ Could not read file: {e}"

    total_lines = len(lines)
    file_size   = len(source)

    # ── SECTION MODE (language-agnostic) ───────────────────────────────────
    if mode == "section":
        try:
            if ',' in str(target):
                start, end = [int(x.strip()) for x in str(target).split(',', 1)]
            else:
                return "❌ section mode requires target='start_line,end_line' e.g. '45,80'"
            start = max(1, start)
            end   = min(total_lines, end)
            chunk = '\n'.join(
                f"{i+1:4d} │ {line}"
                for i, line in enumerate(lines[start-1:end], start=start-1)
            )
            return (
                f"📄 {os.path.basename(path)}  lines {start}–{end} / {total_lines}\n"
                f"{'─'*50}\n{chunk}"
            )
        except Exception as e:
            return f"❌ section error: {e}"

    # ══════════════════════════════════════════════════════════════════════
    # PYTHON AST ANALYSIS
    # ══════════════════════════════════════════════════════════════════════
    if ext == '.py':
        import ast as _ast

        try:
            tree = _ast.parse(source)
        except SyntaxError as se:
            bad_line = lines[se.lineno - 1] if se.lineno and se.lineno <= len(lines) else "?"
            return (
                f"❌ SYNTAX ERROR in {os.path.basename(path)}:\n"
                f"  Line {se.lineno}: {se.msg}\n"
                f"  Code: {bad_line.strip()}\n"
                f"  Fix this before doing anything else."
            )

        # ── IMPORTS ──────────────────────────────────────────────────────
        if mode == "imports":
            out = [f"📦 IMPORTS — {os.path.basename(path)}  ({total_lines} lines)\n{'─'*50}"]
            for node in _ast.walk(tree):
                if isinstance(node, _ast.Import):
                    for alias in node.names:
                        label = f" as {alias.asname}" if alias.asname else ""
                        out.append(f"  line {node.lineno:4d} │ import {alias.name}{label}")
                elif isinstance(node, _ast.ImportFrom):
                    mod   = node.module or ''
                    names = ', '.join((a.asname or a.name) for a in node.names)
                    out.append(f"  line {node.lineno:4d} │ from {mod} import {names}")
            return '\n'.join(out) or "No imports found."

        # ── MAP MODE — full skeleton ──────────────────────────────────────
        if mode == "map":
            out = [
                f"🗺️  STRUCTURE MAP — {os.path.basename(path)}\n"
                f"    {total_lines} lines | {file_size:,} chars\n"
                f"{'═'*54}"
            ]

            imports = []
            for node in tree.body:
                if isinstance(node, _ast.Import):
                    imports += [a.name for a in node.names]
                elif isinstance(node, _ast.ImportFrom):
                    imports.append(f"{node.module}.*")
            if imports:
                out.append(f"  IMPORTS: {', '.join(imports[:12])}"
                           + (" ..." if len(imports) > 12 else ""))

            for node in tree.body:
                if isinstance(node, _ast.ClassDef):
                    bases = ', '.join(
                        getattr(b, 'id', getattr(b, 'attr', '?'))
                        for b in node.bases
                    )
                    end_line = max(
                        (getattr(n, 'lineno', node.lineno) for n in _ast.walk(node)),
                        default=node.lineno
                    )
                    out.append(
                        f"\n  CLASS {node.name}({bases})"
                        f"  [lines {node.lineno}–{end_line}]"
                    )

                    for item in node.body:
                        if isinstance(item, _ast.Assign):
                            for t in item.targets:
                                name = getattr(t, 'id', '?')
                                val  = item.value
                                if isinstance(val, _ast.Call):
                                    func = getattr(val.func, 'id',
                                                   getattr(val.func, 'attr', '?'))
                                    first = ''
                                    if val.args:
                                        first = getattr(val.args[0], 'id',
                                                        getattr(val.args[0], 'attr', ''))
                                    out.append(
                                        f"    {name} = {func}({first})"
                                        f"  [line {item.lineno}]"
                                    )
                        elif isinstance(item, _ast.AnnAssign):
                            name = getattr(item.target, 'id', '?')
                            ann  = _ast.unparse(item.annotation) if hasattr(_ast, 'unparse') else '?'
                            out.append(f"    {name}: {ann}  [line {item.lineno}]")

                    for item in node.body:
                        if isinstance(item, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                            args = [a.arg for a in item.args.args if a.arg != 'self']
                            prefix = 'async ' if isinstance(item, _ast.AsyncFunctionDef) else ''
                            out.append(
                                f"    {prefix}def {item.name}({', '.join(args)})"
                                f"  [line {item.lineno}]"
                            )

                elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    args   = [a.arg for a in node.args.args]
                    prefix = 'async ' if isinstance(node, _ast.AsyncFunctionDef) else ''
                    out.append(
                        f"\n  {prefix}def {node.name}({', '.join(args)})"
                        f"  [line {node.lineno}]"
                    )

            out.append(
                f"\n{'─'*54}\n"
                f"  ➡ Next: use mode='extract' target='ClassName' for full class source,\n"
                f"          or mode='fields' target='ClassName' for column/field list only,\n"
                f"          or mode='section' target='start,end' for raw lines."
            )
            return '\n'.join(out)

        # ── EXTRACT — pull a named class or function ───────────────────────
        if mode == "extract":
            if not target:
                return "❌ extract mode requires target='ClassName' or target='function_name'"
            for node in _ast.walk(tree):
                if (isinstance(node, (_ast.ClassDef, _ast.FunctionDef, _ast.AsyncFunctionDef))
                        and node.name == target):
                    end_line = max(
                        (getattr(n, 'lineno', node.lineno) for n in _ast.walk(node)),
                        default=node.lineno
                    )
                    chunk = '\n'.join(
                        f"{i+1:4d} │ {line}"
                        for i, line in enumerate(
                            lines[node.lineno - 1:end_line], start=node.lineno - 1
                        )
                    )
                    return (
                        f"📄 {target}  lines {node.lineno}–{end_line} "
                        f"/ {total_lines}  [{os.path.basename(path)}]\n"
                        f"{'─'*54}\n{chunk}"
                    )
            return (
                f"❌ '{target}' not found in {os.path.basename(path)}.\n"
                f"Run mode='map' to see all available names."
            )

        # ── FIELDS — Pydantic / SQLAlchemy column inventory ───────────────
        if mode == "fields":
            if not target:
                return "❌ fields mode requires target='ClassName'"
            for node in _ast.walk(tree):
                if isinstance(node, _ast.ClassDef) and node.name == target:
                    out = [
                        f"🗂️  FIELDS — {target}  [{os.path.basename(path)}]\n{'─'*50}"
                    ]
                    found_any = False
                    for item in node.body:
                        if isinstance(item, _ast.AnnAssign):
                            name     = getattr(item.target, 'id', '?')
                            ann      = (_ast.unparse(item.annotation)
                                        if hasattr(_ast, 'unparse') else '?')
                            optional = 'Optional' in ann or 'None' in ann
                            default  = ''
                            if item.value:
                                if isinstance(item.value, _ast.Constant):
                                    default = f" = {item.value.value!r}"
                                elif isinstance(item.value, _ast.Call):
                                    func = getattr(item.value.func, 'id',
                                                   getattr(item.value.func, 'attr', ''))
                                    default = f" = {func}(...)"
                            flag = " [optional]" if optional else " [required]"
                            out.append(f"  {name}: {ann}{default}{flag}  [line {item.lineno}]")
                            found_any = True
                        elif isinstance(item, _ast.Assign):
                            for t in item.targets:
                                name = getattr(t, 'id', '?')
                                val  = item.value
                                if isinstance(val, _ast.Call):
                                    func = getattr(val.func, 'id',
                                                   getattr(val.func, 'attr', '?'))
                                    args_strs = []
                                    for a in val.args[:2]:
                                        args_strs.append(
                                            getattr(a, 'id',
                                            getattr(a, 'attr',
                                            str(getattr(a, 'value', '?'))))
                                        )
                                    kwargs = {
                                        kw.arg: getattr(kw.value, 'value',
                                                        getattr(kw.value, 'id', '?'))
                                        for kw in val.keywords if kw.arg
                                    }
                                    nullable = kwargs.get('nullable', True)
                                    fk       = 'foreign_key' in str(kwargs) or 'ForeignKey' in str(args_strs)
                                    flags    = []
                                    if nullable is False: flags.append("NOT NULL")
                                    if fk:                flags.append("FK")
                                    if kwargs.get('primary_key'): flags.append("PK")
                                    flag_str = f"  [{', '.join(flags)}]" if flags else ""
                                    out.append(
                                        f"  {name} = {func}({', '.join(args_strs)})"
                                        f"{flag_str}  [line {item.lineno}]"
                                    )
                                    found_any = True
                    if not found_any:
                        out.append("  (no annotated fields or Column() assignments found)")
                    return '\n'.join(out)
            return (
                f"❌ Class '{target}' not found in {os.path.basename(path)}.\n"
                f"Run mode='map' to see all class names."
            )

    # ══════════════════════════════════════════════════════════════════════
    # TYPESCRIPT / JAVASCRIPT ANALYSIS
    # ══════════════════════════════════════════════════════════════════════
    if ext in ('.ts', '.tsx', '.js', '.jsx'):
        import re as _re

        # ── IMPORTS ──────────────────────────────────────────────────────
        if mode == "imports":
            out  = [f"📦 IMPORTS — {os.path.basename(path)}  ({total_lines} lines)\n{'─'*50}"]
            patt = _re.compile(
                r"^(?:import|export)\s.*?(?:from\s+['\"](.+?)['\"]|require\(['\"](.+?)['\"]\))",
                _re.MULTILINE
            )
            for i, line in enumerate(lines, 1):
                m = patt.match(line.strip())
                if m:
                    out.append(f"  line {i:4d} │ {line.strip()}")
            return '\n'.join(out)

        # ── MAP MODE ─────────────────────────────────────────────────────
        if mode == "map":
            out = [
                f"🗺️  STRUCTURE MAP — {os.path.basename(path)}\n"
                f"    {total_lines} lines | {file_size:,} chars\n"
                f"{'═'*54}"
            ]

            iface_re   = _re.compile(r'^\s*(?:export\s+)?interface\s+(\w+)')
            type_re    = _re.compile(r'^\s*(?:export\s+)?type\s+(\w+)\s*=')
            class_re   = _re.compile(r'^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)')
            fn_re      = _re.compile(
                r'^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)'
            )
            arrow_re   = _re.compile(
                r'^\s*(?:export\s+)?const\s+(\w+)\s*[=:]\s*(?:async\s*)?\('
            )
            field_re   = _re.compile(r'^\s+(\w+)\??:\s*(.+?)[;,]?\s*$')

            current_block = None
            brace_depth   = 0

            for i, line in enumerate(lines, 1):
                m = iface_re.match(line)
                if m:
                    current_block = (m.group(1), 'interface', i)
                    brace_depth   = line.count('{') - line.count('}')
                    out.append(f"\n  INTERFACE {m.group(1)}  [line {i}]")
                    continue

                m = type_re.match(line)
                if m:
                    out.append(f"\n  TYPE {m.group(1)}  [line {i}]")
                    continue

                m = class_re.match(line)
                if m:
                    current_block = (m.group(1), 'class', i)
                    brace_depth   = line.count('{') - line.count('}')
                    out.append(f"\n  CLASS {m.group(1)}  [line {i}]")
                    continue

                m = fn_re.match(line) or arrow_re.match(line)
                if m and not current_block:
                    out.append(f"  fn {m.group(1)}()  [line {i}]")
                    continue

                if current_block:
                    brace_depth += line.count('{') - line.count('}')
                    fm = field_re.match(line)
                    if fm and brace_depth > 0:
                        optional = '?' in line.split(':')[0]
                        flag     = " [optional]" if optional else " [required]"
                        out.append(
                            f"    {fm.group(1)}: {fm.group(2).rstrip(';, ')}{flag}"
                            f"  [line {i}]"
                        )
                    if brace_depth <= 0:
                        current_block = None

            out.append(
                f"\n{'─'*54}\n"
                f"  ➡ Next: mode='extract' target='InterfaceName' for full definition,\n"
                f"          mode='section' target='start,end' for raw lines."
            )
            return '\n'.join(out)

        # ── EXTRACT — pull a named interface/class/function ────────────────
        if mode == "extract":
            if not target:
                return "❌ extract mode requires target='InterfaceName'"
            import re as _re
            start_line = None
            patt = _re.compile(
                rf'(?:interface|class|type|function|const)\s+{_re.escape(target)}\b'
            )
            for i, line in enumerate(lines, 1):
                if patt.search(line):
                    start_line = i
                    break
            if not start_line:
                return (
                    f"❌ '{target}' not found in {os.path.basename(path)}.\n"
                    f"Run mode='map' to see all available names."
                )
            depth    = 0
            end_line = start_line
            started  = False
            for i in range(start_line - 1, total_lines):
                depth    += lines[i].count('{') - lines[i].count('}')
                end_line = i + 1
                if depth > 0:
                    started = True
                if started and depth <= 0:
                    break
            chunk = '\n'.join(
                f"{i+1:4d} │ {line}"
                for i, line in enumerate(lines[start_line-1:end_line], start=start_line-1)
            )
            return (
                f"📄 {target}  lines {start_line}–{end_line} "
                f"/ {total_lines}  [{os.path.basename(path)}]\n"
                f"{'─'*54}\n{chunk}"
            )

        # ── FIELDS — TypeScript interface field listing ────────────────────
        if mode == "fields":
            if not target:
                return "❌ fields mode requires target='InterfaceName'"
            import re as _re
            patt    = _re.compile(
                rf'(?:interface|type)\s+{_re.escape(target)}\b'
            )
            in_block = False
            depth    = 0
            out      = [f"🗂️  FIELDS — {target}  [{os.path.basename(path)}]\n{'─'*50}"]
            for i, line in enumerate(lines, 1):
                if patt.search(line):
                    in_block = True
                if in_block:
                    depth += line.count('{') - line.count('}')
                    fm = _re.match(r'^\s+(\w+)(\?)?\s*:\s*(.+?)[;,]?\s*$', line)
                    if fm:
                        name     = fm.group(1)
                        optional = bool(fm.group(2))
                        typ      = fm.group(3).rstrip(';, ')
                        flag     = " [optional]" if optional else " [required]"
                        out.append(f"  {name}: {typ}{flag}  [line {i}]")
                    if in_block and depth <= 0 and '{' in ''.join(lines[:i]):
                        break
            if len(out) == 1:
                return (
                    f"❌ Interface/Type '{target}' not found.\n"
                    f"Run mode='map' to see all names."
                )
            return '\n'.join(out)

    # ══════════════════════════════════════════════════════════════════════
    # FALLBACK — unsupported file type
    # ══════════════════════════════════════════════════════════════════════
    if mode == "map":
        preview = '\n'.join(f"{i+1:4d} │ {l}" for i, l in enumerate(lines[:30]))
        return (
            f"📄 {os.path.basename(path)}  [{total_lines} lines | {file_size:,} chars]\n"
            f"File type '{ext}' — Python/TS AST not available. Preview (first 30 lines):\n"
            f"{'─'*50}\n{preview}\n{'─'*50}\n"
            f"Use mode='section' target='start,end' to read specific ranges."
        )

    return (
        f"❌ mode='{mode}' not supported for '{ext}' files.\n"
        f"Supported modes for this type: section."
    )
