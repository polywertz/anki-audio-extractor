These settings are remembered from the extraction dialog; you don't normally need to edit them here.

- `last_output_dir`: folder the dialog starts in.
- `name_files_after`: `"original"` (file name from the package), `"first field"`, `"pattern"` (use `name_pattern`), or the name of a note field.
- `name_pattern`: pattern used when naming files after several fields, such as `"{Front} - {Back}"`.
- `if_file_exists`: `"rename"` (keep both and add a number), `"skip"`, or `"overwrite"`.
- `write_csv`: also save `audio_notes.csv`, which lists the note each file belongs to.
- `write_playlist`: also save `audio_playlist.m3u8`, which plays the files in card order.
- `number_prefix`: start file names with their note's position in card order (`0001_`, `0002_`, …).
- `subfolder_per_deck`: put each file in a folder named after its note's deck, with subdecks as subfolders.
- `excluded_audio_fields`: fields unticked under "Take audio from"; audio played only from these fields is left out.
