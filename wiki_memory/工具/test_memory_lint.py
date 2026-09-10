"""Standard-library regression tests; run this file directly from any cwd."""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

import memory_lint as lint


class MemoryLintTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.root = self.repo / "wiki_memory"
        self.root.mkdir()
        self.write("README.md", "# Memory\n")

    def write(self, relative, body):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def page(self, relative, *, topic="topic", kind="process", type="knowledge", status="active", sources="[]", body="Content"):
        return self.write(relative, f"""---
type: {type}
status: {status}
kind: {kind}
importance: medium
updated: 2026-09-10
topic: {topic}
source_logs: {sources}
supersedes: null
---
# Page

{body}
""")

    def errors(self):
        return lint.validate_pages(self.root, lint.load_pages(self.root))

    def test_relative_code_and_parent_markdown_links_resolve(self):
        (self.repo / "engine.py").write_text("# code", encoding="utf-8")
        (self.repo / "README.md").write_text("# Product", encoding="utf-8")
        self.page("知识/模块/引擎.md", body="[code](../../../engine.py) [product](../../../README.md)")
        self.write("README.md", "[module](./知识/模块/引擎.md)")
        self.assertEqual(self.errors(), [])

    def test_percent_encoded_paths_and_anchors_resolve(self):
        self.write("含 空格.md", "# Source")
        self.write("README.md", "[source](./含%20空格.md#section) [anchor](#here)")
        self.assertEqual(self.errors(), [])

    def test_dotfile_source_keeps_original_name(self):
        (self.repo / ".gitignore").write_text("dist/", encoding="utf-8")
        self.write("README.md", "[ignore](../.gitignore)")
        self.assertEqual(self.errors(), [])

    def test_missing_file_is_rejected(self):
        self.write("README.md", "[missing](../does-not-exist.py)")
        self.assertTrue(any("broken link" in error for error in self.errors()))

    def test_missing_source_log_is_rejected(self):
        self.page("当前状态/状态.md", type="state", sources='["[[日志/不存在]]"]')
        self.write("README.md", "[state](./当前状态/状态.md)")
        self.assertTrue(any("broken link '日志/不存在'" in error for error in self.errors()))

    def test_existing_source_log_is_valid_and_referenced(self):
        self.page("当前状态/状态.md", type="state", sources='["[[日志/记录]]"]')
        self.page("日志/记录.md", type="log", status="draft", topic="event")
        self.write("README.md", "[state](./当前状态/状态.md)")
        self.assertEqual(self.errors(), [])

    def test_duplicate_active_topic_is_rejected(self):
        self.page("当前状态/一.md", type="state")
        self.page("当前状态/二.md", type="state")
        self.write("README.md", "[one](./当前状态/一.md) [two](./当前状态/二.md)")
        self.assertTrue(any("multiple active state" in error for error in self.errors()))

    def test_missing_required_fields_are_rejected(self):
        self.write("当前状态/状态.md", "# Missing metadata")
        self.write("README.md", "[state](./当前状态/状态.md)")
        self.assertTrue(any("missing frontmatter fields" in error for error in self.errors()))

    def test_invalid_date_and_source_type_are_reported_without_crash(self):
        path = self.page("当前状态/状态.md", type="state", sources="3")
        path.write_text(path.read_text(encoding="utf-8").replace("2026-09-10", "2026-02-30"), encoding="utf-8")
        self.write("README.md", "[state](./当前状态/状态.md)")
        errors = self.errors()
        self.assertTrue(any("real YYYY-MM-DD" in error for error in errors))
        self.assertTrue(any("source_logs must be a list" in error for error in errors))

    def test_duplicate_adr_number_is_rejected(self):
        self.page("决策/ADR-001-一.md", type="decision", topic="one")
        self.page("决策/ADR-001-二.md", type="decision", topic="two")
        self.assertTrue(any("duplicate decision number" in error for error in self.errors()))

    def test_orphan_is_rejected(self):
        self.page("知识/孤儿.md")
        self.assertTrue(any("orphan page" in error for error in self.errors()))

    def test_index_is_github_markdown_with_six_columns(self):
        self.page("日志/2026-09-10-记录.md", type="log", status="draft", topic="event", body="- 目标：验证 | 边界")
        self.write("README.md", "[index](./日志/MOC_工作日志.md)")
        output = lint.index_logs(self.root, lint.load_pages(self.root))
        text = output.read_text(encoding="utf-8")
        row = next(line for line in text.splitlines() if line.startswith("| 2026-09-10"))
        self.assertEqual(len(re.split(r"(?<!\\)\|", row)), 8)
        self.assertIn("[Page](./2026-09-10-记录.md)", row)
        self.assertNotIn("[[", text)
        self.assertEqual(self.errors(), [])


if __name__ == "__main__":
    unittest.main()
