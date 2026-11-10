from __future__ import annotations

import unittest

from bibfetch.matching import (
    choose_best,
    normalize_title,
    rank_candidates,
    score_candidate,
    select_location,
    title_similarity,
    venue_family,
    venue_matches,
)
from bibfetch.models import HeadingEntry


def entry(**overrides: object) -> HeadingEntry:
    values = {
        "index": 1,
        "line_no": 10,
        "raw_heading": "",
        "year": 2026,
        "venue": "ACL",
        "title": "Debate-of-Thoughts: Resolving Knowledge Conflicts in LLMs Through Internal Deliberation",
    }
    values.update(overrides)
    return HeadingEntry(**values)  # type: ignore[arg-type]


def work(
    work_id: str,
    *,
    title: str,
    year: int = 2026,
    work_type: str = "conference-paper",
    source_name: str = "Proceedings of the Association for Computational Linguistics",
    source_type: str = "conference",
    doi: str | None = "10.18653/v1/2026.acl-long.1651",
) -> dict[str, object]:
    return {
        "id": f"https://openalex.org/{work_id}",
        "doi": f"https://doi.org/{doi}" if doi else None,
        "title": title,
        "publication_year": year,
        "type": work_type,
        "authorships": [
            {
                "author": {"display_name": "Guocong Li"},
                "raw_author_name": "Guocong Li",
            }
        ],
        "primary_location": {
            "doi": f"https://doi.org/{doi}" if doi else None,
            "landing_page_url": f"https://doi.org/{doi}" if doi else "https://example.test",
            "source": {"display_name": source_name, "type": source_type},
        },
        "locations": [],
        "biblio": {"volume": "1", "first_page": "1", "last_page": "10"},
    }


class MatchingTests(unittest.TestCase):
    def test_title_normalization_ignores_position_prefix_and_punctuation(self) -> None:
        self.assertEqual(
            normalize_title("Position: Ideas Should be the Center of ML Research"),
            normalize_title("Ideas Should Be the Center of ML Research"),
        )
        self.assertGreater(
            title_similarity(
                "Position: AI Researchers Must Help Lead Arms Control",
                "AI Researchers Must Help Lead Arms Control",
            ),
            0.99,
        )

    def test_formal_conference_record_beats_other_repository_record(self) -> None:
        item = entry()
        title = item.title
        other = work(
            "W1",
            title=title,
            work_type="other",
            source_name="Underline Science Inc.",
            source_type="repository",
            doi="10.48448/g8kt-az79",
        )
        formal = work("W2", title=title)

        candidates = rank_candidates([other, formal], item)
        best = choose_best(candidates)

        self.assertIsNotNone(best)
        assert best is not None
        self.assertTrue(best.is_formal)
        self.assertIn("Association for Computational Linguistics", best.source_name)

    def test_preprint_is_used_when_no_formal_record_exists(self) -> None:
        item = entry(year=2025, venue="ICLR", title="Mutual Reasoning Makes Smaller LLMs Stronger Problem-Solvers")
        preprint = work(
            "W3",
            title=item.title,
            year=2024,
            work_type="preprint",
            source_name="arXiv (Cornell University)",
            source_type="repository",
            doi="10.48550/arxiv.2408.06195",
        )
        best = choose_best(rank_candidates([preprint], item), min_score=5.0)
        self.assertIsNotNone(best)
        assert best is not None
        self.assertTrue(best.is_preprint)

    def test_select_location_prefers_published_conference_location(self) -> None:
        record = work("W4", title="A paper")
        conference = {
            "doi": "https://doi.org/10.1234/conf",
            "is_published": True,
            "version": "publishedVersion",
            "landing_page_url": "https://doi.org/10.1234/conf",
            "source": {"display_name": "Proceedings of ACL", "type": "conference"},
        }
        preprint = {
            "doi": "https://doi.org/10.48550/arxiv.1234.5678",
            "version": "submittedVersion",
            "landing_page_url": "https://arxiv.org/abs/1234.5678",
            "source": {"display_name": "arXiv", "type": "repository"},
        }
        record["primary_location"] = preprint
        record["locations"] = [preprint, conference]

        selected = select_location(record, "ACL")
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual("conference", selected["source"]["type"])

    def test_venue_matching_aliases(self) -> None:
        self.assertEqual("acl_findings", venue_family("ACL Findings"))
        self.assertEqual("neurips", venue_family("NeurIPS"))
        self.assertEqual("arxiv", venue_family("arXiv (cs.AI)"))
        self.assertTrue(
            venue_matches(
                "ACL",
                "Proceedings of the conference - Association for Computational Linguistics. Meeting",
            )
        )
        self.assertTrue(venue_matches("TOIS", "ACM Transactions on Information Systems"))
        self.assertFalse(venue_matches("ICLR", "arXiv (Cornell University)"))

    def test_low_similarity_is_rejected(self) -> None:
        item = entry(title="A Completely Different Paper About Databases")
        candidate = score_candidate(work("W5", title="Debate-of-Thoughts"), item)
        self.assertLess(candidate.title_similarity, 0.5)
        self.assertEqual([], rank_candidates([candidate.work], item))


if __name__ == "__main__":
    unittest.main()
