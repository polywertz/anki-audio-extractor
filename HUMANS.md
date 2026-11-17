**Version 1.5.0** · Works with Anki 2.1.55 and newer · Tested on Anki 26.09.4

Having trouble? Open an issue on GitHub and include a download link to the `.apkg` file that's causing it: https://github.com/polywertz/anki-audio-extractor/issues

---

# Anki Audio Extractor

Copy the audio out of your Anki decks into a normal folder. Your files get readable names, sort in study order, and can come with a playlist that plays them in the same order.

**On this page:** Installation · Quick start · Three ways to start · Features · The export options · Compatibility · Support

---

## Installation

1. In Anki, go to **Tools → Add-ons → Get Add-ons…**
2. Paste the code from the top of this page.
3. Restart Anki.

---

## Quick start

1. Go to **Tools → Extract Audio from Package** and pick an `.apkg` file.
2. Choose an output folder, then click **Extract**.
3. Click **Open Folder** to see your audio.

To get readable names, set **Name files after** to **First field** before you click **Extract**.

---

## Three ways to start

- **A package file you downloaded:** **Tools → Extract Audio from Package**
- **A deck you already have:** on the deck list, click the **gear** next to the deck → **Export Audio…**
- **Only some cards:** in the **Browser**, select the cards → **Notes → Export Audio from Selected Cards…** (also in the right-click menu)

All three open the same export dialog.

---

## Features

**Where audio can come from**
- **A package file** (`.apkg` or `.colpkg`). Anki reads it without importing it, so your collection stays as it is.
- **A deck in your collection.** Its subdecks are included, and so are its cards that are in a filtered deck right now.
- **Cards you select in the Browser.**
- **Packages in Anki's newer compressed format**, the default since Anki 2.1.50.

**Choosing which audio to export**
- Untick fields to leave their audio out. For example, keep the word audio and skip the example-sentence audio.
- The file count updates as you tick and untick fields.
- **Audio types:** mp3, ogg, oga, opus, wav, m4a, aac, flac, spx, webm, amr, wma, aif and aiff.

**Naming files**
- Keep the original file names, or name each file after a field: `猫 (neko).mp3` instead of `sapi5js-42ecd8a4.mp3`.
- Combine fields with a pattern such as `{Word} - {Meaning}`. If a field is empty, it's left out along with the separator next to it.
- Add numbers (`0001_`, `0002_`, …) so the files sort in the order you study the cards.
- Put each deck's audio in its own folder: `Japanese::Vocab` becomes `Japanese/Vocab/`.
- **Preview Names…** lists every file's new name before anything is written.

**Extra files you can save**
- `audio_notes.csv`: a spreadsheet with each file's deck, note type, tags and fields.
- `audio_playlist.m3u8`: a playlist in card order. It opens in VLC, phone music players and most other audio apps.

**Safety and convenience**
- Extracted files keep their original date. From a deck or the Browser, that's the date of the file in your media folder. From a package, it's the date stored in the package: older Anki versions store each file's own date, while current Anki stores the date the package was exported.
- When a file with the same name already exists, choose to keep both, skip the new one, or overwrite the old one. Files from the same export never overwrite each other.
- The add-on remembers your choices for next time.
- The export runs in the background, so Anki stays responsive.
- **Open Folder** takes you straight to the files when the export finishes.
- When you export from a deck or from the Browser, the add-on tells you about any audio the cards use that's missing from your media folder.

---

## The export options

- **Output folder:** where the audio is saved.
- **Take audio from:** appears when notes have audio in more than one field. Untick a field to leave its audio out.
- **Name files after:** the original name, the first field, any field you pick, or **Pattern of several fields…**
- **Pattern:** field names in braces, for example `{Word} ({Reading})`.
- **Number files in card order:** adds `0001_`, `0002_`, … to the start of each name. Files from the same note share a number.
- **Put each deck's audio in its own folder:** makes a folder for each deck and subfolders for subdecks.
- **If a file already exists:** keep both (`hello (2).mp3`), skip, or overwrite.
- **Also save audio_notes.csv:** saves a spreadsheet with each file's note details.
- **Also save audio_playlist.m3u8:** saves a playlist in card order.
- **Preview Names…:** shows every new name, and whether that file would be numbered, skipped or replace an existing file.

**Tips**
- **Order you study in:** turn on numbering, or save a playlist if you'd rather keep the names clean.
- **Word audio only:** untick the sentence field under **Take audio from**.
- **A file kept its original name:** either no note plays that file, or the field you chose is empty for that note.

---

## Compatibility

- Anki 2.1.55 and newer, including Anki 23, 24, 25 and 26.
- Tested on Windows and macOS.

---

## Support

- Report a problem or ask for a feature: https://github.com/polywertz/anki-audio-extractor/issues
- Source code: https://github.com/polywertz/anki-audio-extractor

## License

MIT
