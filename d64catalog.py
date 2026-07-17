#!/usr/bin/env python3
"""
d64catalog.py - Recursively scan a directory for Commodore disk/tape images
(D64, D71, D80, D81, D82, T64, TAP, PRG, CRT) and catalog their contents
into a SQLite database.

Usage:
    python3 d64catalog.py scan /path/to/images catalog.db
    python3 d64catalog.py scan /path/to/images catalog.db --force --verbose
    python3 d64catalog.py search catalog.db 'turbo*'
    python3 d64catalog.py search catalog.db 'demo AND NOT intro' --type PRG

Schema:
    disks(id, path, filename, image_type, diskname, diskname_raw,
          dos_id, size_bytes, mtime, scanned_at)
    files(id, disk_id -> disks.id, name, name_raw, file_type, blocks,
          size_bytes, locked, splat, start_track, start_sector, load_addr)
"""

import argparse
import configparser
import csv
import logging
import os
import sqlite3
import sys
import time
from json import dumps
from pathlib import Path

APP_NAME = "D64Catalog"


def config_path():
    if sys.platform == "win32":
        base = (
            Path(
                os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")
            )
            / APP_NAME
        )
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = (
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / APP_NAME.lower()
        )
    return base / "config.ini"


def config_database():
    """Default DB path from the shared config, or None."""
    p = config_path()
    if not p.is_file():
        return None
    cfg = configparser.ConfigParser()
    cfg.read(p, encoding="utf-8-sig")
    return cfg.get("paths", "database", fallback=None)


# ---------------------------------------------------------------------------
# PETSCII handling
# ---------------------------------------------------------------------------


def petscii_to_ascii(raw):
    """Best-effort PETSCII -> ASCII transliteration for display/search.
    Lossless data lives in the *_raw BLOB columns; this is for humans."""
    out = []
    for b in raw:
        if 0x20 <= b <= 0x5F:  # space, punctuation, digits, A-Z
            out.append(chr(b))
        elif 0xC1 <= b <= 0xDA:  # shifted letters -> lowercase
            out.append(chr(b - 0xC1 + ord("a")))
        elif 0x41 <= b <= 0x5A:  # already covered above, kept explicit
            out.append(chr(b))
        elif b == 0xA0:  # shift-space padding
            out.append(" ")
        else:
            out.append("?")
    return "".join(out).rstrip()


def strip_a0(raw):
    """Trim trailing $A0 padding (and trailing NULs) from a PETSCII field."""
    return raw.rstrip(b"\xa0\x00")


CBM_FILE_TYPES = {0: "DEL", 1: "SEQ", 2: "PRG", 3: "USR", 4: "REL", 5: "CBM"}


def decode_type_byte(t):
    """Split a CBM directory file-type byte into (name, locked, splat)."""
    ftype = CBM_FILE_TYPES.get(t & 0x07, "?%02X" % (t & 0x07))
    locked = 1 if (t & 0x40) else 0
    splat = 0 if (t & 0x80) else 1  # bit 7 clear = improperly closed (*)
    return ftype, locked, splat


# ---------------------------------------------------------------------------
# Parsed-result containers (plain dicts keep it simple)
# ---------------------------------------------------------------------------


def make_file_entry(
    name_raw,
    ftype,
    locked=0,
    splat=0,
    blocks=None,
    size_bytes=None,
    start_track=None,
    start_sector=None,
    load_addr=None,
):
    return {
        "name_raw": bytes(name_raw),
        "name": petscii_to_ascii(strip_a0(name_raw)),
        "file_type": ftype,
        "locked": locked,
        "splat": splat,
        "blocks": blocks,
        "size_bytes": size_bytes,
        "start_track": start_track,
        "start_sector": start_sector,
        "load_addr": load_addr,
    }


# ---------------------------------------------------------------------------
# D64
# ---------------------------------------------------------------------------


def _d64_sectors_on_track(track):
    if track <= 17:
        return 21
    if track <= 24:
        return 19
    if track <= 30:
        return 18
    return 17


def _d64_offset(track, sector):
    off = 0
    for t in range(1, track):
        off += _d64_sectors_on_track(t)
    return (off + sector) * 256


D64_SIZES = {
    174848: 35,
    175531: 35,  # 35 tracks, without/with error bytes
    196608: 40,
    197376: 40,  # 40 tracks
    205312: 42,
    206114: 42,  # 42 tracks (rare)
}


# ---------------------------------------------------------------------------
# Shared CBM directory chain walker
# ---------------------------------------------------------------------------


def _walk_cbm_directory(data, start, offset_fn, valid_ts, max_sectors=200):
    """Follow the t/s link chain of a CBM directory and yield file entries.
    Loop-protected: hostile or corrupt images can't spin us forever."""
    files = []
    visited = set()
    track, sector = start

    while track != 0:
        if (track, sector) in visited or not valid_ts(track, sector):
            break
        visited.add((track, sector))
        if len(visited) > max_sectors:
            break

        base = offset_fn(track, sector)
        if base + 256 > len(data):
            break
        block = data[base : base + 256]

        for i in range(8):
            e = block[i * 32 : (i + 1) * 32]
            type_byte = e[2]
            if type_byte == 0x00:
                continue  # scratched / empty slot
            ftype, locked, splat = decode_type_byte(type_byte)
            name_raw = strip_a0(e[5:21])
            blocks = e[30] | (e[31] << 8)

            # PRG load address lives in the first two data bytes of the
            # file's first sector (right after the 2-byte t/s link).
            # $0801 = C64 BASIC, $1001 = VIC-20, $1C01 = C128, $0401 = PET.
            load_addr = None
            if ftype == "PRG" and valid_ts(e[3], e[4]):
                fo = offset_fn(e[3], e[4])
                if fo + 4 <= len(data):
                    load_addr = data[fo + 2] | (data[fo + 3] << 8)

            files.append(
                make_file_entry(
                    name_raw,
                    ftype,
                    locked,
                    splat,
                    blocks=blocks,
                    size_bytes=blocks * 254 if blocks else 0,
                    start_track=e[3],
                    start_sector=e[4],
                    load_addr=load_addr,
                )
            )

        track, sector = block[0], block[1]

    return files


def parse_d64(data, stem=None):
    tracks = D64_SIZES.get(len(data))
    if tracks is None:
        raise ValueError("unrecognized D64 size: %d bytes" % len(data))

    bam = _d64_offset(18, 0)
    diskname_raw = strip_a0(data[bam + 0x90 : bam + 0xA0])
    dos_id = petscii_to_ascii(data[bam + 0xA2 : bam + 0xA4])

    files = _walk_cbm_directory(
        data,
        start=(18, 1),
        offset_fn=_d64_offset,
        valid_ts=lambda t, s: 1 <= t <= tracks
        and s < _d64_sectors_on_track(t),
    )
    return diskname_raw, dos_id, files


# ---------------------------------------------------------------------------
# D71 (1571 double-sided: side 2 repeats the side-1 zone layout)
# ---------------------------------------------------------------------------


def _d71_sectors_on_track(track):
    return _d64_sectors_on_track((track - 1) % 35 + 1)


def _d71_offset(track, sector):
    off = 0
    for t in range(1, track):
        off += _d71_sectors_on_track(t)
    return (off + sector) * 256


def parse_d71(data, stem=None):
    if len(data) not in (349696, 351062):  # without/with error bytes
        raise ValueError("unrecognized D71 size: %d bytes" % len(data))

    # Header layout matches the D64: BAM/name at 18/0, directory at 18/1.
    bam = _d71_offset(18, 0)
    diskname_raw = strip_a0(data[bam + 0x90 : bam + 0xA0])
    dos_id = petscii_to_ascii(data[bam + 0xA2 : bam + 0xA4])

    files = _walk_cbm_directory(
        data,
        start=(18, 1),
        offset_fn=_d71_offset,
        valid_ts=lambda t, s: 1 <= t <= 70 and s < _d71_sectors_on_track(t),
    )
    return diskname_raw, dos_id, files


# ---------------------------------------------------------------------------
# D81
# ---------------------------------------------------------------------------


def _d81_offset(track, sector):
    return ((track - 1) * 40 + sector) * 256


def parse_d81(data, stem=None):
    if len(data) not in (819200, 822400):  # without/with error bytes
        raise ValueError("unrecognized D81 size: %d bytes" % len(data))

    hdr = _d81_offset(40, 0)
    diskname_raw = strip_a0(data[hdr + 0x04 : hdr + 0x14])
    dos_id = petscii_to_ascii(data[hdr + 0x16 : hdr + 0x18])

    files = _walk_cbm_directory(
        data,
        start=(40, 3),
        offset_fn=_d81_offset,
        valid_ts=lambda t, s: 1 <= t <= 80 and s < 40,
    )
    return diskname_raw, dos_id, files


# ---------------------------------------------------------------------------
# D80 / D82 (8050 single-sided / 8250 double-sided PET disks)
#
# Geometry per Peter Schepers' D80-D82.TXT: 77 tracks per side in four
# density zones. BAM lives on track 38, header on 39/0, directory chain
# starts at 39/1 (max 28 sectors * 8 entries = 224 files). Directory
# entries use the same 32-byte layout as the 1541, so the shared walker
# handles them unchanged.
# ---------------------------------------------------------------------------


def _d8x_sectors_on_track(track):
    # Zone layout repeats on side 2 of a D82 (tracks 78-154 mirror 1-77).
    t = (track - 1) % 77 + 1
    if t <= 39:
        return 29
    if t <= 53:
        return 27
    if t <= 64:
        return 25
    return 23


def _d8x_offset(track, sector):
    off = 0
    for t in range(1, track):
        off += _d8x_sectors_on_track(t)
    return (off + sector) * 256


def _parse_d8x(data, tracks):
    hdr = _d8x_offset(39, 0)
    # Header 39/0: name at $06 (A0-padded), disk ID at $18-$19.
    diskname_raw = strip_a0(data[hdr + 0x06 : hdr + 0x17])
    dos_id = petscii_to_ascii(data[hdr + 0x18 : hdr + 0x1A])

    files = _walk_cbm_directory(
        data,
        start=(39, 1),
        offset_fn=_d8x_offset,
        valid_ts=lambda t, s: 1 <= t <= tracks
        and s < _d8x_sectors_on_track(t),
    )
    return diskname_raw, dos_id, files


def parse_d80(data, stem=None):
    # Check if mislabeled d82
    if len(data) in (1066496, 1070662):
        return _parse_d8x(data, 154)
    # 2083 sectors * 256; +2083 for the error-byte variant.
    if len(data) not in (533248, 535331):
        raise ValueError("unrecognized D80 size: %d bytes" % len(data))
    return _parse_d8x(data, 77)


def parse_d82(data, stem=None):
    # Check if mislabeled d80
    if len(data) in (533248, 535331):
        return _parse_d8x(data, 77)
    # 4166 sectors * 256; +4166 for the error-byte variant.
    if len(data) not in (1066496, 1070662):
        raise ValueError("unrecognized D82 size: %d bytes" % len(data))
    return _parse_d8x(data, 154)


# ---------------------------------------------------------------------------
# T64 (tape archive; famously full of broken headers)
# ---------------------------------------------------------------------------


def parse_t64(data, stem=None):
    if len(data) < 64 or not data[:3] == b"C64":
        raise ValueError("missing C64 tape signature")

    max_entries = data[0x22] | (data[0x23] << 8)
    used_entries = data[0x24] | (data[0x25] << 8)
    tapename_raw = data[0x28:0x40].rstrip(b"\x20\x00")

    # Broken-header workarounds: many T64s claim 0 used entries but contain 1,
    # and some claim 0 max entries. Scan up to max(1, max_entries) slots and
    # trust the per-entry type byte instead of the header count.
    scan = max(max_entries, used_entries, 1)
    files = []
    for i in range(scan):
        base = 64 + i * 32
        if base + 32 > len(data):
            break
        e = data[base : base + 32]
        entry_type = e[0]
        if entry_type == 0:
            continue  # free slot
        c64_type_byte = e[1]
        if c64_type_byte == 0:
            # Some tools write entry_type=1 with a zero C64 type; call it PRG.
            ftype, locked, splat = "PRG", 0, 0
        else:
            ftype, locked, splat = decode_type_byte(c64_type_byte)

        start_addr = e[2] | (e[3] << 8)
        end_addr = e[4] | (e[5] << 8)
        offset = e[8] | (e[9] << 8) | (e[10] << 16) | (e[11] << 24)

        size = end_addr - start_addr
        if size <= 0 or offset + size > len(data):
            # The classic $C3C6 end-address bug and friends. Fall back to
            # whatever data is actually present after the offset.
            size = max(0, len(data) - offset)

        name_raw = e[16:32].rstrip(b"\x20\x00\xa0")
        files.append(
            make_file_entry(
                name_raw,
                ftype,
                locked,
                splat,
                size_bytes=size,
                load_addr=start_addr,
            )
        )

    return tapename_raw, None, files


# ---------------------------------------------------------------------------
# PRG (bare program file: no directory, name comes from the host filename)
# ---------------------------------------------------------------------------


def parse_prg(data, stem=None):
    if len(data) < 3:
        raise ValueError("PRG too short: %d bytes" % len(data))
    stem = stem or ""
    load_addr = data[0] | (data[1] << 8)
    entry = {
        # ASCII-native format: name is pre-decoded, raw is the same bytes.
        "name_raw": stem.encode("ascii", "replace"),
        "name": stem,
        "file_type": "PRG",
        "locked": 0,
        "splat": 0,
        "blocks": None,
        "size_bytes": len(data) - 2,
        "start_track": None,
        "start_sector": None,
        "load_addr": load_addr,
    }
    # Returning diskname as str (not bytes) signals ASCII-native to the
    # caller, which then skips PETSCII transliteration.
    return "", None, [entry]


# ---------------------------------------------------------------------------
# CRT (cartridge image: header + CHIP packets)
# ---------------------------------------------------------------------------

CRT_CHIP_TYPES = {0: "ROM", 1: "RAM", 2: "FLSH"}


def parse_crt(data, stem=None):
    if len(data) < 0x40 or data[0:16] != b"C64 CARTRIDGE   ":
        raise ValueError("missing C64 CARTRIDGE signature")

    header_len = int.from_bytes(data[0x10:0x14], "big")
    if header_len < 0x40 or header_len > len(data):
        header_len = 0x40  # tolerate a bogus header length
    hw_type = int.from_bytes(data[0x16:0x18], "big")
    cart_name = data[0x20:0x40].rstrip(b"\x00 ").decode("ascii", "replace")

    files = []
    off = header_len
    while off + 16 <= len(data):
        if data[off : off + 4] != b"CHIP":
            break  # trailing garbage; stop cleanly
        packet_len = int.from_bytes(data[off + 4 : off + 8], "big")
        chip_type = int.from_bytes(data[off + 8 : off + 10], "big")
        bank = int.from_bytes(data[off + 10 : off + 12], "big")
        load = int.from_bytes(data[off + 12 : off + 14], "big")
        rom_size = int.from_bytes(data[off + 14 : off + 16], "big")

        name = "BANK %02d @ $%04X" % (bank, load)
        files.append(
            {
                "name_raw": name.encode("ascii"),
                "name": name,
                "file_type": CRT_CHIP_TYPES.get(
                    chip_type, "CHP%d" % chip_type
                ),
                "locked": 0,
                "splat": 0,
                "blocks": None,
                "size_bytes": rom_size,
                "start_track": None,
                "start_sector": None,
                "load_addr": load,
            }
        )
        if packet_len < 16:
            break  # malformed; avoid infinite loop
        off += packet_len

    # Hardware/mapper type rides in dos_id as 'HWnn' (numeric; see the
    # CRT spec's hardware type table for meanings, e.g. 32 = EasyFlash).
    return cart_name, "HW%d" % hw_type, files


# ---------------------------------------------------------------------------
# TAP (container-level only: TAP is a raw pulse stream with no directory,
# so the host filename stands in as the program name. If real CBM header
# decoding is added later, a --force rescan upgrades these rows in place.)
# ---------------------------------------------------------------------------


def parse_tap(data, stem=None):
    if len(data) < 0x14 or data[0:12] not in (
        b"C64-TAPE-RAW",
        b"C16-TAPE-RAW",
    ):
        raise ValueError("missing TAPE-RAW signature")

    version = data[0x0C]
    payload = int.from_bytes(data[0x10:0x14], "little")
    if payload == 0 or payload > len(data) - 0x14:
        payload = len(data) - 0x14  # header lies; trust the file

    stem = stem or ""
    entry = {
        "name_raw": stem.encode("ascii", "replace"),
        "name": stem,
        "file_type": "TAP",  # contents unenumerated
        "locked": 0,
        "splat": 0,
        "blocks": None,
        "size_bytes": payload,
        "start_track": None,
        "start_sector": None,
        "load_addr": None,
    }
    # dos_id carries the TAP version byte (0 = original, 1 = extended).
    return "", "V%d" % version, [entry]


PARSERS = {
    ".d64": ("D64", parse_d64),
    ".d71": ("D71", parse_d71),
    ".d80": ("D80", parse_d80),
    ".d81": ("D81", parse_d81),
    ".d82": ("D82", parse_d82),
    ".t64": ("T64", parse_t64),
    ".tap": ("TAP", parse_tap),
    ".prg": ("PRG", parse_prg),
    ".crt": ("CRT", parse_crt),
}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS disks (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    filename     TEXT NOT NULL,
    image_type   TEXT NOT NULL,
    diskname     TEXT,
    diskname_raw BLOB,
    dos_id       TEXT,
    size_bytes   INTEGER,
    mtime        REAL,
    scanned_at   TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY,
    disk_id      INTEGER NOT NULL REFERENCES disks(id) ON DELETE CASCADE,
    name         TEXT,
    name_raw     BLOB,
    file_type    TEXT,
    blocks       INTEGER,
    size_bytes   INTEGER,
    locked       INTEGER NOT NULL DEFAULT 0,
    splat        INTEGER NOT NULL DEFAULT 0,
    start_track  INTEGER,
    start_sector INTEGER,
    load_addr    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_files_disk ON files(disk_id);
CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);
CREATE INDEX IF NOT EXISTS idx_disks_diskname ON disks(diskname);
"""


def open_db(path):
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def rebuild_fts(con):
    """Drop and rebuild the full-text index from scratch.

    This DB is write-once-per-scan, read-many, so a full rebuild at the end
    of each scan is simpler and safer than trigger-based syncing. UNINDEXED
    columns ride along as join keys without polluting the token index."""
    with con:
        con.execute("DROP TABLE IF EXISTS search_fts")
        con.execute(
            "CREATE VIRTUAL TABLE search_fts USING fts5("
            "name, diskname, filename, file_id UNINDEXED, disk_id UNINDEXED)"
        )
        # LEFT JOIN so disks with zero directory entries are still findable
        # by image filename or diskname (file_id is NULL for those rows).
        con.execute(
            "INSERT INTO search_fts (name, diskname, filename, file_id, "
            "disk_id) "
            "SELECT f.name, d.diskname, d.filename, f.id, d.id "
            "FROM disks d LEFT JOIN files f ON f.disk_id = d.id"
        )


def has_fts5(con):
    try:
        con.execute("SELECT fts5(?)", ("",))
        return True
    except sqlite3.OperationalError:
        return False


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def find_images(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext in PARSERS:
                yield os.path.join(dirpath, fn)


def catalog_image(con, root, full_path, force=False, verbose=False):
    rel = os.path.relpath(full_path, root)
    ext = os.path.splitext(full_path)[1].lower()
    image_type, parser = PARSERS[ext]

    st = os.stat(full_path)
    row = con.execute(
        "SELECT id, mtime, size_bytes FROM disks WHERE path = ?", (rel,)
    ).fetchone()

    if row and not force and row[1] == st.st_mtime and row[2] == st.st_size:
        logging.debug("  skip (unchanged): %s", rel)

    with open(full_path, "rb") as fh:
        data = fh.read()

    stem = os.path.splitext(os.path.basename(full_path))[0]
    try:
        diskname_raw, dos_id, files = parser(data, stem)
    except ValueError as exc:
        logging.warning("%s: %s", rel, exc)
        return "error"

    if isinstance(diskname_raw, str):
        # ASCII-native format (PRG/CRT): no PETSCII transliteration needed.
        diskname = diskname_raw
        diskname_raw = diskname.encode("ascii", "replace")
    else:
        diskname = petscii_to_ascii(diskname_raw)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    with con:  # one transaction per image
        if row:
            disk_id = row[0]
            con.execute("DELETE FROM files WHERE disk_id = ?", (disk_id,))
            con.execute(
                "UPDATE disks SET filename=?, image_type=?, diskname=?, "
                "diskname_raw=?, dos_id=?, size_bytes=?, mtime=?, scanned_at=? "
                "WHERE id=?",
                (
                    os.path.basename(full_path),
                    image_type,
                    diskname,
                    bytes(diskname_raw),
                    dos_id,
                    st.st_size,
                    st.st_mtime,
                    now,
                    disk_id,
                ),
            )
        else:
            cur = con.execute(
                "INSERT INTO disks (path, filename, image_type, diskname, "
                "diskname_raw, dos_id, size_bytes, mtime, scanned_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    rel,
                    os.path.basename(full_path),
                    image_type,
                    diskname,
                    bytes(diskname_raw),
                    dos_id,
                    st.st_size,
                    st.st_mtime,
                    now,
                ),
            )
            disk_id = cur.lastrowid

        con.executemany(
            "INSERT INTO files (disk_id, name, name_raw, file_type, blocks, "
            "size_bytes, locked, splat, start_track, start_sector, load_addr) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    disk_id,
                    f["name"],
                    f["name_raw"],
                    f["file_type"],
                    f["blocks"],
                    f["size_bytes"],
                    f["locked"],
                    f["splat"],
                    f["start_track"],
                    f["start_sector"],
                    f["load_addr"],
                )
                for f in files
            ],
        )

    logging.debug(
        '  %s [%s] "%s" - %d files', rel, image_type, diskname, len(files)
    )
    return "updated" if row else "added"


def cmd_scan(args):
    root = os.path.abspath(args.directory)
    if not os.path.isdir(root):
        logging.error("Not a directory: %s", root)
        return 2

    if not args.database and not config_database():
        logging.error("No database specified.")
        return 2

    con = open_db(args.database if args.database else config_database())
    counts = {"added": 0, "updated": 0, "skipped": 0, "error": 0}

    for path in find_images(root):
        result = catalog_image(
            con, root, path, force=args.force, verbose=args.verbose
        )
        counts[result] += 1

    changed = counts["added"] + counts["updated"]
    fts_note = ""
    if has_fts5(con):
        fts_missing = (
            con.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='search_fts'"
            ).fetchone()[0]
            == 0
        )
        if changed or fts_missing:
            rebuild_fts(con)
            fts_note = " Search index rebuilt."
    else:
        fts_note = " (FTS5 unavailable in this SQLite; search disabled.)"

    con.close()
    total = sum(counts.values())
    logging.info(
        "Done. %d images: %d added, %d updated, %d skipped, %d errors.%s",
        total,
        counts["added"],
        counts["updated"],
        counts["skipped"],
        counts["error"],
        fts_note,
    )
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    if not args.database and not config_database():
        logging.error("No database specified.")
        return 2

    con: sqlite3.Connection = open_db(
        args.database if args.database else config_database()
    )
    if not has_fts5(con):
        logging.error("This SQLite build lacks FTS5")
        return 2
    fts_exists = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='search_fts'"
    ).fetchone()[0]
    if not fts_exists:
        logging.error("No search index; run a scan first")
        return 2

    sql: str = (
        "SELECT d.path, d.diskname, f.name, f.file_type, f.blocks, "
        "f.size_bytes, f.splat, f.load_addr "
        "FROM search_fts s "
        "JOIN files f ON f.id = s.file_id "
        "JOIN disks d ON d.id = s.disk_id "
        "WHERE search_fts MATCH ? "
    )
    params: list[str] = [args.query]
    if args.type:
        sql += "AND f.file_type = ? "
        params.append(args.type.upper())
    sql += "ORDER BY rank LIMIT ?"
    params.append(args.limit)

    try:
        rows = con.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        # Typically a bad FTS5 query string (unbalanced quotes, stray *, ...)
        logging.error("query error: %s", exc)
        logging.info("hint: quote phrases, use * only as a suffix: turbo*")
        return 2

    if not rows:
        logging.info("no matches for: %s", args.query)
        return 1

    writer = csv.writer(sys.stdout)
    jout: list[dict[str, str | int]] = []
    if args.csv:
        writer.writerow(
            ["load_addr", "name", "flag", "ftype", "size", "diskname", "path"]
        )
    for path, diskname, name, ftype, blocks, size, splat, load_addr in rows:
        size_str: str = (
            "%d blk" % blocks if blocks is not None else "%d B" % size
        )
        flag: str = "*" if splat else " "
        if load_addr is None:
            load_addr = 0
        if args.json:
            jout.append(
                {
                    "load_addr": load_addr,
                    "name": name,
                    "flag": flag,
                    "ftype": ftype,
                    "size": size_str,
                    "diskname": diskname,
                    "path": path,
                }
            )
        elif args.csv:
            writer.writerow(
                [load_addr, name, flag, ftype, size_str, diskname, path]
            )
        else:
            logging.info(
                "%d %-24s %s%-4s %8s  [%s] %s",
                load_addr,
                name,
                flag,
                ftype,
                size_str,
                diskname,
                path,
            )
    if args.json:
        print(dumps(jout))
    logging.info("%d match(es)", len(rows))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Catalog and search Commodore disk/tape images "
        "(D64/D71/D80/D81/D82/T64/TAP/PRG/CRT) in SQLite."
    )

    ap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="enable verbose logging",
    )

    sub = ap.add_subparsers(dest="command", required=True)

    ap_scan = sub.add_parser("scan", help="scan a directory tree for images")
    ap_scan.add_argument(
        "directory", help="root directory to scan recursively"
    )
    ap_scan.add_argument(
        "database",
        nargs="?",
        default=None,
        help="SQLite database path (default: from config)",
    )
    ap_scan.add_argument(
        "--force",
        action="store_true",
        help="re-parse images even if mtime/size unchanged",
    )
    ap_scan.set_defaults(func=cmd_scan)

    ap_search = sub.add_parser(
        "search", help="full-text search over file and disk names"
    )
    ap_search.add_argument("database", help="SQLite database path")
    ap_search.add_argument(
        "query",
        help='FTS5 query: turbo* | "exact phrase" | demo AND NOT intro',
    )
    ap_search.add_argument(
        "--type", help="filter by file type (PRG, SEQ, ...)"
    )
    ap_search.add_argument(
        "--limit", type=int, default=50, help="max results (default 50)"
    )
    ap_search.add_argument("--csv", help="Output as csv", action="store_true")
    ap_search.add_argument(
        "--json", help="Output as json", action="store_true"
    )
    ap_search.set_defaults(func=cmd_search)

    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
