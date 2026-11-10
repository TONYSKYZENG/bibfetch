"""PyQt5 front-end for :mod:`bibfetch`.

The window is a small literature manager on top of the Markdown parsing and
OpenAlex lookup pipeline:

* the left table lists titles and publication years, newest first;
* entries that only have an arXiv record are marked with ``!``;
* the right pane lets the user paste a complete BibTeX entry to override the
  automatically resolved record;
* the toolbar can compile a single, year-sorted ``.bib`` file;
* everything (including manual overrides) is saved in a JSON project file.

Run with ``python3 -m gui`` or ``bibfetch-gui`` after installing the optional
GUI dependencies.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

from PyQt5.QtCore import (
    QFileSystemWatcher,
    QObject,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
    pyqtSlot,
)
from PyQt5.QtGui import QBrush, QColor, QFont, QKeySequence
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QAction,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from bibfetch.models import HeadingEntry, ResolvedEntry
from bibfetch.openalex import ApiError
from bibfetch.pipeline import resolve_entry

from .project import (
    PROJECT_SUFFIX,
    STATUS_FOUND,
    STATUS_MANUAL,
    STATUS_MISSING,
    STATUS_PENDING,
    EntryRecord,
    Project,
    ProjectOptions,
    build_client,
    parse_bibtex_entry,
)

PROJECT_FILTER = "bibfetch 工程 (*.bibgui.json *.json);;所有文件 (*)"
MARKDOWN_FILTER = "Markdown (*.md *.markdown);;所有文件 (*)"
BIBTEX_FILTER = "BibTeX (*.bib);;所有文件 (*)"

STATUS_LABELS = {
    STATUS_PENDING: "待抓取",
    STATUS_FOUND: "已解析",
    STATUS_MISSING: "缺失",
    STATUS_MANUAL: "手动",
}

STATUS_COLORS = {
    STATUS_PENDING: QColor("#8a8a8a"),
    STATUS_FOUND: QColor("#1f7a3d"),
    STATUS_MISSING: QColor("#c0392b"),
    STATUS_MANUAL: QColor("#1d5fa8"),
}

ARXIV_TOOLTIP = "只找到 arXiv 记录，建议在右侧手动覆盖为正式版本"
ARXIV_COLOR = QColor("#d17a00")


class _YearItem(QTableWidgetItem):
    """Numeric year cell so sorting never treats years as text."""

    def __init__(self, year: int | None) -> None:
        super().__init__(str(year) if year else "—")
        self._year = int(year or 0)
        self.setTextAlignment(Qt.AlignCenter)

    def __lt__(self, other: QTableWidgetItem) -> bool:  # type: ignore[override]
        return self._year < int(getattr(other, "_year", 0))


class FetchWorker(QObject):
    """Resolve headings on a background thread (never touch Qt widgets)."""

    progress = pyqtSignal(int, int, str)
    resolved = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)
    finished = pyqtSignal(bool)

    def __init__(
        self,
        items: list[tuple[str, HeadingEntry]],
        options: ProjectOptions,
        existing_keys: set[str],
    ) -> None:
        super().__init__()
        self._items = items
        self._options = options
        self._existing_keys = set(existing_keys)
        self._cancelled = False

    def cancel(self) -> None:
        # A plain Python flag is enough: the worker checks it between requests.
        self._cancelled = True

    @pyqtSlot()
    def run(self) -> None:
        client = cache = None
        try:
            try:
                client, cache = build_client(self._options)
            except Exception as exc:  # pragma: no cover - defensive
                for record_id, _heading in self._items:
                    self.failed.emit(record_id, f"初始化 OpenAlex 客户端失败：{exc}")
                return
            used_keys = set(self._existing_keys)
            total = len(self._items)
            for position, (record_id, heading) in enumerate(self._items, start=1):
                if self._cancelled:
                    break
                self.progress.emit(position - 1, total, heading.title)
                try:
                    resolved = resolve_entry(
                        client,
                        heading,
                        min_similarity=self._options.min_similarity,
                        min_score=self._options.min_score,
                        used_keys=used_keys,
                    )
                except ApiError as exc:
                    self.failed.emit(record_id, f"OpenAlex 请求失败：{exc}")
                    continue
                except Exception as exc:  # pragma: no cover - defensive
                    traceback.print_exc()
                    self.failed.emit(record_id, f"抓取失败：{exc}")
                    continue
                if resolved.citation_key:
                    used_keys.add(resolved.citation_key)
                self.resolved.emit(record_id, resolved)
            self.progress.emit(min(len(self._items), total), total, "")
        finally:
            if cache is not None:
                try:
                    cache.flush()
                except OSError:
                    pass
            self.finished.emit(self._cancelled)


class SettingsDialog(QDialog):
    """OpenAlex/cache settings.  The API key intentionally stays in memory."""

    def __init__(self, options: ProjectOptions, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("项目设置")
        self.setMinimumWidth(470)
        self._options = options

        heading_level = QSpinBox()
        heading_level.setRange(1, 6)
        heading_level.setValue(options.heading_level)

        min_similarity = QDoubleSpinBox()
        min_similarity.setRange(0.0, 1.0)
        min_similarity.setSingleStep(0.01)
        min_similarity.setDecimals(2)
        min_similarity.setValue(options.min_similarity)

        min_score = QDoubleSpinBox()
        min_score.setRange(0.0, 100.0)
        min_score.setSingleStep(0.5)
        min_score.setDecimals(1)
        min_score.setValue(options.min_score)

        per_page = QSpinBox()
        per_page.setRange(1, 200)
        per_page.setValue(options.per_page)

        mailto = QLineEdit(options.mailto)
        mailto.setPlaceholderText("you@example.com（OpenAlex polite pool）")

        api_key = QLineEdit(options.api_key)
        api_key.setEchoMode(QLineEdit.Password)
        api_key.setPlaceholderText("也可使用环境变量 OPENALEX_API_KEY")

        use_cache = QCheckBox("启用磁盘缓存")
        use_cache.setChecked(options.use_cache)

        cache_ttl = QDoubleSpinBox()
        cache_ttl.setRange(0.0, 3650.0)
        cache_ttl.setDecimals(1)
        cache_ttl.setValue(options.cache_ttl)
        cache_ttl.setSuffix(" 天")

        form = QFormLayout()
        form.addRow("Markdown 标题层级", heading_level)
        form.addRow("最低标题相似度", min_similarity)
        form.addRow("最低综合得分", min_score)
        form.addRow("每次查询候选数", per_page)
        form.addRow("OpenAlex 邮箱", mailto)
        form.addRow("OpenAlex API Key", api_key)
        form.addRow("缓存", use_cache)
        form.addRow("缓存有效期", cache_ttl)

        note = QLabel("API Key 只保存在当前进程内存中，不会写入工程 JSON。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #666;")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

        self._widgets = {
            "heading_level": heading_level,
            "min_similarity": min_similarity,
            "min_score": min_score,
            "per_page": per_page,
            "mailto": mailto,
            "api_key": api_key,
            "use_cache": use_cache,
            "cache_ttl": cache_ttl,
        }

    def apply_to(self, options: ProjectOptions) -> None:
        options.heading_level = self._widgets["heading_level"].value()
        options.min_similarity = self._widgets["min_similarity"].value()
        options.min_score = self._widgets["min_score"].value()
        options.per_page = self._widgets["per_page"].value()
        options.mailto = self._widgets["mailto"].text().strip()
        options.api_key = self._widgets["api_key"].text()
        options.use_cache = self._widgets["use_cache"].isChecked()
        options.cache_ttl = self._widgets["cache_ttl"].value()


class ManualInsertDialog(QDialog):
    """Dialog allowing the user to manually insert a literature entry."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("手动插入文献")
        self.resize(560, 500)
        self.setMinimumWidth(480)

        quick_box = QGroupBox("快捷解析（可选）")
        quick_layout = QVBoxLayout(quick_box)
        quick_hint = QLabel("可粘贴 Markdown 标题行（例如 2024. CVPR. Paper Title）快速填充字段：")
        quick_hint.setStyleSheet("color: #666;")
        self.quick_input = QLineEdit()
        self.quick_input.setPlaceholderText("例如：##### 2024. CVPR. My Great Paper")
        btn_quick_parse = QPushButton("从标题解析")
        btn_quick_parse.clicked.connect(self._parse_quick_heading)
        quick_h = QHBoxLayout()
        quick_h.addWidget(self.quick_input, 1)
        quick_h.addWidget(btn_quick_parse)
        quick_layout.addWidget(quick_hint)
        quick_layout.addLayout(quick_h)

        fields_box = QGroupBox("文献元数据")
        fields_form = QFormLayout(fields_box)

        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("必填，例如：Attention Is All You Need")

        self.year_spin = QSpinBox()
        self.year_spin.setRange(1900, 2100)
        self.year_spin.setValue(2026)

        self.venue_edit = QLineEdit()
        self.venue_edit.setPlaceholderText("例如：NeurIPS, ICLR, CVPR, arXiv...")

        fields_form.addRow("标题 (*)", self.title_edit)
        fields_form.addRow("年份", self.year_spin)
        fields_form.addRow("会议 / 期刊", self.venue_edit)

        bib_box = QGroupBox("BibTeX 条目（可选，可直接粘贴覆盖）")
        bib_layout = QVBoxLayout(bib_box)
        self.bibtex_edit = QPlainTextEdit()
        mono = QFont("Menlo")
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSize(11)
        self.bibtex_edit.setFont(mono)
        self.bibtex_edit.setPlaceholderText("@inproceedings{key,\n  title = {Paper Title},\n  year = {2026},\n}")

        btn_parse_bib = QPushButton("从 BibTeX 自动解析并填充上方字段")
        btn_parse_bib.clicked.connect(self._parse_bibtex_text)
        bib_layout.addWidget(self.bibtex_edit, 1)
        bib_layout.addWidget(btn_parse_bib)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(quick_box)
        layout.addWidget(fields_box)
        layout.addWidget(bib_box, 1)
        layout.addWidget(buttons)

    def _parse_quick_heading(self) -> None:
        text = self.quick_input.text().strip()
        if not text:
            return
        clean = text.lstrip("#").strip()
        from bibfetch.md_parser import parse_heading_body

        parsed = parse_heading_body(clean)
        if parsed:
            year, venue, title = parsed
            self.year_spin.setValue(year)
            self.venue_edit.setText(venue)
            self.title_edit.setText(title)
        else:
            QMessageBox.information(
                self,
                "解析提示",
                "未识别出标准的 年份. 会议. 标题 格式，请直接在下方手动输入。",
            )

    def _parse_bibtex_text(self) -> None:
        text = self.bibtex_edit.toPlainText().strip()
        if not text:
            return
        parsed = parse_bibtex_entry(text)
        if parsed.get("title"):
            self.title_edit.setText(parsed["title"])
        if parsed.get("year"):
            self.year_spin.setValue(parsed["year"])
        if parsed.get("venue"):
            self.venue_edit.setText(parsed["venue"])

    def _validate_and_accept(self) -> None:
        title = self.title_edit.text().strip()
        if not title:
            parsed = parse_bibtex_entry(self.bibtex_edit.toPlainText().strip())
            if parsed.get("title"):
                self.title_edit.setText(parsed["title"])
                title = parsed["title"]
        if not title:
            QMessageBox.warning(self, "缺少标题", "文献标题为必填项。")
            self.title_edit.setFocus()
            return
        self.accept()

    def get_data(self) -> dict[str, Any]:
        return {
            "title": self.title_edit.text().strip(),
            "year": self.year_spin.value(),
            "venue": self.venue_edit.text().strip(),
            "bibtex": self.bibtex_edit.toPlainText().strip(),
        }


class MainWindow(QMainWindow):
    """Literature manager window."""

    def __init__(self, project: Project | None = None) -> None:
        super().__init__()
        self.project = project or Project(name="未命名工程")
        self._dirty = False
        self._current_id: str | None = None
        self._editor_loading = False
        self._editor_dirty = False
        self._suppress_selection = False
        self._row_by_id: dict[str, int] = {}
        self._worker: FetchWorker | None = None
        self._thread: QThread | None = None
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_markdown_file_changed)

        self.setWindowTitle("bibfetch GUI")
        self.resize(1280, 780)
        self.setMinimumSize(980, 600)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_central_widget()
        self._build_status_bar()
        self._refresh_table()
        self._update_title()

    # ------------------------------------------------------------------ UI
    def _build_actions(self) -> None:
        self.action_new = QAction("新建工程", self)
        self.action_new.setShortcut(QKeySequence.New)
        self.action_new.setStatusTip("创建一个新的文献管理工程")
        self.action_new.triggered.connect(self.new_project)

        self.action_open = QAction("打开工程", self)
        self.action_open.setShortcut(QKeySequence.Open)
        self.action_open.setStatusTip("打开 .bibgui.json 工程")
        self.action_open.triggered.connect(self.open_project)

        self.action_save = QAction("保存工程", self)
        self.action_save.setShortcut(QKeySequence.Save)
        self.action_save.setStatusTip("把当前状态保存到 JSON 工程")
        self.action_save.triggered.connect(lambda: self.save_project(save_as=False))

        self.action_save_as = QAction("工程另存为…", self)
        self.action_save_as.setShortcut("Ctrl+Shift+S")
        self.action_save_as.triggered.connect(lambda: self.save_project(save_as=True))

        self.action_import = QAction("导入 Markdown…", self)
        self.action_import.setShortcut("Ctrl+I")
        self.action_import.setStatusTip("解析 Markdown 中的 ##### 年份. 会议. 标题")
        self.action_import.triggered.connect(self.import_markdown)

        self.action_sync = QAction("刷新源 MD", self)
        self.action_sync.setShortcut("F6")
        self.action_sync.setStatusTip("从源 Markdown 文件同步最新修改（自动保留手动插入和覆盖）")
        self.action_sync.triggered.connect(lambda: self.sync_markdown(silent=False))

        self.action_insert = QAction("手动插入文献…", self)
        self.action_insert.setShortcut("Ctrl+Shift+N")
        self.action_insert.setStatusTip("手动插入一条新文献条目")
        self.action_insert.triggered.connect(self.insert_entry_dialog)

        self.action_delete = QAction("删除选中条目", self)
        self.action_delete.setShortcut(QKeySequence.Delete)
        self.action_delete.setStatusTip("从工程中删除当前选中的文献条目")
        self.action_delete.triggered.connect(self.delete_selected_entry)

        self.action_fetch_missing = QAction("抓取缺失", self)
        self.action_fetch_missing.setShortcut("F5")
        self.action_fetch_missing.setStatusTip("为尚未抓取的条目查询 OpenAlex")
        self.action_fetch_missing.triggered.connect(lambda: self.fetch_records(only_selected=False))

        self.action_fetch_selected = QAction("抓取选中", self)
        self.action_fetch_selected.setShortcut("Ctrl+Return")
        self.action_fetch_selected.setStatusTip("重新抓取当前选中的条目")
        self.action_fetch_selected.triggered.connect(lambda: self.fetch_records(only_selected=True))

        self.action_settings = QAction("设置…", self)
        self.action_settings.setShortcut("Ctrl+,")
        self.action_settings.triggered.connect(self.open_settings)

        self.action_compile = QAction("编译 BibTeX", self)
        self.action_compile.setShortcut("Ctrl+B")
        self.action_compile.setStatusTip("生成按年份降序排列的 all-in-one BibTeX 文件")
        self.action_compile.triggered.connect(self.compile_bibtex)

        self.action_quit = QAction("退出", self)
        self.action_quit.setShortcut(QKeySequence.Quit)
        self.action_quit.triggered.connect(self.close)

        self.action_about = QAction("关于", self)
        self.action_about.triggered.connect(self.show_about)

    def _build_menus(self) -> None:
        menu_file = self.menuBar().addMenu("文件")
        menu_file.addAction(self.action_new)
        menu_file.addAction(self.action_open)
        menu_file.addSeparator()
        menu_file.addAction(self.action_save)
        menu_file.addAction(self.action_save_as)
        menu_file.addSeparator()
        menu_file.addAction(self.action_import)
        menu_file.addAction(self.action_sync)
        menu_file.addSeparator()
        menu_file.addAction(self.action_compile)
        menu_file.addSeparator()
        menu_file.addAction(self.action_quit)

        menu_edit = self.menuBar().addMenu("操作")
        menu_edit.addAction(self.action_insert)
        menu_edit.addAction(self.action_delete)
        menu_edit.addSeparator()
        menu_edit.addAction(self.action_fetch_missing)
        menu_edit.addAction(self.action_fetch_selected)
        menu_edit.addSeparator()
        menu_edit.addAction(self.action_sync)
        menu_edit.addSeparator()
        menu_edit.addAction(self.action_settings)

        menu_help = self.menuBar().addMenu("帮助")
        menu_help.addAction(self.action_about)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("主工具栏", self)
        toolbar.setObjectName("main-toolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(toolbar)

        toolbar.addAction(self.action_new)
        toolbar.addAction(self.action_open)
        toolbar.addAction(self.action_save)
        toolbar.addSeparator()
        toolbar.addAction(self.action_import)
        toolbar.addAction(self.action_sync)
        toolbar.addAction(self.action_insert)
        toolbar.addSeparator()
        toolbar.addAction(self.action_fetch_missing)
        toolbar.addAction(self.action_fetch_selected)
        toolbar.addSeparator()

        # Compile button on toolbar per user requirement
        compile_button = QPushButton("编译 BibTeX")
        compile_button.setToolTip("生成按年份降序排列的 all-in-one BibTeX（Ctrl+B）")
        compile_button.clicked.connect(self.compile_bibtex)
        compile_button.setStyleSheet(
            "QPushButton { font-weight: 600; padding: 4px 14px; background-color: #2b78e4; color: white; border-radius: 3px; }"
        )
        toolbar.addWidget(compile_button)
        toolbar.addAction(self.action_settings)

    def _build_central_widget(self) -> None:
        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(self._build_table_pane())
        splitter.addWidget(self._build_editor_pane())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([760, 520])
        self.setCentralWidget(splitter)

    def _build_table_pane(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(8, 8, 4, 8)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("筛选"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("按标题、会议/期刊或引用键过滤…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self.filter_edit, 1)
        layout.addLayout(filter_row)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["!", "标题", "年份", "状态", "引用键"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setSortingEnabled(True)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.resizeSection(0, 30)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.resizeSection(2, 62)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.resizeSection(3, 76)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        header.resizeSection(4, 155)
        self.table.sortItems(2, Qt.DescendingOrder)
        self.table.currentCellChanged.connect(self._on_current_cell_changed)
        self.table.cellClicked.connect(lambda r, c: self._on_current_cell_changed(r, c))
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_table_context_menu)
        layout.addWidget(self.table, 1)
        return pane

    def _build_editor_pane(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(4, 8, 8, 8)

        self.editor_title = QLabel("未选择条目")
        self.editor_title.setWordWrap(True)
        title_font = self.editor_title.font()
        title_font.setPointSize(title_font.pointSize() + 2)
        title_font.setBold(True)
        self.editor_title.setFont(title_font)
        layout.addWidget(self.editor_title)

        self.editor_badge = QLabel("")
        self.editor_badge.setWordWrap(True)
        layout.addWidget(self.editor_badge)

        details = QGroupBox("条目信息")
        detail_form = QFormLayout(details)
        detail_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.detail_widgets: dict[str, QLineEdit] = {}
        for key, label in (
            ("raw_title", "Markdown 标题"),
            ("matched_title", "匹配标题"),
            ("year", "年份"),
            ("venue", "会议 / 期刊"),
            ("source", "数据源"),
            ("doi", "DOI"),
            ("key", "引用键"),
        ):
            widget = QLineEdit()
            widget.setReadOnly(True)
            self.detail_widgets[key] = widget
            detail_form.addRow(label, widget)
        layout.addWidget(details)

        issues_box = QGroupBox("警告 / 错误")
        issues_layout = QVBoxLayout(issues_box)
        self.issues_view = QPlainTextEdit()
        self.issues_view.setReadOnly(True)
        self.issues_view.setMaximumHeight(110)
        issues_layout.addWidget(self.issues_view)
        layout.addWidget(issues_box)

        manual_box = QGroupBox("BibTeX（可手动覆盖）")
        manual_layout = QVBoxLayout(manual_box)
        hint = QLabel(
            "直接粘贴完整的 @article/@inproceedings/@misc 条目，点击“应用覆盖”后"
            "将优先于自动抓取结果；点击“恢复自动”删除覆盖。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")
        manual_layout.addWidget(hint)

        self.bibtex_edit = QPlainTextEdit()
        self.bibtex_edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        mono = QFont("Menlo")
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSize(12)
        self.bibtex_edit.setFont(mono)
        self.bibtex_edit.setPlaceholderText("@inproceedings{key,\n  title = {...},\n  year = {2026},\n}")
        self.bibtex_edit.textChanged.connect(self._on_editor_text_changed)
        manual_layout.addWidget(self.bibtex_edit, 1)

        self.apply_button = QPushButton("应用覆盖")
        self.apply_button.setDefault(True)
        self.apply_button.clicked.connect(lambda: self._apply_current_manual(silent=False))
        self.revert_button = QPushButton("恢复自动")
        self.revert_button.clicked.connect(self._revert_current_manual)
        self.copy_button = QPushButton("复制")
        self.copy_button.clicked.connect(self._copy_editor_text)
        self.delete_button = QPushButton("删除文献")
        self.delete_button.clicked.connect(self.delete_selected_entry)
        button_row = QHBoxLayout()
        button_row.addWidget(self.apply_button)
        button_row.addWidget(self.revert_button)
        button_row.addWidget(self.copy_button)
        button_row.addWidget(self.delete_button)
        button_row.addStretch(1)
        manual_layout.addLayout(button_row)
        layout.addWidget(manual_box, 1)

        self._set_editor_enabled(False)
        return pane

    def _build_status_bar(self) -> None:
        self.summary_label = QLabel("")
        self.statusBar().addPermanentWidget(self.summary_label)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress)
        self.path_label = QLabel("")
        self.statusBar().addWidget(self.path_label, 1)

    # ------------------------------------------------------------- project
    def new_project(self) -> None:
        if not self._confirm_discard_changes():
            return
        self.project = Project(name="未命名工程")
        self._current_id = None
        self._dirty = False
        self._update_file_watcher()
        self.bibtex_edit.clear()
        self._refresh_table()
        self._update_title()
        self.statusBar().showMessage("已新建空工程", 4000)

    def open_project(self) -> None:
        if not self._confirm_discard_changes():
            return
        filename, _ = QFileDialog.getOpenFileName(
            self, "打开工程", str(Path.cwd()), PROJECT_FILTER
        )
        if filename:
            self.open_project_path(filename)

    def open_project_path(self, filename: str) -> None:
        try:
            project = Project.load(filename)
        except Exception as exc:
            QMessageBox.critical(self, "打开工程失败", str(exc))
            return
        self.project = project
        self._current_id = None
        self._dirty = False
        self._update_file_watcher()
        self._refresh_table()
        self._update_title()
        self.statusBar().showMessage(f"已打开 {filename}", 5000)

    def save_project(self, *, save_as: bool) -> bool:
        target: Path | None = None if save_as else self.project.path
        if target is None:
            suggested = self.project.path
            if suggested is None:
                base = self.project.markdown_path or Path.cwd() / self.project.name
                suggested = Path(str(base.with_suffix("")) + PROJECT_SUFFIX)
            filename, _ = QFileDialog.getSaveFileName(
                self, "保存工程", str(suggested), PROJECT_FILTER
            )
            if not filename:
                return False
            if not filename.lower().endswith((".json", ".bibgui.json")):
                filename += PROJECT_SUFFIX
            target = Path(filename)
        try:
            self.project.save(target)
        except Exception as exc:
            QMessageBox.critical(self, "保存工程失败", str(exc))
            return False
        self._dirty = False
        self._update_title()
        self.statusBar().showMessage(f"工程已保存到 {target}", 5000)
        return True

    def import_markdown(self) -> None:
        start = str(self.project.markdown_path or Path.cwd())
        filename, _ = QFileDialog.getOpenFileName(
            self, "导入 Markdown", start, MARKDOWN_FILTER
        )
        if not filename:
            return
        try:
            count, issues = self.project.import_markdown(
                filename, heading_level=self.project.options.heading_level
            )
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", str(exc))
            return
        self._update_file_watcher()
        self._mark_dirty()
        self._refresh_table()
        self.statusBar().showMessage(f"已导入 {count} 条标题", 5000)
        if issues:
            lines = [f"[{issue.severity}] {issue.code}: {issue.message}" for issue in issues]
            QMessageBox.warning(
                self,
                "导入提示",
                "部分标题被跳过：\n\n" + "\n".join(lines[:20])
                + ("\n…" if len(lines) > 20 else ""),
            )

    def sync_markdown(self, *, silent: bool = False) -> None:
        if not self.project.markdown_path:
            if not silent:
                self.import_markdown()
            return
        if not self.project.markdown_path.exists():
            if not silent:
                QMessageBox.warning(
                    self,
                    "源文件不存在",
                    f"找不到源 Markdown 文件：\n{self.project.markdown_path}",
                )
            return
        try:
            total, new_count, issues = self.project.sync_markdown()
        except Exception as exc:
            if not silent:
                QMessageBox.critical(self, "同步失败", str(exc))
            return
        self._mark_dirty()
        self._refresh_table()
        msg = f"已从源 Markdown 同步（共 {total} 条，新增 {new_count} 条）"
        self.statusBar().showMessage(msg, 5000)
        if not silent:
            info = f"源 Markdown 同步完成：\n总计 {total} 条文献，本次新增 {new_count} 条。"
            if issues:
                lines = [f"[{issue.severity}] {issue.code}: {issue.message}" for issue in issues]
                info += "\n\n提示：\n" + "\n".join(lines[:15])
            QMessageBox.information(self, "同步完成", info)

    def insert_entry_dialog(self) -> None:
        dialog = ManualInsertDialog(self)
        if dialog.exec_() == QDialog.Accepted:
            data = dialog.get_data()
            try:
                record = self.project.insert_entry(
                    title=data["title"],
                    year=data["year"],
                    venue=data["venue"],
                    bibtex=data["bibtex"],
                )
            except ValueError as exc:
                QMessageBox.warning(self, "插入文献失败", str(exc))
                return
            self._mark_dirty()
            self._refresh_table(select_id=record.id)
            self.statusBar().showMessage(f"已手动插入文献：{record.title}", 5000)

    def delete_selected_entry(self) -> None:
        record = self._current_record()
        if record is None:
            return
        answer = QMessageBox.question(
            self,
            "删除文献",
            f"确定从工程中删除以下文献？\n\n{record.display_title}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.project.remove_entry(record.id)
        self._current_id = None
        self._mark_dirty()
        self._refresh_table()
        self.statusBar().showMessage(f"已删除：{record.display_title}", 4000)

    def _show_table_context_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction(self.action_insert)
        if self._current_record() is not None:
            menu.addAction(self.action_fetch_selected)
            menu.addSeparator()
            menu.addAction("复制 BibTeX", self._copy_editor_text)
            menu.addSeparator()
            menu.addAction(self.action_delete)
        menu.exec_(self.table.viewport().mapToGlobal(pos))

    def _update_file_watcher(self) -> None:
        watched = self._watcher.files()
        if watched:
            self._watcher.removePaths(watched)
        if self.project.markdown_path and self.project.markdown_path.exists():
            self._watcher.addPath(str(self.project.markdown_path.resolve()))

    def _on_markdown_file_changed(self, path: str) -> None:
        QTimer.singleShot(400, lambda: self._handle_file_changed(path))

    def _handle_file_changed(self, path: str) -> None:
        if self.project.markdown_path and self.project.markdown_path.exists():
            resolved = str(self.project.markdown_path.resolve())
            if resolved not in self._watcher.files():
                self._watcher.addPath(resolved)
            self.sync_markdown(silent=True)
            lines = [f"[{issue.severity}] {issue.code}: {issue.message}" for issue in issues]
            QMessageBox.warning(
                self,
                "导入提示",
                "部分标题被跳过：\n\n" + "\n".join(lines[:20])
                + ("\n…" if len(lines) > 20 else ""),
            )

    # ---------------------------------------------------------------- fetch
    def fetch_records(self, *, only_selected: bool) -> None:
        if self._thread is not None and self._thread.isRunning():
            QMessageBox.information(self, "正在抓取", "已有抓取任务在运行，请稍候。")
            return
        if not self.project.entries:
            QMessageBox.information(self, "没有条目", "请先导入 Markdown 文件。")
            return

        targets: list[EntryRecord] = []
        if only_selected:
            record = self._current_record()
            if record is None:
                QMessageBox.information(self, "没有选中条目", "请先在左侧列表中选择一条文献。")
                return
            targets = [record]
        else:
            targets = [
                record
                for record in self.project.entries
                if not record.checked or not record.bibtex
            ]
            if not targets:
                answer = QMessageBox.question(
                    self,
                    "全部已抓取",
                    "所有条目都已有自动结果。是否重新抓取全部条目？\n"
                    "（手动覆盖不会被删除）",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    return
                targets = list(self.project.entries)

        target_ids = {record.id for record in targets}
        existing_keys = {
            record.citation_key
            for record in self.project.entries
            if record.citation_key and record.id not in target_ids
        }
        items = [(record.id, record.heading()) for record in targets]

        self._set_fetching(True, len(items))
        self._thread = QThread(self)
        self._worker = FetchWorker(items, self.project.options, existing_keys)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_fetch_progress)
        self._worker.resolved.connect(self._on_record_resolved)
        self._worker.failed.connect(self._on_record_failed)
        self._worker.finished.connect(self._on_fetch_finished)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()
        self.statusBar().showMessage(f"开始抓取 {len(items)} 条文献…")

    def _set_fetching(self, running: bool, total: int = 0) -> None:
        self.action_fetch_missing.setEnabled(not running)
        self.action_fetch_selected.setEnabled(not running)
        self.action_import.setEnabled(not running)
        self.progress.setVisible(running)
        if running:
            self.progress.setRange(0, max(1, total))
            self.progress.setValue(0)

    @pyqtSlot(int, int, str)
    def _on_fetch_progress(self, done: int, total: int, title: str) -> None:
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(done)
        if title:
            self.statusBar().showMessage(f"抓取中 {done + 1}/{total}：{title}")

    @pyqtSlot(str, object)
    def _on_record_resolved(self, record_id: str, resolved: object) -> None:
        record = self._record_by_id(record_id)
        if record is None or not isinstance(resolved, ResolvedEntry):
            return
        record.apply_resolved(resolved)
        self._mark_dirty()
        self._refresh_table(select_id=self._current_id or record_id)

    @pyqtSlot(str, str)
    def _on_record_failed(self, record_id: str, message: str) -> None:
        record = self._record_by_id(record_id)
        if record is None:
            return
        record.mark_error("openalex_request_failed", message)
        self._mark_dirty()
        self._refresh_table(select_id=self._current_id or record_id)

    @pyqtSlot(bool)
    def _on_fetch_finished(self, cancelled: bool) -> None:
        self._worker = None
        self._thread = None
        self._set_fetching(False)
        self._refresh_table(select_id=self._current_id)
        message = "抓取已取消" if cancelled else "抓取完成"
        self.statusBar().showMessage(message, 5000)

    # -------------------------------------------------------------- compile
    def compile_bibtex(self) -> None:
        if not self.project.entries:
            QMessageBox.information(self, "没有条目", "请先导入 Markdown 文件。")
            return
        if not any(record.effective_bibtex for record in self.project.entries):
            QMessageBox.information(
                self,
                "没有可编译的条目",
                "当前工程还没有任何 BibTeX。请先抓取元数据，或在右侧手动粘贴条目。",
            )
            return

        target = self.project.output_path
        if target is None:
            if self.project.markdown_path is not None:
                target = self.project.markdown_path.with_suffix(".bib")
            else:
                target = Path.cwd() / f"{self.project.name}.bib"
            filename, _ = QFileDialog.getSaveFileName(
                self, "保存 all-in-one BibTeX", str(target), BIBTEX_FILTER
            )
            if not filename:
                return
            if not filename.lower().endswith(".bib"):
                filename += ".bib"
            target = Path(filename)

        try:
            path, written, total, warnings = self.project.generate(target)
        except Exception as exc:
            QMessageBox.critical(self, "编译失败", str(exc))
            return

        self._mark_dirty()
        self.statusBar().showMessage(f"已生成 {path}", 8000)
        text = (
            f"已生成 all-in-one BibTeX：\n{path}\n\n"
            f"写入 {written}/{total} 条，按年份降序排列。"
        )
        if warnings:
            text += "\n\n注意：\n" + "\n".join(warnings[:20])
        QMessageBox.information(self, "编译完成", text)

    # -------------------------------------------------------------- settings
    def open_settings(self) -> None:
        dialog = SettingsDialog(self.project.options, self)
        if dialog.exec_() == QDialog.Accepted:
            dialog.apply_to(self.project.options)
            self._mark_dirty()
            self.statusBar().showMessage("设置已更新（API Key 不会写入工程）", 5000)

    # ---------------------------------------------------------------- table
    def _on_filter_changed(self, _text: str) -> None:
        self._refresh_table(select_id=self._current_id)

    def _filtered_entries(self) -> list[EntryRecord]:
        needle = self.filter_edit.text().strip().casefold()
        if not needle:
            return list(self.project.entries)
        result: list[EntryRecord] = []
        for record in self.project.entries:
            haystack = " ".join(
                (
                    record.display_title,
                    record.title,
                    record.venue,
                    record.citation_key or "",
                    record.doi or "",
                )
            ).casefold()
            if needle in haystack:
                result.append(record)
        return result

    def _refresh_table(self, select_id: str | None = None) -> None:
        selected = select_id or self._current_id or self._selected_table_id()
        header = self.table.horizontalHeader()
        sort_column = header.sortIndicatorSection()
        sort_order = header.sortIndicatorOrder()

        entries = self._filtered_entries()
        entries.sort(key=lambda record: (-(record.display_year or 0), record.display_title.casefold()))

        self._suppress_selection = True
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for row, record in enumerate(entries):
            self.table.insertRow(row)

            flag_item = QTableWidgetItem("!" if record.is_arxiv_only else "")
            flag_item.setData(Qt.UserRole, record.id)
            flag_item.setTextAlignment(Qt.AlignCenter)
            if record.is_arxiv_only:
                flag_item.setForeground(QBrush(ARXIV_COLOR))
                font = flag_item.font()
                font.setBold(True)
                flag_item.setFont(font)
                flag_item.setToolTip(ARXIV_TOOLTIP)
            self.table.setItem(row, 0, flag_item)

            title_item = QTableWidgetItem(record.display_title)
            title_item.setData(Qt.UserRole, record.id)
            title_item.setToolTip(
                f"{record.display_title}\n\n会议/期刊：{record.venue or '—'}\n"
                f"原始标题：{record.title}\n状态：{STATUS_LABELS.get(record.status, record.status)}"
            )
            self.table.setItem(row, 1, title_item)

            self.table.setItem(row, 2, _YearItem(record.display_year))

            status_item = QTableWidgetItem(STATUS_LABELS.get(record.status, record.status))
            status_item.setTextAlignment(Qt.AlignCenter)
            status_item.setForeground(QBrush(STATUS_COLORS.get(record.status, QColor("#333"))))
            self.table.setItem(row, 3, status_item)

            key_item = QTableWidgetItem(record.citation_key or "")
            key_item.setToolTip(record.citation_key or "")
            self.table.setItem(row, 4, key_item)

        self.table.setSortingEnabled(True)
        self.table.sortItems(sort_column, sort_order)
        self.table.resizeRowsToContents()

        self._row_by_id = {}
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None:
                record_id = item.data(Qt.UserRole)
                if record_id:
                    self._row_by_id[str(record_id)] = row

        if selected not in self._row_by_id and self._row_by_id:
            # Keep a useful selection after filtering or after a re-import.
            selected = str(next(iter(self._row_by_id)))
        if selected in self._row_by_id:
            self.table.setCurrentCell(self._row_by_id[selected], 1)
        else:
            self.table.clearSelection()
            selected = None
        self._suppress_selection = False

        if selected:
            record = self._record_by_id(selected)
            if record is not None:
                self._load_record(record)
        else:
            self._current_id = None
            self._load_record(None)

        self._update_summary()

    def _selected_table_id(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        value = item.data(Qt.UserRole) if item is not None else None
        return str(value) if value else None

    def _record_by_id(self, record_id: str | None) -> EntryRecord | None:
        if not record_id:
            return None
        for record in self.project.entries:
            if record.id == record_id:
                return record
        return None

    def _current_record(self) -> EntryRecord | None:
        return self._record_by_id(self._current_id)

    def _on_current_cell_changed(self, current_row: Any, _current_col: Any = 0, *args) -> None:
        if self._suppress_selection or self._editor_loading:
            return
        if hasattr(current_row, "row"):
            row = current_row.row()
        elif isinstance(current_row, int):
            row = current_row
        else:
            return
        if row < 0 or row >= self.table.rowCount():
            return

        item = self.table.item(row, 0)
        new_id = item.data(Qt.UserRole) if item is not None else None
        if not new_id:
            return
        new_id = str(new_id)
        if new_id == self._current_id:
            return

        if self._editor_dirty and self._current_id is not None:
            answer = QMessageBox.question(
                self,
                "未应用的修改",
                "编辑区还有未应用的修改。是否把它们保存为手动覆盖？",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if answer == QMessageBox.Cancel:
                self._select_table_record(self._current_id)
                return
            if answer == QMessageBox.Save:
                if not self._apply_current_manual(silent=True):
                    self._select_table_record(self._current_id)
                    return

        record = self._record_by_id(new_id)
        if record is not None:
            self._current_id = new_id
            self._load_record(record)

    def _select_table_record(self, record_id: str | None) -> None:
        if not record_id or record_id not in self._row_by_id:
            return
        self._suppress_selection = True
        self.table.setCurrentCell(self._row_by_id[record_id], 1)
        self._suppress_selection = False

    # --------------------------------------------------------------- editor
    def _load_record(self, record: EntryRecord | None) -> None:
        self._editor_loading = True
        try:
            if record is None:
                self._current_id = None
                self.editor_title.setText("未选择条目")
                self.editor_badge.setText("")
                self.editor_badge.setStyleSheet("")
                for widget in self.detail_widgets.values():
                    widget.clear()
                self.issues_view.clear()
                self.bibtex_edit.clear()
                self._set_editor_enabled(False)
                self._editor_dirty = False
                return

            self._current_id = record.id
            self._set_editor_enabled(True)
            self.editor_title.setText(record.display_title or record.title)
            if record.is_arxiv_only:
                self.editor_badge.setText("⚠ 只找到 arXiv 记录")
                self.editor_badge.setStyleSheet("color: #b06000; font-weight: 600;")
            elif record.manual_bibtex:
                label = "✓ 当前使用手动覆盖"
                if record.is_manual_entry:
                    label += "（手动插入条目）"
                self.editor_badge.setText(label)
                self.editor_badge.setStyleSheet("color: #1d5fa8; font-weight: 600;")
            elif record.is_manual_entry:
                self.editor_badge.setText("✦ 手动插入条目（尚未录入 BibTeX）")
                self.editor_badge.setStyleSheet("color: #7b1fa2; font-weight: 600;")
            else:
                self.editor_badge.setText("")
                self.editor_badge.setStyleSheet("")

            self.detail_widgets["raw_title"].setText(record.title)
            self.detail_widgets["matched_title"].setText(record.display_title)
            self.detail_widgets["year"].setText(str(record.display_year) if record.display_year else "—")
            self.detail_widgets["venue"].setText(record.venue)
            source = record.effective_source or "—"
            if record.is_manual_entry:
                source = f"手动插入 · {source}"
            elif record.is_arxiv_only:
                source = f"{source} · arXiv only"
            self.detail_widgets["source"].setText(source)
            self.detail_widgets["doi"].setText(record.doi or "—")
            self.detail_widgets["key"].setText(record.citation_key or "—")

            if record.issues:
                lines = []
                for issue in record.issues:
                    if isinstance(issue, dict):
                        sev = issue.get("severity", "warning")
                        code = issue.get("code", "?")
                        msg = issue.get("message", "")
                    else:
                        sev = getattr(issue, "severity", "warning")
                        code = getattr(issue, "code", "?")
                        msg = getattr(issue, "message", str(issue))
                    lines.append(f"[{sev}] {code}: {msg}")
                self.issues_view.setPlainText("\n".join(lines))
            else:
                self.issues_view.setPlainText("无")

            self.bibtex_edit.setPlainText(record.effective_bibtex or "")
            self._editor_dirty = False
        finally:
            self._editor_loading = False

    def _set_editor_enabled(self, enabled: bool) -> None:
        self.bibtex_edit.setEnabled(enabled)
        self.apply_button.setEnabled(enabled)
        self.copy_button.setEnabled(enabled)
        self.revert_button.setEnabled(enabled)
        self.delete_button.setEnabled(enabled)

    def _on_editor_text_changed(self) -> None:
        if not self._editor_loading:
            self._editor_dirty = True
            self.revert_button.setEnabled(self._current_record() is not None)

    def _apply_current_manual(self, *, silent: bool) -> bool:
        record = self._current_record()
        if record is None:
            return False
        text = self.bibtex_edit.toPlainText()
        try:
            key = record.apply_manual(text)
        except ValueError as exc:
            if not silent:
                QMessageBox.warning(self, "无法应用覆盖", str(exc))
            return False

        self._dirty = True
        self._editor_dirty = False
        self._refresh_table(select_id=record.id)
        if not silent:
            self.statusBar().showMessage(f"已保存手动覆盖：{key}", 5000)
        return True

    def _revert_current_manual(self) -> None:
        record = self._current_record()
        if record is None:
            return
        if not record.manual_bibtex:
            QMessageBox.information(self, "没有覆盖", "当前条目没有手动覆盖。")
            return
        answer = QMessageBox.question(
            self,
            "恢复自动结果",
            "确定删除当前条目的手动 BibTeX 覆盖？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        record.clear_manual()
        self._editor_dirty = False
        self._dirty = True
        self._refresh_table(select_id=record.id)
        self.statusBar().showMessage("已恢复自动结果", 5000)

    def _copy_editor_text(self) -> None:
        QApplication.clipboard().setText(self.bibtex_edit.toPlainText())
        self.statusBar().showMessage("已复制 BibTeX 到剪贴板", 3000)

    # --------------------------------------------------------------- helpers
    def _update_summary(self) -> None:
        counts = self.project.counts()
        self.summary_label.setText(
            "共 {total} 条 · 已解析 {found} · 手动 {manual} · 缺失 {missing} · "
            "待抓取 {pending} · arXiv {arxiv}".format(
                total=counts["total"],
                found=counts[STATUS_FOUND],
                manual=counts[STATUS_MANUAL],
                missing=counts[STATUS_MISSING],
                pending=counts[STATUS_PENDING],
                arxiv=counts["arxiv"],
            )
        )
        if self.project.path:
            self.path_label.setText(str(self.project.path))
            self.path_label.setToolTip(str(self.project.path))
        elif self.project.markdown_path:
            self.path_label.setText(f"Markdown: {self.project.markdown_path}")
            self.path_label.setToolTip(str(self.project.markdown_path))
        else:
            self.path_label.setText("尚未保存工程" if not self.project.entries else "工程未保存")
            self.path_label.setToolTip("")

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._update_title()
        self._update_summary()

    def _update_title(self) -> None:
        marker = " *" if self._dirty else ""
        self.setWindowTitle(f"{self.project.name}{marker} — bibfetch GUI")

    def _confirm_discard_changes(self) -> bool:
        if not self._dirty:
            return True
        answer = QMessageBox.question(
            self,
            "未保存的工程",
            "当前工程有未保存的修改。是否先保存？",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Save:
            return self.save_project(save_as=False)
        return True

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            "关于 bibfetch GUI",
            "<b>bibfetch GUI</b><br><br>"
            "从 Markdown 标题管理文献、查询 OpenAlex，并生成按年份降序排列的 "
            "all-in-one BibTeX。<br><br>"
            "工程保存为 <code>*.bibgui.json</code>，手动覆盖优先于自动结果。",
        )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if not self._confirm_discard_changes():
            event.ignore()
            return
        if self._worker is not None:
            self._worker.cancel()
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(2000)
        event.accept()


def main(argv: list[str] | None = None) -> int:
    def _gui_excepthook(exc_type, exc_val, exc_tb):
        traceback.print_exception(exc_type, exc_val, exc_tb)

    sys.excepthook = _gui_excepthook

    argv = list(sys.argv if argv is None else argv)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(argv)
    app.setApplicationName("bibfetch GUI")
    app.setOrganizationName("bibfetch")

    window = MainWindow()
    arguments = app.arguments()[1:]
    if arguments:
        candidate = Path(arguments[0]).expanduser()
        if candidate.is_file():
            window.open_project_path(str(candidate))
    window.show()
    return app.exec_()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
