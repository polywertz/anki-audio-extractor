"""Tests for the add-on's dialogs and menu entries.

Qt runs offscreen and Anki's main window is replaced by a small stand-in, so
these run without starting Anki. Background operations run immediately.
"""

import os
import sys
import types
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from anki.collection import Collection, ExportAnkiPackageOptions  # noqa: E402
from aqt.qt import (  # noqa: E402
    QApplication,
    QDialogButtonBox,
    QMainWindow,
    QMenu,
    QMessageBox,
    Qt,
    QTableWidget,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeAddonManager:
    def __init__(self):
        self.config = {}

    def getConfig(self, _name):
        return dict(self.config)

    def writeConfig(self, _name, conf):
        self.config = dict(conf)


@pytest.fixture(scope="session")
def mw(qapp):
    import aqt

    window = QMainWindow()
    window.form = types.SimpleNamespace(menuTools=QMenu())
    window.addonManager = FakeAddonManager()
    window.col = None
    aqt.mw = window
    return window


@pytest.fixture(scope="session")
def addon(mw):
    sys.path.insert(0, str(ROOT))
    import anki_audio_extractor_extension

    return anki_audio_extractor_extension


@pytest.fixture(autouse=True)
def fresh_config(request):
    if "addon" in request.fixturenames:
        request.getfixturevalue("mw").addonManager.config = {}


class Calls:
    """Records calls to the message helpers instead of showing windows."""

    def __init__(self, addon, monkeypatch):
        self.info, self.warnings, self.tooltips, self.opened = [], [], [], []
        monkeypatch.setattr(addon, "showInfo", lambda msg, parent=None: self.info.append(msg))
        monkeypatch.setattr(addon, "showWarning", lambda msg, parent=None: self.warnings.append(msg))
        monkeypatch.setattr(addon, "tooltip", lambda msg, parent=None: self.tooltips.append(msg))
        monkeypatch.setattr(addon, "openFolder", self.opened.append)


@pytest.fixture
def calls(addon, monkeypatch):
    return Calls(addon, monkeypatch)


class FakeOp:
    """Stand-in for QueryOp that runs straight away, without the collection lock."""

    def __init__(self, op_fn, on_success, col):
        self.op_fn, self.on_success, self.col = op_fn, on_success, col
        self.on_failure = None

    def failure(self, on_failure):
        self.on_failure = on_failure
        return self

    def run_in_background(self):
        try:
            result = self.op_fn(self.col)
        except Exception as e:
            self.on_failure(e)
        else:
            self.on_success(result)


@pytest.fixture
def ops(addon, mw, monkeypatch):
    started = []

    def run(parent, op_fn, on_success, progress, needs_collection=False):
        started.append((progress, needs_collection))
        return FakeOp(op_fn, on_success, mw.col)

    monkeypatch.setattr(addon, "_run", run)
    return started


@pytest.fixture
def col(mw, tmp_path):
    """A collection with audio on two decks, a picture note and a missing file."""
    (tmp_path / "col").mkdir()
    c = Collection(str(tmp_path / "col" / "c.anki2"))
    for i in range(1, 5):
        p = tmp_path / f"a{i}.mp3"
        p.write_bytes(f"audio {i}".encode())
        c.media.add_file(str(p))
    basic = c.models.by_name("Basic")
    cards = {}
    for key, deck, front, back in [
        ("cat", "Lang::JP", "猫", "cat [sound:a1.mp3]"),
        ("dog", "Lang::JP", "犬 [sound:a2.mp3]", "dog [sound:a3.mp3]"),
        ("pic", "Lang::JP", "picture", '<img src="x.png">'),
        ("fox", "Other", "fox", "[sound:a4.mp3][sound:gone.mp3]"),
    ]:
        note = c.new_note(basic)
        note["Front"], note["Back"] = front, back
        c.add_note(note, c.decks.id(deck))
        cards[key] = note.card_ids()[0]
    mw.col = c
    c.test_cards = cards
    yield c
    mw.col = None
    c.close()


@pytest.fixture
def source(addon, col):
    return addon.SelectedCards(col, list(col.test_cards.values()))


def make_dialog(addon, mw, src, order_hint="in Browser order"):
    return addon.OptionsDialog(src, "on 4 selected notes", order_hint, mw)


def ok_button(dialog):
    return dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)


# --- Options dialog -------------------------------------------------------------


def test_dialog_defaults(addon, mw, source):
    d = make_dialog(addon, mw, source)
    assert d.summary.text() == "4 audio files on 4 selected notes"
    assert d.output.text() == os.path.expanduser("~")
    assert d.naming.currentData() is None
    assert [d.naming.itemText(i) for i in range(d.naming.count())] == [
        "Original file name", "First field of the note", "Field: Front", "Field: Back",
        "Pattern of several fields…",
    ]
    assert not d.pattern.isVisibleTo(d)
    assert d.pattern.text() == "{Front} - {Back}"
    assert d.conflict.currentData() == addon.RENAME
    assert not any(w.isChecked() for w in (d.number, d.csv, d.playlist, d.subfolders))
    assert "in Browser order" in d.number.text() and "in Browser order" in d.playlist.text()
    assert ok_button(d).isEnabled()


def test_field_list_updates_count(addon, mw, source):
    d = make_dialog(addon, mw, source)
    assert [d.fields.item(i).text() for i in range(d.fields.count())] == ["Front", "Back"]
    d.fields.item(1).setCheckState(Qt.CheckState.Unchecked)
    assert d.summary.text().startswith("1 audio file ")
    assert d.options().audio_fields == frozenset({"Front"})
    d.fields.item(0).setCheckState(Qt.CheckState.Unchecked)
    assert d.summary.text().startswith("0 audio files")
    assert not ok_button(d).isEnabled()


def test_field_list_hidden_with_one_audio_field(addon, mw, col):
    src = addon.SelectedCards(col, [col.test_cards["cat"]])
    d = make_dialog(addon, mw, src)
    assert d.fields is None
    assert d.options().audio_fields is None


def test_pattern_row_follows_naming_choice(addon, mw, source):
    d = make_dialog(addon, mw, source)
    d.naming.setCurrentIndex(d.naming.findData(addon._PATTERN))
    assert d.pattern.isVisibleTo(d) and d.pattern_label.isVisibleTo(d)
    d.naming.setCurrentIndex(0)
    assert not d.pattern.isVisibleTo(d)


@pytest.mark.parametrize(
    "naming,pattern,expected_field,expected_pattern",
    [
        (None, "", None, None),
        ("{first field}", "", "{first field}", None),
        ("Back", "", "Back", None),
        ("{pattern}", " {Back} ({Front}) ", None, "{Back} ({Front})"),
    ],
)
def test_options(addon, mw, source, tmp_path, naming, pattern, expected_field, expected_pattern):
    d = make_dialog(addon, mw, source)
    d.naming.setCurrentIndex(d.naming.findData(naming))
    if pattern:
        d.pattern.setText(pattern)
    d.output.setText(f"  {tmp_path}  ")
    d.conflict.setCurrentIndex(d.conflict.findData(addon.SKIP))
    for w in (d.number, d.csv, d.playlist, d.subfolders):
        w.setChecked(True)
    o = d.options()
    assert (o.name_field, o.name_pattern) == (expected_field, expected_pattern)
    assert o.on_conflict == addon.SKIP
    assert o.number_prefix and o.write_csv and o.write_playlist and o.subfolder_per_deck
    assert d.output_dir() == str(tmp_path)


def test_output_dir_expands_home(addon, mw, source):
    d = make_dialog(addon, mw, source)
    d.output.setText("~/audio")
    assert d.output_dir() == os.path.join(os.path.expanduser("~"), "audio")


@pytest.mark.parametrize(
    "output,pattern,message",
    [
        ("  ", None, "Choose an output folder."),
        ("/out", "no braces", "Put at least one field name in braces"),
        ("/out", "{Front} {Nope}", "These aren't fields of the notes: Nope"),
    ],
)
def test_accept_rejects_bad_input(addon, mw, source, calls, output, pattern, message):
    d = make_dialog(addon, mw, source)
    d.output.setText(output)
    if pattern is not None:
        d.naming.setCurrentIndex(d.naming.findData(addon._PATTERN))
        d.pattern.setText(pattern)
    d.accept()
    assert d.result() == 0
    assert calls.warnings and calls.warnings[0].startswith(message)
    if pattern == "{Front} {Nope}":
        assert "Fields: Front, Back" in calls.warnings[0]


def test_accept(addon, mw, source, calls, tmp_path):
    d = make_dialog(addon, mw, source)
    d.output.setText(str(tmp_path))
    d.naming.setCurrentIndex(d.naming.findData(addon._PATTERN))
    d.pattern.setText("{Front}")
    d.accept()
    assert d.result() == 1 and calls.warnings == []


def test_browse(addon, mw, source, monkeypatch):
    d = make_dialog(addon, mw, source)
    monkeypatch.setattr(addon.QFileDialog, "getExistingDirectory", lambda *a: "/picked")
    d._browse()
    assert d.output.text() == "/picked"
    monkeypatch.setattr(addon.QFileDialog, "getExistingDirectory", lambda *a: "")
    d._browse()  # cancelled: unchanged
    assert d.output.text() == "/picked"


@pytest.mark.parametrize(
    "naming,saved",
    [(None, "original"), ("{first field}", "first field"), ("Back", "Back"), ("{pattern}", "pattern")],
)
def test_settings_saved_and_restored(addon, mw, source, tmp_path, naming, saved):
    d = make_dialog(addon, mw, source)
    d.output.setText(str(tmp_path))
    d.naming.setCurrentIndex(d.naming.findData(naming))
    d.pattern.setText("{Back} ({Front})")
    d.conflict.setCurrentIndex(d.conflict.findData(addon.OVERWRITE))
    for w in (d.number, d.csv, d.playlist, d.subfolders):
        w.setChecked(True)
    d.remember()
    conf = mw.addonManager.config
    assert conf["name_files_after"] == saved
    assert conf["name_pattern"] == "{Back} ({Front})"
    assert conf["last_output_dir"] == str(tmp_path)
    assert conf["if_file_exists"] == addon.OVERWRITE
    assert conf["number_prefix"] and conf["write_csv"] and conf["write_playlist"]
    assert conf["subfolder_per_deck"]

    again = make_dialog(addon, mw, source)
    assert again.naming.currentData() == naming
    assert again.pattern.text() == "{Back} ({Front})"
    assert again.pattern.isVisibleTo(again) == (naming == addon._PATTERN)
    assert again.output.text() == str(tmp_path)
    assert again.conflict.currentData() == addon.OVERWRITE
    assert all(w.isChecked() for w in (again.number, again.csv, again.playlist, again.subfolders))


def test_saved_field_that_no_longer_exists_falls_back(addon, mw, source):
    mw.addonManager.config = {"name_files_after": "Expression"}
    assert make_dialog(addon, mw, source).naming.currentData() is None


def test_excluded_fields_remembered_per_field(addon, mw, source):
    mw.addonManager.config = {"excluded_audio_fields": ["Sentence"]}  # from another deck
    d = make_dialog(addon, mw, source)
    d.output.setText("/out")
    d.fields.item(1).setCheckState(Qt.CheckState.Unchecked)
    d.remember()
    assert mw.addonManager.config["excluded_audio_fields"] == ["Back", "Sentence"]

    again = make_dialog(addon, mw, source)
    assert again.fields.item(1).checkState() == Qt.CheckState.Unchecked
    again.fields.item(1).setCheckState(Qt.CheckState.Checked)
    again.remember()
    assert mw.addonManager.config["excluded_audio_fields"] == ["Sentence"]


def test_package_without_readable_notes(addon, mw, tmp_path):
    class NoNotes(addon.AudioSource):
        def __init__(self):
            super().__init__()
            self._media = {"a.mp3": lambda: b"a"}

    mw.addonManager.config = {"name_files_after": "first field", "number_prefix": True,
                              "write_csv": True, "subfolder_per_deck": True}
    d = make_dialog(addon, mw, NoNotes(), "in card order")
    for w in (d.naming, d.number, d.csv, d.subfolders):
        assert not w.isEnabled()
    assert d.naming.currentData() is None
    assert not any(w.isChecked() for w in (d.number, d.csv, d.subfolders))
    assert d.playlist.isEnabled()  # still possible with original names
    assert d.naming.toolTip() == "The notes in this package couldn't be read."


# --- Preview --------------------------------------------------------------------


def test_preview(addon, mw, source, tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    (out / "猫.mp3").write_bytes(b"old")
    shown = []
    monkeypatch.setattr(addon.PreviewDialog, "exec", lambda self: shown.append(self))
    d = make_dialog(addon, mw, source)
    d.output.setText(str(out))
    d.naming.setCurrentIndex(d.naming.findData("Front"))
    d._preview()
    table = shown[0].findChild(QTableWidget)
    rows = [tuple(table.item(r, c).text() for c in range(3)) for r in range(table.rowCount())]
    assert rows == [
        ("a1.mp3", "猫 (2).mp3", "Numbered: name already taken"),
        ("a2.mp3", "犬.mp3", ""),
        ("a3.mp3", "犬 (2).mp3", ""),  # two clips on one note: numbered, nothing to flag
        ("a4.mp3", "fox.mp3", ""),
    ]
    assert [table.horizontalHeaderItem(c).text() for c in range(3)] == [
        "Original name", "Saved as", "Note",
    ]
    label = shown[0].findChild(addon.QLabel)
    assert label.text() == "4 files would be saved like this (1 with a note). Nothing has been written yet."
    assert os.listdir(out) == ["猫.mp3"]


def test_preview_status_text(addon, mw):
    files = [
        addon.ExtractedFile("a.mp3", "a.mp3", addon.SKIPPED),
        addon.ExtractedFile("b.mp3", "b.mp3", addon.OVERWRITTEN),
        addon.ExtractedFile("c.mp3", "c.mp3", addon.WRITTEN),
    ]
    table = addon.PreviewDialog(files, mw).findChild(QTableWidget)
    assert [table.item(r, 2).text() for r in range(3)] == [
        "Skipped: file already exists", "Replaces the existing file", "",
    ]


def test_preview_checks_input_first(addon, mw, source, calls, monkeypatch):
    monkeypatch.setattr(addon.PreviewDialog, "exec", lambda self: pytest.fail("shown"))
    d = make_dialog(addon, mw, source)
    d.output.setText("")
    d._preview()
    assert calls.warnings == ["Choose an output folder."]


# --- Summary and the finish message ----------------------------------------------


def test_summary_plain(addon):
    result = addon.ExtractionResult(files=[addon.ExtractedFile("a.mp3", "a.mp3", addon.WRITTEN)])
    assert addon._summary("/out", result) == "Extracted 1 audio file to:\n/out"


def test_summary_everything(addon):
    E = addon.ExtractedFile
    result = addon.ExtractionResult(
        files=[
            E("a", "a", addon.RENAMED), E("b", "b", addon.OVERWRITTEN), E("c", "c", addon.OVERWRITTEN),
            E("d", "d", addon.SKIPPED),
        ],
        csv_path="/out/audio_notes.csv",
        playlist_path="/out/audio_playlist.m3u8",
        missing=[f"m{i}.mp3" for i in range(6)],
    )
    assert addon._summary("/out", result).split("\n\n") == [
        "Extracted 3 audio files to:\n/out",
        "1 given a numbered name because a file with that name already existed.",
        "2 replaced existing files.",
        "1 skipped because a file with that name already existed.",
        "6 files used by the cards are missing from your media folder (Tools → Check Media): "
        "m0.mp3, m1.mp3, m2.mp3, m3.mp3, m4.mp3 …",
        "Also saved audio_playlist.m3u8 and audio_notes.csv.",
    ]


def test_summary_one_missing(addon):
    result = addon.ExtractionResult(missing=["gone.mp3"], csv_path="/out/audio_notes.csv")
    text = addon._summary("/out", result)
    assert "1 file used by the cards is missing from your media folder (Tools → Check Media): gone.mp3" in text
    assert text.endswith("Also saved audio_notes.csv.")


def _click_after_exec(monkeypatch, label):
    """Make QMessageBox.exec click the button with this text."""
    texts = []

    def exec_(box):
        texts.append(box.text())
        button = next(b for b in box.buttons() if b.text().replace("&", "") == label)
        button.click()

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    return texts


@pytest.mark.parametrize("label,opened", [("Open Folder", ["/out"]), ("Close", [])])
def test_done_message(addon, mw, calls, monkeypatch, label, opened):
    texts = _click_after_exec(monkeypatch, label)
    addon._show_done("/out", addon.ExtractionResult(), mw)
    assert texts == ["Extracted 0 audio files to:\n/out"]
    assert calls.opened == opened


# --- The flow from dialog to extraction --------------------------------------------


class TrackedSource:
    """Wraps a source to see whether it gets closed."""

    def __init__(self, src):
        self.src, self.closed = src, False

    def __getattr__(self, name):
        return getattr(self.src, name)

    def close(self):
        self.closed = True


def test_show_dialog_extracts(addon, mw, source, ops, calls, tmp_path, monkeypatch):
    tmp_path = tmp_path / "out"
    mw.addonManager.config = {"last_output_dir": str(tmp_path), "write_playlist": True}
    monkeypatch.setattr(addon.OptionsDialog, "exec", lambda self: True)
    texts = _click_after_exec(monkeypatch, "Open Folder")
    tracked = TrackedSource(source)
    addon._show_dialog(tracked, "on 4 notes", "in Browser order", mw, "none")
    assert sorted(os.listdir(tmp_path)) == ["a1.mp3", "a2.mp3", "a3.mp3", "a4.mp3", "audio_playlist.m3u8"]
    assert ops == [("Extracting audio...", False)]
    assert tracked.closed
    assert texts[0].startswith(f"Extracted 4 audio files to:\n{tmp_path}")
    assert "gone.mp3" in texts[0]
    assert calls.opened == [str(tmp_path)]
    assert mw.addonManager.config["write_playlist"] is True  # settings remembered


def test_show_dialog_cancelled(addon, mw, source, ops, monkeypatch):
    monkeypatch.setattr(addon.OptionsDialog, "exec", lambda self: False)
    tracked = TrackedSource(source)
    addon._show_dialog(tracked, "x", "y", mw, "none")
    assert tracked.closed and ops == []
    assert mw.addonManager.config == {}


def test_show_dialog_extraction_fails(addon, mw, source, ops, calls, monkeypatch):
    monkeypatch.setattr(addon.OptionsDialog, "exec", lambda self: True)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(source, "extract", boom)
    tracked = TrackedSource(source)
    addon._show_dialog(tracked, "x", "y", mw, "none")
    assert tracked.closed
    assert calls.warnings == ["An error occurred: disk full"]


def test_show_dialog_no_audio(addon, mw, col, calls):
    src = TrackedSource(addon.SelectedCards(col, [col.test_cards["pic"]]))
    addon._show_dialog(src, "x", "y", mw, "No audio here.")
    assert calls.info == ["No audio here."]
    assert src.closed


def test_show_dialog_no_audio_but_missing_files(addon, mw, col, tmp_path, calls):
    note = col.get_card(col.test_cards["pic"]).note()
    note["Back"] = "[sound:gone.mp3][sound:gone2.mp3]"
    col.update_note(note)
    addon._show_dialog(addon.SelectedCards(col, [col.test_cards["pic"]]), "x", "y", mw, "No audio.")
    assert calls.info == ["No audio.\n\n2 files used by the cards are missing from your media folder."]


@pytest.mark.parametrize(
    "err,message",
    [
        (lambda a: a.ExtractionError("Not a valid package."), "Not a valid package."),
        (lambda a: ValueError("odd"), "An error occurred: odd"),
    ],
)
def test_on_failure(addon, calls, err, message):
    addon._on_failure(err(addon))
    assert calls.warnings == [message]


# --- Tools menu: packages ---------------------------------------------------------


@pytest.fixture
def package(tmp_path):
    c = Collection(str(tmp_path / "p.anki2"))
    (tmp_path / "hi.mp3").write_bytes(b"hi")
    c.media.add_file(str(tmp_path / "hi.mp3"))
    note = c.new_note(c.models.by_name("Basic"))
    note["Front"], note["Back"] = "hi", "[sound:hi.mp3]"
    c.add_note(note, c.decks.id("Default"))
    path = str(tmp_path / "deck.apkg")
    c.export_anki_package(
        out_path=path, options=ExportAnkiPackageOptions(with_media=True), limit=None
    )
    c.close()
    return path


def test_tools_menu_entry(addon, mw):
    assert "Extract Audio from Package" in [a.text() for a in mw.form.menuTools.actions()]


def test_select_package(addon, mw, ops, package, monkeypatch):
    monkeypatch.setattr(addon.QFileDialog, "getOpenFileName", lambda *a: (package, ""))
    shown = []
    monkeypatch.setattr(addon, "_show_dialog", lambda *a: shown.append(a))
    addon.select_apkg_file()
    src, title, order_hint, parent, empty = shown[0]
    assert isinstance(src, addon.Package) and src.audio_names == ["hi.mp3"]
    assert (title, order_hint, parent, empty) == (
        "in deck.apkg", "in card order", mw, "No audio files found in the package.",
    )
    assert ops == [("Reading package...", False)]
    src.close()


def test_select_package_cancelled(addon, ops, monkeypatch):
    monkeypatch.setattr(addon.QFileDialog, "getOpenFileName", lambda *a: ("", ""))
    addon.select_apkg_file()
    assert ops == []


def test_select_bad_package(addon, ops, calls, tmp_path, monkeypatch):
    bad = tmp_path / "bad.apkg"
    bad.write_bytes(b"nope")
    monkeypatch.setattr(addon.QFileDialog, "getOpenFileName", lambda *a: (str(bad), ""))
    addon.select_apkg_file()
    assert calls.warnings == [f"{bad} is not a valid Anki package."]


# --- Deck list gear menu ----------------------------------------------------------


def test_deck_menu_entry(addon, monkeypatch):
    assert addon._add_deck_action in addon.gui_hooks.deck_browser_will_show_options_menu._hooks
    exported = []
    monkeypatch.setattr(addon, "export_deck", exported.append)
    menu = QMenu()
    addon._add_deck_action(menu, 42)
    (action,) = menu.actions()
    assert action.text() == "Export Audio…"
    action.trigger()
    assert exported == [42]


def test_export_deck(addon, mw, col, ops, monkeypatch):
    shown = []
    monkeypatch.setattr(addon, "_show_dialog", lambda *a: shown.append(a))
    addon.export_deck(col.decks.id("Lang"))
    src, title, order_hint, parent, empty = shown[0]
    assert src.audio_names == ["a1.mp3", "a2.mp3", "a3.mp3"]  # subdeck included, Other left out
    assert (title, order_hint, parent, empty) == (
        "in Lang (3 notes)", "in deck order", mw, "No cards in Lang play any audio.",
    )
    assert ops == [("Finding audio...", True)]


# --- Browser ------------------------------------------------------------------------


class FakeIndex:
    def __init__(self, row):
        self._row = row

    def row(self):
        return self._row


def fake_browser(card_ids_by_row, clicked_rows, broken=False):
    def selected():
        if broken:
            raise AttributeError("renamed in a newer Anki")
        return [FakeIndex(r) for r in clicked_rows]

    model = types.SimpleNamespace(get_card_ids=lambda idx: [card_ids_by_row[i.row()] for i in idx])
    browser = QMainWindow()  # a real QObject, so actions can be parented to it
    browser.table = types.SimpleNamespace(_selected=selected, _model=model)
    browser.selected_cards = lambda: [card_ids_by_row[r] for r in clicked_rows]
    browser.form = types.SimpleNamespace(menu_Notes=QMenu())
    return browser


def test_browser_selection_in_table_order(addon):
    browser = fake_browser([10, 20, 30], clicked_rows=[2, 0])
    assert addon._selected_cards_in_table_order(browser) == [10, 30]


def test_browser_selection_fallback(addon):
    browser = fake_browser([10, 20, 30], clicked_rows=[2, 0], broken=True)
    assert addon._selected_cards_in_table_order(browser) == [30, 10]


def test_browser_menu_entry(addon, monkeypatch):
    assert addon._add_browser_action in addon.gui_hooks.browser_menus_did_init._hooks
    browser = fake_browser([], [])
    exported = []
    monkeypatch.setattr(addon, "export_from_browser", exported.append)
    addon._add_browser_action(browser)
    action = browser.form.menu_Notes.actions()[-1]
    assert action.text() == "Export Audio from Selected Cards…"
    action.trigger()
    assert exported == [browser]


def test_export_from_browser(addon, mw, col, ops, monkeypatch):
    c = col.test_cards
    browser = fake_browser([c["fox"], c["cat"]], clicked_rows=[1, 0])
    shown = []
    monkeypatch.setattr(addon, "_show_dialog", lambda *a: shown.append(a))
    addon.export_from_browser(browser)
    src, title, order_hint, parent, empty = shown[0]
    assert src.audio_names == ["a4.mp3", "a1.mp3"]  # Browser order, not click order
    assert (title, order_hint, parent, empty) == (
        "on 2 selected notes", "in Browser order", browser, "The selected cards don't play any audio.",
    )
    assert ops == [("Finding audio...", True)]


def test_export_from_browser_nothing_selected(addon, calls, ops):
    addon.export_from_browser(fake_browser([], []))
    assert calls.tooltips == ["Select some cards first."] and ops == []


# --- QueryOp wrapper ---------------------------------------------------------------


def test_run_builds_query_op(addon, mw, monkeypatch):
    made = []

    class FakeQueryOp:
        def __init__(self, parent, op, success):
            made.append(self)
            self.progress, self.no_collection = None, False

        def with_progress(self, label):
            self.progress = label
            return self

        def without_collection(self):
            self.no_collection = True
            return self

    monkeypatch.setattr(addon, "QueryOp", FakeQueryOp)
    op = addon._run(mw, lambda col: None, lambda r: None, "Working...")
    assert op.progress == "Working..." and op.no_collection
    op = addon._run(mw, lambda col: None, lambda r: None, "Working...", needs_collection=True)
    assert not op.no_collection


def test_blank_pattern_keeps_saved_one(addon, mw, source):
    mw.addonManager.config = {"name_pattern": "{Back}"}
    d = make_dialog(addon, mw, source)
    d.pattern.setText("  ")
    d.output.setText("/out")
    d.remember()
    assert mw.addonManager.config["name_pattern"] == "{Back}"


def test_remember_without_field_list_keeps_exclusions(addon, mw, col):
    mw.addonManager.config = {"excluded_audio_fields": ["Sentence"]}
    d = make_dialog(addon, mw, addon.SelectedCards(col, [col.test_cards["cat"]]))
    d.output.setText("/out")
    d.remember()
    assert mw.addonManager.config["excluded_audio_fields"] == ["Sentence"]


def test_preview_label_when_nothing_to_flag(addon, mw):
    files = [addon.ExtractedFile("a.mp3", "a.mp3", addon.WRITTEN)]
    label = addon.PreviewDialog(files, mw).findChild(addon.QLabel)
    assert label.text() == "1 file would be saved like this. Nothing has been written yet."
