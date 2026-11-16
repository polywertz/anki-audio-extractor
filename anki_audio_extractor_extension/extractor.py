"""Extract audio files from Anki .apkg / .colpkg packages.

This module has no Qt/aqt imports so it can be tested outside of Anki.

Two package layouts exist:

* Legacy (Anki < 2.1.50, or "Support older Anki versions" ticked on export):
  ``media`` is a JSON object ``{"0": "name.mp3", ...}`` and each numbered zip
  member holds the raw file.
* Modern (default since 2.1.50, collection stored as ``collection.anki21b``):
  ``media`` is a zstd-compressed ``MediaEntries`` protobuf and every numbered
  zip member is zstd-compressed.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from typing import Callable, Optional

AUDIO_EXTENSIONS = (
    ".mp3", ".ogg", ".oga", ".opus", ".wav", ".m4a", ".aac",
    ".flac", ".spx", ".webm", ".amr", ".wma", ".aif", ".aiff",
)


class ExtractionError(Exception):
    pass


def is_audio(name: str) -> bool:
    return name.lower().endswith(AUDIO_EXTENSIONS)


def _safe_name(name: str) -> Optional[str]:
    """Strip any directory parts so a malicious package can't write outside output_dir."""
    base = os.path.basename(name.replace("\\", "/"))
    if base in ("", ".", ".."):
        return None
    return base


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


def _is_modern(zf: zipfile.ZipFile) -> bool:
    return "collection.anki21b" in zf.namelist()


def _write(dst_dir: str, name: str, data: bytes) -> str:
    path = os.path.join(dst_dir, name)
    with open(path, "wb") as f:
        f.write(data)
    return name


def _extract_legacy(zf: zipfile.ZipFile, output_dir: str) -> list[str]:
    try:
        media_map = json.loads(zf.read("media").decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError) as e:
        raise ExtractionError(f"Could not read the media list: {e}") from e

    names = set(zf.namelist())
    extracted = []
    for member, real_name in media_map.items():
        name = _safe_name(real_name)
        if name and is_audio(name) and member in names:
            extracted.append(_write(output_dir, name, zf.read(member)))
    return extracted


def _extract_modern_direct(
    zf: zipfile.ZipFile, output_dir: str, decompress: Callable[[bytes], bytes]
) -> list[str]:
    from anki.import_export_pb2 import MediaEntries

    entries = MediaEntries()
    entries.ParseFromString(decompress(zf.read("media")))

    names = set(zf.namelist())
    extracted = []
    for index, entry in enumerate(entries.entries):
        member = (
            str(entry.legacy_zip_filename)
            if entry.HasField("legacy_zip_filename")
            else str(index)
        )
        name = _safe_name(entry.name)
        if name and is_audio(name) and member in names:
            extracted.append(_write(output_dir, name, decompress(zf.read(member))))
    return extracted


def _extract_via_import(apkg_path: str, output_dir: str) -> list[str]:
    """Fallback: let Anki's own importer unpack the package into a throwaway collection.

    Only media referenced by the package's notes is kept by the importer, which is
    everything Anki itself would export.
    """
    from anki.collection import (
        Collection,
        ImportAnkiPackageOptions,
        ImportAnkiPackageRequest,
    )

    with tempfile.TemporaryDirectory() as tmp:
        col = Collection(os.path.join(tmp, "extract.anki2"))
        try:
            col.import_anki_package(
                ImportAnkiPackageRequest(
                    package_path=apkg_path, options=ImportAnkiPackageOptions()
                )
            )
            media_dir = col.media.dir()
            extracted = []
            for name in sorted(os.listdir(media_dir)):
                if is_audio(name):
                    shutil.copy2(os.path.join(media_dir, name), os.path.join(output_dir, name))
                    extracted.append(name)
            return extracted
        finally:
            col.close(downgrade=False)


def extract_audio_from_apkg(apkg_path: str, output_dir: str) -> list[str]:
    """Copy every audio file in ``apkg_path`` into ``output_dir``.

    Returns the list of extracted file names. Raises ExtractionError on bad input.
    """
    if not os.path.isfile(apkg_path):
        raise ExtractionError(f"File does not exist: {apkg_path}")
    os.makedirs(output_dir, exist_ok=True)

    try:
        with zipfile.ZipFile(apkg_path) as zf:
            if "media" not in zf.namelist():
                raise ExtractionError("No media list found in the package.")
            if not _is_modern(zf):
                return _extract_legacy(zf, output_dir)
            decompress = _zstd_decompressor()
            if decompress is not None:
                return _extract_modern_direct(zf, output_dir, decompress)
    except zipfile.BadZipFile as e:
        raise ExtractionError(f"{apkg_path} is not a valid Anki package.") from e

    return _extract_via_import(apkg_path, output_dir)
