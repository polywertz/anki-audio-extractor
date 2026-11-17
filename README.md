# Anki Audio Extractor

An Anki add-on that copies the audio files out of an Anki package (`.apkg` or `.colpkg`) into a folder you choose.

Works with Anki 2.1.55 and later, including packages in the newer compressed format that Anki has exported by default since 2.1.50. Tested on Anki 26.09.4.

## Usage

There are two ways to start:

- **From a package file:** **Tools → Extract Audio from Package**, then pick an `.apkg` or `.colpkg`. The package is read without importing it into your collection.
- **From your own cards:** in the Browser, select cards, then **Notes → Export Audio from Selected Cards…** (also in the right-click menu). Audio comes straight from your media folder.

A dialog then lets you choose:

- **Output folder.**
- **Take audio from**: shown when the notes play audio from more than one field. Untick fields to leave their audio out, for example to keep word audio and skip example-sentence audio. The file count updates as you go.
- **Name files after**: keep the original file names, or name each file after its note's first field or any field you pick (for example `猫 (neko).mp3` instead of `sapi5js-42ecd8a4.mp3`). HTML, sound tags and characters that aren't allowed in file names are removed. A file keeps its original name if no note plays it or the chosen field is empty. When a file is used by several notes, the first one in card order is used.
- **Number files in card order**: adds `0001_`, `0002_`, … so the files sort the way you study them. For packages, that's deck order, then new-card position. For the Browser, it's the order shown in the Browser. Files from the same note share a number.
- **If a file already exists**: keep both (the new file gets a number, such as `hello (2).mp3`), skip it, or overwrite it. If two files from the same extraction end up with the same name, the later one always gets a number, so nothing is lost.
- **Also save audio_notes.csv**: a spreadsheet with one row per file and note, listing the number, deck, note type, tags and every field as plain text. Audio in a package that no note plays is listed with empty note columns.

Your choices are remembered for next time. Reading and extracting run in the background. When exporting from the Browser, the closing message lists any files the cards refer to that are missing from your media folder.

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
