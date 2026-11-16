import os

from aqt import mw
from aqt.operations import QueryOp
from aqt.qt import QAction, QFileDialog
from aqt.utils import qconnect, showInfo, showWarning

from .extractor import ExtractionError, extract_audio_from_apkg


def _on_done(output_dir: str, extracted: list[str]) -> None:
    if extracted:
        showInfo(
            f"Successfully extracted {len(extracted)} audio files to:\n{output_dir}",
            parent=mw,
        )
    else:
        showInfo("No audio files found in the package.", parent=mw)


def _on_failure(err: Exception) -> None:
    if isinstance(err, ExtractionError):
        showWarning(str(err), parent=mw)
    else:
        showWarning(f"An error occurred: {err}", parent=mw)


def select_apkg_file() -> None:
    file_path, _ = QFileDialog.getOpenFileName(
        mw,
        "Select Anki Package File",
        "",
        "Anki Package Files (*.apkg *.colpkg)",
    )
    if not file_path:
        return

    output_dir = QFileDialog.getExistingDirectory(
        mw, "Select Output Directory", os.path.expanduser("~")
    )
    if not output_dir:
        return

    op = QueryOp(
        parent=mw,
        op=lambda _col: extract_audio_from_apkg(file_path, output_dir),
        success=lambda extracted: _on_done(output_dir, extracted),
    ).failure(_on_failure).with_progress("Extracting audio...")
    if hasattr(op, "without_collection"):  # Anki 23.10+: don't block other collection ops
        op = op.without_collection()
    op.run_in_background()


action = QAction("Extract Audio from Package", mw)
qconnect(action.triggered, select_apkg_file)
mw.form.menuTools.addAction(action)
