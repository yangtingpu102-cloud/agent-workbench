#!/usr/bin/env python3
"""agent-workbench helper. Stdlib only.

  python scripts/wb.py new <plugin> <skill>   scaffold a skill (and the plugin if new)
  python scripts/wb.py validate               check skills, manifests and both marketplaces
  python scripts/wb.py budget [--max N]       estimate always-loaded token cost of this repo's plugins
  python scripts/wb.py lint [PATH]            audit a project's CLAUDE.md / AGENTS.md / skills token budget
  python scripts/wb.py link [--host H]        link skills into ~/.claude/skills and ~/.agents/skills
  python scripts/wb.py unlink [--host H]      remove only links that point into this repo
  python scripts/wb.py parity                 compare Claude Code and Codex (shared rules, this repo's plugins); read-only
  python scripts/wb.py sync --host H          show how a host's global rules differ from shared/ai-shared.md;
                                              --apply writes only the marked block, after a backup

Set WB_HOME to use a directory other than your home (useful for testing).
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"
CLAUDE_MKT = ROOT / ".claude-plugin" / "marketplace.json"
CODEX_MKT = ROOT / ".agents" / "plugins" / "marketplace.json"
TEMPLATE = ROOT / "templates" / "skill" / "SKILL.md"
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_SKILL_LINES = 500

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ---------- helpers ----------

def est_tokens(text: str) -> int:
    """Rough estimate: about 4 ASCII chars per token, about 1 token per non-ASCII (e.g. CJK) char."""
    ascii_n = sum(1 for c in text if ord(c) < 128)
    return round(ascii_n / 4 + (len(text) - ascii_n))


def rel(p: Path) -> str:
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:  # outside the repository: show the full path
        return str(p)


def load_json(path: Path, errors: list[str]) -> dict | None:
    if not path.is_file():
        errors.append(f"{rel(path)}: missing")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        errors.append(f"{rel(path)}: invalid JSON ({e})")
        return None


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def read_frontmatter(path: Path) -> tuple[dict[str, str], str]:
    """Parse the flat YAML subset skills use: `key: value`, indented continuations, > and | blocks."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta: dict[str, str] = {}
    key = None
    for line in text[3:end].strip("\n").splitlines():
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m:
            key = m.group(1)
            value = m.group(2).strip()
            meta[key] = "" if value in (">", "|", ">-", "|-") else value
        elif key and line[:1] in (" ", "\t"):
            meta[key] = (meta[key] + " " + line.strip()).strip()
    for k, v in meta.items():
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            meta[k] = v[1:-1]
    return meta, text[end + 4:].lstrip("\n")


def plugin_dirs() -> list[Path]:
    return sorted(p for p in PLUGINS.iterdir() if p.is_dir()) if PLUGINS.is_dir() else []


def skill_files() -> list[Path]:
    return sorted(PLUGINS.glob("*/skills/*/SKILL.md"))


def source_path(entry: dict) -> str | None:
    src = entry.get("source")
    return src.get("path") if isinstance(src, dict) else src


# ---------- new ----------

def create_plugin(name: str, description: str) -> None:
    base = PLUGINS / name
    common = {"name": name, "version": "0.1.0", "description": description,
              "author": {"name": "wayne yan"}, "license": "MIT"}
    save_json(base / ".claude-plugin" / "plugin.json",
              {"$schema": "https://anthropic.com/claude-code/plugin.schema.json", **common})
    save_json(base / ".codex-plugin" / "plugin.json",
              {**common, "skills": "./skills/",
               "interface": {"displayName": name.replace("-", " ").title(), "shortDescription": description,
                             "category": "Developer Tools", "capabilities": ["Read"]}})
    claude = json.loads(CLAUDE_MKT.read_text(encoding="utf-8"))
    claude["plugins"].append({"name": name, "source": f"./plugins/{name}", "description": description})
    save_json(CLAUDE_MKT, claude)
    codex = json.loads(CODEX_MKT.read_text(encoding="utf-8"))
    codex["plugins"].append({"name": name, "source": {"source": "local", "path": f"./plugins/{name}"},
                             "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                             "category": "Developer Tools"})
    save_json(CODEX_MKT, codex)
    print(f"created plugin {rel(base)} and listed it in both marketplaces")


def cmd_new(args) -> int:
    for n in (args.plugin, args.skill):
        if not NAME_RE.match(n) or len(n) > 64:
            print(f"invalid name '{n}': use lowercase letters, digits and single hyphens (max 64)")
            return 1
    if not (PLUGINS / args.plugin).is_dir():
        create_plugin(args.plugin, args.description or "TODO: one-line description")
    skill_dir = PLUGINS / args.plugin / "skills" / args.skill
    if skill_dir.exists():
        print(f"{rel(skill_dir)} already exists")
        return 1
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(TEMPLATE.read_text(encoding="utf-8").replace("{{name}}", args.skill),
                                        encoding="utf-8", newline="\n")
    print(f"created {rel(skill_dir)}/SKILL.md. Write the description first, then run: python scripts/wb.py validate")
    return 0


# ---------- validate ----------

def cmd_validate(_args) -> int:
    errors: list[str] = []
    warnings: list[str] = []

    markets: dict[str, dict[str, dict]] = {}
    for label, path in (("claude", CLAUDE_MKT), ("codex", CODEX_MKT)):
        data = load_json(path, errors)
        entries = data.get("plugins", []) if data else []
        names = [e.get("name") for e in entries]
        for dup in sorted({str(n) for n in names if names.count(n) > 1}):
            errors.append(f"{rel(path)}: plugin '{dup}' is listed more than once")
        markets[label] = {e.get("name"): e for e in entries}

    for plugin in plugin_dirs():
        name = plugin.name
        manifests = {}
        for label, sub in (("claude", ".claude-plugin"), ("codex", ".codex-plugin")):
            data = load_json(plugin / sub / "plugin.json", errors)
            if data is None:
                continue
            manifests[label] = data
            if data.get("name") != name:
                errors.append(f"plugins/{name}/{sub}/plugin.json: name '{data.get('name')}' != directory '{name}'")
        if len(manifests) == 2:
            for field in ("version", "description"):
                if manifests["claude"].get(field) != manifests["codex"].get(field):
                    errors.append(f"plugins/{name}: '{field}' differs between the Claude and Codex manifests")
        for label, entries in markets.items():
            entry = entries.get(name)
            if entry is None:
                errors.append(f"plugins/{name}: not listed in the {label} marketplace")
                continue
            path = source_path(entry)
            if not path or not path.startswith("./") or (ROOT / path).resolve() != plugin.resolve():
                errors.append(f"{label} marketplace: '{name}' source {path!r} should be './plugins/{name}'")
        for hooks_file in sorted(plugin.glob("hooks/*.json")):
            load_json(hooks_file, errors)
        codex_hooks = manifests.get("codex", {}).get("hooks")
        if isinstance(codex_hooks, str) and not (plugin / codex_hooks).is_file():
            errors.append(f"plugins/{name}/.codex-plugin/plugin.json: hooks file {codex_hooks!r} not found")
        if not list(plugin.glob("skills/*/SKILL.md")):
            warnings.append(f"plugins/{name}: has no skills")

    for label, entries in markets.items():
        for n in entries:
            if not (PLUGINS / str(n)).is_dir():
                errors.append(f"{label} marketplace lists '{n}' but plugins/{n} does not exist")

    for skill_md in skill_files():
        where = rel(skill_md)
        meta, body = read_frontmatter(skill_md)
        if not meta:
            errors.append(f"{where}: missing YAML frontmatter")
            continue
        name, desc = meta.get("name", ""), meta.get("description", "")
        if name != skill_md.parent.name:
            errors.append(f"{where}: name '{name}' must equal the directory name '{skill_md.parent.name}'")
        if not NAME_RE.match(name) or len(name) > 64:
            errors.append(f"{where}: name must be lowercase letters, digits and single hyphens (max 64)")
        if not desc:
            errors.append(f"{where}: description is required")
        elif len(desc) > 1024:
            errors.append(f"{where}: description is {len(desc)} chars (max 1024)")
        elif "use when" not in desc.lower():
            warnings.append(f"{where}: description should say 'Use when ...' with trigger phrases")
        if "TODO" in desc or "TODO" in body:
            warnings.append(f"{where}: still contains TODO")
        lines = len(body.splitlines())
        if lines > MAX_SKILL_LINES:
            warnings.append(f"{where}: {lines} lines; move detail into references/ (target < {MAX_SKILL_LINES})")

    for w in warnings:
        print(f"warn  {w}")
    for e in errors:
        print(f"ERROR {e}")
    print(f"{len(plugin_dirs())} plugins, {len(skill_files())} skills: "
          f"{len(errors)} errors, {len(warnings)} warnings")
    return 1 if errors else 0


# ---------- budget ----------

def cmd_budget(args) -> int:
    print("Estimates only (ASCII/4 + 1 per non-ASCII char). Exact: `claude plugin details <plugin>` or /context.\n")
    user_total = 0
    print("Loaded in EVERY session of every user who installs the plugins (skill name + description):")
    for skill_md in skill_files():
        meta, body = read_frontmatter(skill_md)
        t_meta = est_tokens(meta.get("name", "") + " " + meta.get("description", ""))
        user_total += t_meta
        print(f"  {t_meta:>6}  {rel(skill_md)}   (body when used: ~{est_tokens(body)})")
    print(f"  {user_total:>6}  TOTAL per session\n")

    print("Loaded only for contributors working inside this repo:")
    for name in ("AGENTS.md", "CLAUDE.md"):
        p = ROOT / name
        if p.is_file():
            print(f"  {est_tokens(p.read_text(encoding='utf-8')):>6}  {name}")

    if args.max is not None and user_total > args.max:
        print(f"\nFAIL: per-session skill metadata ~{user_total} tokens exceeds --max {args.max}")
        return 1
    return 0


# ---------- lint ----------

CODEX_DOC_MAX_BYTES = 32 * 1024  # Codex default project_doc_max_bytes
CLAUDE_MAX_LINES = 200           # Claude Code guidance for CLAUDE.md size
IMPORT_DEPTH = 5                 # Claude Code follows @imports up to 5 hops
EXACT_MODEL = "claude-opus-5-5"
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target", ".tox"}
SKILL_DIRS = {"claude": [".claude/skills"], "codex": [".agents/skills", ".codex/skills"]}
LEVELS = ("ERROR", "warn", "info")


class Loaded:
    def __init__(self, path: Path, scope: str, how: str = ""):
        self.path, self.scope, self.how = path, scope, how
        self.text = path.read_text(encoding="utf-8", errors="replace")
        self.tokens = est_tokens(self.text)
        self.lines = len(self.text.splitlines())


def strip_code(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    return re.sub(r"`[^`\n]*`", "", text)


def paragraphs(text: str) -> set[str]:
    return {p for p in (" ".join(b.split()) for b in re.split(r"\n\s*\n", text)) if len(p) >= 40}


def same_file(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


class Lint:
    def __init__(self, root: Path, home: Path, use_global: bool):
        self.root, self.home, self.use_global = root.resolve(), home, use_global
        self.notes: list[tuple[str, str]] = []

    def note(self, level: str, msg: str) -> None:
        if (level, msg) not in self.notes:
            self.notes.append((level, msg))

    def show(self, p: Path) -> str:
        p = p.resolve()
        for base, prefix in ((self.root, ""), (self.home.resolve(), "~/")):
            if p.is_relative_to(base):
                return prefix + p.relative_to(base).as_posix()
        return str(p)

    def imports(self, path: Path, text: str) -> tuple[list[Path], list[str]]:
        """Claude Code `@path` imports outside code: relative to the importing file, `~/` = home."""
        found, missing = [], []
        for m in re.finditer(r"(?:^|\s)@(\S+)", strip_code(text)):
            raw = m.group(1).rstrip(".,;:)")
            if raw[:2] in ("~/", "~\\"):
                target = self.home / raw[2:]
            else:
                target = Path(raw) if Path(raw).is_absolute() else path.parent / raw
            if target.is_file():
                found.append(target)
            elif "/" in raw or "\\" in raw or raw.lower().endswith(".md"):
                missing.append(raw)
        return found, missing

    def load(self, path: Path, scope: str, out: list[Loaded], how: str = "", depth: int = 0) -> None:
        if any(same_file(path, f.path) for f in out):
            return
        item = Loaded(path, scope, how)
        out.append(item)
        found, missing = self.imports(path, item.text)
        for raw in missing:
            self.note("warn", f"{self.show(path)}: @{raw} not found")
        if depth < IMPORT_DEPTH:
            for target in found:
                self.load(target, scope, out, "@import", depth + 1)

    def claude_files(self) -> list[Loaded]:
        out: list[Loaded] = []
        if self.use_global and (self.home / ".claude" / "CLAUDE.md").is_file():
            self.load(self.home / ".claude" / "CLAUDE.md", "user", out)
        present = [p for p in (self.root / "CLAUDE.md", self.root / ".claude" / "CLAUDE.md",
                               self.root / "CLAUDE.local.md") if p.is_file()]
        for p in present:
            self.load(p, "project", out)
        if not any(p.name == "CLAUDE.md" for p in present) and (self.root / "AGENTS.md").is_file():
            self.load(self.root / "AGENTS.md", "project", out, "read natively when there is no CLAUDE.md")
        return out

    def subdirs(self) -> list[Path]:
        found = []
        for dirpath, dirnames, _ in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            if Path(dirpath) != self.root:
                found.append(Path(dirpath))
        return found

    def ancestry(self, d: Path) -> list[Path]:
        """Directories from the project root down to d, inclusive."""
        chain = [d]
        while chain[-1] != self.root:
            chain.append(chain[-1].parent)
        return chain[::-1]

    def nested_claude(self, root_files: list[Loaded]) -> list[tuple[Path, list[Loaded]]]:
        """CLAUDE.md below the root: loaded with its ancestors when Claude works inside that directory."""
        out = []
        for d in self.subdirs():
            if (d / "CLAUDE.md").is_file():
                files = [f for f in root_files if f.scope == "project"]
                for anc in self.ancestry(d)[1:]:
                    if (anc / "CLAUDE.md").is_file():
                        self.load(anc / "CLAUDE.md", "project", files)
                out.append((d / "CLAUDE.md", files))
        return out

    def codex_doc(self, d: Path) -> Path | None:
        return next((d / n for n in ("AGENTS.override.md", "AGENTS.md") if (d / n).is_file()), None)

    def chain_docs(self, d: Path) -> list[Path]:
        """Codex concatenates one doc per directory from the project root down to the working dir."""
        return [doc for doc in map(self.codex_doc, self.ancestry(d)) if doc]

    def chain_bytes(self, d: Path) -> int:
        return sum(doc.stat().st_size for doc in self.chain_docs(d))

    def chain_tokens(self, d: Path) -> int:
        return sum(est_tokens(doc.read_text(encoding="utf-8", errors="replace")) for doc in self.chain_docs(d))

    def codex_files(self) -> tuple[list[Loaded], list[tuple[Path, int, int]]]:
        out: list[Loaded] = []
        if self.use_global:
            env_home = os.environ.get("CODEX_HOME") if not os.environ.get("WB_HOME") else None
            doc = self.codex_doc(Path(env_home) if env_home else self.home / ".codex")
            if doc:
                out.append(Loaded(doc, "user"))
        doc = self.codex_doc(self.root)
        if doc:
            out.append(Loaded(doc, "project"))
        nested = [(self.codex_doc(d), self.chain_bytes(d), self.chain_tokens(d))
                  for d in self.subdirs() if self.codex_doc(d)]
        return out, nested

    def skills(self, host: str) -> list[tuple[str, str, int, int]]:
        """(location, scope, skill count, metadata tokens) for skills the host loads every session."""
        rows = []
        for rel_dir in SKILL_DIRS[host]:
            bases = [(self.root / rel_dir, "project")] + ([(self.home / rel_dir, "user")] if self.use_global else [])
            for base, scope in bases:
                files = sorted(base.glob("*/SKILL.md"))
                tokens = 0
                for md in files:
                    meta, _ = read_frontmatter(md)
                    desc = meta.get("description", "")
                    if len(desc) > 1024:
                        self.note("ERROR", f"{self.show(md)}: description is {len(desc)} chars (max 1024)")
                    tokens += est_tokens(meta.get("name", "") + " " + desc)
                if files:
                    rows.append((self.show(base), scope, len(files), tokens))
        return rows

    def check_files(self, host: str, files: list[Loaded]) -> None:
        owner: dict[str, Loaded] = {}
        wasted, sample, pairs = 0, "", set()
        for f in files:
            for para in paragraphs(f.text):
                if para in owner:
                    wasted += est_tokens(para)
                    sample = sample or para
                    pairs.add(f"{self.show(owner[para].path)} = {self.show(f.path)}")
                else:
                    owner[para] = f
        if wasted:
            self.note("warn", f"{host}: ~{wasted} tokens of paragraphs load twice ({'; '.join(sorted(pairs))}), "
                              f"e.g. \"{sample[:60]}...\"")
        for f in files:
            non_ascii = sum(1 for ch in f.text if ord(ch) > 127)
            if f.text and non_ascii / len(f.text) > 0.3:
                self.note("info", f"{self.show(f.path)}: {round(100 * non_ascii / len(f.text))}% non-ASCII, so the "
                                  "estimate is rough (try --exact); English instructions usually cost somewhat fewer tokens")

    def check_drift(self, claude: list[Loaded]) -> None:
        claude_md, agents_md = self.root / "CLAUDE.md", self.root / "AGENTS.md"
        if not (claude_md.is_file() and agents_md.is_file()) or any(same_file(agents_md, f.path) for f in claude):
            return
        a = paragraphs(agents_md.read_text(encoding="utf-8", errors="replace"))
        shared = a & paragraphs(claude_md.read_text(encoding="utf-8", errors="replace"))
        if a and len(shared) / len(a) >= 0.3:
            self.note("info", f"CLAUDE.md repeats {len(shared)} of {len(a)} AGENTS.md paragraphs; "
                              "replace the copy with `@AGENTS.md` so the two cannot drift apart")


def exact_counter():
    """Exact Claude token counts via the official SDK (optional; this repo stays stdlib-only otherwise)."""
    try:
        from anthropic import Anthropic
    except ImportError:
        return None, "the `anthropic` package is not installed: uv run --with anthropic python scripts/wb.py lint --exact"
    try:
        client = Anthropic()

        def count(text: str) -> int:
            return client.messages.count_tokens(
                model=EXACT_MODEL, messages=[{"role": "user", "content": text}]).input_tokens

        overhead = count("a") - 1  # tokens the message wrapper adds
    except Exception as e:  # missing credentials, network, API errors: fall back to estimates
        return None, f"token counting failed ({type(e).__name__}: {e})"
    return (lambda text: max(0, count(text) - overhead) if text.strip() else 0), ""


def print_host(title: str, files: list[Loaded], skill_rows: list[tuple[str, str, int, int]], lint: Lint) -> int:
    print(title)
    rows = [(f.tokens, lint.show(f.path), f.scope, f.how) for f in files]
    rows += [(t, f"{n} skill(s) in {loc}", scope, "name + description") for loc, scope, n, t in skill_rows]
    for tokens, label, scope, how in rows:
        print(f"  {tokens:>7}  {label}  ({scope}{', ' + how if how else ''})")
    project = sum(r[0] for r in rows if r[2] == "project")
    user = sum(r[0] for r in rows if r[2] == "user")
    print(f"  {project + user:>7}  TOTAL  (project {project}, user {user})\n")
    return project


def cmd_lint(args) -> int:
    root = Path(args.path)
    if not root.is_dir():
        print(f"not a directory: {root}")
        return 1
    lint = Lint(root, link_home(), not args.no_global)
    claude = lint.claude_files()
    claude_nested = lint.nested_claude(claude)
    codex, codex_nested = lint.codex_files()
    claude_skills, codex_skills = lint.skills("claude"), lint.skills("codex")
    all_claude = {id(f): f for f in claude + [f for _, files in claude_nested for f in files]}

    label = "estimated: ASCII/4 + 1 per non-ASCII char"
    if args.exact:
        counter, problem = exact_counter()
        try:
            if not counter:
                raise RuntimeError(problem)
            for f in all_claude.values():
                f.tokens = counter(f.text)
            label = f"Claude counted exactly with {EXACT_MODEL}; Codex {label}"
        except Exception as e:  # a failure mid-run reverts every file, so the table never mixes methods
            for f in all_claude.values():
                f.tokens = est_tokens(f.text)
            lint.note("warn", f"--exact skipped, showing estimates: {e}")

    print(f"Instruction budget for {lint.root}\n({label}; skill metadata is always estimated)\n")
    totals = {"Claude Code at the repo root": print_host("Claude Code: loaded every session", claude, claude_skills, lint),
              "Codex at the repo root": print_host("Codex: loaded every session at the repo root",
                                                   codex, codex_skills, lint)}
    if claude_nested:
        print("Claude Code: nested CLAUDE.md, loaded with its ancestors when working inside that directory")
        for doc, files in claude_nested:
            tokens = sum(f.tokens for f in files)
            totals[f"the Claude chain ending at {lint.show(doc)}"] = tokens
            print(f"  {tokens:>7}  chain ending at {lint.show(doc)}")
        print()
    if codex_nested:
        print("Codex: nested docs, loaded with their ancestors when working inside that directory")
        for doc, size, tokens in codex_nested:
            totals[f"the Codex chain ending at {lint.show(doc)}"] = tokens
            print(f"  {tokens:>7}  chain ending at {lint.show(doc)} ({size} bytes)")
        print()

    plugin_skills = list(lint.root.glob("plugins/*/skills/*/SKILL.md"))
    if plugin_skills:
        lint.note("info", f"{len(plugin_skills)} plugin skill(s) under plugins/ are not counted: they load only "
                          "where the plugin is installed (see `wb.py budget`)")
    for f in all_claude.values():
        if f.lines > CLAUDE_MAX_LINES and f.scope == "project":
            lint.note("warn", f"{lint.show(f.path)}: {f.lines} lines (Claude Code suggests under {CLAUDE_MAX_LINES})")
    root_chain = [(f.path, lint.chain_bytes(lint.root)) for f in codex if f.scope == "project"]
    for doc, size in root_chain + [(doc, size) for doc, size, _ in codex_nested]:
        if size > CODEX_DOC_MAX_BYTES:
            lint.note("ERROR", f"{lint.show(doc)}: the Codex doc chain is {size} bytes; Codex stops reading at "
                               f"{CODEX_DOC_MAX_BYTES} (project_doc_max_bytes), so the rest is cut off")
    for f in codex:
        if lint.imports(f.path, f.text)[0]:
            lint.note("info", f"{lint.show(f.path)}: has @import lines; Codex is not documented to expand them, "
                              "so the imported file may not load in Codex")
    lint.check_files("Claude Code", claude)
    lint.check_files("Codex", codex)
    lint.check_drift(claude)

    for level in LEVELS:
        for lvl, msg in lint.notes:
            if lvl == level:
                print(f"{level:<5} {msg}")
    errors = sum(1 for lvl, _ in lint.notes if lvl == "ERROR")
    over = {name: t for name, t in totals.items() if args.max_tokens is not None and t > args.max_tokens}
    for name, tokens in over.items():
        print(f"FAIL  {name} costs ~{tokens} tokens of project instructions per session (--max-tokens {args.max_tokens})")
    return 1 if errors or over else 0


# ---------- shared rules: sync / parity ----------

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

SHARED_FILE = ROOT / "shared" / "ai-shared.md"
START_MARK, END_MARK = "<!-- ai-shared:start -->", "<!-- ai-shared:end -->"
DIFF_MAX_LINES = 120
RULES_FILE_NAMES = {"AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "CLAUDE.local.md"}
# Slides and document tooling is Codex's. Sync never replaces text that mentions it.
PROTECTED_RE = re.compile(r"slide|presentation|pptx?\b|spreadsheet|\bxlsx?\b|\bpdf\b|\bdocx?\b|document", re.I)
LINE_RE = re.compile(r"[^\n]*\n|[^\n]+")
FENCE_RE = re.compile(r"^ {0,3}(```|~~~)")
HEADING_RE = re.compile(r"^ {0,3}#{1,6}(\s|$)")
SETEXT_RE = re.compile(r"^ {0,3}(=+|-+)\s*$")
BULLET_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s")


class SyncError(Exception):
    pass


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME") if not os.environ.get("WB_HOME") else None
    return Path(env) if env else link_home() / ".codex"


def rule_file(host: str) -> Path:
    return link_home() / ".claude" / "CLAUDE.md" if host == "claude" else codex_home() / "AGENTS.md"


def read_raw(path: Path) -> tuple[bytes, str]:
    data = path.read_bytes()
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError:
        raise SyncError(f"{path} is not valid UTF-8; nothing was changed")


def split_lines(raw: str) -> list[str]:
    """Lines with their own line endings kept, so joining them gives back exactly the original text."""
    return LINE_RE.findall(raw)


def eol_of(line: str) -> str:
    return "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")


def dominant_eol(raw: str) -> str:
    return "\r\n" if raw.count("\r\n") * 2 > raw.count("\n") else "\n"


def fenced_flags(lines: list[str]) -> list[bool]:
    """True for lines inside a fenced code block (and the fence lines), where markers and headings are examples."""
    flags, fence = [], None
    for line in lines:
        match = FENCE_RE.match(line)
        if match:
            fence = match.group(1) if fence is None else (None if match.group(1) == fence else fence)
            flags.append(True)
        else:
            flags.append(fence is not None)
    return flags


def canonical_rules() -> str:
    if not SHARED_FILE.is_file():
        raise SyncError(f"{rel(SHARED_FILE)} is missing")
    text = SHARED_FILE.read_text(encoding="utf-8").replace("\r\n", "\n").strip("\n")
    if not text or START_MARK in text or END_MARK in text:
        raise SyncError(f"{rel(SHARED_FILE)} must be plain rules text without the ai-shared markers")
    return text


def render_block(rules: str) -> str:
    return f"{START_MARK}\n{rules}\n{END_MARK}"


def find_span(lines: list[str]) -> tuple[int, int] | None:
    """Line indexes of the block's start and end markers. Only a marker alone on its line counts, outside code."""
    flags = fenced_flags(lines)
    starts = [i for i, line in enumerate(lines) if not flags[i] and line.strip() == START_MARK]
    ends = [i for i, line in enumerate(lines) if not flags[i] and line.strip() == END_MARK]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or starts[0] > ends[0]:
        raise SyncError("the ai-shared markers are unbalanced (exactly one start followed by one end is needed); "
                        "fix the file by hand first")
    return starts[0], ends[0]


def refuse_protected(text: str, what: str) -> None:
    hit = PROTECTED_RE.search(text)
    if hit:
        raise SyncError(f"{what} mentions {hit.group(0)!r}. Slides and document tooling belongs to Codex and sync "
                        "never overwrites it; move that text out of the way first")


def adopt_span(lines: list[str], heading: str) -> tuple[int, int]:
    """Where the section under `heading` starts and ends. It must be a plain bullet list, so that replacing it
    cannot delete another section (a Setext or indented heading, a table, a code block, prose)."""
    flags = fenced_flags(lines)
    found = [i for i, line in enumerate(lines) if not flags[i] and line.strip() == heading.strip()]
    if len(found) != 1:
        raise SyncError(f"the heading {heading!r} must appear exactly once outside code blocks (found {len(found)})")
    start, end = found[0], len(lines)
    for j in range(start + 1, len(lines)):
        if flags[j]:
            continue
        line, following = lines[j], (lines[j + 1] if j + 1 < len(lines) else "")
        setext_title = bool(line.strip()) and not BULLET_RE.match(line) and bool(SETEXT_RE.match(following))
        if HEADING_RE.match(line) or line.startswith("@") or setext_title:
            end = j
            break
    for j in range(start + 1, end):
        line = lines[j]
        if flags[j] or (line.strip() and not BULLET_RE.match(line) and not line.startswith((" ", "\t"))):
            raise SyncError(f"the section under {heading!r} is not a plain bullet list (line {j + 1}: "
                            f"{line.strip()[:50]!r}); replacing it could delete other content. Use --insert instead")
    return start, end


def apply_rules(raw: str, rules: str, adopt_heading: str | None, insert: bool) -> tuple[str, str]:
    """The new file text. Everything outside the replaced span is carried over as the very same characters."""
    lines = split_lines(raw)
    block = render_block(rules).split("\n")
    span = find_span(lines)
    if span:
        start, end = span
        refuse_protected("".join(lines[start:end + 1]), "the existing ai-shared block")
        eol = eol_of(lines[start]) or dominant_eol(raw)
        new_block = [line + eol for line in block[:-1]] + [block[-1] + eol_of(lines[end])]
        return "".join(lines[:start] + new_block + lines[end + 1:]), "Only the marked ai-shared block changes."
    if adopt_heading:
        start, end = adopt_span(lines, adopt_heading)
        refuse_protected("".join(lines[start:end]), f"the section '{adopt_heading}'")
        eol = eol_of(lines[start]) or dominant_eol(raw)
        tail = lines[end:]
        new_block = [line + eol for line in block] + ([eol] if tail else [])
        return "".join(lines[:start] + new_block + tail), f"Only the section '{adopt_heading}' changes."
    if insert:
        eol = dominant_eol(raw)
        return eol.join(block) + eol + (eol + raw if raw else ""), "Only a new block is added at the top."
    raise SyncError("the file has no ai-shared block. Say how to add it: --adopt-heading '<existing heading>' "
                    "replaces that section with the block, --insert adds the block at the top")


def make_backup(path: Path, data: bytes) -> Path:
    stamp = f"{dt.datetime.now():%Y%m%d-%H%M%S-%f}"
    for n in range(100):
        backup = path.with_name(f"{path.name}.bak-{stamp}" + (f"-{n}" if n else ""))
        try:
            with open(backup, "xb") as handle:  # exclusive: an earlier backup is never overwritten
                handle.write(data)
            return backup
        except FileExistsError:
            continue
    raise SyncError("could not find an unused backup name")


def write_checked(path: Path, expected_old: bytes, new_bytes: bytes) -> None:
    """Replace the file, but only if it still holds the bytes the change was planned from."""
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_bytes(new_bytes)
        shutil.copymode(path, temp)
        if path.read_bytes() != expected_old:
            raise SyncError(f"{path} changed while syncing; nothing was written. Run it again")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def cmd_sync(args) -> int:
    try:
        rules = canonical_rules()
        path = Path(args.file) if args.file else rule_file(args.host)
        if args.apply and args.file and path.name not in RULES_FILE_NAMES:
            raise SyncError(f"--apply with --file only writes rules files ({', '.join(sorted(RULES_FILE_NAMES))}), "
                            f"not {path.name}")
        if not path.is_file():
            raise SyncError(f"{path} does not exist")
        old_bytes, old = read_raw(path)
        new, scope = apply_rules(old, rules, args.adopt_heading, args.insert)
    except SyncError as e:
        print(f"error: {e}")
        return 2
    if new == old:
        print(f"{path}: already in sync with {rel(SHARED_FILE)}")
        return 0
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"{path} (now)", f"{path} (after sync)",
                                     lineterm="", n=2))
    print("\n".join(diff[:DIFF_MAX_LINES]))
    if len(diff) > DIFF_MAX_LINES:
        print(f"... {len(diff) - DIFF_MAX_LINES} more diff lines")
    print(f"\n{scope}")
    if not args.apply:
        print("Dry run: nothing was written. Add --apply to write it; a backup is kept next to the file.")
        return 1
    try:
        backup = make_backup(path, old_bytes)
        try:
            write_checked(path, old_bytes, new.encode("utf-8"))
        except SyncError:
            backup.unlink(missing_ok=True)
            raise
        if path.read_bytes() != new.encode("utf-8"):
            raise SyncError(f"the written file does not match what was planned; restore it from {backup}")
    except SyncError as e:
        print(f"error: {e}")
        return 2
    print(f"Written. Backup: {backup}")
    return 0


def rules_status(host: str, rules: str) -> tuple[str, bool]:
    path = rule_file(host)
    where = short(path)
    if not path.is_file():
        return f"{where}: file not found", True
    try:
        lines = split_lines(read_raw(path)[1])
        span = find_span(lines)
    except SyncError as e:
        return f"{where}: DIFFERS ({e})", True
    if span is None:
        return f"{where}: DIFFERS (no ai-shared block, so its own copy of the rules can drift)", True
    current = "\n".join(line.rstrip("\r\n") for line in lines[span[0]:span[1] + 1])
    if current != render_block(rules):
        return f"{where}: DIFFERS (the block is not the shared text)", True
    return f"{where}: in sync", False


def toml_enabled_plugins(path: Path) -> dict[str, bool]:
    """`[plugins."name@market"]` tables and their `enabled` flag, read with a real TOML parser."""
    if tomllib is None:
        raise SyncError("reading Codex's config.toml needs Python 3.11 or newer")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise SyncError(f"{path} could not be read as TOML ({e})")
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return {}
    return {key: value.get("enabled") is True for key, value in plugins.items() if isinstance(value, dict)}


def read_json_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def version_key(name: str) -> tuple:
    return tuple((0, int(part)) if part.isdigit() else (-1, part) for part in re.findall(r"\d+|[A-Za-z]+", name))


def payload(copy: Path) -> dict[str, str]:
    """sha256 of every file that carries behavior: skills and hooks."""
    files = {}
    for sub in ("skills", "hooks"):
        base = copy / sub
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                    files[path.relative_to(copy).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def plugin_copy_info(enabled: bool, where: str, copy: Path | None, recorded: str | None = None) -> dict:
    version, found = recorded, bool(copy and copy.is_dir())
    if found:
        for manifest in (copy / ".claude-plugin" / "plugin.json", copy / ".codex-plugin" / "plugin.json"):
            try:
                version = json.loads(manifest.read_text(encoding="utf-8")).get("version") or version
                break
            except (OSError, ValueError):
                continue
    return {"enabled": enabled, "where": where, "version": version, "found": found,
            "files": payload(copy) if found else {}, "also_cached": []}


def diff_payload(repo: dict[str, str], host: dict[str, str]) -> list[str]:
    changed = [f"{name} (changed)" for name in sorted(repo) if name in host and repo[name] != host[name]]
    missing = [f"{name} (missing)" for name in sorted(repo) if name not in host]
    extra = [f"{name} (extra)" for name in sorted(host) if name not in repo]
    return changed + missing + extra


def repo_marketplace() -> str | None:
    name = read_json_file(PLUGINS.parent / ".claude-plugin" / "marketplace.json").get("name")
    return name if isinstance(name, str) and name else None


def host_plugin_state(host: str, market: str) -> tuple[dict[str, dict], str | None]:
    """What a host loads for this repo's plugins, found by their marketplace-qualified identity `name@market`."""
    state: dict[str, dict] = {}
    if host == "claude":
        base = link_home() / ".claude"
        enabled = read_json_file(base / "settings.json").get("enabledPlugins") or {}
        registry = read_json_file(base / "plugins" / "installed_plugins.json")
        installed = registry.get("plugins", registry)
        markets = read_json_file(base / "plugins" / "known_marketplaces.json")
        for key, on in enabled.items():
            name, _, key_market = key.partition("@")
            if key_market != market:
                continue
            entries = [e for e in (installed.get(key) or []) if isinstance(e, dict)]
            entry = next((e for e in reversed(entries) if e.get("scope") == "user"), entries[-1] if entries else {})
            source = (markets.get(market) or {}).get("source") or {}
            live = Path(source["path"]) / "plugins" / name if source.get("source") == "directory" and source.get("path") else None
            if live and live.is_dir():  # a directory marketplace loads straight from the checkout
                state[name] = plugin_copy_info(bool(on), "live from the checkout", live)
            else:
                install = Path(entry["installPath"]) if entry.get("installPath") else None
                state[name] = plugin_copy_info(bool(on), "cached copy", install, entry.get("version"))
        return state, None
    config = codex_home() / "config.toml"
    try:
        enabled = toml_enabled_plugins(config) if config.is_file() else {}
    except SyncError as e:
        return state, str(e)
    for key, on in enabled.items():
        name, _, key_market = key.partition("@")
        if key_market != market:
            continue
        cache = codex_home() / "plugins" / "cache" / market / name
        versions = sorted((p for p in cache.glob("*") if p.is_dir()), key=lambda p: version_key(p.name)) if cache.is_dir() else []
        info = plugin_copy_info(on, "cached copy", versions[-1] if versions else None)
        info["also_cached"] = [p.name for p in versions[:-1]]  # older copies Codex may still hold; shown, not judged
        state[name] = info
    return state, None


def repo_plugins() -> dict[str, dict]:
    return {plugin.name: plugin_copy_info(True, "repo", plugin) for plugin in plugin_dirs()}


def cmd_parity(_args) -> int:
    try:
        rules = canonical_rules()
    except SyncError as e:
        print(f"error: {e}")
        return 2
    market = repo_marketplace()
    if not market:
        print("error: cannot read this repo's marketplace name from .claude-plugin/marketplace.json")
        return 2
    differs = 0
    print(f"Shared rules ({rel(SHARED_FILE)})")
    for host in ("claude", "codex"):
        line, bad = rules_status(host, rules)
        differs += bad
        print(f"  {host:<6} {line}")
    repo = repo_plugins()
    states, problems_by_host = {}, {}
    for host in ("claude", "codex"):
        states[host], problems_by_host[host] = host_plugin_state(host, market)
    print(f"\nPlugins of this repo (marketplace '{market}'), as each host loads them")
    for host, problem in problems_by_host.items():
        if problem:
            differs += 1
            print(f"  {host:<6} CANNOT COMPARE: {problem}")
    for name, info in repo.items():
        print(f"  {name} (repo {info['version']})")
        for host in ("claude", "codex"):
            if problems_by_host[host]:
                continue
            s = states[host].get(name)
            if not s:
                differs += 1
                print(f"    {host:<6} NOT INSTALLED from '{market}'")
                continue
            problems = []
            if not s["enabled"]:
                problems.append("disabled")
            if not s["found"]:
                problems.append("installed copy not found")
            else:
                if s["version"] != info["version"]:
                    problems.append(f"version {s['version']}")
                drift = diff_payload(info["files"], s["files"])
                if drift:
                    problems.append("files differ: " + ", ".join(drift[:3]) + (f" (+{len(drift) - 3} more)" if len(drift) > 3 else ""))
            differs += bool(problems)
            note = f"; older cached copies: {', '.join(s['also_cached'])}" if s["also_cached"] else ""
            print(f"    {host:<6} {s['version']} ({s['where']}) " + ("DIFFERS: " + "; ".join(problems) if problems else "ok") + note)
    hooked = [name for name, info in repo.items() if any(f.startswith("hooks/") for f in info["files"])]
    if hooked:
        print("\nHooks (a difference in the files themselves is counted above)")
        for name in hooked:
            claude, codex = states["claude"].get(name), states["codex"].get(name)
            c = "active (Claude runs an enabled plugin's hooks itself)" \
                if claude and claude["enabled"] and any(f.startswith("hooks/") for f in claude["files"]) else "not active"
            x = ("hook files present, UNVERIFIED: Codex runs plugin hooks only after you trust them with /hooks"
                 if codex and any(f.startswith("hooks/") for f in codex["files"]) else "no hook files in the installed copy")
            print(f"  {name}: claude {c}; codex {x}")
    print("\nNot compared on purpose: plugins outside this repo (slides, documents, pdf, spreadsheets, ...), models and "
          "effort, and the graphify and RTK sections of the rules files.")
    print("\n" + ("DIFFERENCES FOUND" if differs else "in sync"))
    return 1 if differs else 0


# ---------- link / unlink ----------

def link_home() -> Path:
    return Path(os.environ.get("WB_HOME") or Path.home())


def short(p: Path) -> str:
    return str(p).replace(str(link_home()), "~", 1)


def link_bases(host: str) -> list[Path]:
    home = link_home()
    bases = []
    if host in ("claude", "both"):
        bases.append(home / ".claude" / "skills")
    if host in ("codex", "both"):
        bases.append(home / ".agents" / "skills")
    return bases


def is_link(p: Path) -> bool:
    return p.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(p))


def link_target(p: Path) -> Path:
    return Path(os.path.realpath(p))


def cmd_link(args) -> int:
    for base in link_bases(args.host):
        base.mkdir(parents=True, exist_ok=True)
        for skill_md in skill_files():
            src = skill_md.parent.resolve()
            dst = base / src.name
            if is_link(dst):
                state = "already linked" if link_target(dst) == src else f"skipped: links to {link_target(dst)}"
                print(f"{state}  {short(dst)}")
                continue
            if dst.exists():
                print(f"skipped: a real directory already exists  {short(dst)}")
                continue
            if os.name == "nt":  # a junction needs no admin rights or Developer Mode
                import _winapi
                create = getattr(_winapi, "CreateJunction", None)
                if create:  # direct API call: no cmd.exe parsing of & or % in paths
                    create(str(src), str(dst))
                else:
                    subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)], check=True, capture_output=True)
            else:
                dst.symlink_to(src, target_is_directory=True)
            print(f"linked  {short(dst)} -> {rel(src)}")
    return 0


def cmd_unlink(args) -> int:
    root = ROOT.resolve()
    for base in link_bases(args.host):
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            if is_link(entry) and link_target(entry).is_relative_to(root):
                if os.name == "nt":
                    os.rmdir(entry)  # removes the junction only, never the target
                else:
                    entry.unlink()
                print(f"unlinked  {short(entry)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new")
    p.add_argument("plugin")
    p.add_argument("skill")
    p.add_argument("--description", help="plugin description, used only when the plugin is new")
    sub.add_parser("validate")
    p = sub.add_parser("budget")
    p.add_argument("--max", type=int, help="fail if per-session skill metadata exceeds N tokens")
    p = sub.add_parser("lint")
    p.add_argument("path", nargs="?", default=".", help="project to lint (default: current directory)")
    p.add_argument("--max-tokens", type=int, help="fail if a host's project instructions exceed N tokens")
    p.add_argument("--no-global", action="store_true", help="skip user-level files in your home directory")
    p.add_argument("--exact", action="store_true", help="count Claude tokens with the API (needs `anthropic`)")
    for name in ("link", "unlink"):
        p = sub.add_parser(name)
        p.add_argument("--host", choices=["claude", "codex", "both"], default="both")
    sub.add_parser("parity")
    p = sub.add_parser("sync")
    p.add_argument("--host", choices=["claude", "codex"], required=True)
    p.add_argument("--file", help="target file instead of the host's global rules file")
    p.add_argument("--apply", action="store_true", help="write the change; a backup is kept next to the file")
    p.add_argument("--adopt-heading", help="replace the existing section under this heading with the block")
    p.add_argument("--insert", action="store_true", help="add the block at the top of a file that has none")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    return {"new": cmd_new, "validate": cmd_validate, "budget": cmd_budget, "lint": cmd_lint,
            "link": cmd_link, "unlink": cmd_unlink, "parity": cmd_parity, "sync": cmd_sync}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
