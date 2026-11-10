from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gui.project import (
    EntryRecord,
    Project,
    ProjectOptions,
    extract_bibtex_key,
    looks_like_arxiv,
    parse_bibtex_entry,
    parse_bibtex_identity,
)


SAMPLE_BIBTEX = """@inproceedings{author2026paper,
  author = {Test Author},
  title = {A Paper Title},
  year = {2026},
  booktitle = {ACL},
}"""


class ProjectTests(unittest.TestCase):
    def test_project_json_roundtrip_keeps_manual_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = Project.new(base / "demo.bibgui.json")
            project.markdown_path = base / "notes.md"
            project.output_path = base / "refs.bib"
            project.options = ProjectOptions(
                heading_level=4,
                min_similarity=0.8,
                min_score=4.0,
                per_page=10,
                mailto="me@example.com",
                api_key="session-secret",
            )
            record = EntryRecord(
                id="abc123",
                line=3,
                raw_heading="2026\\. ACL. A Paper Title",
                year=2026,
                venue="ACL",
                title="A Paper Title",
            )
            record.checked = True
            record.bibtex = SAMPLE_BIBTEX
            record.citation_key = "author2026paper"
            record.matched_title = "A Paper Title"
            record.matched_year = 2026
            project.entries = [record]

            path = project.save()
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("api_key", payload["options"])
            self.assertEqual("demo", payload["name"])

            loaded = Project.load(path)
            self.assertEqual(4, loaded.options.heading_level)
            self.assertEqual("me@example.com", loaded.options.mailto)
            self.assertEqual("", loaded.options.api_key)
            self.assertEqual(1, len(loaded.entries))
            self.assertEqual(SAMPLE_BIBTEX, loaded.entries[0].bibtex)
            self.assertEqual("found", loaded.entries[0].status)

    def test_import_preserves_overrides_and_skips_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            notes = base / "notes.md"
            notes.write_text(
                "\n".join(
                    [
                        "##### 2026\\. ACL. Paper One",
                        "##### 2026\\. ICLR. Paper Two",
                        "##### 2026\\. ICLR. Paper Two",
                    ]
                ),
                encoding="utf-8",
            )
            project = Project.new(base / "notes.bibgui.json")
            count, issues = project.import_markdown(notes)
            self.assertEqual(2, count)
            self.assertTrue(any(issue.code == "duplicate_heading" for issue in issues))

            first = project.entries[0]
            first.apply_manual(SAMPLE_BIBTEX)
            first.checked = True

            notes.write_text(
                "\n".join(
                    [
                        "##### 2027\\. ACL. Paper One",
                        "##### 2026\\. ICLR. Paper Three",
                    ]
                ),
                encoding="utf-8",
            )
            count, _ = project.import_markdown(notes)
            self.assertEqual(2, count)
            self.assertEqual(2027, project.entries[0].year)
            self.assertEqual(SAMPLE_BIBTEX + "\n", project.entries[0].manual_bibtex)
            self.assertEqual("manual", project.entries[0].status)
            self.assertEqual("Paper Three", project.entries[1].title)

    def test_generate_prefers_manual_override_and_writes_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = Project.new(base / "gen.bibgui.json")
            project.markdown_path = base / "notes.md"

            auto = EntryRecord(
                id="auto",
                line=1,
                raw_heading="2026\\. ACL. Auto Paper",
                year=2026,
                venue="ACL",
                title="Auto Paper",
            )
            auto.checked = True
            auto.bibtex = "@article{auto2026paper,\n  title = {Auto Paper},\n  year = {2026},\n}"
            auto.citation_key = "auto2026paper"

            manual = EntryRecord(
                id="manual",
                line=2,
                raw_heading="2026\\. ICLR. Manual Paper",
                year=2026,
                venue="ICLR",
                title="Manual Paper",
            )
            manual.apply_manual("@misc{pasted2026manual,\n  title = {Manual Paper},\n  year = {2026},\n}")

            project.entries = [auto, manual]
            target, written, total, warnings = project.generate(base / "refs.bib")

            self.assertEqual(2, written)
            self.assertEqual(2, total)
            self.assertEqual([], warnings)
            text = target.read_text(encoding="utf-8")
            self.assertIn("@article{auto2026paper", text)
            self.assertIn("@misc{pasted2026manual", text)
            self.assertIn("Entries: 2/2 found", text)

    def test_duplicate_keys_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = Project.new(base / "dup.bibgui.json")
            first = EntryRecord(id="a", line=1, raw_heading="", year=2020, venue="", title="One")
            second = EntryRecord(id="b", line=2, raw_heading="", year=2021, venue="", title="Two")
            first.apply_manual("@misc{same,\n  title = {One},\n}")
            second.apply_manual("@misc{same,\n  title = {Two},\n}")
            project.entries = [first, second]

            _target, written, _total, warnings = project.generate(base / "refs.bib")
            self.assertEqual(2, written)
            self.assertTrue(any("引用键重复" in warning for warning in warnings))

    def test_manual_override_clears_arxiv_warning(self) -> None:
        record = EntryRecord(
            id="rec1",
            line=1,
            raw_heading="2024. arXiv. Title",
            year=2024,
            venue="arXiv",
            title="Title",
            is_arxiv=True,
        )
        self.assertTrue(record.is_arxiv_only)
        # Applying manual override must suppress arxiv warning per requirement
        record.apply_manual(SAMPLE_BIBTEX)
        self.assertFalse(record.is_arxiv_only)

    def test_manual_insert_and_sync_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            notes = base / "notes.md"
            notes.write_text("##### 2026. ACL. Paper A\n", encoding="utf-8")
            project = Project.new(base / "project.bibgui.json")
            total, issues = project.import_markdown(notes)
            self.assertEqual(1, total)

            # Insert a manual entry not in the markdown
            manual_entry = project.insert_entry(
                title="Manually Inserted Paper",
                year=2025,
                venue="CVPR",
                bibtex="@inproceedings{manual2025,\n  title = {Manually Inserted Paper},\n  year = {2025},\n}",
            )
            self.assertTrue(manual_entry.is_manual_entry)
            self.assertEqual(2, len(project.entries))

            # Update markdown on disk by adding Paper B
            notes.write_text(
                "##### 2026. ACL. Paper A\n##### 2024. NeurIPS. Paper B\n",
                encoding="utf-8",
            )

            # Sync from markdown: should have Paper A, Paper B, AND the manually inserted entry!
            total_after, new_count, _ = project.sync_markdown()
            self.assertEqual(3, len(project.entries))
            self.assertEqual(3, total_after)
            self.assertEqual(1, new_count)
            titles = [e.title for e in project.entries]
            self.assertIn("Paper A", titles)
            self.assertIn("Paper B", titles)
            self.assertIn("Manually Inserted Paper", titles)

            # Remove manual entry
            removed = project.remove_entry(manual_entry.id)
            self.assertTrue(removed)
            self.assertEqual(2, len(project.entries))

    def test_generate_sorted_by_year_descending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = Project.new(base / "sort.bibgui.json")
            project.markdown_path = base / "notes.md"

            e2023 = EntryRecord(id="e2023", line=1, raw_heading="", year=2023, venue="", title="Paper 2023")
            e2023.apply_manual("@article{p2023,\n  title = {Paper 2023},\n  year = {2023},\n}")
            e2026 = EntryRecord(id="e2026", line=2, raw_heading="", year=2026, venue="", title="Paper 2026")
            e2026.apply_manual("@article{p2026,\n  title = {Paper 2026},\n  year = {2026},\n}")
            e2024 = EntryRecord(id="e2024", line=3, raw_heading="", year=2024, venue="", title="Paper 2024")
            e2024.apply_manual("@article{p2024,\n  title = {Paper 2024},\n  year = {2024},\n}")

            # Insert out of order
            project.entries = [e2023, e2026, e2024]
            target, written, total, _ = project.generate(base / "out.bib")
            content = target.read_text(encoding="utf-8")

            # Check that p2026 comes before p2024, and p2024 comes before p2023
            pos_2026 = content.find("@article{p2026")
            pos_2024 = content.find("@article{p2024")
            pos_2023 = content.find("@article{p2023")
            self.assertTrue(pos_2026 < pos_2024 < pos_2023)


class BibtexHelpersTests(unittest.TestCase):
    def test_extract_key(self) -> None:
        self.assertEqual("author2026paper", extract_bibtex_key(SAMPLE_BIBTEX))
        self.assertIsNone(extract_bibtex_key("not bibtex"))

    def test_parse_identity(self) -> None:
        title, year = parse_bibtex_identity(SAMPLE_BIBTEX)
        self.assertEqual("A Paper Title", title)
        self.assertEqual(2026, year)

    def test_arxiv_detection(self) -> None:
        self.assertTrue(looks_like_arxiv("arXiv (Cornell University)"))
        self.assertTrue(looks_like_arxiv(None, "https://arxiv.org/abs/2401.00001"))
        self.assertFalse(looks_like_arxiv("ACL", "10.18653/v1/2026.acl-long.1"))

    def test_parse_bibtex_entry(self) -> None:
        parsed = parse_bibtex_entry(SAMPLE_BIBTEX)
        self.assertEqual("author2026paper", parsed["key"])
        self.assertEqual("A Paper Title", parsed["title"])
        self.assertEqual(2026, parsed["year"])
        self.assertEqual("ACL", parsed["venue"])

    def test_apply_manual_rejects_invalid_input(self) -> None:
        record = EntryRecord(id="x", line=1, raw_heading="", year=2020, venue="", title="Title")
        with self.assertRaises(ValueError):
            record.apply_manual("title = {No header}")


if __name__ == "__main__":
    unittest.main()
