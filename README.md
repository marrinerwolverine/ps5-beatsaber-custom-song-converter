# Beat Saber Deluxe — PS5 (PSVR2) Custom Song Studio (`1.37`)

Converts community custom songs from **BeatSaver** (`.zip` packages, `.bplist` playlists, map folders, or direct BeatSaver search/IDs)—or **auto-generates playable tracks from any `.mp3`/`.wav`/`.ogg`/`.flac` music file**—directly into native **PS5 / PSVR2 Beat Saber** (`PPSA15035`) game assets for a jailbroken PS5.

**The game version is permanently `1.37` (`01.037.000`)** and the tool now operates directly on the backport **`.exfat` game image** (`PPSA15035 Beat Saber (01.037.000).exfat`) as well as extracted folder dumps:

- **Backport `.exfat` Container Support:** Open the game image directly — the tool reads/writes files inside the AMPRIDX3 container (master index + size-derived 64KB-aligned payload), keeps the index and physical layout perfectly consistent, and preserves pristine backups so any slot restores byte-identically. No more "tool said installed but the game kept the old song" — everything is written into the actual image the PS5 loads.
- **`1.37` Addressables Engine:** Per-song `BeatmapLevelsData` `UnityFS` (`0x243` LZ4HC) bundles with `0x6D39BF3E` Vorbis `FSB5` audio, tempo-flattened `v4.0.0` beatmap/lightshow/`audio.gz` `TextAsset`s (sparse JSON matching the real game files), byte-exact level-pack `CAB` splicing, same-length `aa/catalog.json` `m_Crc: 0` updates, optional PS5 `Standard 4KB` BC7 cover-art pasting, and a SHA-1-verified FTP push (`ampr_emu.index` last).
- **Legacy `1.12` folder support is retained** (`Media/sharedassets48.assets`, 234 OST + DLC slots).

---

## What's New in `v1.3.0`

Fixes reported by real hardware users (thanks R_Jay!):

1. **Songs now really save on `1.37`.** The whole `1.37` pipeline operates directly inside the backport `.exfat` image (or folder dump) through a consistent storage layer, so every install lands in the exact files the PS5 loads.
2. **No more crash on full-length songs.** The audio step used to die with a native stack overflow (libsndfile's recursive OGG write) on real-music songs of roughly 20-30 seconds or longer. OGG encoding now streams in bounded chunks — a 90-second dense mix encodes cleanly (regression-tested).
3. **Auto-made maps no longer arrive early.** The auto-mapper now anchors notes to the audio's **absolute beat phase** (notes land exactly on the detected beats) and emits `_songTimeOffset: 0`; community maps' `_songTimeOffset` is honored natively via the `v4` `bpmData` sample anchor.
4. **New "Joyful / Chilled" auto-map style** — gentler note density, fewer bombs/walls, comfortable swings. The three existing styles are untouched.
5. **Damage-tolerant catalog:** a corrupt DLC bundle inside a backport image (e.g. a bad `bts_pack`) is skipped with a warning instead of breaking the whole load.

---

## Quick Start (Windows Neon Studio GUI)
1. Clone or download this repository.
2. Double-click:
   ```text
   Launch PS5 Beat Saber Converter.bat
   ```
   *(First launch automatically creates `.venv` and installs dependencies.)*
3. Pick your game in the top bar — your **`.exfat` backport image** is auto-discovered, or browse to the folder that contains it (an extracted folder dump also works).
4. Search BeatSaver, open a `.zip`, or paste (`Ctrl+V`) any music file to auto-map a track, choose a target slot, and click **`⚡ CONVERT & INSTALL INTO PS5 BEAT SABER`**.
5. Flash the updated `.exfat` image to your USB drive (or push the changed files over **FTP** from the Library page) and play.

---

## Command-Line Usage
```bash
pip install -r requirements.txt

# Check detected game version and installed custom songs (works on .exfat images)
python app.py --game-dir "D:\Dumps\PPSA15035 Beat Saber (01.037.000).exfat" status

# List available OST + DLC slots
python app.py --game-dir "D:\Dumps\PPSA15035 Beat Saber (01.037.000).exfat" list-slots

# Download & install a BeatSaver map into a slot (add --cover for BC7 cover art)
python app.py --game-dir "D:\Dumps\PPSA15035 Beat Saber (01.037.000).exfat" install --beatsaver 30560 --slot SpookyBeat

# Auto-generate a 5-difficulty track from any MP3/WAV/OGG file and install it
python app.py --game-dir "D:\Dumps\PPSA15035 Beat Saber (01.037.000).exfat" make-track --audio "C:\Music\song.mp3" --slot SpookyBeat

# Batch-fill an entire pack from a BeatSaver .bplist playlist or folder
python app.py --game-dir "D:\Dumps\PPSA15035 Beat Saber (01.037.000).exfat" install-pack-batch --pack Queen --source "C:\Playlists\queen.bplist"

# Restore a single slot or reset everything to 100% stock
python app.py --game-dir "D:\Dumps\PPSA15035 Beat Saber (01.037.000).exfat" restore --slot SpookyBeat
python app.py --game-dir "D:\Dumps\PPSA15035 Beat Saber (01.037.000).exfat" restore-all
```

---

## Running Tests
```bash
pip install -r requirements.txt pytest
pytest -v
```
To also run the hardware-layout tests against a real backport image (read-only + an isolated mini-container sandbox):
```powershell
$env:PS5_BEATSABER_EXFAT = "C:\path\to\PPSA15035 Beat Saber (01.037.000).exfat"
pytest -v
```

---

## Disclaimer
This project is an educational/modding tool that operates exclusively on your own decrypted game files and community-mapped BeatSaver content. It does **not** include any copyrighted game assets, DLC unlockers, or DRM bypasses.
