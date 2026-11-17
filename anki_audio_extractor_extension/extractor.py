"""Extract audio files from Anki .apkg / .colpkg packages.

This module has no Qt/aqt imports so it can be tested outside of Anki.

Two package layouts exist:

* Legacy (Anki < 2.1.50, or "Support older Anki versions" ticked on export):
  ``media`` is a JSON object ``{"0": "name.mp3", ...}`` and each numbered zip
  member holds the raw file.
* Modern (default since 2.1.50, collection stored as ``collection.anki21b``):
  ``media`` is a zstd-compressed ``MediaEntries`` protobuf and every numbered
  zip member is zstd-compressed.

The package's collection is opened as well, so that audio files can be named
after the notes that use them and listed in a CSV.
"""

from __future__ import annotations

import csv
import html
import json
import os
import re
import shutil
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

AUDIO_EXTENSIONS = (
    ".mp3", ".ogg", ".oga", ".opus", ".wav", ".m4a", ".aac",
    ".flac", ".spx", ".webm", ".amr", ".wma", ".aif", ".aiff",
)

# Naming: ExtractOptions.name_field is None (keep the original name), FIRST_FIELD,
# or a field name. Braces can't appear in Anki field names, so this can't clash.
FIRST_FIELD = "{first field}"

# What to do when a file with the target name is already in the output folder.
RENAME = "rename"  # keep both, add " (2)" etc. to the new file
SKIP = "skip"
OVERWRITE = "overwrite"

# ExtractedFile.status values
WRITTEN = "written"
RENAMED = "renamed"  # written under a numbered name because the name was taken
SKIPPED = "skipped"
OVERWRITTEN = "overwritten"

CSV_NAME = "audio_notes.csv"
_MAX_STEM_BYTES = 150  # leaves room for " (123).ext" under the usual 255-byte limit


class ExtractionError(Exception):
    pass


@dataclass
class ExtractOptions:
    name_field: Optional[str] = None
    on_conflict: str = RENAME
    write_csv: bool = False


@dataclass
class ExtractedFile:
    original: str  # name inside the package
    name: str  # name in the output folder
    status: str


@dataclass
class ExtractionResult:
    files: list[ExtractedFile] = field(default_factory=list)
    csv_path: Optional[str] = None

    @property
    def names(self) -> list[str]:
        return [f.name for f in self.files if f.status != SKIPPED]

    def count(self, status: str) -> int:
        return sum(f.status == status for f in self.files)


@dataclass
class _Note:
    id: int
    note_type: str
    fields: dict[str, str]  # field name -> raw HTML, in note type order
    tags: str
    deck: str


def is_audio(name: str) -> bool:
    return name.lower().endswith(AUDIO_EXTENSIONS)


def _safe_name(name: str) -> Optional[str]:
    """Strip any directory parts so a malicious package can't write outside output_dir."""
    base = os.path.basename(name.replace("\\", "/"))
    if base in ("", ".", ".."):
        return None
    return base


def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)


def _zstd_decompressor() -> Optional[Callable[[bytes], bytes]]:
    try:  # Python 3.14+
        from compression import zstd  # type: ignore

        return zstd.decompress
    except ImportError:
        pass
    try:
        import zstandard  # type: ignore

        def decompress(data: bytes) -> bytes:
            # decompressobj copes with frames that don't record their content size
            return zstandard.ZstdDecompressor().decompressobj().decompress(data)

        return decompress
    except ImportError:
        return None


# --- Turning field HTML into text and file names -----------------------------

_SOUND_RE = re.compile(r"\[sound:(.+?)\]")
_TTS_TAG_RE = re.compile(r"\[/?anki:tts[^\]]*\]")
_CLOZE_RE = re.compile(r"\{\{c\d+::(.*?)(?:::[^}]*)?\}\}", re.S)
_HIDDEN_RE = re.compile(r"<(style|script)\b.*?</\1\s*>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]*>")
_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}


def sound_refs(field_html: str) -> list[str]:
    return [_nfc(html.unescape(m.strip())) for m in _SOUND_RE.findall(field_html)]


def plain_text(field_html: str) -> str:
    s = _SOUND_RE.sub(" ", field_html)
    s = _TTS_TAG_RE.sub("", s)
    s = _CLOZE_RE.sub(r"\1", s)
    s = _HIDDEN_RE.sub(" ", s)
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return " ".join(s.split())


def filename_stem(text: str) -> Optional[str]:
    """Make ``text`` safe to use as a file name on Windows, macOS and Linux."""
    s = " ".join(_ILLEGAL_RE.sub(" ", text).split())
    while len(s.encode("utf-8")) > _MAX_STEM_BYTES:
        s = s[:-1]
    s = s.rstrip(". ")
    if not s:
        return None
    if s.split(".")[0].upper() in _RESERVED:
        s += "_"
    return s


# --- Reading packages ---------------------------------------------------------


class Package:
    """An opened Anki package. Use as a context manager, or call close().

    Opening is the slow part (it may import the package into a scratch
    collection), so a Package can be inspected first and extracted from later.
    """

    def __init__(self, apkg_path: str) -> None:
        if not os.path.isfile(apkg_path):
            raise ExtractionError(f"File does not exist: {apkg_path}")
        self.path = apkg_path
        self._tmp = tempfile.TemporaryDirectory()
        self._col: Any = None
        self._zf: Optional[zipfile.ZipFile] = None
        # (name in package, loader of its bytes), audio only
        self._media: list[tuple[str, Callable[[], bytes]]] = []
        try:
            self._open()
            self._notes, self._refs = self._read_notes()
        except BaseException:
            self.close()
            raise

    # Public API

    @property
    def audio_names(self) -> list[str]:
        return [name for name, _ in self._media]

    @property
    def has_notes(self) -> bool:
        """False if the package's collection couldn't be read (naming/CSV unavailable)."""
        return self._col is not None

    @property
    def field_names(self) -> list[str]:
        """Field names of the notes in the package, in note type order."""
        seen: dict[str, None] = {}
        for note in self._notes:
            for name in note.fields:
                seen.setdefault(name, None)
        return list(seen)

    def extract(self, output_dir: str, options: Optional[ExtractOptions] = None) -> ExtractionResult:
        options = options or ExtractOptions()
        if options.on_conflict not in (RENAME, SKIP, OVERWRITE):
            raise ValueError(f"Unknown conflict policy: {options.on_conflict}")
        os.makedirs(output_dir, exist_ok=True)

        result = ExtractionResult()
        used: set[str] = set()
        for original, load in self._media:
            desired = self._desired_name(original, options.name_field)
            name, status = _resolve(output_dir, desired, used, options.on_conflict)
            if status != SKIPPED:
                with open(os.path.join(output_dir, name), "wb") as f:
                    f.write(load())
            result.files.append(ExtractedFile(original, name, status))

        if options.write_csv and self.has_notes:
            csv_policy = OVERWRITE if options.on_conflict == OVERWRITE else RENAME
            csv_name, _ = _resolve(output_dir, CSV_NAME, used, csv_policy)
            result.csv_path = os.path.join(output_dir, csv_name)
            self._write_csv(result.csv_path, result.files)
        return result

    def close(self) -> None:
        if self._col is not None:
            try:
                self._col.close(downgrade=False)
            except Exception:
                pass
            self._col = None
        if self._zf is not None:
            self._zf.close()
            self._zf = None
        self._tmp.cleanup()

    def __enter__(self) -> "Package":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # Opening

    def _open(self) -> None:
        try:
            self._zf = zipfile.ZipFile(self.path)
        except zipfile.BadZipFile as e:
            raise ExtractionError(f"{self.path} is not a valid Anki package.") from e
        names = self._zf.namelist()
        if "media" not in names:
            raise ExtractionError("No media list found in the package.")

        if "collection.anki21b" not in names:
            self._open_legacy()
        else:
            decompress = _zstd_decompressor()
            if decompress is not None:
                self._open_modern(decompress)
            else:
                self._open_via_import()
        self._media.sort(key=lambda m: m[0])

    def _open_legacy(self) -> None:
        zf = self._zf
        assert zf is not None
        try:
            media_map = json.loads(zf.read("media").decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as e:
            raise ExtractionError(f"Could not read the media list: {e}") from e

        members = set(zf.namelist())
        for member, real_name in media_map.items():
            name = _safe_name(real_name)
            if name and is_audio(name) and member in members:
                self._media.append((name, lambda m=member: zf.read(m)))

        # collection.anki2 is only a "please upgrade" stub when .anki21 exists.
        col_member = "collection.anki21" if "collection.anki21" in members else "collection.anki2"
        if col_member in members:
            self._col = self._try_open_collection(zf.read(col_member))

    def _open_modern(self, decompress: Callable[[bytes], bytes]) -> None:
        from anki.import_export_pb2 import MediaEntries

        zf = self._zf
        assert zf is not None
        entries = MediaEntries()
        entries.ParseFromString(decompress(zf.read("media")))

        members = set(zf.namelist())
        for index, entry in enumerate(entries.entries):
            member = (
                str(entry.legacy_zip_filename)
                if entry.HasField("legacy_zip_filename")
                else str(index)
            )
            name = _safe_name(entry.name)
            if name and is_audio(name) and member in members:
                self._media.append((name, lambda m=member: decompress(zf.read(m))))

        self._col = self._try_open_collection(decompress(zf.read("collection.anki21b")))

    def _try_open_collection(self, data: bytes) -> Any:
        """Open a copy of the package's collection, or return None if that fails.

        Audio can still be extracted under its original names without it.
        """
        try:
            from anki.collection import Collection

            path = os.path.join(self._tmp.name, "package.anki2")
            with open(path, "wb") as f:
                f.write(data)
            return Collection(path)
        except Exception:
            return None

    def _open_via_import(self) -> None:
        """Fallback: let Anki's own importer unpack the package into a scratch collection.

        Used when no zstd module is available. Only media referenced by the
        package's notes is kept by the importer, which is everything Anki itself
        would export.
        """
        from anki.collection import (
            Collection,
            ImportAnkiPackageOptions,
            ImportAnkiPackageRequest,
        )

        assert self._zf is not None
        self._zf.close()
        self._zf = None

        self._col = Collection(os.path.join(self._tmp.name, "import.anki2"))
        # Get the stock note types out of the way so the package's "Basic" etc.
        # aren't imported under renamed names like "Basic+". Anki keeps at least
        # one note type, so rename whichever survives; it has no notes.
        models = self._col.models
        for nt in models.all_names_and_ids():
            models.remove(nt.id)
        for nt in models.all_names_and_ids():
            model = models.get(nt.id)
            model["name"] = f"audio-extractor-scratch-{nt.id}"
            models.update_dict(model)
        self._col.import_anki_package(
            ImportAnkiPackageRequest(package_path=self.path, options=ImportAnkiPackageOptions())
        )
        media_dir = self._col.media.dir()
        for name in os.listdir(media_dir):
            if is_audio(name):
                path = os.path.join(media_dir, name)

                def load(p: str = path) -> bytes:
                    with open(p, "rb") as f:
                        return f.read()

                self._media.append((name, load))

    # Notes

    def _read_notes(self) -> tuple[list[_Note], dict[str, list[_Note]]]:
        """Read every note, and map each sound file name to the notes using it."""
        col = self._col
        if col is None:
            return [], {}
        try:
            decks: dict[int, str] = {}
            for nid, did in col.db.all("select nid, did from cards order by id"):
                if nid not in decks:
                    decks[nid] = col.decks.name(did)

            field_names: dict[int, tuple[str, list[str]]] = {}
            notes, refs = [], {}
            for nid, mid, flds, tags in col.db.all(
                "select id, mid, flds, tags from notes order by id"
            ):
                if mid not in field_names:
                    model = col.models.get(mid)
                    field_names[mid] = (
                        (model["name"], [f["name"] for f in model["flds"]]) if model else ("", [])
                    )
                type_name, names = field_names[mid]
                values = flds.split("\x1f")
                names = names + [f"Field {i + 1}" for i in range(len(names), len(values))]
                note = _Note(nid, type_name, dict(zip(names, values)), tags.strip(), decks.get(nid, ""))
                notes.append(note)
                for value in values:
                    for ref in sound_refs(value):
                        users = refs.setdefault(ref, [])
                        if not users or users[-1] is not note:
                            users.append(note)
            return notes, refs
        except Exception:
            # Unreadable collection: carry on with original names and no CSV.
            self._col.close(downgrade=False)
            self._col = None
            return [], {}

    def _desired_name(self, original: str, name_field: Optional[str]) -> str:
        if not name_field:
            return original
        users = self._refs.get(_nfc(original))
        if not users:
            return original
        note = users[0]  # the oldest note that plays this file
        if name_field == FIRST_FIELD:
            value = next(iter(note.fields.values()), "")
        else:
            value = note.fields.get(name_field, "")
        stem = filename_stem(plain_text(value))
        if not stem:
            return original
        return stem + os.path.splitext(original)[1]

    def _write_csv(self, path: str, files: list[ExtractedFile]) -> None:
        field_cols = self.field_names
        with open(path, "w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel reads UTF-8
            w = csv.writer(f)
            w.writerow(["file", "original_file", "note_id", "deck", "note_type", "tags", *field_cols])
            for item in files:
                users = self._refs.get(_nfc(item.original)) or [None]
                for note in users:
                    if note is None:  # audio that no note plays
                        w.writerow([item.name, item.original, "", "", "", "", *[""] * len(field_cols)])
                        continue
                    w.writerow([
                        item.name, item.original, note.id, note.deck, note.note_type, note.tags,
                        *[plain_text(note.fields.get(c, "")) for c in field_cols],
                    ])


def _resolve(output_dir: str, desired: str, used: set[str], on_conflict: str) -> tuple[str, str]:
    """Pick the output name for ``desired``.

    Names already produced by this extraction (e.g. two clips on one note named
    after the same field) always get a number, so nothing from the package is
    lost. ``on_conflict`` decides what happens with files that were already in
    the output folder.
    """
    stem, ext = os.path.splitext(desired)

    def numbered(start: int, taken: Callable[[str], bool]) -> str:
        i = start
        while True:
            candidate = f"{stem} ({i}){ext}"
            if not taken(candidate):
                return candidate
            i += 1

    name = desired
    if name.lower() in used:
        name = numbered(2, lambda c: c.lower() in used)

    status = WRITTEN
    if os.path.exists(os.path.join(output_dir, name)):
        if on_conflict == SKIP:
            status = SKIPPED
        elif on_conflict == OVERWRITE:
            status = OVERWRITTEN
        else:
            name = numbered(
                2, lambda c: c.lower() in used or os.path.exists(os.path.join(output_dir, c))
            )
            status = RENAMED
    used.add(name.lower())
    return name, status


def extract_audio_from_apkg(
    apkg_path: str, output_dir: str, options: Optional[ExtractOptions] = None
) -> ExtractionResult:
    """Copy every audio file in ``apkg_path`` into ``output_dir``.

    Raises ExtractionError on bad input.
    """
    with Package(apkg_path) as package:
        return package.extract(output_dir, options)
