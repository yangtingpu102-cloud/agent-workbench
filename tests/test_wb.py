"""Tests for scripts/wb.py. Run: python -m unittest discover -s tests -v"""
from __future__ import annotations

import contextlib
import io
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
        self.assertIn("35000  bytes in the chain ending at pkg/AGENTS.md", out)
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

    def test_cjk_heavy_file_gets_a_note(self):
        self.write("CLAUDE.md", "請用繁體中文回答，並且保持回覆簡短。" * 5)
        _, out = self.lint()
        self.assertIn("non-ASCII", out)


class HelpersTest(unittest.TestCase):
    def test_est_tokens(self):
        self.assertEqual(wb.est_tokens("abcd"), 1)
        self.assertEqual(wb.est_tokens("中文"), 2)

    def test_repo_validates(self):
        code, _ = quiet(wb.cmd_validate, None)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
