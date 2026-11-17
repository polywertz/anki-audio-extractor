"""Extract audio files from Anki packages or from cards in a collection.

This module has no Qt/aqt imports so it can be tested outside of Anki.

Two package layouts exist:

* Legacy (Anki < 2.1.50, or "Support older Anki versions" ticked on export):
  ``media`` is a JSON object ``{"0": "name.mp3", ...}`` and each numbered zip
  member holds the raw file.
* Modern (default since 2.1.50, collection stored as ``collection.anki21b``):
  ``media`` is a zstd-compressed ``MediaEntries`` protobuf and every numbered
  zip member is zstd-compressed.

The package's collection is opened as well, so that audio files can be put in
card order, named after the notes that use them and listed in a CSV.

Extracted files keep their modification time where one is known: from the
media folder for decks and Browser selections, and from the zip entry for
packages. Anki before 2.1.50 stored each media file's own time (local time);
the current exporter, which always writes a ``meta`` entry, stores the time
of the export (UTC).
"""

from __future__ import annotations

import calendar
import csv
import html
import json
import os
import re
import tempfile
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

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
PLAYLIST_NAME = "audio_playlist.m3u8"
_MAX_STEM_BYTES = 150  # leaves room for a number prefix and " (123).ext" under 255 bytes


class ExtractionError(Exception):
    pass


@dataclass
class ExtractOptions:
    name_field: Optional[str] = None
    on_conflict: str = RENAME
    write_csv: bool = False
    # Prefix file names with their note's position in card order: "0001_name.mp3".
    # Files from the same note share a number.
    number_prefix: bool = False
    # Only extract audio played from these fields. None means every field, plus
    # audio in a package that no note plays.
    audio_fields: Optional[frozenset[str]] = None
    # Name files with a pattern such as "{Front} - {Back}" instead of one
    # field. Takes precedence over name_field when set.
    name_pattern: Optional[str] = None
    # Put each file in a folder named after its note's deck ("Lang/Japanese/").
    subfolder_per_deck: bool = False
    write_playlist: bool = False


@dataclass
class ExtractedFile:
    original: str  # name in the package / media folder
    name: str  # path in the output folder, "/"-separated when in a deck subfolder
    status: str
    number: Optional[int] = None  # position of its note in card order


@dataclass
class ExtractionResult:
    files: list[ExtractedFile] = field(default_factory=list)
    csv_path: Optional[str] = None
    playlist_path: Optional[str] = None
    missing: list[str] = field(default_factory=list)  # referenced but not in the media folder

    @property
    def names(self) -> list[str]:
        return [f.name for f in self.files if f.status != SKIPPED]

    def count(self, status: str) -> int:
        return sum(f.status == status for f in self.files)


@dataclass(eq=False)  # compared and hashed by identity
class _Note:
    id: int
    note_type: str
    fields: dict[str, str]  # field name -> raw HTML, in note type order
    tags: str
    deck: str
    refs: list[tuple[str, str]]  # (field name, sound file name), in field order


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


def _file_loader(path: str) -> Callable[[], bytes]:
    def load() -> bytes:
        with open(path, "rb") as f:
            return f.read()

    return load


def _zip_time(info: zipfile.ZipInfo, utc: bool) -> Optional[float]:
    """A zip entry's time as a timestamp, or None if it isn't a real time.

    Zip times have no time zone, so the caller says how to read them.
    1980-01-01 is the earliest time a zip can hold, used when none was set.
    """
    if info.date_time[:3] == (1980, 1, 1):
        return None
    try:
        if utc:
            return float(calendar.timegm(info.date_time))
        return time.mktime((*info.date_time, 0, 0, -1))
    except (OverflowError, ValueError):
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


_PATTERN_RE = re.compile(r"\{([^{}]*)\}")


def pattern_fields(pattern: str) -> list[str]:
    """Field names used in a naming pattern such as ``"{Front} - {Back}"``."""
    return [m.strip() for m in _PATTERN_RE.findall(pattern)]


_OPENING = "([（【「『"
_CLOSING = ")]）】」』"


def render_pattern(pattern: str, fields: dict[str, str]) -> str:
    """Fill ``pattern`` with plain-text field values.

    Brackets right next to a field belong to it, and an empty field is left
    out together with its brackets and the separator before it. With
    "{Word} ({Reading}) - {Meaning}", a note without a reading gives
    "猫 - cat", and one without a meaning gives "猫 (neko)". Returns "" if
    every field is empty.
    """
    parts = _PATTERN_RE.split(pattern)  # literal, field, literal, field, ..., literal
    literals, names = parts[0::2], parts[1::2]
    values = [plain_text(fields.get(n.strip(), "")) for n in names]
    if not any(values):
        return ""

    # Split each literal into: closing brackets of the field before it, the
    # separator, and opening brackets of the field after it.
    split = []
    for lit in literals:
        tail = len(lit) - len(lit.lstrip(_CLOSING))
        head = len(lit.rstrip(_OPENING))  # never before tail: rstrip stops at a closing bracket
        split.append((lit[:tail], lit[tail:head], lit[head:]))
    split[0] = ("", split[0][0] + split[0][1], split[0][2])  # text before the first field
    split[-1] = (split[-1][0], split[-1][1] + split[-1][2], "")  # text after the last one

    # Build (text, from_pattern) pieces so cleanup only touches the pattern's own text.
    pieces = [(split[0][1], True)]
    first = True
    for i, value in enumerate(values):
        if not value:
            continue
        if not first:
            # Separator: the text between this field and the kept field before it.
            pieces.append((split[i][1], True))
        pieces += [(split[i][2], True), (value, False), (split[i + 1][0], True)]
        first = False
    pieces.append((split[-1][1], True))
    if not all(values):
        pieces = _drop_unmatched_brackets(pieces)
    return " ".join("".join(text for text, _ in pieces).split())


def _drop_unmatched_brackets(pieces: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """Remove pattern brackets left without a partner, e.g. by a dropped field
    in "{A} ({B}, {C})". Brackets inside field values are left alone."""
    chars = [(ch, n) for n, (text, own) in enumerate(pieces) if own for ch in text]
    positions = [(n, j) for n, (text, own) in enumerate(pieces) if own for j in range(len(text))]
    drop = set()
    stack: list[int] = []
    for k, (ch, _) in enumerate(chars):
        if ch in _OPENING:
            stack.append(k)
        elif ch in _CLOSING:
            if stack and _OPENING.index(chars[stack[-1]][0]) == _CLOSING.index(ch):
                stack.pop()
            else:
                drop.add(k)
    drop.update(stack)
    dropped = {positions[k] for k in drop}
    return [
        ("".join(ch for j, ch in enumerate(text) if (n, j) not in dropped), own)
        for n, (text, own) in enumerate(pieces)
    ]


def _deck_folder(deck: str) -> str:
    """Relative folder for a deck: "Lang::Japanese" -> "Lang/Japanese"."""
    parts = [filename_stem(p) or "_" for p in deck.split("::") if p.strip()]
    return "/".join(parts)


# --- Reading notes ------------------------------------------------------------


def _read_notes(col: Any, card_rows: Iterable[tuple[int, int]], only_listed: bool) -> list[_Note]:
    """Read notes in the order their cards appear in ``card_rows`` ((note id, deck id) pairs).

    Each note's deck is the deck of its first listed card. With ``only_listed``
    False, every note in the collection is read and notes without listed cards
    come last.
    """
    first_deck: dict[int, int] = {}
    for nid, did in card_rows:
        first_deck.setdefault(nid, did)
    if only_listed:
        from anki.utils import ids2str

        sql = f"select id, mid, flds, tags from notes where id in {ids2str(first_deck)}"
    else:
        sql = "select id, mid, flds, tags from notes"
    rows = col.db.all(sql)
    position = {nid: i for i, nid in enumerate(first_deck)}
    rows.sort(key=lambda r: (position.get(r[0], len(position)), r[0]))

    note_types: dict[int, tuple[str, list[str]]] = {}
    deck_names: dict[int, str] = {}
    notes = []
    for nid, mid, flds, tags in rows:
        if mid not in note_types:
            model = col.models.get(mid)
            note_types[mid] = (model["name"], [f["name"] for f in model["flds"]]) if model else ("", [])
        type_name, names = note_types[mid]
        values = flds.split("\x1f")
        names = names + [f"Field {i + 1}" for i in range(len(names), len(values))]
        fields = dict(zip(names, values))
        did = first_deck.get(nid)
        if did is not None and did not in deck_names:
            deck_names[did] = col.decks.name(did)
        refs = [(fname, ref) for fname, value in fields.items() for ref in sound_refs(value)]
        notes.append(_Note(nid, type_name, fields, tags.strip(), deck_names.get(did, ""), refs))
    return notes


_STUDY_ORDER_SQL = (
    "select id, nid, did, (case when odid then odid else did end), type,"
    " (case when odid then odue else due end), ord from cards"
)


def _study_order(col: Any, where: str = "") -> list[tuple[int, int, int]]:
    """Cards in study order: by deck, then new-card position, then creation.

    Cards in a filtered deck count as being in their home deck. Returns
    (card id, note id, deck id) tuples.
    """
    rows = col.db.all(_STUDY_ORDER_SQL + (f" where {where}" if where else ""))
    names: dict[int, str] = {}
    for r in rows:
        if r[3] not in names:
            names[r[3]] = col.decks.name(r[3])
    rows.sort(key=lambda r: (names[r[3]], r[4] != 0, r[5] if r[4] == 0 else 0, r[1], r[6]))
    return [(r[0], r[1], r[3]) for r in rows]


def _package_card_order(col: Any) -> list[tuple[int, int]]:
    return [(nid, did) for _, nid, did in _study_order(col)]


def deck_card_ids(col: Any, deck_id: int) -> list[int]:
    """Card ids in a deck and its subdecks (including cards moved to a filtered
    deck), in study order."""
    from anki.utils import ids2str

    dids = ids2str(col.decks.deck_and_child_ids(deck_id))
    return [cid for cid, _, _ in _study_order(col, f"did in {dids} or odid in {dids}")]


# --- Audio sources ------------------------------------------------------------


@dataclass
class _Planned:
    original: str
    load: Callable[[], bytes]
    users: list[_Note]  # notes that play it (from the chosen fields), in card order
    number: int


class AudioSource:
    """Audio files plus the notes that play them. Subclasses fill the fields."""

    def __init__(self) -> None:
        self._media: dict[str, Callable[[], bytes]] = {}  # file name -> loader, audio only
        self._mtimes: dict[str, float] = {}  # file name -> modification time, where known
        self._notes: list[_Note] = []  # in card order
        self._has_notes = False
        self.missing: list[str] = []

    # Public API

    @property
    def has_notes(self) -> bool:
        """False if the notes couldn't be read (naming, numbering and CSV unavailable)."""
        return self._has_notes

    @property
    def audio_names(self) -> list[str]:
        return [p.original for p in self._plan(None)]

    def audio_count(self, audio_fields: Optional[Iterable[str]] = None) -> int:
        return len(self._plan(frozenset(audio_fields) if audio_fields is not None else None))

    @property
    def field_names(self) -> list[str]:
        """Field names of the notes, in note type order."""
        seen: dict[str, None] = {}
        for note in self._notes:
            for name in note.fields:
                seen.setdefault(name, None)
        return list(seen)

    @property
    def audio_field_names(self) -> list[str]:
        """Fields that play at least one of the audio files."""
        media = self._media_by_nfc()
        seen: dict[str, None] = {}
        for note in self._notes:
            for fname, ref in note.refs:
                if ref in media:
                    seen.setdefault(fname, None)
        return [f for f in self.field_names if f in seen]

    def preview(self, output_dir: str, options: Optional[ExtractOptions] = None) -> list[ExtractedFile]:
        """What extract() would do, without writing anything."""
        _, files, _ = self._assign(output_dir, options or ExtractOptions())
        return files

    def extract(self, output_dir: str, options: Optional[ExtractOptions] = None) -> ExtractionResult:
        options = options or ExtractOptions()
        plan, files, used = self._assign(output_dir, options)
        os.makedirs(output_dir, exist_ok=True)
        result = ExtractionResult(files=files, missing=list(self.missing))
        for item, out in zip(plan, files):
            if out.status != SKIPPED:
                path = os.path.join(output_dir, *out.name.split("/"))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(item.load())
                mtime = self._mtimes.get(item.original)
                if mtime is not None:
                    try:
                        os.utime(path, (mtime, mtime))
                    except OSError:
                        pass  # some file systems refuse; the file itself is fine

        # Lists always go in the top folder; they never skip, so they stay current.
        list_policy = OVERWRITE if options.on_conflict == OVERWRITE else RENAME
        root_used = used.setdefault("", set())
        if options.write_csv and self.has_notes:
            csv_name, _ = _resolve(output_dir, CSV_NAME, root_used, list_policy)
            result.csv_path = os.path.join(output_dir, csv_name)
            self._write_csv(result.csv_path, plan, files)
        if options.write_playlist:
            pl_name, _ = _resolve(output_dir, PLAYLIST_NAME, root_used, list_policy)
            result.playlist_path = os.path.join(output_dir, pl_name)
            self._write_playlist(result.playlist_path, plan, files)
        return result

    def close(self) -> None:
        pass

    def __enter__(self) -> "AudioSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # Internals

    def _media_by_nfc(self) -> dict[str, str]:
        return {_nfc(name): name for name in self._media}

    def _plan(self, audio_fields: Optional[frozenset[str]]) -> list[_Planned]:
        """The files to extract, in card order, each with the notes that play it.

        Files are numbered by the first note that plays them, counting only
        notes that bring in a new file, so the numbers have no gaps. Audio no
        note plays comes last, and only when no field filter is set.
        """
        media = self._media_by_nfc()
        planned: dict[str, _Planned] = {}
        number = 0
        for note in self._notes:
            note_number = None
            for fname, ref in note.refs:
                if audio_fields is not None and fname not in audio_fields:
                    continue
                name = media.get(ref)
                if name is None:
                    continue
                item = planned.get(name)
                if item is None:
                    if note_number is None:
                        number += 1
                        note_number = number
                    planned[name] = _Planned(name, self._media[name], [note], note_number)
                elif item.users[-1] is not note:
                    item.users.append(note)
        if audio_fields is None:
            for name in sorted(n for n in self._media if n not in planned):
                number += 1
                planned[name] = _Planned(name, self._media[name], [], number)
        return list(planned.values())

    def _assign(
        self, output_dir: str, options: ExtractOptions
    ) -> tuple[list[_Planned], list[ExtractedFile], dict[str, set[str]]]:
        """Plan the extraction and pick every output name and status."""
        if options.on_conflict not in (RENAME, SKIP, OVERWRITE):
            raise ValueError(f"Unknown conflict policy: {options.on_conflict}")
        plan = self._plan(options.audio_fields)
        width = max(4, len(str(max((p.number for p in plan), default=0))))
        used: dict[str, set[str]] = {}  # folder -> lower-cased names taken in it
        files = []
        for item in plan:
            desired = self._desired_name(item, options)
            if options.number_prefix:
                desired = f"{item.number:0{width}d}_{desired}"
            folder = ""
            if options.subfolder_per_deck and item.users:
                folder = _deck_folder(item.users[0].deck)
            target = os.path.join(output_dir, *folder.split("/")) if folder else output_dir
            name, status = _resolve(target, desired, used.setdefault(folder, set()), options.on_conflict)
            rel = f"{folder}/{name}" if folder else name
            files.append(ExtractedFile(item.original, rel, status, item.number))
        return plan, files, used

    def _desired_name(self, item: _Planned, options: ExtractOptions) -> str:
        if not item.users or not (options.name_pattern or options.name_field):
            return item.original
        note = item.users[0]  # the first note in card order that plays this file
        if options.name_pattern:
            text = render_pattern(options.name_pattern, note.fields)
        elif options.name_field == FIRST_FIELD:
            text = plain_text(next(iter(note.fields.values()), ""))
        else:
            text = plain_text(note.fields.get(options.name_field, ""))
        stem = filename_stem(text)
        if not stem:
            return item.original
        return stem + os.path.splitext(item.original)[1]

    def _write_playlist(self, path: str, plan: list[_Planned], files: list[ExtractedFile]) -> None:
        """An extended M3U playlist in card order, titled after each note's first field."""
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("#EXTM3U\n")
            for item, out in zip(plan, files):
                title = ""
                if item.users:
                    title = plain_text(next(iter(item.users[0].fields.values()), ""))
                title = title or os.path.splitext(os.path.basename(out.name))[0]
                f.write(f"#EXTINF:-1,{title}\n{out.name}\n")

    def _write_csv(self, path: str, plan: list[_Planned], files: list[ExtractedFile]) -> None:
        field_cols = self.field_names
        with open(path, "w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel reads UTF-8
            w = csv.writer(f)
            w.writerow(
                ["number", "file", "original_file", "note_id", "deck", "note_type", "tags", *field_cols]
            )
            for item, out in zip(plan, files):
                start = [item.number, out.name, out.original]
                if not item.users:  # audio that no note plays
                    w.writerow([*start, "", "", "", "", *[""] * len(field_cols)])
                for note in item.users:
                    w.writerow([
                        *start, note.id, note.deck, note.note_type, note.tags,
                        *[plain_text(note.fields.get(c, "")) for c in field_cols],
                    ])


class SelectedCards(AudioSource):
    """Audio played by the given cards of an open collection, e.g. a Browser selection.

    ``card_ids`` sets the order. Reading needs the collection; extracting only
    copies files from the media folder, so it can run without it.
    """

    def __init__(self, col: Any, card_ids: Sequence[int]) -> None:
        super().__init__()
        from anki.utils import ids2str

        # A card in a filtered deck counts as being in its home deck.
        by_card = {
            cid: (nid, did)
            for cid, nid, did in col.db.all(
                "select id, nid, (case when odid then odid else did end) from cards"
                f" where id in {ids2str(card_ids)}"
            )
        }
        rows = [by_card[c] for c in card_ids if c in by_card]
        self._notes = _read_notes(col, rows, only_listed=True)
        self._has_notes = True
        self.note_count = len(self._notes)

        media_dir = col.media.dir()
        missing: dict[str, None] = {}
        for note in self._notes:
            for _, ref in note.refs:
                name = _safe_name(ref)
                if not name or not is_audio(name) or name in self._media:
                    continue
                path = os.path.join(media_dir, name)
                if os.path.isfile(path):
                    self._media[name] = _file_loader(path)
                    self._mtimes[name] = os.path.getmtime(path)
                else:
                    missing.setdefault(name, None)
        self.missing = list(missing)


class Package(AudioSource):
    """An opened .apkg / .colpkg. Use as a context manager, or call close().

    Opening is the slow part (it may import the package into a scratch
    collection), so a Package can be inspected first and extracted from later.
    """

    def __init__(self, apkg_path: str) -> None:
        super().__init__()
        if not os.path.isfile(apkg_path):
            raise ExtractionError(f"File does not exist: {apkg_path}")
        self.path = apkg_path
        self._tmp = tempfile.TemporaryDirectory()
        self._col: Any = None
        self._zf: Optional[zipfile.ZipFile] = None
        try:
            self._open()
            self._load_notes()
        except BaseException:
            self.close()
            raise

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

    # Opening

    def _open(self) -> None:
        try:
            self._zf = zipfile.ZipFile(self.path)
        except zipfile.BadZipFile as e:
            raise ExtractionError(f"{self.path} is not a valid Anki package.") from e
        names = self._zf.namelist()
        if "media" not in names:
            raise ExtractionError("No media list found in the package.")
        self._utc_times = "meta" in names

        if "collection.anki21b" not in names:
            self._open_legacy()
        else:
            decompress = _zstd_decompressor()
            if decompress is not None:
                self._open_modern(decompress)
            else:
                self._open_via_import()

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
                self._media[name] = lambda m=member: zf.read(m)
                self._set_zip_time(name, member)

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
                self._media[name] = lambda m=member: decompress(zf.read(m))
                self._set_zip_time(name, member)

        self._col = self._try_open_collection(decompress(zf.read("collection.anki21b")))

    def _set_zip_time(self, name: str, member: str) -> None:
        assert self._zf is not None
        mtime = _zip_time(self._zf.getinfo(member), self._utc_times)
        if mtime is not None:
            self._mtimes[name] = mtime

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
        # The importer stamps files with the time of import. Every entry of a
        # modern package carries the export time, so use that instead.
        package_time = _zip_time(self._zf.getinfo("collection.anki21b"), self._utc_times)
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
                self._media[name] = _file_loader(os.path.join(media_dir, name))
                if package_time is not None:
                    self._mtimes[name] = package_time

    def _load_notes(self) -> None:
        if self._col is None:
            return
        try:
            self._notes = _read_notes(self._col, _package_card_order(self._col), only_listed=False)
            self._has_notes = True
        except Exception:
            # Unreadable collection: carry on with original names and no CSV.
            self._notes = []
            self._col.close(downgrade=False)
            self._col = None


def _resolve(output_dir: str, desired: str, used: set[str], on_conflict: str) -> tuple[str, str]:
    """Pick the output name for ``desired``.

    Names already produced by this extraction (e.g. two clips on one note named
    after the same field) always get a number, so nothing is lost.
    ``on_conflict`` decides what happens with files that were already in the
    output folder.
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
