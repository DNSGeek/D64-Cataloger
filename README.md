# D64Catalog

Catalog and search large collections of Commodore disk, tape, and cartridge
images. A stdlib-only Python scanner walks a directory tree, parses each
image's internal directory, and stores everything in a SQLite database with
FTS5 full-text search. An optional cross-platform GUI front end written in
PureBasic sits on top of the same database.

Ever tried to remember which of your 40,000 disk images has that one PRG you
half-remember from 1987? This is for that.

## Supported formats

| Format | Extension | What gets cataloged                                   |
|--------|-----------|-------------------------------------------------------|
| D64    | .d64      | Disk name, ID, full directory (35/40/42-track, with or without error info) |
| D71    | .d71      | Same as D64; double-sided 1571 layout                 |
| D80    | .d80      | Single sided PET disk. Disk name, ID, full directory  |
| D81    | .d81      | Disk name, ID, full directory                         |
| D82    | .d82      | Double sided PET disk. Disk name, ID, full directory  |
| T64    | .t64      | Tape name and entries, including repairs for the common broken-header cases |
| TAP    | .tap      | Container only: filename stands in as the program name (TAP has no directory) |
| PRG    | .prg      | Single file: name from the host filename, load address from the first two bytes |
| CRT    | .crt      | Cartridge name, mapper type, one entry per CHIP packet with bank and load address |

Per-file data includes the PETSCII name (stored both raw and as an ASCII
transliteration), CBM file type (PRG/SEQ/USR/REL/DEL/CBM), block count,
locked and splat flags, and start track/sector where applicable.

## Requirements

- Python 3.6 or later. No third-party packages; the scanner is standard
  library only.
- FTS5 support in SQLite (included in every normal Python build). If it is
  missing, scanning still works and the CLI reports search as unavailable.
- For the GUI: [PureBasic 6.x](https://www.purebasic.com/) to compile D64Catalog.pb. The GUI itself has
  no runtime dependencies beyond the compiled executable; SQLite is
  statically linked by PureBasic. The free version is sufficient for this.

## Quick start

```
# Build or update the catalog
python3 d64catalog.py scan /path/to/your/images catalog.db

# Find things
python3 d64catalog.py search catalog.db 'boulder dash'
python3 d64catalog.py search catalog.db 'turbo*'
python3 d64catalog.py search catalog.db 'demo AND NOT intro' --type PRG
```

Rescans are incremental: images whose size and mtime are unchanged are
skipped. Use --force to re-parse everything. Search covers file names,
disk names, and image filenames, ranked by relevance (bm25). Standard FTS5
query syntax applies: bare terms AND together, quotes make phrases, a
trailing * makes a prefix.

Exit codes from search are scriptable: 0 = matches found, 1 = no matches,
2 = error.

## The GUI

Compile D64Catalog.pb with PureBasic on Windows, Linux, or macOS. It
expects d64catalog.py in the same directory as the executable (scanning
shells out to it; adjust the constants at the top of the .pb file if your
python3 lives somewhere unusual).

The GUI lets you pick a library root and a database file, kick off scans
with live progress, and search with per-format include filters and two
find modes: image names (one row per matching image) and file names inside
images (one row per matching directory entry).

## Database schema

Two real tables plus a rebuildable search index:

```
disks: id, path, filename, image_type, diskname, diskname_raw,
       dos_id, size_bytes, mtime, scanned_at
files: id, disk_id -> disks.id, name, name_raw, file_type, blocks,
       size_bytes, locked, splat, start_track, start_sector, load_addr
```

The search_fts virtual table is derived data, dropped and rebuilt at the
end of any scan that changed something. Feel free to query the database
directly from your own tools; the raw PETSCII name BLOBs are preserved
alongside the ASCII columns for anything that needs byte-exact names.

Some fields are overloaded per format, on purpose:

- dos_id holds the two-character DOS ID for disks, 'HWnn' (the numeric
  mapper/hardware type) for CRTs, and 'Vn' (TAP version byte) for TAPs.
- files.size_bytes is blocks x 254 for disk files, the actual data length
  for T64/PRG/CRT entries, and the pulse-stream payload length for TAPs.

## Format notes and known limitations

- T64 headers lie constantly in the wild. The parser tolerates the
  "0 used entries" bug and the bogus $C3C6 end-address bug, falling back
  to the data actually present in the file. Multi-file T64s with broken
  end addresses may report imprecise sizes for all but the last file.
- TAP entries are container-level stubs. A TAP is a raw pulse stream, so
  there is no directory to read without decoding the tape encoding. The
  'TAP' file_type and 'Vn' dos_id mark these rows, so a future decoding
  pass can find and upgrade them in place with a --force rescan.
- PETSCII names are transliterated to ASCII on a best-effort basis for
  the searchable columns; characters with no reasonable ASCII equivalent
  become '?'. The raw bytes are always preserved in the *_raw columns.
- GEOS files appear as their underlying CBM type (usually USR); the GEOS
  info block is not parsed.
- Corrupt or truncated images are skipped with a warning and do not stop
  the scan.

## License

GPLv2. See LICENSE.

A Gopher Broke Software production.
