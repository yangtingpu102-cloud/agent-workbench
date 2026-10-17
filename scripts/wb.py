#!/usr/bin/env python3
"""agent-workbench helper. Stdlib only.

  python scripts/wb.py new <plugin> <skill>   scaffold a skill (and the plugin if new)
  python scripts/wb.py validate               check skills, manifests and both marketplaces
  python scripts/wb.py budget [--max N]       estimate always-loaded token cost of this repo's plugins
  python scripts/wb.py lint [PATH]            audit a project's CLAUDE.md / AGENTS.md / skills token budget
  python scripts/wb.py link [--host H]        link skills into ~/.claude/skills and ~/.agents/skills
  python scripts/wb.py unlink [--host H]      remove only links that point into this repo

Set WB_HOME to link into a directory other than your home (useful for testing).
"""
from __future__ import annotations

import argparse
import json
import os
import re
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
    return p.relative_to(ROOT).as_posix()


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
    return ap


def main() -> int:
    args = build_parser().parse_args()
    return {"new": cmd_new, "validate": cmd_validate, "budget": cmd_budget, "lint": cmd_lint,
            "link": cmd_link, "unlink": cmd_unlink}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
