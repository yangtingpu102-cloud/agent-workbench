#!/usr/bin/env python3
"""agent-workbench helper. Stdlib only.

  python scripts/wb.py new <plugin> <skill>   scaffold a skill (and the plugin if new)
  python scripts/wb.py validate               check skills, manifests and both marketplaces
  python scripts/wb.py budget [--max N]       estimate always-loaded token cost
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
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


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
                                        encoding="utf-8")
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
        lines = body.count("\n") + 1
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new")
    p.add_argument("plugin")
    p.add_argument("skill")
    p.add_argument("--description", help="plugin description, used only when the plugin is new")
    sub.add_parser("validate")
    p = sub.add_parser("budget")
    p.add_argument("--max", type=int, help="fail if per-session skill metadata exceeds N tokens")
    for name in ("link", "unlink"):
        p = sub.add_parser(name)
        p.add_argument("--host", choices=["claude", "codex", "both"], default="both")
    args = ap.parse_args()
    return {"new": cmd_new, "validate": cmd_validate, "budget": cmd_budget,
            "link": cmd_link, "unlink": cmd_unlink}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
