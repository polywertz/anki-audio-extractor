# Anki Audio Extractor

An Anki add-on that copies the audio files out of an Anki package (`.apkg` or `.colpkg`) into a folder you choose.

Works with Anki 2.1.55 and later, including packages in the newer compressed format that Anki has exported by default since 2.1.50. Tested on Anki 26.09.4.

## Usage

In Anki, open **Tools → Extract Audio from Package**, pick a package, then pick an output folder. Extraction runs in the background.

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
