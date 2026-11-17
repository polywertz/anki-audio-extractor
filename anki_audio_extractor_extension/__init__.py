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
    QMenu,
    QMessageBox,
    QPushButton,
    Qt,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from aqt.utils import openFolder, qconnect, showInfo, showWarning, tooltip

from .extractor import (
    FIRST_FIELD,
    OVERWRITE,
    OVERWRITTEN,
    PLAYLIST_NAME as _PLAYLIST,
    RENAME,
    RENAMED,
    SKIP,
    SKIPPED,
    WRITTEN,
    ExtractedFile,
    ExtractionError,
    ExtractionResult,
    ExtractOptions,
    Package,
    SelectedCards,
    AudioSource,
    deck_card_ids,
    pattern_fields,
)

# Combo box value for "name files with a pattern". Braces can't be in field names.
_PATTERN = "{pattern}"

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
        self.naming.addItem("Pattern of several fields…", _PATTERN)
        saved = conf.get("name_files_after", "original")
        saved = {"original": None, "first field": FIRST_FIELD, "pattern": _PATTERN}.get(saved, saved)
        self.naming.setCurrentIndex(max(0, self.naming.findData(saved)))
        self.naming.setToolTip(
            "Files no note plays, or whose note has nothing in that field,\n"
            "keep their original name."
        )
        form.addRow("Name files after:", self.naming)

        fields = source.field_names
        example = " - ".join(f"{{{f}}}" for f in fields[:2]) or "{Front} - {Back}"
        self.pattern = QLineEdit(conf.get("name_pattern") or example)
        self.pattern.setPlaceholderText(example)
        self.pattern.setToolTip(
            "Put field names in braces, for example " + example + ".\n"
            "Empty fields are left out, along with the text next to them.\n\n"
            "Fields: " + ", ".join(fields)
        )
        self.pattern_label = QLabel("Pattern:")
        form.addRow(self.pattern_label, self.pattern)
        qconnect(self.naming.currentIndexChanged, lambda _i: self._update_pattern_row())
        self._update_pattern_row()

        self.number = QCheckBox(f"Number files {order_hint} (0001_, 0002_, …)")
        self.number.setToolTip("Files from the same note get the same number.")
        self.number.setChecked(bool(conf.get("number_prefix", False)))
        form.addRow("", self.number)

        self.conflict = QComboBox()
        for value, label in _CONFLICT_CHOICES:
            self.conflict.addItem(label, value)
        self.conflict.setCurrentIndex(max(0, self.conflict.findData(conf.get("if_file_exists", RENAME))))
        form.addRow("If a file already exists:", self.conflict)

        self.subfolders = QCheckBox("Put each deck's audio in its own folder")
        self.subfolders.setToolTip(
            "Subdecks become subfolders, e.g. Japanese::Vocab → Japanese/Vocab.\n"
            "Audio no note plays goes in the output folder itself."
        )
        self.subfolders.setChecked(bool(conf.get("subfolder_per_deck", False)))
        form.addRow("", self.subfolders)

        self.csv = QCheckBox("Also save audio_notes.csv listing each file's note")
        self.csv.setChecked(bool(conf.get("write_csv", False)))
        form.addRow("", self.csv)

        self.playlist = QCheckBox(f"Also save {_PLAYLIST} to play the files {order_hint}")
        self.playlist.setToolTip("Opens in VLC, phone music players and most other audio apps.")
        self.playlist.setChecked(bool(conf.get("write_playlist", False)))
        form.addRow("", self.playlist)

        if not source.has_notes:
            for w in (self.naming, self.number, self.csv, self.subfolders):
                w.setEnabled(False)
                w.setToolTip("The notes in this package couldn't be read.")
            self.naming.setCurrentIndex(0)
            for w in (self.number, self.csv, self.subfolders):
                w.setChecked(False)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Extract")
        preview = self.buttons.addButton("Preview Names…", QDialogButtonBox.ButtonRole.ActionRole)
        qconnect(preview.clicked, self._preview)
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

    def _update_pattern_row(self) -> None:
        shown = self.naming.currentData() == _PATTERN
        self.pattern.setVisible(shown)
        self.pattern_label.setVisible(shown)
        self.adjustSize()

    def _pattern_problem(self) -> Optional[str]:
        if self.naming.currentData() != _PATTERN:
            return None
        used = pattern_fields(self.pattern.text())
        if not used:
            return "Put at least one field name in braces in the pattern, for example {Front}."
        unknown = [f for f in used if f not in self._source.field_names]
        if unknown:
            return (
                f"These aren't fields of the notes: {', '.join(unknown)}\n\n"
                f"Fields: {', '.join(self._source.field_names)}"
            )
        return None

    def _check(self) -> bool:
        problem = (
            "Choose an output folder." if not self.output.text().strip() else self._pattern_problem()
        )
        if problem:
            showWarning(problem, parent=self)
            return False
        return True

    def _preview(self) -> None:
        if not self._check():
            return
        files = self._source.preview(self.output_dir(), self.options())
        PreviewDialog(files, self).exec()

    def accept(self) -> None:
        if not self._check():
            return
        super().accept()

    def output_dir(self) -> str:
        return os.path.expanduser(self.output.text().strip())

    def options(self) -> ExtractOptions:
        naming: Optional[str] = self.naming.currentData()
        return ExtractOptions(
            name_field=None if naming == _PATTERN else naming,
            name_pattern=self.pattern.text().strip() if naming == _PATTERN else None,
            on_conflict=self.conflict.currentData(),
            write_csv=self.csv.isChecked(),
            write_playlist=self.playlist.isChecked(),
            number_prefix=self.number.isChecked(),
            subfolder_per_deck=self.subfolders.isChecked(),
            audio_fields=self._selected_fields(),
        )

    def remember(self) -> None:
        naming: Optional[str] = self.naming.currentData()
        conf = _config()
        conf.update(
            last_output_dir=self.output_dir(),
            name_files_after={None: "original", FIRST_FIELD: "first field", _PATTERN: "pattern"}.get(
                naming, naming
            ),
            if_file_exists=self.conflict.currentData(),
            write_csv=self.csv.isChecked(),
            write_playlist=self.playlist.isChecked(),
            number_prefix=self.number.isChecked(),
            subfolder_per_deck=self.subfolders.isChecked(),
        )
        if self.pattern.text().strip():
            conf["name_pattern"] = self.pattern.text().strip()
        if self.fields is not None:
            # Only update the fields shown here, so other decks' choices survive.
            shown = {self.fields.item(i).text() for i in range(self.fields.count())}
            selected = self._selected_fields()
            excluded = set(conf.get("excluded_audio_fields", [])) - shown
            if selected is not None:
                excluded |= shown - selected
            conf["excluded_audio_fields"] = sorted(excluded)
        _save_config(conf)


_STATUS_TEXT = {
    RENAMED: "Numbered: name already taken",
    SKIPPED: "Skipped: file already exists",
    OVERWRITTEN: "Replaces the existing file",
}


class PreviewDialog(QDialog):
    def __init__(self, files: Sequence[ExtractedFile], parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preview Names")
        layout = QVBoxLayout(self)
        changed = sum(f.status != WRITTEN for f in files)
        text = f"{_plural(len(files), 'file')} would be saved like this"
        if changed:
            text += f" ({changed} with a note)"
        layout.addWidget(QLabel(text + ". Nothing has been written yet."))

        table = QTableWidget(len(files), 3)
        table.setHorizontalHeaderLabels(["Original name", "Saved as", "Note"])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        for row, f in enumerate(files):
            for col, value in enumerate((f.original, f.name, _STATUS_TEXT.get(f.status, ""))):
                table.setItem(row, col, QTableWidgetItem(value))
        table.resizeColumnsToContents()
        layout.addWidget(table)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        qconnect(buttons.rejected, self.reject)
        layout.addWidget(buttons)
        self.resize(760, 480)


def _show_done(output_dir: str, result: ExtractionResult, parent: QWidget) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle("Extract Audio")
    box.setIcon(QMessageBox.Icon.Information)
    box.setText(_summary(output_dir, result))
    open_button = box.addButton("Open Folder", QMessageBox.ButtonRole.ActionRole)
    box.addButton(QMessageBox.StandardButton.Close)
    box.setDefaultButton(open_button)
    box.exec()
    if box.clickedButton() is open_button:
        openFolder(output_dir)


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
    saved = [os.path.basename(p) for p in (result.playlist_path, result.csv_path) if p]
    if saved:
        lines.append(f"Also saved {' and '.join(saved)}.")
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
        _show_done(output_dir, result, parent)

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


# --- Deck list → gear menu → Export Audio --------------------------------------


def export_deck(deck_id: int) -> None:
    deck_name = mw.col.decks.name(deck_id)

    def load(col: Any) -> SelectedCards:
        return SelectedCards(col, deck_card_ids(col, deck_id))

    def loaded(source: SelectedCards) -> None:
        _show_dialog(
            source,
            f"in {deck_name} ({_plural(source.note_count, 'note')})",
            "in deck order",
            mw,
            f"No cards in {deck_name} play any audio.",
        )

    _run(mw, load, loaded, "Finding audio...", needs_collection=True).failure(
        _on_failure
    ).run_in_background()


def _add_deck_action(menu: QMenu, deck_id: int) -> None:
    deck_action = menu.addAction("Export Audio…")
    qconnect(deck_action.triggered, lambda: export_deck(deck_id))


gui_hooks.deck_browser_will_show_options_menu.append(_add_deck_action)
