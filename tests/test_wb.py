"""Tests for scripts/wb.py. Run: python -m unittest discover -s tests -v"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import wb  # noqa: E402


def quiet(fn, *args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*args)
    return code, buf.getvalue()


class LintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root, self.home = base / "repo", base / "home"
        self.root.mkdir()
        self.home.mkdir()
        env = {k: v for k, v in os.environ.items() if k != "CODEX_HOME"}
        env["WB_HOME"] = str(self.home)
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write(self, rel: str, text: str, base: Path | None = None) -> Path:
        path = (base or self.root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def lint(self, *extra: str) -> tuple[int, str]:
        args = wb.build_parser().parse_args(["lint", str(self.root), *extra])
        return quiet(wb.cmd_lint, args)

    def test_claude_follows_imports_and_reports_missing_ones(self):
        self.write("CLAUDE.md", "@AGENTS.md\n@docs/missing.md\n")
        self.write("AGENTS.md", "rules " * 50)
        code, out = self.lint()
        self.assertEqual(code, 0)
        self.assertIn("AGENTS.md  (project, @import)", out)
        self.assertIn("@docs/missing.md not found", out)

    def test_imports_inside_code_and_emails_are_ignored(self):
        self.write("CLAUDE.md", "Mail a@b.com or see `@AGENTS.md`.\n\n```\n@AGENTS.md\n```\n")
        self.write("AGENTS.md", "rules")
        _, out = self.lint()
        self.assertNotIn("@import", out)
        self.assertNotIn("not found", out)

    def test_agents_md_counts_for_claude_when_there_is_no_claude_md(self):
        self.write("AGENTS.md", "rules " * 20)
        _, out = self.lint()
        self.assertIn("read natively when there is no CLAUDE.md", out)

    def test_codex_doc_over_32_kib_is_an_error(self):
        self.write("AGENTS.md", "x" * (wb.CODEX_DOC_MAX_BYTES + 100))
        code, out = self.lint()
        self.assertEqual(code, 1)
        self.assertIn("Codex stops reading", out)

    def test_nested_codex_chain_counts_parent_docs(self):
        self.write("AGENTS.md", "x" * 20000)
        self.write("pkg/AGENTS.md", "y" * 15000)
        code, out = self.lint()
        self.assertEqual(code, 1)
        self.assertIn("chain ending at pkg/AGENTS.md (35000 bytes)", out)
        self.assertIn("ERROR pkg/AGENTS.md", out)

    def test_paragraph_loaded_twice_across_user_and_project(self):
        para = "Always run the full test suite before you report that a change works.\n"
        self.write(".claude/CLAUDE.md", para, base=self.home)
        self.write("CLAUDE.md", "Project notes.\n\n" + para)
        _, out = self.lint()
        self.assertIn("load twice", out)
        self.assertIn("~/.claude/CLAUDE.md = CLAUDE.md", out)

    def test_copied_agents_md_suggests_an_import(self):
        text = "\n\n".join(f"Rule number {i}: keep this instruction short and specific." for i in range(4))
        self.write("AGENTS.md", text)
        self.write("CLAUDE.md", text)
        _, out = self.lint()
        self.assertIn("replace the copy with `@AGENTS.md`", out)

    def test_no_global_skips_home_files(self):
        self.write(".claude/CLAUDE.md", "user rules", base=self.home)
        self.write("CLAUDE.md", "project rules")
        _, out = self.lint("--no-global")
        self.assertNotIn("~/.claude/CLAUDE.md", out)

    def test_skill_description_over_1024_chars_is_an_error(self):
        self.write(".claude/skills/demo/SKILL.md", f"---\nname: demo\ndescription: {'d' * 1100}\n---\nbody\n")
        code, out = self.lint()
        self.assertEqual(code, 1)
        self.assertIn("1 skill(s) in .claude/skills", out)

    def test_max_tokens_fails_the_run(self):
        self.write("AGENTS.md", "word " * 400)
        code, out = self.lint("--max-tokens", "100")
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)

    def test_nested_claude_md_is_reported_with_its_chain(self):
        self.write("CLAUDE.md", "root " * 40)
        self.write("pkg/CLAUDE.md", "pkg " * 40)
        _, out = self.lint()
        self.assertIn("nested CLAUDE.md", out)
        self.assertIn("chain ending at pkg/CLAUDE.md", out)

    def test_max_tokens_covers_nested_chains(self):
        self.write("AGENTS.md", "small root doc")
        self.write("pkg/AGENTS.md", "word " * 2000)
        code, out = self.lint("--max-tokens", "1000")
        self.assertEqual(code, 1)
        self.assertIn("FAIL  the Codex chain ending at pkg/AGENTS.md", out)

    def test_exact_failure_mid_run_falls_back_to_estimates(self):
        self.write("CLAUDE.md", "@AGENTS.md\n")
        self.write("AGENTS.md", "rules " * 50)
        calls = []

        def flaky(text):
            calls.append(text)
            if len(calls) > 1:
                raise ConnectionError("network down")
            return 7

        with mock.patch.object(wb, "exact_counter", return_value=(flaky, "")):
            code, out = self.lint("--exact")
        self.assertEqual(code, 0)
        self.assertIn("--exact skipped, showing estimates: network down", out)
        self.assertNotIn("counted exactly", out)

    def test_line_count_ignores_the_trailing_newline(self):
        self.write("CLAUDE.md", "line\n" * wb.CLAUDE_MAX_LINES)
        _, out = self.lint()
        self.assertNotIn("Claude Code suggests", out)
        self.write("CLAUDE.md", "line\n" * (wb.CLAUDE_MAX_LINES + 1))
        _, out = self.lint()
        self.assertIn("Claude Code suggests", out)

    def test_plugin_skills_get_a_pointer_to_budget(self):
        self.write("plugins/p/skills/s/SKILL.md", "---\nname: s\ndescription: d\n---\n")
        _, out = self.lint()
        self.assertIn("see `wb.py budget`", out)

    def test_cjk_heavy_file_gets_a_note(self):
        self.write("CLAUDE.md", "請用繁體中文回答，並且保持回覆簡短。" * 5)
        _, out = self.lint()
        self.assertIn("non-ASCII", out)


RULES = "# Shared rules\n\n- Lead with the outcome.\n- Ask before destructive actions."
SLIDES = "- Slides and documents are made in Codex; its presentation setup is the reference.\n"


class SyncTest(unittest.TestCase):
    """`wb.py sync` changes only the marked span, never writes without --apply, and never touches slides tooling."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.tmp.name)
        self.shared = self.base / "ai-shared.md"
        self.shared.write_text(RULES + "\n", encoding="utf-8", newline="\n")
        patcher = mock.patch.object(wb, "SHARED_FILE", self.shared)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def target(self, text: str, newline: str = "\n") -> Path:
        path = self.base / "AGENTS.md"
        path.write_bytes(text.replace("\n", newline).encode("utf-8"))
        return path

    def sync(self, path: Path, *extra: str) -> tuple[int, str]:
        args = wb.build_parser().parse_args(["sync", "--host", "codex", "--file", str(path), *extra])
        return quiet(wb.cmd_sync, args)

    def test_only_the_marked_block_changes_and_crlf_is_kept(self):
        before = f"intro\n\n{wb.START_MARK}\nold rules\n{wb.END_MARK}\n\n@RTK.md\n\n## Slides\n{SLIDES}"
        path = self.target(before, "\r\n")
        code, out = self.sync(path, "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(path.read_bytes().decode("utf-8"), before.replace("old rules", RULES).replace("\n", "\r\n"))

    def test_bytes_outside_the_block_never_change_even_with_mixed_line_endings(self):
        head, tail = b"intro\r\nlf only\n", b"\r\ntail lf\ntail crlf\r\n"
        path = self.base / "AGENTS.md"
        path.write_bytes(head + f"{wb.START_MARK}\r\nold\r\n{wb.END_MARK}".encode() + tail)
        self.assertEqual(self.sync(path, "--apply")[0], 0)
        block = "\r\n".join([wb.START_MARK, *RULES.split("\n"), wb.END_MARK]).encode()
        self.assertEqual(path.read_bytes(), head + block + tail)

    def test_insert_keeps_the_original_file_exactly(self):
        original = "\n\n# Mine\n\n- keep me  \n"
        path = self.target(original)
        self.assertEqual(self.sync(path, "--insert", "--apply")[0], 0)
        block = "\n".join([wb.START_MARK, *RULES.split("\n"), wb.END_MARK])
        self.assertEqual(path.read_text(encoding="utf-8"), block + "\n" + "\n" + original)  # original, byte for byte

    def test_dry_run_shows_the_diff_and_writes_nothing(self):
        path = self.target(f"{wb.START_MARK}\nold rules\n{wb.END_MARK}\n")
        original = path.read_bytes()
        code, out = self.sync(path)
        self.assertEqual(code, 1)
        self.assertIn("-old rules", out)
        self.assertIn("+- Lead with the outcome.", out)
        self.assertIn("Dry run", out)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.base.glob("*.bak-*")), [])

    def test_apply_keeps_a_backup_and_a_second_run_is_a_no_op(self):
        path = self.target(f"{wb.START_MARK}\nold rules\n{wb.END_MARK}\n")
        original = path.read_bytes()
        self.assertEqual(self.sync(path, "--apply")[0], 0)
        backups = list(self.base.glob("AGENTS.md.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        code, out = self.sync(path)
        self.assertEqual(code, 0)
        self.assertIn("already in sync", out)

    def test_adopt_heading_replaces_just_that_section(self):
        text = f"# Old defaults\n\n- a\n- b\n\n@RTK.md\n\n## Slides\n{SLIDES}"
        path = self.target(text)
        self.assertEqual(self.sync(path, "--adopt-heading", "# Old defaults", "--apply")[0], 0)
        self.assertEqual(path.read_text(encoding="utf-8"),
                         f"{wb.START_MARK}\n{RULES}\n{wb.END_MARK}\n\n@RTK.md\n\n## Slides\n{SLIDES}")

    def test_adopt_stops_at_setext_and_indented_headings(self):
        for name, nxt in (("setext", "Other section\n=============\n\n- keep me\n"), ("indented", "  ## Next\n- keep me\n")):
            path = self.target(f"# Old\n\n- old one\n- old two\n\n{nxt}")
            code, out = self.sync(path, "--adopt-heading", "# Old", "--apply")
            self.assertEqual(code, 0, f"{name}: {out}")
            after = path.read_text(encoding="utf-8")
            self.assertTrue(after.endswith(nxt), name)
            self.assertNotIn("old one", after, name)

    def test_adopt_refuses_a_section_that_is_not_a_plain_bullet_list(self):
        cases = {
            "prose": "# Old\n\n- a\nsome paragraph text\n",
            "code block": "# Old\n\n- a\n```\ncode\n```\n",
            "table": "# Old\n\n| a | b |\n|---|---|\n",
            "slides": "# Old\n\n- Make slides in Codex.\n",
            "documents": "# Old\n\n- Keep the documents tidy.\n",
            "pdf": "# Old\n\n- Export a PDF.\n",
        }
        for name, text in cases.items():
            path = self.target(text)
            original = path.read_bytes()
            code, out = self.sync(path, "--adopt-heading", "# Old", "--apply")
            self.assertEqual((code, path.read_bytes()), (2, original), name)
        self.assertIn("belongs to Codex", self.sync(self.target(cases["slides"]), "--adopt-heading", "# Old")[1])
        self.assertIn("not a plain bullet list", self.sync(self.target(cases["prose"]), "--adopt-heading", "# Old")[1])

    def test_a_block_that_holds_slides_instructions_is_not_replaced(self):
        path = self.target(f"{wb.START_MARK}\n- Make slides in Codex.\n{wb.END_MARK}\n")
        original = path.read_bytes()
        code, out = self.sync(path, "--apply")
        self.assertEqual((code, path.read_bytes()), (2, original))
        self.assertIn("belongs to Codex", out)

    def test_marker_text_in_examples_is_not_a_marker(self):
        example = (f"Docs example:\n```\n{wb.START_MARK}\nsample\n{wb.END_MARK}\n```\n"
                   f"Inline: {wb.START_MARK} is the opening marker.\n")
        path = self.target(example + f"{wb.START_MARK}\nold\n{wb.END_MARK}\n")
        self.assertEqual(self.sync(path, "--apply")[0], 0)
        after = path.read_text(encoding="utf-8")
        self.assertTrue(after.startswith(example))
        self.assertIn(RULES, after)
        only_examples = self.target(example)
        code, out = self.sync(only_examples, "--apply")
        self.assertEqual(code, 2)
        self.assertIn("no ai-shared block", out)

    def test_a_file_without_a_block_needs_an_explicit_way_in(self):
        path = self.target("# Mine\n\n- keep me\n")
        original = path.read_bytes()
        code, out = self.sync(path, "--apply")
        self.assertEqual((code, path.read_bytes()), (2, original))
        self.assertIn("--adopt-heading", out)

    def test_apply_with_a_file_only_writes_rules_files(self):
        notes = self.base / "notes.txt"
        notes.write_text(f"{wb.START_MARK}\nx\n{wb.END_MARK}\n", encoding="utf-8")
        before = notes.read_bytes()
        self.assertEqual(self.sync(notes, "--apply")[0], 2)
        self.assertEqual(notes.read_bytes(), before)
        self.assertEqual(self.sync(notes)[0], 1)  # a dry run only reads

    def test_bad_inputs_are_refused_without_touching_the_file(self):
        for bad in (f"{wb.START_MARK}\nx\n", f"{wb.END_MARK}\n{wb.START_MARK}\n",
                    f"{wb.START_MARK}\n{wb.END_MARK}\n{wb.START_MARK}\n{wb.END_MARK}\n"):
            path = self.target(bad)
            code, out = self.sync(path, "--apply")
            self.assertEqual((code, path.read_bytes()), (2, bad.encode("utf-8")), bad)
            self.assertIn("unbalanced", out)
        self.assertEqual(self.sync(self.base / "missing.md")[0], 2)
        broken = self.base / "AGENTS.md"
        broken.write_bytes(b"\xff\xfe not utf-8")
        self.assertEqual(self.sync(broken, "--apply")[0], 2)
        self.assertEqual(broken.read_bytes(), b"\xff\xfe not utf-8")
        self.shared.write_text(f"{wb.START_MARK}\nbad\n", encoding="utf-8")
        self.assertEqual(self.sync(self.target(f"{wb.START_MARK}\nx\n{wb.END_MARK}\n"))[0], 2)

    def test_a_file_that_changed_since_it_was_read_is_not_overwritten(self):
        path = self.base / "AGENTS.md"
        path.write_bytes(b"A")
        path.write_bytes(b"B")  # edited after the change was planned from "A"
        with self.assertRaises(wb.SyncError):
            wb.write_checked(path, b"A", b"C")
        self.assertEqual(path.read_bytes(), b"B")
        self.assertEqual([p.name for p in self.base.iterdir() if p.name.endswith(".tmp")], [])

    def test_backups_in_the_same_instant_never_overwrite_each_other(self):
        class Frozen(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return dt.datetime(2026, 1, 1, 0, 0, 0, 0)

        path = self.base / "AGENTS.md"
        path.write_bytes(b"x")
        with mock.patch.object(wb.dt, "datetime", Frozen):
            first, second = wb.make_backup(path, b"one"), wb.make_backup(path, b"two")
        self.assertNotEqual(first, second)
        self.assertEqual((first.read_bytes(), second.read_bytes()), (b"one", b"two"))

    def test_the_real_shared_file_is_plain_rules(self):
        with mock.patch.object(wb, "SHARED_FILE", Path(wb.__file__).resolve().parent.parent / "shared" / "ai-shared.md"):
            rules = wb.canonical_rules()
        self.assertGreaterEqual(rules.count("\n- "), 5)
        self.assertNotIn("ai-shared", rules)
        self.assertIsNone(wb.PROTECTED_RE.search(rules))  # the shared text itself never talks about slides tooling


class ParityTest(unittest.TestCase):
    """`wb.py parity` compares only this repo's plugins and the shared block; everything else is ignored."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.repo = self.base / "repo"
        self.write(self.repo / "ai-shared.md", RULES + "\n")
        self.write(self.repo / ".claude-plugin" / "marketplace.json", '{"name": "market"}')
        self.write_plugin(self.repo / "plugins" / "demo", "1.0.0", ["alpha", "beta"], hooks=True)
        env = {k: v for k, v in os.environ.items() if k != "CODEX_HOME"}
        env["WB_HOME"] = str(self.home)
        for patcher in (mock.patch.dict(os.environ, env, clear=True),
                        mock.patch.object(wb, "SHARED_FILE", self.repo / "ai-shared.md"),
                        mock.patch.object(wb, "PLUGINS", self.repo / "plugins")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def write(path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def write_plugin(self, folder: Path, version: str, skills: list[str], hooks: bool = False) -> None:
        self.write(folder / ".claude-plugin" / "plugin.json", f'{{"name": "demo", "version": "{version}"}}')
        for name in skills:
            self.write(folder / "skills" / name / "SKILL.md", "---\nname: x\n---\n")
        if hooks:
            self.write(folder / "hooks" / "hooks.json", "{}")

    def block(self, text: str = RULES) -> str:
        return f"{wb.START_MARK}\n{text}\n{wb.END_MARK}\n"

    def claude(self, rules: str, source: str = "directory", cached: tuple[str, list[str]] | None = None) -> None:
        base = self.home / ".claude"
        self.write(base / "CLAUDE.md", rules)
        self.write(base / "settings.json", '{"enabledPlugins": {"demo@market": true}}')
        install = base / "plugins" / "cache" / "market" / "demo" / "0.9.0"
        if cached:
            self.write_plugin(install, *cached)
        self.write(base / "plugins" / "installed_plugins.json", json.dumps(
            {"plugins": {"demo@market": [{"version": "0.9.0", "installPath": str(install)}]}}))
        where = {"source": "directory", "path": str(self.repo)} if source == "directory" else {"source": "github"}
        self.write(base / "plugins" / "known_marketplaces.json", json.dumps({"market": {"source": where}}))

    def codex(self, rules: str, version: str = "1.0.0", skills: tuple[str, ...] = ("alpha", "beta"),
              hooks: bool = True) -> None:
        base = self.home / ".codex"
        self.write(base / "AGENTS.md", rules)
        self.write(base / "config.toml", '[plugins."demo@market"]\nenabled = true\n\n'
                   '[plugins."presentations@openai-primary-runtime"]\nenabled = true\n\n'
                   '[plugins."documents@openai-primary-runtime"]\nenabled = false\n')
        self.write_plugin(base / "plugins" / "cache" / "market" / "demo" / version, version, list(skills), hooks)

    def codex_copy(self, version: str = "1.0.0") -> Path:
        return self.home / ".codex" / "plugins" / "cache" / "market" / "demo" / version

    def parity(self) -> tuple[int, str]:
        return quiet(wb.cmd_parity, None)

    def test_everything_in_sync(self):
        self.claude(self.block())
        self.codex(self.block())
        code, out = self.parity()
        self.assertEqual(code, 0, out)
        self.assertIn("live from the checkout) ok", out)
        self.assertIn("in sync", out.splitlines()[-1])

    def test_slides_and_document_tooling_is_never_compared(self):
        self.claude(self.block())
        self.codex(self.block())
        code, out = self.parity()
        self.assertEqual(code, 0, out)
        compared = out.split("Not compared on purpose")[0]  # the results, before the sentence that names what is skipped
        for word in ("presentations", "documents", "openai-primary-runtime"):
            self.assertNotIn(word, compared)
        self.assertNotIn("openai-primary-runtime", out)
        self.assertIn("slides, documents, pdf, spreadsheets", out)  # said plainly, as a deliberate omission

    def test_a_codex_file_with_its_own_copy_of_the_rules_differs(self):
        self.claude(self.block())
        self.codex("# Efficient execution defaults\n\n- its own copy\n")
        code, out = self.parity()
        self.assertEqual(code, 1)
        self.assertIn("no ai-shared block", out)

    def test_a_changed_block_differs(self):
        self.claude(self.block(RULES + "\n- extra"))
        self.codex(self.block())
        code, out = self.parity()
        self.assertEqual(code, 1)
        self.assertIn("the block is not the shared text", out)

    def test_an_outdated_cached_copy_differs_but_a_live_checkout_does_not(self):
        self.claude(self.block(), source="github", cached=("0.9.0", ["alpha"]))
        self.codex(self.block(), version="0.5.0", skills=("alpha",))
        code, out = self.parity()
        self.assertEqual(code, 1)
        self.assertIn("version 0.9.0", out)
        self.assertIn("version 0.5.0", out)
        self.assertEqual(out.count("skills/beta/SKILL.md (missing)"), 2)
        self.claude(self.block())  # now a directory marketplace: loaded live, so the stale cache does not matter
        self.assertIn("claude 1.0.0 (live from the checkout) ok", self.parity()[1])

    def test_changed_skill_content_differs_even_with_the_same_version_and_names(self):
        self.claude(self.block())
        self.codex(self.block())
        (self.codex_copy() / "skills" / "alpha" / "SKILL.md").write_text("---\nname: x\n---\nstale body\n", encoding="utf-8")
        code, out = self.parity()
        self.assertEqual(code, 1)
        self.assertIn("skills/alpha/SKILL.md (changed)", out)

    def test_extra_skills_and_missing_hooks_are_drift(self):
        self.claude(self.block())
        self.codex(self.block(), skills=("alpha", "beta", "gamma"), hooks=False)
        code, out = self.parity()
        self.assertEqual(code, 1)
        self.assertIn("hooks/hooks.json (missing)", out)
        self.assertIn("skills/gamma/SKILL.md (extra)", out)

    def test_a_same_named_plugin_from_another_marketplace_does_not_count(self):
        self.claude(self.block())
        self.write(self.home / ".claude" / "settings.json", '{"enabledPlugins": {"demo@other": true}}')
        self.codex(self.block())
        code, out = self.parity()
        self.assertEqual(code, 1)
        self.assertIn("NOT INSTALLED from 'market'", out)

    def test_the_newest_cached_version_is_chosen_by_number_not_by_time(self):
        self.claude(self.block())
        self.codex(self.block())
        self.write_plugin(self.codex_copy("0.9.0"), "0.9.0", ["alpha"], False)
        os.utime(self.codex_copy("0.9.0"), (4102444800, 4102444800))  # an older version with a far newer timestamp
        code, out = self.parity()
        self.assertEqual(code, 0, out)
        self.assertIn("older cached copies: 0.9.0", out)

    @unittest.skipIf(wb.tomllib is None, "reading Codex's config needs Python 3.11+")
    def test_plugin_tables_inside_toml_strings_are_not_settings(self):
        self.claude(self.block())
        self.codex(self.block())
        self.write(self.home / ".codex" / "config.toml", 'note = """\n[plugins."demo@market"]\nenabled = true\n"""\n')
        code, out = self.parity()
        self.assertEqual(code, 1)
        self.assertIn("NOT INSTALLED", out)

    @unittest.skipIf(wb.tomllib is None, "reading Codex's config needs Python 3.11+")
    def test_a_config_that_is_not_toml_is_reported_not_guessed(self):
        self.claude(self.block())
        self.codex(self.block())
        self.write(self.home / ".codex" / "config.toml", '[plugins."demo@market"\nenabled = true\n')
        code, out = self.parity()
        self.assertEqual(code, 1)
        self.assertIn("CANNOT COMPARE", out)

    def test_missing_and_disabled_plugins_are_reported(self):
        self.claude(self.block())
        base = self.home / ".codex"
        self.write(base / "AGENTS.md", self.block())
        self.write(base / "config.toml", '[plugins."demo@market"]\nenabled = false\n')
        code, out = self.parity()
        self.assertEqual(code, 1)
        self.assertIn("disabled", out)
        (base / "config.toml").write_text("", encoding="utf-8")
        self.assertIn("NOT INSTALLED", self.parity()[1])

    def test_codex_hook_trust_is_flagged_as_unverified_not_assumed(self):
        self.claude(self.block())
        self.codex(self.block())
        _, out = self.parity()
        self.assertIn("claude active", out)
        self.assertIn("UNVERIFIED", out)
        self.assertIn("/hooks", out)

    def test_parity_never_writes(self):
        self.claude(self.block())
        self.codex("# its own\n")
        before = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        self.parity()
        after = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


class HelpersTest(unittest.TestCase):
    def test_est_tokens(self):
        self.assertEqual(wb.est_tokens("abcd"), 1)
        self.assertEqual(wb.est_tokens("中文"), 2)

    def test_repo_validates(self):
        code, _ = quiet(wb.cmd_validate, None)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
