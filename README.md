# PS5 Beat Saber (PSVR2) — Custom Song Converter

Converts community custom songs from **BeatSaver** (`.zip` packages, map folders, or direct BeatSaver search/IDs) directly into native **PS5 / PSVR2 Beat Saber** (`PPSA15035` / `PPSA14734`) game assets for a jailbroken PS5.

Beat Saber version 1.12

---

## Features
- **True In-Game Song Titles & Artists:** Updates `BeatmapLevelSO` (and `PreviewBeatmapLevelSO` where present) directly inside Unity's PS5 `.assets` files so your custom song title, artist, mapper, BPM, and preview play natively in-game—no runtime `.prx` plugin required.
- **50 Unlocked Base-Game Slots Ready Out of the Box:** Automatically indexes all 50 Always-Owned songs across *OST Vol. 1–6*, *Extras*, and *Camellia* (plus any DLC packs you own).
- **Lossless PS5 Audio (`PCM16 FSB5`):** Decodes BeatSaver `.egg`, `.ogg`, `.wav`, `.mp3`, and `.flac` audio into 32-byte-aligned stereo `44.1 kHz` / `48 kHz` FMOD `FSB5` (`codec=2`) streams matched byte-for-byte to Unity `2021.3.16f1` on PS5, with automatic anti-clipping normalization and `PerceivedLoudnessPerLevel` volume matching.
- **Universal BeatSaver Map Support (V2, V3, and V4):** Automatically converts BeatSaver `2.x`, `3.x`, and `4.x` charts (including arcs, chains, BPM changes, rotation events, and classic lighting compatibility) into PS5-native UTF-16LE gzipped `V3.3.0` `BeatmapDataSO` payloads.
- **Party / Friends & Family Ready:** Automatically generates balanced **Easy, Normal, Hard, Expert, and Expert+** difficulties when a BeatSaver map only includes Expert+, plus **One Saber, No Arrows, 90°, and 360°** modes on multi-mode slots.
- **Zero-Overwrite Safe Backups & Instant Restore:** Never overwrites original game audio inside `sharedassets*.resource`; appends custom audio cleanly to the tail, backs up pristine `.assets` files outside `PPSA*-app`, and lets you restore single slots or reset 100% back to factory stock in one click.
- **Optional Fast FTP Delta Push:** Push only modified `.assets` files and appended `.resource` tail bytes to `ftpsrv` / `etaHEN` on your PS5 without re-copying the 2.9 GB base audio file.

---

## Requirements
- **Windows / macOS / Linux** with **Python 3.10+** (Windows includes a one-click `.bat` launcher and Tkinter GUI).
- A **jailbroken PS5** (e.g., via etaHEN / Itemzflow / ShadowMount Plus) and a **decrypted dump of your own legally owned PS5 Beat Saber** (`PPSA15035-app` or `PPSA14734-app`, patch `01.012.000` / Unity `2021.3.16f1`).

---

## Quick Start (Windows Desktop App)
1. Clone or download this repository.
2. Double-click:
   ```text
   Launch PS5 Beat Saber Converter.bat
   ```
   *(First launch automatically creates `.venv` and installs `soundfile` and `numpy`.)*
3. Click **Browse...** and select your decrypted `PPSA15035-app` / `PPSA14734-app` folder (auto-detected if placed in a sibling folder).
4. **Step 1 (Left):** Search BeatSaver directly in the app, paste a BeatSaver ID/URL, or click **Load Local BeatSaver .zip...**.
5. **Step 2 (Right):** Pick one of the **50 Free Unlocked Base-Game Song Slots** (*OST Vol. 1–6*, *Extras*, or *Camellia*).
6. **Step 3 (Bottom-Right):** Click **Convert & Install Custom Song into PS5 Beat Saber**.
7. Mount/copy your updated `PPSA*-app` folder on your modded PS5 (or use **Tab 2 → Push Updates to PS5 via FTP**) and play!

---

## Command-Line Usage
```bash
# Install dependencies (if not using the .bat launcher)
pip install -r requirements.txt

# List the 50 free Always-Owned PS5 song slots
python app.py --game-dir "/path/to/PPSA15035-app" list-slots

# Search BeatSaver from the terminal
python app.py search "Crab Rave"

# Download & install a BeatSaver map by ID into a PS5 slot
python app.py --game-dir "/path/to/PPSA15035-app" install --beatsaver 30560 --slot SpookyBeat

# Install a local BeatSaver .zip file
python app.py --game-dir "/path/to/PPSA15035-app" install --map-path "/path/to/MyMap.zip" --slot FitBeat

# Verify binary integrity of all installed custom songs
python app.py --game-dir "/path/to/PPSA15035-app" verify

# Restore a single slot or restore everything to 100% factory stock
python app.py --game-dir "/path/to/PPSA15035-app" restore --slot SpookyBeat
python app.py --game-dir "/path/to/PPSA15035-app" restore-all
```

---

## Running Tests
```bash
pip install -r requirements.txt pytest
pytest -v
```
To also run the end-to-end integration tests against your local PS5 dump:
```powershell
$env:PS5_BEATSABER_DUMP = "C:\path\to\PPSA15035-app"
pytest -v
```

---

## Disclaimer
This project is an educational/modding tool that operates exclusively on your own decrypted game files and community-mapped BeatSaver content. It does **not** include any copyrighted game assets, DLC unlockers, or DRM bypasses.
