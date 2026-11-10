from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bibfetch.md_parser import (
    clean_markdown_text,
    is_html_document,
    parse_document_file,
    parse_heading_body,
    parse_html,
    parse_markdown,
    resolve_document_path,
)


class MarkdownParserTests(unittest.TestCase):
    def test_parses_escaped_period_and_skips_empty_heading(self) -> None:
        text = "\n".join(
            [
                "#### not this level",
                "##### ",
                "##### 2026\\. ACL Findings. Failure Makes the Agent Stronger: Enhancing Accuracy",
                "##### 2023\\. ICLR. ReAct: Synergizing Reasoning and Acting in Language Models",
            ]
        )
        entries, issues = parse_markdown(text, heading_level=5)

        self.assertEqual([], issues)
        self.assertEqual(2, len(entries))
        self.assertEqual(2026, entries[0].year)
        self.assertEqual("ACL Findings", entries[0].venue)
        self.assertEqual(
            "Failure Makes the Agent Stronger: Enhancing Accuracy",
            entries[0].title,
        )
        self.assertEqual(3, entries[0].line_no)
        self.assertEqual("ReAct: Synergizing Reasoning and Acting in Language Models", entries[1].title)

    def test_venue_may_contain_periods(self) -> None:
        parsed = parse_heading_body(r"2026\. arXiv (cs.AI). A title with a colon: yes")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        year, venue, title = parsed
        self.assertEqual(2026, year)
        self.assertEqual("arXiv (cs.AI)", venue)
        self.assertEqual("A title with a colon: yes", title)

    def test_unparseable_heading_is_reported(self) -> None:
        entries, issues = parse_markdown("##### A heading without metadata\n")
        self.assertEqual([], entries)
        self.assertEqual(1, len(issues))
        self.assertEqual("heading_parse_failed", issues[0].code)

    def test_clean_markdown_text(self) -> None:
        self.assertEqual(
            "FaithEval: Can Your Model Stay Faithful?",
            clean_markdown_text(r"**FaithEval**: Can Your Model Stay _Faithful_?"),
        )
        self.assertEqual("A & B", clean_markdown_text("A \\& B"))
        self.assertEqual("LLM_based systems", clean_markdown_text("LLM_based systems"))

    def test_parse_html_basic(self) -> None:
        html_content = """
        <html>
        <body>
            <h1>Title</h1>
            <h5>2026. ACL Findings. Failure Makes the Agent Stronger</h5>
            <h5></h5>
            <h5><span class="c1">2023. ICLR. ReAct: Synergizing Reasoning</span></h5>
            <h4>2024. CVPR. Ignored Level</h4>
        </body>
        </html>
        """
        entries, issues = parse_html(html_content, heading_level=5)
        self.assertEqual([], issues)
        self.assertEqual(2, len(entries))
        self.assertEqual(2026, entries[0].year)
        self.assertEqual("ACL Findings", entries[0].venue)
        self.assertEqual("Failure Makes the Agent Stronger", entries[0].title)
        self.assertEqual(2023, entries[1].year)
        self.assertEqual("ICLR", entries[1].venue)
        self.assertEqual("ReAct: Synergizing Reasoning", entries[1].title)

    def test_parse_html_rvvae_document(self) -> None:
        rvvae_html = Path("usr/RVVAE/RVVAE.html")
        if rvvae_html.exists():
            entries, issues = parse_document_file("usr/RVVAE")
            self.assertEqual(21, len(entries))
            self.assertEqual([], issues)
            self.assertEqual(2026, entries[0].year)
            self.assertEqual("ACL Findings", entries[0].venue)

    def test_resolve_document_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            doc_dir = base / "SampleDoc"
            doc_dir.mkdir()
            html_file = doc_dir / "SampleDoc.html"
            html_file.write_text("<h5>2024. CVPR. Paper Title</h5>", encoding="utf-8")

            # Resolve directory
            resolved = resolve_document_path(doc_dir)
            self.assertEqual(html_file, resolved)

            # Parse directory
            entries, _ = parse_document_file(doc_dir)
            self.assertEqual(1, len(entries))
            self.assertEqual("Paper Title", entries[0].title)


if __name__ == "__main__":
    unittest.main()
