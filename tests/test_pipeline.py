from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bibfetch.pipeline import PipelineOptions, run_pipeline


def _work(
    work_id: str,
    title: str,
    *,
    year: int = 2026,
    work_type: str = "conference-paper",
    source_name: str = "Proceedings of the Association for Computational Linguistics",
    source_type: str = "conference",
    doi: str | None = None,
) -> dict[str, object]:
    return {
        "id": f"https://openalex.org/{work_id}",
        "doi": f"https://doi.org/{doi}" if doi else None,
        "title": title,
        "publication_year": year,
        "type": work_type,
        "authorships": [{"author": {"display_name": "Test Author"}}],
        "primary_location": {
            "doi": f"https://doi.org/{doi}" if doi else None,
            "landing_page_url": f"https://doi.org/{doi}" if doi else "https://example.test",
            "source": {"display_name": source_name, "type": source_type},
        },
        "locations": [],
        "biblio": {},
    }


class FakeClient:
    def __init__(self, works: dict[str, list[dict[str, object]]]) -> None:
        self.works = works
        self.broad_calls = 0

    def search_title(self, title: str) -> list[dict[str, object]]:
        return self.works.get(title, [])

    def search_broad(self, title: str) -> list[dict[str, object]]:
        self.broad_calls += 1
        return []

    def fetch_bibtex(self, doi: str) -> str:
        return f"@inproceedings{{Crossref_Key, title={{A title}}, DOI={{{doi}}}, year={{2026}}}}"


class PipelineTests(unittest.TestCase):
    def test_merges_found_entries_and_writes_missing_report(self) -> None:
        markdown = "\n".join(
            [
                "##### 2026\\. ACL. A Found Paper",
                "##### 2025\\. ICLR. A Preprint Only Paper",
                "##### 2026\\. ACL. A Missing Paper",
            ]
        )
        found = _work("W1", "A Found Paper", doi="10.18653/v1/2026.acl-long.1")
        preprint = _work(
            "W2",
            "A Preprint Only Paper",
            year=2024,
            work_type="preprint",
            source_name="arXiv (Cornell University)",
            source_type="repository",
            doi="10.48550/arxiv.2401.00001",
        )
        client = FakeClient(
            {
                "A Found Paper": [found],
                "A Preprint Only Paper": [preprint],
                "A Missing Paper": [],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "notes.md"
            source.write_text(markdown, encoding="utf-8")
            output = base / "refs.bib"

            result = run_pipeline(source, output, client, options=PipelineOptions())

            self.assertEqual(3, result.total_headings)
            self.assertEqual(2, result.found)
            self.assertEqual(1, result.missing)
            self.assertTrue(output.exists())
            self.assertEqual(2, output.read_text(encoding="utf-8").count("@inproceedings"))

            missing_text = (base / "refs.missing.txt").read_text(encoding="utf-8")
            self.assertIn("A Missing Paper", missing_text)
            self.assertIn("not_found", missing_text)

            report = json.loads((base / "refs.report.json").read_text(encoding="utf-8"))
            self.assertEqual(3, report["summary"]["headings"])
            self.assertEqual(2, report["summary"]["found"])
            self.assertEqual(1, report["summary"]["missing"])
            statuses = [entry["status"] for entry in report["entries"]]
            self.assertEqual(["found", "found", "missing"], statuses)
            codes = [
                issue["code"]
                for entry in report["entries"]
                for issue in entry["issues"]
            ]
            self.assertIn("preprint_only", codes)
            self.assertIn("not_found", codes)

    def test_duplicate_heading_is_skipped(self) -> None:
        markdown = "\n".join(
            [
                "##### 2026\\. ACL. The Same Paper",
                "##### 2026\\. ICLR. The Same Paper",
            ]
        )
        client = FakeClient(
            {
                "The Same Paper": [
                    _work("W1", "The Same Paper", doi="10.18653/v1/2026.acl-long.1")
                ]
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "notes.md"
            source.write_text(markdown, encoding="utf-8")
            result = run_pipeline(source, base / "refs.bib", client, options=PipelineOptions())

            self.assertEqual(1, len(result.entries))
            self.assertTrue(any(issue.code == "duplicate_heading" for issue in result.global_issues))


if __name__ == "__main__":
    unittest.main()
