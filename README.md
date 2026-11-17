# Anki Audio Extractor

An Anki add-on that copies the audio files out of an Anki package (`.apkg` or `.colpkg`) into a folder you choose.

Works with Anki 2.1.55 and later, including packages in the newer compressed format that Anki has exported by default since 2.1.50. Tested on Anki 26.09.4.

## Usage

In Anki, open **Tools → Extract Audio from Package** and pick a package. A dialog then lets you choose:

- **Output folder.**
- **Name files after**: keep the original file names, or name each file after its note's first field or any field you pick (for example `猫 (neko).mp3` instead of `sapi5js-42ecd8a4.mp3`). HTML, sound tags and characters that aren't allowed in file names are removed. A file keeps its original name if no note plays it or the chosen field is empty. When a file is used by several notes, the oldest note is used.
- **If a file already exists**: keep both (the new file gets a number, such as `hello (2).mp3`), skip it, or overwrite it. If two files from the same package end up with the same name, the later one always gets a number, so nothing is lost.
- **Also save audio_notes.csv**: a spreadsheet with one row per file and note, listing the deck, note type, tags and every field as plain text. Files that no note plays are listed with empty note columns.

Your choices are remembered for next time. Extraction runs in the background.

## Install from source

Copy `anki_audio_extractor_extension/` into your Anki add-ons folder (**Tools → Add-ons → View Files**) and restart Anki.

## Tests

The tests build real packages with the `anki` Python package:

```bash
pip install anki pytest zstandard
pytest tests
```

## License

MIT
