from __future__ import annotations

import unittest

from bibfetch.bibtex import (
    construct_bibtex,
    extract_first_author,
    make_citation_key,
    normalize_crossref_bibtex,
    rewrite_citation_key,
)
from bibfetch.matching import score_candidate
from bibfetch.models import HeadingEntry


def make_entry(**overrides: object) -> HeadingEntry:
    values = {
        "index": 1,
        "line_no": 1,
        "raw_heading": "",
        "year": 2026,
        "venue": "ACL",
        "title": "Failure Makes the Agent Stronger: Enhancing Accuracy",
    }
    values.update(overrides)
    return HeadingEntry(**values)  # type: ignore[arg-type]


def make_work(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": "https://openalex.org/W1",
        "doi": "https://doi.org/10.18653/v1/2026.findings-acl.618",
        "title": "Failure makes the agent stronger: Enhancing Accuracy",
        "publication_year": 2026,
        "type": "conference-paper",
        "authorships": [
            {"author": {"display_name": "Junhao Su"}},
            {"author": {"display_name": "Yuanliang Wan"}},
        ],
        "primary_location": {
            "doi": "https://doi.org/10.18653/v1/2026.findings-acl.618",
            "landing_page_url": "https://doi.org/10.18653/v1/2026.findings-acl.618",
            "source": {
                "display_name": "Findings of the Association for Computational Linguistics: ACL",
                "type": "conference",
                "host_organization_name": "Association for Computational Linguistics",
            },
        },
        "locations": [],
        "biblio": {"volume": "", "issue": "", "first_page": "12712", "last_page": "12734"},
    }
    values.update(overrides)
    return values


class BibtexTests(unittest.TestCase):
    def test_rewrite_citation_key(self) -> None:
        raw = "@inproceedings{Su_2026, title={A title}, year={2026}}"
        rewritten = rewrite_citation_key(raw, "su2026title")
        self.assertTrue(rewritten.startswith("@inproceedings{su2026title,"))
        self.assertIn("title={A title}", rewritten)

    def test_normalizes_crossref_pages_and_month(self) -> None:
        raw = "@article{K, title={T}, pages={1\u201355}, month=Jan, year={2025}}"
        normalized = normalize_crossref_bibtex(raw, "huang2025survey")
        self.assertTrue(normalized.startswith("@article{huang2025survey,"))
        self.assertIn("pages={1--55}", normalized)
        self.assertIn("month={Jan}", normalized)

    def test_generates_readable_unique_keys(self) -> None:
        used: set[str] = set()
        first = make_citation_key("Junhao Su", 2026, "Failure Makes the Agent Stronger", used)
        second = make_citation_key("Junhao Su", 2026, "Failure Makes the Agent Stronger", used)
        self.assertEqual("su2026failure", first)
        self.assertEqual("su2026failurea", second)

    def test_author_family_handles_commas_and_missing_names(self) -> None:
        self.assertEqual("yao2022react", make_citation_key("Yao, Shunyu", 2022, "ReAct", set()))
        self.assertEqual("anon2024untitled", make_citation_key(None, 2024, "", set()))

    def test_construct_conference_entry(self) -> None:
        entry = make_entry()
        record = make_work()
        candidate = score_candidate(record, entry)
        bibtex = construct_bibtex(record, candidate, entry, "su2026failure")

        self.assertIn("@inproceedings{su2026failure,", bibtex)
        self.assertIn("author = {Junhao Su and Yuanliang Wan}", bibtex)
        self.assertIn("booktitle = {Findings of the Association for Computational Linguistics: ACL}", bibtex)
        self.assertIn("pages = {12712--12734}", bibtex)
        self.assertIn("doi = {10.18653/v1/2026.findings-acl.618}", bibtex)
        self.assertIn("publisher = {Association for Computational Linguistics}", bibtex)

    def test_construct_arxiv_entry(self) -> None:
        entry = make_entry(year=2023, venue="ICLR", title="ReAct: Synergizing Reasoning and Acting")
        record = make_work(
            doi="https://doi.org/10.48550/arxiv.2210.03629",
            title="ReAct: Synergizing Reasoning and Acting",
            publication_year=2022,
            type="preprint",
            primary_location={
                "doi": "https://doi.org/10.48550/arxiv.2210.03629",
                "landing_page_url": "https://arxiv.org/abs/2210.03629",
                "source": {"display_name": "arXiv (Cornell University)", "type": "repository"},
            },
            biblio={},
        )
        candidate = score_candidate(record, entry)
        bibtex = construct_bibtex(record, candidate, entry, "yao2022react")

        self.assertIn("@misc{yao2022react,", bibtex)
        self.assertIn("eprint = {2210.03629}", bibtex)
        self.assertIn("archivePrefix = {arXiv}", bibtex)

    def test_escapes_special_characters(self) -> None:
        entry = make_entry(title="A & B: 100% #1 _test_")
        record = make_work(title="A & B: 100% #1 _test_")
        candidate = score_candidate(record, entry)
        bibtex = construct_bibtex(record, candidate, entry, "su2026a")
        self.assertIn(r"A \& B: 100\% \#1 \_test\_", bibtex)

    def test_doi_and_url_are_not_tex_escaped(self) -> None:
        entry = make_entry()
        record = make_work(
            doi="https://doi.org/10.1145/foo_bar",
            primary_location={
                "doi": "https://doi.org/10.1145/foo_bar",
                "landing_page_url": "https://example.test/path?x=1&y=2",
                "source": {"display_name": "Journal", "type": "journal"},
            },
        )
        candidate = score_candidate(record, entry)
        bibtex = construct_bibtex(record, candidate, entry, "su2026a")
        self.assertIn("doi = {10.1145/foo_bar}", bibtex)
        self.assertIn("url = {https://example.test/path?x=1&y=2}", bibtex)

    def test_extract_first_author(self) -> None:
        bibtex = "@article{x,\n  author = {{ACL} and Su, Junhao},\n  title = {T}\n}"
        self.assertEqual("ACL", extract_first_author(bibtex))


if __name__ == "__main__":
    unittest.main()
