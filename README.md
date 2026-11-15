# PS5 Beat Saber (PSVR2) — Custom Song & Stage Studio (`1.12` + `1.37`)

Converts community custom songs from **BeatSaver** (`.zip` packages, `.bplist` playlists, map folders, or direct BeatSaver search/IDs)—or **auto-generates playable tracks from any `.mp3`/`.wav`/`.ogg`/`.flac` music file**—directly into native **PS5 / PSVR2 Beat Saber** (`PPSA15035` / `PPSA14734`) game assets for a jailbroken PS5.

Supports **both** PS5 Beat Saber generations automatically out of the box:
- **Beat Saber `1.12` (`contentVersion 01.012.000`, Unity `2021.3.16f1`):** Monolithic `Media/sharedassets*.assets` + `PCM16 FSB5` tail-append engine (all 234 OST + DLC slots).
- **Beat Saber `1.37` (`contentVersion 01.037.000`, Unity `2022.3.33f1` Addressables):** Hardware-proven per-song `BeatmapLevelsData` `UnityFS` (`0x243` LZ4HC) engine with `0x6D39BF3E` Vorbis `FSB5` audio + 0-offset sync verification, tempo-flattened `v4.0.0` beatmap/lightshow/`audio.gz` JSON, byte-exact level-pack `BeatmapLevelSO` + 10s preview splicing, same-length `aa/catalog.json` `m_Crc: 0` updates, backport `ampr_emu.index` file-size updates, and optional PS5 `Standard 4KB` BC7 `SongArtworks` cover art pasting (208 Standard-only OST + DLC slots across 35 packs).

---

## Key Features
- **Hardware-Verified Dual Backend (`1.12` & `1.37`):** Automatically detects `Media/sharedassets48.assets` (`1.12`) vs `Media/StreamingAssets/BeatmapLevelsData/` + `aa/catalog.json` (`1.37`) and `ampr_emu.index`.
- **True In-Game Song Titles, Artists, Lengths, Previews & Stages:** Updates `BeatmapLevelSO` natively on both `1.12` and `1.37`—no runtime `.prx` plugin required.
- **Full OST + DLC Pack Support & Batch Pack Importer:** Unlocks all OST, Extras, and DLC Music Packs in the slot browser, plus 1-click **`📦 Batch Fill Pack...`** from BeatSaver `.bplist` playlists or folders of `.zip` / music files.
- **Offline Audio-to-Beatmap Studio (`Ctrl+V` Any Music File):** Copy & paste (`Ctrl+V`) or select any `.mp3`, `.wav`, `.ogg`, `.flac`, or `.egg` file to detect BPM & beat phase via multi-band spectral flux and procedurally generate all 5 difficulties (`Easy` → `Expert+`) with wrist-parity flow and a synced stage lightshow.
- **Universal BeatSaver Schema Support (`V2`, `V3`, and `V4`):** Automatically fills missing lower difficulties (`Easy`, `Normal`, `Hard`, `Expert`) for friends & family.
- **Stages, Neon Colors & 3D Models:** Per-song Stage Arena overrides on both `1.12` & `1.37`, `ColorSchemeSO` neon saber/light themes & Wavefront `.obj` 3D stage geometry injection on `1.12`, and optional PS5 `Standard 4KB` BC7 cover-art atlas pasting on `1.37`.
- **Pristine Backups, Surgical Restore & Verified FTP Push:** Keeps pristine backups outside `PPSA*-app`, restores single slots or full factory state cleanly, and supports direct PS5 FTP push (`1.12` delta tail push; `1.37` whole-file SHA-1 read-back verified push with `ampr_emu.index` last).

---

## Quick Start (Windows Neon Studio GUI)
1. Clone or download this repository.
2. Double-click:
   ```text
   Launch PS5 Beat Saber Converter.bat
   ```
   *(First launch automatically creates `.venv` and installs dependencies.)*
3. Pick your decrypted `PPSA15035-app` (`1.12` or `1.37`) folder in the top bar.
4. Search BeatSaver, open a `.zip`, or paste (`Ctrl+V`) any music file to auto-map a track, choose a target slot, and click **`⚡ CONVERT & INSTALL INTO PS5 BEAT SABER`**.

---

## Command-Line Usage
```bash
pip install -r requirements.txt

# Check detected game version (1.12 vs 1.37) and installed custom songs
python app.py --game-dir "/path/to/PPSA15035-app" status

# List available OST + DLC slots
python app.py --game-dir "/path/to/PPSA15035-app" list-slots

# Download & install a BeatSaver map into a slot (add --cover on 1.37 for experimental BC7 cover art)
python app.py --game-dir "/path/to/PPSA15035-app" install --beatsaver 30560 --slot SpookyBeat

# Auto-generate a 5-difficulty track from any MP3/WAV/OGG file and install it
python app.py --game-dir "/path/to/PPSA15035-app" make-track --audio "/path/to/song.mp3" --slot SpookyBeat

# Batch-fill an entire pack from a BeatSaver .bplist playlist or folder
python app.py --game-dir "/path/to/PPSA15035-app" install-pack-batch --pack Queen --source "/path/to/playlist.bplist"

# Restore a single slot or reset everything to 100% stock
python app.py --game-dir "/path/to/PPSA15035-app" restore --slot SpookyBeat
python app.py --game-dir "/path/to/PPSA15035-app" restore-all
```

---

## Running Tests
```bash
pip install -r requirements.txt pytest
pytest -v
```
To run the full hardware-layout integration tests against local `1.12` and/or `1.37` dumps:
```powershell
$env:PS5_BEATSABER_DUMP = "C:\path\to\PPSA15035-app 1.12"
$env:PS5_BEATSABER_DUMP_137 = "C:\path\to\PPSA15035-app 1.37\PPSA15035-app"
pytest -v
```

---

## Disclaimer
This project is an educational/modding tool that operates exclusively on your own decrypted game files and community-mapped BeatSaver content. It does **not** include any copyrighted game assets, DLC unlockers, or DRM bypasses.
