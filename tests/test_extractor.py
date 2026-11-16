"""Tests for the Anki add-on's extractor.

Needs the `anki` Python package (pip install anki), which is used to build real
packages in every format the current Anki can produce.

    pip install anki pytest zstandard
    pytest tests
"""

import importlib.util
import json
import os
import zipfile
from pathlib import Path

import pytest

from anki.collection import Collection, ExportAnkiPackageOptions

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "extractor", ROOT / "anki_audio_extractor_extension" / "extractor.py"
)
extractor = importlib.util.module_from_spec(_spec)
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
    names = extractor.extract_audio_from_apkg(packages["modern"], str(tmp_path))
    _check_output(tmp_path, names)


def test_modern_package_with_zstandard_module(packages, tmp_path, monkeypatch):
    zstandard = pytest.importorskip("zstandard")
    monkeypatch.setattr(
        extractor,
        "_zstd_decompressor",
        lambda: lambda d: zstandard.ZstdDecompressor().decompressobj().decompress(d),
    )
    names = extractor.extract_audio_from_apkg(packages["modern"], str(tmp_path))
    _check_output(tmp_path, names)


def test_modern_package_import_fallback(packages, tmp_path, monkeypatch):
    # Anki's bundled Python may have no zstd module; then Anki's importer is used.
    monkeypatch.setattr(extractor, "_zstd_decompressor", lambda: None)
    names = extractor.extract_audio_from_apkg(packages["modern"], str(tmp_path))
    _check_output(tmp_path, names)


def test_colpkg(packages, tmp_path):
    names = extractor.extract_audio_from_apkg(packages["colpkg"], str(tmp_path))
    _check_output(tmp_path, names)


def test_colpkg_import_fallback(packages, tmp_path, monkeypatch):
    monkeypatch.setattr(extractor, "_zstd_decompressor", lambda: None)
    names = extractor.extract_audio_from_apkg(packages["colpkg"], str(tmp_path))
    _check_output(tmp_path, names)


def test_legacy_package(packages, tmp_path):
    names = extractor.extract_audio_from_apkg(packages["legacy"], str(tmp_path))
    _check_output(tmp_path, names)


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
    names = extractor.extract_audio_from_apkg(str(pkg), str(out))
    assert "orphan.mp3" in names
    assert (out / "orphan.mp3").read_bytes() == b"orphan"


def test_no_media(packages, tmp_path):
    assert extractor.extract_audio_from_apkg(packages["nomedia"], str(tmp_path)) == []
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
    names = extractor.extract_audio_from_apkg(str(pkg), str(out))
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
