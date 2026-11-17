import os
from typing import Any, Optional

from aqt import mw
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
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from aqt.utils import qconnect, showInfo, showWarning

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


def _run(op_fn, on_success, progress: str) -> QueryOp:
    op = QueryOp(parent=mw, op=lambda _col: op_fn(), success=on_success).with_progress(progress)
    if hasattr(op, "without_collection"):  # Anki 23.10+: don't block other collection ops
        op = op.without_collection()
    return op


def _on_failure(err: Exception) -> None:
    if isinstance(err, ExtractionError):
        showWarning(str(err), parent=mw)
    else:
        showWarning(f"An error occurred: {err}", parent=mw)


class OptionsDialog(QDialog):
    def __init__(self, package: Package, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Extract Audio")
        conf = _config()
        count = len(package.audio_names)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(f"{count} audio file{'s' if count != 1 else ''} in {os.path.basename(package.path)}")
        )
        form = QFormLayout()
        layout.addLayout(form)

        self.output = QLineEdit(conf.get("last_output_dir") or os.path.expanduser("~"))
        browse = QPushButton("Browse…")
        qconnect(browse.clicked, self._browse)
        row = QHBoxLayout()
        row.addWidget(self.output)
        row.addWidget(browse)
        form.addRow("Output folder:", row)

        self.naming = QComboBox()
        self.naming.addItem("Original file name", None)
        self.naming.addItem("First field of the note", FIRST_FIELD)
        for name in package.field_names:
            self.naming.addItem(f"Field: {name}", name)
        saved = conf.get("name_files_after", "original")
        saved = None if saved == "original" else FIRST_FIELD if saved == "first field" else saved
        self.naming.setCurrentIndex(max(0, self.naming.findData(saved)))
        self.naming.setToolTip(
            "Files no note plays, or whose note has nothing in that field,\n"
            "keep their original name."
        )
        form.addRow("Name files after:", self.naming)

        self.conflict = QComboBox()
        for value, label in _CONFLICT_CHOICES:
            self.conflict.addItem(label, value)
        self.conflict.setCurrentIndex(max(0, self.conflict.findData(conf.get("if_file_exists", RENAME))))
        form.addRow("If a file already exists:", self.conflict)

        self.csv = QCheckBox("Also save audio_notes.csv listing each file's note")
        self.csv.setChecked(bool(conf.get("write_csv", False)))
        form.addRow("", self.csv)

        if not package.has_notes:
            for w in (self.naming, self.csv):
                w.setEnabled(False)
                w.setToolTip("The notes in this package couldn't be read.")
            self.naming.setCurrentIndex(0)
            self.csv.setChecked(False)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Extract")
        qconnect(buttons.accepted, self.accept)
        qconnect(buttons.rejected, self.reject)
        layout.addWidget(buttons)

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
        )
        _save_config(conf)


def _summary(output_dir: str, result: ExtractionResult) -> str:
    written = len(result.names)
    if not result.files:
        return "No audio files found in the package."
    lines = [f"Extracted {written} audio file{'s' if written != 1 else ''} to:\n{output_dir}"]
    if n := result.count(RENAMED):
        lines.append(f"{n} given a numbered name because a file with that name already existed.")
    if n := result.count(OVERWRITTEN):
        lines.append(f"{n} replaced existing files.")
    if n := result.count(SKIPPED):
        lines.append(f"{n} skipped because a file with that name already existed.")
    if result.csv_path:
        lines.append(f"Note details saved to {os.path.basename(result.csv_path)}.")
    return "\n\n".join(lines)


def _on_opened(package: Package) -> None:
    if not package.audio_names:
        package.close()
        showInfo("No audio files found in the package.", parent=mw)
        return

    dialog = OptionsDialog(package, mw)
    if not dialog.exec():
        package.close()
        return
    dialog.remember()
    output_dir, options = dialog.output_dir(), dialog.options()

    def on_done(result: ExtractionResult) -> None:
        package.close()
        showInfo(_summary(output_dir, result), parent=mw)

    def on_failure(err: Exception) -> None:
        package.close()
        _on_failure(err)

    _run(
        lambda: package.extract(output_dir, options), on_done, "Extracting audio..."
    ).failure(on_failure).run_in_background()


def select_apkg_file() -> None:
    file_path, _ = QFileDialog.getOpenFileName(
        mw,
        "Select Anki Package File",
        "",
        "Anki Package Files (*.apkg *.colpkg)",
    )
    if not file_path:
        return
    _run(lambda: Package(file_path), _on_opened, "Reading package...").failure(
        _on_failure
    ).run_in_background()


action = QAction("Extract Audio from Package", mw)
qconnect(action.triggered, select_apkg_file)
mw.form.menuTools.addAction(action)
