from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication, QMessageBox

    from gui.app import MainWindow, ManualInsertDialog

    HAS_PYQT5 = True
except ImportError:
    HAS_PYQT5 = False

from gui.project import EntryRecord, Project, ProjectOptions

if HAS_PYQT5:
    # Prevent modal dialogs from blocking headless tests
    QMessageBox.information = lambda *args, **kwargs: QMessageBox.Ok
    QMessageBox.warning = lambda *args, **kwargs: QMessageBox.Ok
    QMessageBox.critical = lambda *args, **kwargs: QMessageBox.Ok
    QMessageBox.question = lambda *args, **kwargs: QMessageBox.Yes

_APP = None


def get_app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


@unittest.skipUnless(HAS_PYQT5, "PyQt5 not installed")
class GuiAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = get_app()

    def test_main_window_display_and_sort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            proj = Project.new(base / "test.bibgui.json")
            proj.markdown_path = base / "notes.md"

            # Entry 1: 2023, formal
            e1 = EntryRecord(id="e1", line=1, raw_heading="", year=2023, venue="CVPR", title="Old Paper")
            # Entry 2: 2026, arxiv only -> should show !
            e2 = EntryRecord(id="e2", line=2, raw_heading="", year=2026, venue="arXiv", title="Preprint Paper", is_arxiv=True)
            # Entry 3: 2025, arxiv but manual override -> should NOT show !
            e3 = EntryRecord(id="e3", line=3, raw_heading="", year=2025, venue="arXiv", title="Overridden Paper", is_arxiv=True)
            e3.apply_manual("@article{e3_key,\n  title = {Overridden Paper},\n  year = {2025},\n}")

            proj.entries = [e1, e2, e3]
            win = MainWindow(proj)

            # Table should have 3 rows
            self.assertEqual(3, win.table.rowCount())

            # Row 0 should be newest year (2026: Preprint Paper)
            item_row0_flag = win.table.item(0, 0)
            item_row0_title = win.table.item(0, 1)
            item_row0_year = win.table.item(0, 2)
            self.assertEqual("!", item_row0_flag.text())
            self.assertEqual("Preprint Paper", item_row0_title.text())
            self.assertEqual("2026", item_row0_year.text())

            # Row 1 should be 2025 (Overridden Paper) - must NOT show !
            item_row1_flag = win.table.item(1, 0)
            item_row1_title = win.table.item(1, 1)
            item_row1_year = win.table.item(1, 2)
            self.assertEqual("", item_row1_flag.text())
            self.assertEqual("Overridden Paper", item_row1_title.text())
            self.assertEqual("2025", item_row1_year.text())

            # Row 2 should be 2023 (Old Paper)
            item_row2_flag = win.table.item(2, 0)
            item_row2_title = win.table.item(2, 1)
            item_row2_year = win.table.item(2, 2)
            self.assertEqual("", item_row2_flag.text())
            self.assertEqual("Old Paper", item_row2_title.text())
            self.assertEqual("2023", item_row2_year.text())

            # Test clicking specifically on the "!" cell (column 0) of the arXiv row
            win.table.setCurrentCell(0, 0)
            self.assertEqual("e2", win._current_id)
            self.assertEqual("Preprint Paper", win.editor_title.text())
            self.assertIn("arXiv", win.editor_badge.text())

            # Test selecting Row 0 and manually overriding it
            win.table.setCurrentCell(0, 1)
            self.assertEqual("e2", win._current_id)
            win.bibtex_edit.setPlainText("@misc{preprint2026,\n  title = {Preprint Paper},\n  year = {2026},\n}")
            win.apply_button.click()

            # Now Row 0's exclamation mark should disappear!
            new_flag = win.table.item(0, 0)
            self.assertEqual("", new_flag.text())

            # Test compile action
            out_bib = base / "all_in_one.bib"
            win.project.output_path = out_bib
            win.compile_bibtex()
            self.assertTrue(out_bib.exists())
            content = out_bib.read_text(encoding="utf-8")
            self.assertIn("@misc{preprint2026", content)
            self.assertIn("@article{e3_key", content)

    def test_manual_insert_dialog_parsing(self) -> None:
        dialog = ManualInsertDialog()
        # Test quick heading parse
        dialog.quick_input.setText("##### 2024. CVPR. Quick Heading Paper")
        dialog._parse_quick_heading()
        self.assertEqual("Quick Heading Paper", dialog.title_edit.text())
        self.assertEqual(2024, dialog.year_spin.value())
        self.assertEqual("CVPR", dialog.venue_edit.text())

        # Test bibtex parsing
        dialog.bibtex_edit.setPlainText("""@inproceedings{vaswani2017attention,
  title = {Attention Is All You Need},
  year = {2017},
  booktitle = {NeurIPS},
}""")
        dialog._parse_bibtex_text()
        self.assertEqual("Attention Is All You Need", dialog.title_edit.text())
        self.assertEqual(2017, dialog.year_spin.value())
        self.assertEqual("NeurIPS", dialog.venue_edit.text())
        data = dialog.get_data()
        self.assertEqual("Attention Is All You Need", data["title"])
        self.assertEqual(2017, data["year"])
        self.assertEqual("NeurIPS", data["venue"])


if __name__ == "__main__":
    unittest.main()
