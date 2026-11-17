"""Tests for the Anki add-on's extractor.

Needs the `anki` Python package (pip install anki), which is used to build real
packages in every format the current Anki can produce.

    pip install anki pytest zstandard
    pytest tests
"""

import importlib.util
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

from anki.collection import Collection, ExportAnkiPackageOptions

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "extractor", ROOT / "anki_audio_extractor_extension" / "extractor.py"
)
extractor = importlib.util.module_from_spec(_spec)
sys.modules["extractor"] = extractor  # dataclasses look the module up here
_spec.loader.exec_module(extractor)

MEDIA = {
    "hello.mp3": b"ID3 fake mp3 " * 100,
    "bye.ogg": b"OggS fake " * 50,
    "clip.wav": b"RIFF wav " * 30,
    "track.opus": b"opus " * 10,
    "sub dir name ü.flac": b"fLaC " * 10,
    "pic.png": b"\x89PNG fake",
}
AUDIO = sorted(n for n in MEDIA if not n.endswith(".png"))


@pytest.fixture(scope="session")
def packages(tmp_path_factory):
    src = tmp_path_factory.mktemp("src")
    out = tmp_path_factory.mktemp("pkgs")
    col = Collection(str(src / "c.anki2"))
    for name, data in MEDIA.items():
        p = src / name
        p.write_bytes(data)
        col.media.add_file(str(p))
    note = col.new_note(col.models.by_name("Basic"))
    note["Front"] = " ".join(f"[sound:{n}]" for n in AUDIO)
    note["Back"] = '<img src="pic.png">'
    col.add_note(note, col.decks.id("Default"))

    def export(name, legacy, media=True):
        path = str(out / name)
        col.export_anki_package(
            out_path=path,
            options=ExportAnkiPackageOptions(
                with_scheduling=False, with_deck_configs=False, with_media=media, legacy=legacy
            ),
            limit=None,
        )
        return path

    pkgs = {
        "modern": export("modern.apkg", legacy=False),
        "legacy": export("legacy.apkg", legacy=True),
        "nomedia": export("nomedia.apkg", legacy=False, media=False),
    }
    pkgs["colpkg"] = str(out / "full.colpkg")
    col.export_collection_package(pkgs["colpkg"], include_media=True, legacy=False)
    col.close()
    return pkgs


def _check_output(out_dir, names):
    assert sorted(names) == AUDIO
    assert sorted(os.listdir(out_dir)) == AUDIO
    for n in AUDIO:
        assert (out_dir / n).read_bytes() == MEDIA[n], n


def test_modern_package_direct(packages, tmp_path):
    result = extractor.extract_audio_from_apkg(packages["modern"], str(tmp_path))
    _check_output(tmp_path, result.names)


def test_modern_package_with_zstandard_module(packages, tmp_path, monkeypatch):
    zstandard = pytest.importorskip("zstandard")
    monkeypatch.setattr(
        extractor,
        "_zstd_decompressor",
        lambda: lambda d: zstandard.ZstdDecompressor().decompressobj().decompress(d),
    )
    result = extractor.extract_audio_from_apkg(packages["modern"], str(tmp_path))
    _check_output(tmp_path, result.names)


def test_modern_package_import_fallback(packages, tmp_path, monkeypatch):
    # Anki's bundled Python may have no zstd module; then Anki's importer is used.
    monkeypatch.setattr(extractor, "_zstd_decompressor", lambda: None)
    result = extractor.extract_audio_from_apkg(packages["modern"], str(tmp_path))
    _check_output(tmp_path, result.names)


def test_colpkg(packages, tmp_path):
    result = extractor.extract_audio_from_apkg(packages["colpkg"], str(tmp_path))
    _check_output(tmp_path, result.names)


def test_colpkg_import_fallback(packages, tmp_path, monkeypatch):
    monkeypatch.setattr(extractor, "_zstd_decompressor", lambda: None)
    result = extractor.extract_audio_from_apkg(packages["colpkg"], str(tmp_path))
    _check_output(tmp_path, result.names)


def test_legacy_package(packages, tmp_path):
    result = extractor.extract_audio_from_apkg(packages["legacy"], str(tmp_path))
    _check_output(tmp_path, result.names)


def test_legacy_keeps_unreferenced_audio(packages, tmp_path):
    pkg = tmp_path / "orphan.apkg"
    with zipfile.ZipFile(packages["legacy"]) as src, zipfile.ZipFile(pkg, "w") as dst:
        for info in src.infolist():
            if info.filename == "media":
                m = json.loads(src.read("media"))
                m["99"] = "orphan.mp3"
                dst.writestr("media", json.dumps(m))
            else:
                dst.writestr(info, src.read(info))
        dst.writestr("99", b"orphan")
    out = tmp_path / "out"
    names = extractor.extract_audio_from_apkg(str(pkg), str(out)).names
    assert "orphan.mp3" in names
    assert (out / "orphan.mp3").read_bytes() == b"orphan"


def test_no_media(packages, tmp_path):
    assert extractor.extract_audio_from_apkg(packages["nomedia"], str(tmp_path)).files == []
    assert os.listdir(tmp_path) == []


def test_creates_output_dir(packages, tmp_path):
    out = tmp_path / "a" / "b"
    extractor.extract_audio_from_apkg(packages["modern"], str(out))
    assert sorted(os.listdir(out)) == AUDIO


def test_path_traversal_is_blocked(tmp_path):
    pkg = tmp_path / "evil.apkg"
    with zipfile.ZipFile(pkg, "w") as z:
        z.writestr("collection.anki2", b"")
        z.writestr("media", json.dumps({"0": "../../evil.mp3", "1": "..\\win.mp3"}))
        z.writestr("0", b"x")
        z.writestr("1", b"y")
    out = tmp_path / "out"
    names = extractor.extract_audio_from_apkg(str(pkg), str(out)).names
    assert sorted(names) == ["evil.mp3", "win.mp3"]
    assert sorted(os.listdir(out)) == ["evil.mp3", "win.mp3"]
    assert not (tmp_path / "evil.mp3").exists()


def test_missing_file(tmp_path):
    with pytest.raises(extractor.ExtractionError, match="does not exist"):
        extractor.extract_audio_from_apkg(str(tmp_path / "nope.apkg"), str(tmp_path))


def test_not_a_zip(tmp_path):
    bad = tmp_path / "bad.apkg"
    bad.write_bytes(b"not a zip")
    with pytest.raises(extractor.ExtractionError, match="not a valid"):
        extractor.extract_audio_from_apkg(str(bad), str(tmp_path / "out"))


def test_zip_without_media_list(tmp_path):
    pkg = tmp_path / "x.apkg"
    with zipfile.ZipFile(pkg, "w") as z:
        z.writestr("collection.anki2", b"")
    with pytest.raises(extractor.ExtractionError, match="No media list"):
        extractor.extract_audio_from_apkg(str(pkg), str(tmp_path / "out"))


@pytest.mark.parametrize(
    "name,expected",
    [("a.MP3", True), ("b.m4a", True), ("c.opus", True), ("d.png", False), ("mp3", False)],
)
def test_is_audio(name, expected):
    assert extractor.is_audio(name) is expected


# --- Naming, CSV and conflicts ------------------------------------------------

RICH_MEDIA = {f"a{i}.mp3": f"audio {i}".encode() for i in range(1, 6)}


@pytest.fixture(scope="session")
def rich_packages(tmp_path_factory):
    src = tmp_path_factory.mktemp("rich_src")
    out = tmp_path_factory.mktemp("rich_pkgs")
    col = Collection(str(src / "c.anki2"))
    for name, data in RICH_MEDIA.items():
        p = src / name
        p.write_bytes(data)
        col.media.add_file(str(p))

    deck = col.decks.id("Lang::Japanese")
    basic = col.models.by_name("Basic")

    def add(model, tags=(), **fields):
        note = col.new_note(model)
        for k, v in fields.items():
            note[k] = v
        note.tags = list(tags)
        col.add_note(note, deck)

    add(basic, tags=["animal"], Front="猫 <b>(neko)</b>", Back="cat[sound:a1.mp3]")
    add(basic, Front="What? / Why?", Back="[sound:a2.mp3]<br>[sound:a3.mp3]")
    add(basic, Front="[sound:a4.mp3]", Back="")
    add(basic, Front="dog", Back="[sound:a1.mp3]")

    mm = col.models
    vocab = mm.new("Vocab")
    for f in ("Word", "Audio"):
        mm.add_field(vocab, mm.new_field(f))
    tmpl = mm.new_template("Card 1")
    tmpl["qfmt"], tmpl["afmt"] = "{{Word}}", "{{Audio}}"
    mm.add_template(vocab, tmpl)
    mm.add(vocab)
    add(mm.by_name("Vocab"), Word="hello &amp; bye", Audio="[sound:a5.mp3]")

    def export(name, legacy):
        path = str(out / name)
        col.export_anki_package(
            out_path=path,
            options=ExportAnkiPackageOptions(with_media=True, legacy=legacy),
            limit=None,
        )
        return path

    pkgs = {"modern": export("modern.apkg", False), "legacy": export("legacy.apkg", True)}
    col.close()
    return pkgs


@pytest.fixture(params=["modern", "legacy", "import"])
def rich(request, rich_packages, monkeypatch):
    if request.param == "import":
        monkeypatch.setattr(extractor, "_zstd_decompressor", lambda: None)
        return rich_packages["modern"]
    return rich_packages[request.param]


def _contents(out_dir):
    return {p.name: p.read_bytes() for p in out_dir.iterdir()}


def test_field_names(rich):
    with extractor.Package(rich) as pkg:
        assert pkg.has_notes
        assert pkg.field_names == ["Front", "Back", "Word", "Audio"]
        assert pkg.audio_names == sorted(RICH_MEDIA)


def test_name_after_field(rich, tmp_path):
    opts = extractor.ExtractOptions(name_field="Front")
    result = extractor.extract_audio_from_apkg(rich, str(tmp_path), opts)
    assert {f.original: f.name for f in result.files} == {
        "a1.mp3": "猫 (neko).mp3",  # first note wins when two notes share a file
        "a2.mp3": "What Why.mp3",
        "a3.mp3": "What Why (2).mp3",  # same note, same field: numbered, not lost
        "a4.mp3": "a4.mp3",  # field holds only the sound tag
        "a5.mp3": "a5.mp3",  # note type has no Front field
    }
    assert _contents(tmp_path) == {
        "猫 (neko).mp3": b"audio 1",
        "What Why.mp3": b"audio 2",
        "What Why (2).mp3": b"audio 3",
        "a4.mp3": b"audio 4",
        "a5.mp3": b"audio 5",
    }


def test_name_after_first_field(rich, tmp_path):
    opts = extractor.ExtractOptions(name_field=extractor.FIRST_FIELD)
    result = extractor.extract_audio_from_apkg(rich, str(tmp_path), opts)
    assert {f.original: f.name for f in result.files}["a5.mp3"] == "hello & bye.mp3"
    assert {f.original: f.name for f in result.files}["a1.mp3"] == "猫 (neko).mp3"


def test_csv(rich, tmp_path):
    import csv

    opts = extractor.ExtractOptions(name_field="Front", write_csv=True)
    result = extractor.extract_audio_from_apkg(rich, str(tmp_path), opts)
    assert result.csv_path == str(tmp_path / "audio_notes.csv")
    with open(result.csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == [
        "number", "file", "original_file", "note_id", "deck", "note_type", "tags",
        "Front", "Back", "Word", "Audio",
    ]
    by_file = {}
    for r in rows:
        by_file.setdefault(r["file"], []).append(r)
    assert [r["Front"] for r in by_file["猫 (neko).mp3"]] == ["猫 (neko)", "dog"]
    first = by_file["猫 (neko).mp3"][0]
    assert (first["deck"], first["note_type"], first["tags"], first["Back"]) == (
        "Lang::Japanese", "Basic", "animal", "cat",
    )
    assert by_file["a5.mp3"][0]["Word"] == "hello & bye"
    assert by_file["a5.mp3"][0]["Front"] == ""
    assert len(rows) == 6


@pytest.mark.parametrize(
    "policy,expected_status,expected_files",
    [
        ("rename", "renamed", {"a1.mp3": b"old", "a1 (2).mp3": b"audio 1"}),
        ("skip", "skipped", {"a1.mp3": b"old"}),
        ("overwrite", "overwritten", {"a1.mp3": b"audio 1"}),
    ],
)
def test_conflicts(rich_packages, tmp_path, policy, expected_status, expected_files):
    (tmp_path / "a1.mp3").write_bytes(b"old")
    opts = extractor.ExtractOptions(on_conflict=policy)
    result = extractor.extract_audio_from_apkg(rich_packages["modern"], str(tmp_path), opts)
    a1 = next(f for f in result.files if f.original == "a1.mp3")
    assert a1.status == expected_status
    assert result.count(expected_status) == 1
    files = _contents(tmp_path)
    for name, data in expected_files.items():
        assert files[name] == data
    assert len(files) == len(expected_files) + 4


def test_rerun_with_skip_writes_nothing_new(rich_packages, tmp_path):
    opts = extractor.ExtractOptions(name_field="Front", on_conflict="skip")
    extractor.extract_audio_from_apkg(rich_packages["modern"], str(tmp_path), opts)
    before = _contents(tmp_path)
    result = extractor.extract_audio_from_apkg(rich_packages["modern"], str(tmp_path), opts)
    assert result.count("skipped") == 5
    assert _contents(tmp_path) == before


def test_csv_does_not_replace_existing_csv(rich_packages, tmp_path):
    (tmp_path / "audio_notes.csv").write_text("mine")
    opts = extractor.ExtractOptions(write_csv=True)
    result = extractor.extract_audio_from_apkg(rich_packages["modern"], str(tmp_path), opts)
    assert result.csv_path == str(tmp_path / "audio_notes (2).csv")
    assert (tmp_path / "audio_notes.csv").read_text() == "mine"


@pytest.mark.parametrize(
    "html_in,text",
    [
        ("a<br>b", "a b"),
        ("[sound:x.mp3] word", "word"),
        ("{{c1::Paris::city}} is nice", "Paris is nice"),
        ("[anki:tts lang=ja_JP]猫[/anki:tts]", "猫"),
        ("&nbsp;x&lt;y", "x<y"),
        ("<style>.a{}</style>hi", "hi"),
    ],
)
def test_plain_text(html_in, text):
    assert extractor.plain_text(html_in) == text


@pytest.mark.parametrize(
    "text,stem",
    [
        ('a/b:c*?"<>|', "a b c"),
        ("ends with dots...", "ends with dots"),
        ("CON", "CON_"),
        ("nul.txt", "nul.txt_"),
        ("", None),
        ("???", None),
    ],
)
def test_filename_stem(text, stem):
    assert extractor.filename_stem(text) == stem


def test_filename_stem_byte_limit():
    stem = extractor.filename_stem("猫" * 200)
    assert len(stem.encode()) <= 150 and set(stem) == {"猫"}


# --- Card order, number prefixes, field filter, selected cards ----------------


def test_number_prefix(rich, tmp_path):
    opts = extractor.ExtractOptions(number_prefix=True)
    result = extractor.extract_audio_from_apkg(rich, str(tmp_path), opts)
    # Card order; files on the same note share a number; note 4 only replays a1.
    assert [f.name for f in result.files] == [
        "0001_a1.mp3", "0002_a2.mp3", "0002_a3.mp3", "0003_a4.mp3", "0004_a5.mp3",
    ]
    assert _contents(tmp_path)["0002_a3.mp3"] == b"audio 3"


def test_number_prefix_with_field_names(rich, tmp_path):
    opts = extractor.ExtractOptions(name_field="Front", number_prefix=True)
    names = extractor.extract_audio_from_apkg(rich, str(tmp_path), opts).names
    assert names[:3] == ["0001_猫 (neko).mp3", "0002_What Why.mp3", "0002_What Why (2).mp3"]


def test_package_follows_new_card_position(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    col = Collection(str(src / "c.anki2"))
    cids = []
    for word in ("one", "two", "three"):
        (src / f"{word}.mp3").write_bytes(word.encode())
        col.media.add_file(str(src / f"{word}.mp3"))
        note = col.new_note(col.models.by_name("Basic"))
        note["Front"], note["Back"] = word, f"[sound:{word}.mp3]"
        col.add_note(note, col.decks.id("Default"))
        cids.append(note.card_ids()[0])
    col.sched.reposition_new_cards(list(reversed(cids)), 1, 1, False, False)
    pkg = str(tmp_path / "p.apkg")
    col.export_anki_package(
        out_path=pkg, options=ExportAnkiPackageOptions(with_scheduling=True, with_media=True), limit=None
    )
    col.close()
    opts = extractor.ExtractOptions(number_prefix=True, name_field="Front")
    names = extractor.extract_audio_from_apkg(pkg, str(tmp_path / "out"), opts).names
    assert names == ["0001_three.mp3", "0002_two.mp3", "0003_one.mp3"]


def test_orphans_numbered_last(packages, tmp_path):
    pkg = tmp_path / "orphan.apkg"
    with zipfile.ZipFile(packages["legacy"]) as src, zipfile.ZipFile(pkg, "w") as dst:
        for info in src.infolist():
            if info.filename == "media":
                m = json.loads(src.read("media"))
                m["99"] = "orphan.mp3"
                dst.writestr("media", json.dumps(m))
            else:
                dst.writestr(info, src.read(info))
        dst.writestr("99", b"orphan")
    opts = extractor.ExtractOptions(number_prefix=True)
    names = extractor.extract_audio_from_apkg(str(pkg), str(tmp_path / "out"), opts).names
    assert names[-1] == "0002_orphan.mp3"
    assert all(n.startswith("0001_") for n in names[:-1])


def test_audio_field_filter(rich, tmp_path):
    with extractor.Package(rich) as pkg:
        assert pkg.audio_field_names == ["Front", "Back", "Audio"]
        assert pkg.audio_count({"Back"}) == 3
        assert pkg.audio_count() == 5
        opts = extractor.ExtractOptions(audio_fields=frozenset({"Back", "Audio"}), number_prefix=True)
        result = pkg.extract(str(tmp_path), opts)
    assert sorted(_contents(tmp_path)) == [
        "0001_a1.mp3", "0002_a2.mp3", "0002_a3.mp3", "0003_a5.mp3",
    ]


@pytest.fixture
def live_col(tmp_path):
    """A collection like the user's own: notes, a media folder, one missing file."""
    (tmp_path / "live").mkdir()
    col = Collection(str(tmp_path / "live" / "c.anki2"))
    for name, data in RICH_MEDIA.items():
        (tmp_path / name).write_bytes(data)
        col.media.add_file(str(tmp_path / name))
    basic = col.models.by_name("Basic")
    cards = {}
    for key, front, back in [
        ("cat", "cat", "[sound:a1.mp3][sound:a2.mp3]"),
        ("dog", "dog [sound:a3.mp3]", "[sound:gone.mp3]"),
        ("pic", "picture", '<img src="x.png">'),
        ("fox", "fox", "[sound:a4.mp3]"),
    ]:
        note = col.new_note(basic)
        note["Front"], note["Back"] = front, back
        col.add_note(note, col.decks.id("Mine::Sub"))
        cards[key] = note.card_ids()[0]
    yield col, cards
    col.close()


def test_selected_cards_in_selection_order(live_col, tmp_path):
    col, cards = live_col
    src = extractor.SelectedCards(col, [cards["dog"], cards["pic"], cards["cat"]])
    assert src.note_count == 3
    assert src.missing == ["gone.mp3"]
    out = tmp_path / "out"
    opts = extractor.ExtractOptions(name_field="Front", number_prefix=True, write_csv=True)
    result = src.extract(str(out), opts)
    assert result.names == ["0001_dog.mp3", "0002_cat.mp3", "0002_cat (2).mp3"]
    assert result.missing == ["gone.mp3"]
    assert (out / "0001_dog.mp3").read_bytes() == b"audio 3"
    assert (out / "0002_cat (2).mp3").read_bytes() == b"audio 2"
    import csv

    with open(result.csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [(r["number"], r["deck"], r["Front"]) for r in rows] == [
        ("1", "Mine::Sub", "dog"), ("2", "Mine::Sub", "cat"), ("2", "Mine::Sub", "cat"),
    ]


def test_selected_cards_field_filter(live_col, tmp_path):
    col, cards = live_col
    src = extractor.SelectedCards(col, list(cards.values()))
    assert src.audio_field_names == ["Front", "Back"]
    opts = extractor.ExtractOptions(audio_fields=frozenset({"Back"}))
    assert src.extract(str(tmp_path / "out"), opts).names == ["a1.mp3", "a2.mp3", "a4.mp3"]


def test_selected_cards_no_audio(live_col, tmp_path):
    col, cards = live_col
    src = extractor.SelectedCards(col, [cards["pic"]])
    assert src.audio_names == [] and src.missing == []
