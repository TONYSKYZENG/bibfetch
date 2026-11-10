from __future__ import annotations

import unittest

from bibfetch.md_parser import clean_markdown_text, parse_heading_body, parse_markdown


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


if __name__ == "__main__":
    unittest.main()
