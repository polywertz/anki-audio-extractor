import os
from typing import Any, Callable, Optional, Sequence

from aqt import gui_hooks, mw
from aqt.browser import Browser
from aqt.operations import QueryOp
from aqt.qt import (
    QAction,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    Qt,
    QVBoxLayout,
    QWidget,
)
from aqt.utils import qconnect, showInfo, showWarning, tooltip

from .extractor import (
    FIRST_FIELD,
    OVERWRITE,
    OVERWRITTEN,
    RENAME,
    RENAMED,
    SKIP,
    SKIPPED,
    ExtractionError,
    ExtractionResult,
    ExtractOptions,
    Package,
    SelectedCards,
    AudioSource,
)

_CONFLICT_CHOICES = [
    (RENAME, "Keep both (add a number to the new file)"),
    (SKIP, "Skip it"),
    (OVERWRITE, "Overwrite it"),
]


def _config() -> dict[str, Any]:
    return mw.addonManager.getConfig(__name__) or {}


def _save_config(conf: dict[str, Any]) -> None:
    mw.addonManager.writeConfig(__name__, conf)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _on_failure(err: Exception) -> None:
    if isinstance(err, ExtractionError):
        showWarning(str(err), parent=mw)
    else:
        showWarning(f"An error occurred: {err}", parent=mw)


class OptionsDialog(QDialog):
    def __init__(self, source: AudioSource, title: str, order_hint: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Extract Audio")
        self._source = source
        self._title = title
        conf = _config()

        layout = QVBoxLayout(self)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        form = QFormLayout()
        layout.addLayout(form)

        self.output = QLineEdit(conf.get("last_output_dir") or os.path.expanduser("~"))
        browse = QPushButton("Browse…")
        qconnect(browse.clicked, self._browse)
        row = QHBoxLayout()
        row.addWidget(self.output)
        row.addWidget(browse)
        form.addRow("Output folder:", row)

        # Which fields to take audio from; only worth asking with several.
        self.fields: Optional[QListWidget] = None
        audio_fields = source.audio_field_names
        if len(audio_fields) > 1:
            excluded = set(conf.get("excluded_audio_fields", []))
            self.fields = QListWidget()
            for name in audio_fields:
                item = QListWidgetItem(name)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Unchecked if name in excluded else Qt.CheckState.Checked
                )
                self.fields.addItem(item)
            self.fields.setMaximumHeight(min(160, 24 * len(audio_fields) + 8))
            self.fields.setToolTip("Only extract audio played from the ticked fields.")
            qconnect(self.fields.itemChanged, lambda _item: self._update_count())
            form.addRow("Take audio from:", self.fields)

        self.naming = QComboBox()
        self.naming.addItem("Original file name", None)
        self.naming.addItem("First field of the note", FIRST_FIELD)
        for name in source.field_names:
            self.naming.addItem(f"Field: {name}", name)
        saved = conf.get("name_files_after", "original")
        saved = None if saved == "original" else FIRST_FIELD if saved == "first field" else saved
        self.naming.setCurrentIndex(max(0, self.naming.findData(saved)))
        self.naming.setToolTip(
            "Files no note plays, or whose note has nothing in that field,\n"
            "keep their original name."
        )
        form.addRow("Name files after:", self.naming)

        self.number = QCheckBox(f"Number files {order_hint} (0001_, 0002_, …)")
        self.number.setToolTip("Files from the same note get the same number.")
        self.number.setChecked(bool(conf.get("number_prefix", False)))
        form.addRow("", self.number)

        self.conflict = QComboBox()
        for value, label in _CONFLICT_CHOICES:
            self.conflict.addItem(label, value)
        self.conflict.setCurrentIndex(max(0, self.conflict.findData(conf.get("if_file_exists", RENAME))))
        form.addRow("If a file already exists:", self.conflict)

        self.csv = QCheckBox("Also save audio_notes.csv listing each file's note")
        self.csv.setChecked(bool(conf.get("write_csv", False)))
        form.addRow("", self.csv)

        if not source.has_notes:
            for w in (self.naming, self.number, self.csv):
                w.setEnabled(False)
                w.setToolTip("The notes in this package couldn't be read.")
            self.naming.setCurrentIndex(0)
            self.number.setChecked(False)
            self.csv.setChecked(False)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Extract")
        qconnect(self.buttons.accepted, self.accept)
        qconnect(self.buttons.rejected, self.reject)
        layout.addWidget(self.buttons)
        self._update_count()

    def _selected_fields(self) -> Optional[frozenset[str]]:
        if self.fields is None:
            return None
        items = [self.fields.item(i) for i in range(self.fields.count())]
        if all(i.checkState() == Qt.CheckState.Checked for i in items):
            return None  # everything, including audio no note plays
        return frozenset(i.text() for i in items if i.checkState() == Qt.CheckState.Checked)

    def _update_count(self) -> None:
        count = self._source.audio_count(self._selected_fields())
        self.summary.setText(f"{_plural(count, 'audio file')} {self._title}")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(count > 0)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Output Folder", self.output.text())
        if path:
            self.output.setText(path)

    def accept(self) -> None:
        if not self.output.text().strip():
            showWarning("Choose an output folder.", parent=self)
            return
        super().accept()

    def output_dir(self) -> str:
        return os.path.expanduser(self.output.text().strip())

    def options(self) -> ExtractOptions:
        return ExtractOptions(
            name_field=self.naming.currentData(),
            on_conflict=self.conflict.currentData(),
            write_csv=self.csv.isChecked(),
            number_prefix=self.number.isChecked(),
            audio_fields=self._selected_fields(),
        )

    def remember(self) -> None:
        name_field: Optional[str] = self.naming.currentData()
        conf = _config()
        conf.update(
            last_output_dir=self.output_dir(),
            name_files_after=(
                "original" if name_field is None
                else "first field" if name_field == FIRST_FIELD
                else name_field
            ),
            if_file_exists=self.conflict.currentData(),
            write_csv=self.csv.isChecked(),
            number_prefix=self.number.isChecked(),
        )
        if self.fields is not None:
            # Only update the fields shown here, so other decks' choices survive.
            shown = {self.fields.item(i).text() for i in range(self.fields.count())}
            selected = self._selected_fields()
            excluded = set(conf.get("excluded_audio_fields", [])) - shown
            if selected is not None:
                excluded |= shown - selected
            conf["excluded_audio_fields"] = sorted(excluded)
        _save_config(conf)


def _summary(output_dir: str, result: ExtractionResult) -> str:
    written = len(result.names)
    lines = [f"Extracted {_plural(written, 'audio file')} to:\n{output_dir}"]
    if n := result.count(RENAMED):
        lines.append(f"{n} given a numbered name because a file with that name already existed.")
    if n := result.count(OVERWRITTEN):
        lines.append(f"{n} replaced existing files.")
    if n := result.count(SKIPPED):
        lines.append(f"{n} skipped because a file with that name already existed.")
    if result.missing:
        shown = ", ".join(result.missing[:5]) + (" …" if len(result.missing) > 5 else "")
        lines.append(
            f"{_plural(len(result.missing), 'file')} used by the cards "
            f"{'is' if len(result.missing) == 1 else 'are'} missing from your media folder "
            f"(Tools → Check Media): {shown}"
        )
    if result.csv_path:
        lines.append(f"Note details saved to {os.path.basename(result.csv_path)}.")
    return "\n\n".join(lines)


def _run(parent: QWidget, op_fn: Callable[[Any], Any], on_success, progress: str,
         needs_collection: bool = False) -> QueryOp:
    op = QueryOp(parent=parent, op=op_fn, success=on_success).with_progress(progress)
    if not needs_collection and hasattr(op, "without_collection"):
        op = op.without_collection()  # Anki 23.10+: don't block other collection ops
    return op


def _show_dialog(
    source: AudioSource, title: str, order_hint: str, parent: QWidget, empty_message: str
) -> None:
    if source.audio_count() == 0:
        source.close()
        message = empty_message
        if source.missing:
            n = len(source.missing)
            message += (
                f"\n\n{_plural(n, 'file')} used by the cards {'is' if n == 1 else 'are'} "
                "missing from your media folder."
            )
        showInfo(message, parent=parent)
        return

    dialog = OptionsDialog(source, title, order_hint, parent)
    if not dialog.exec():
        source.close()
        return
    dialog.remember()
    output_dir, options = dialog.output_dir(), dialog.options()

    def on_done(result: ExtractionResult) -> None:
        source.close()
        showInfo(_summary(output_dir, result), parent=parent)

    def on_failure(err: Exception) -> None:
        source.close()
        _on_failure(err)

    _run(
        parent, lambda _col: source.extract(output_dir, options), on_done, "Extracting audio..."
    ).failure(on_failure).run_in_background()


# --- Tools → Extract Audio from Package ---------------------------------------


def select_apkg_file() -> None:
    file_path, _ = QFileDialog.getOpenFileName(
        mw,
        "Select Anki Package File",
        "",
        "Anki Package Files (*.apkg *.colpkg)",
    )
    if not file_path:
        return

    def opened(package: Package) -> None:
        _show_dialog(
            package,
            f"in {os.path.basename(file_path)}",
            "in card order",
            mw,
            "No audio files found in the package.",
        )

    _run(mw, lambda _col: Package(file_path), opened, "Reading package...").failure(
        _on_failure
    ).run_in_background()


action = QAction("Extract Audio from Package", mw)
qconnect(action.triggered, select_apkg_file)
mw.form.menuTools.addAction(action)


# --- Browser → Notes → Export Audio from Selected Cards -----------------------


def _selected_cards_in_table_order(browser: Browser) -> Sequence[int]:
    """Selected card ids in the order shown in the Browser.

    Qt reports selected rows in the order they were clicked, so sort them by row.
    """
    table = browser.table
    try:
        rows = sorted(table._selected(), key=lambda index: index.row())
        return table._model.get_card_ids(rows)
    except Exception:  # private API changed; fall back to Anki's order
        return browser.selected_cards()


def export_from_browser(browser: Browser) -> None:
    card_ids = _selected_cards_in_table_order(browser)
    if not card_ids:
        tooltip("Select some cards first.", parent=browser)
        return

    def loaded(source: SelectedCards) -> None:
        _show_dialog(
            source,
            f"on {_plural(source.note_count, 'selected note')}",
            "in Browser order",
            browser,
            "The selected cards don't play any audio.",
        )

    _run(
        browser,
        lambda col: SelectedCards(col, card_ids),
        loaded,
        "Finding audio...",
        needs_collection=True,
    ).failure(_on_failure).run_in_background()


def _add_browser_action(browser: Browser) -> None:
    browser_action = QAction("Export Audio from Selected Cards…", browser)
    qconnect(browser_action.triggered, lambda: export_from_browser(browser))
    browser.form.menu_Notes.addSeparator()
    browser.form.menu_Notes.addAction(browser_action)


gui_hooks.browser_menus_did_init.append(_add_browser_action)
