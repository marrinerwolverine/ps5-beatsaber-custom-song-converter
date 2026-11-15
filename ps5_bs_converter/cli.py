"""
Command-Line Interface for PS5 Beat Saber (PSVR2) Custom Song Converter & Studio.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

from .auto_mapper import (
    STYLE_PRESETS,
    export_package_to_beatsaver_zip,
    generate_track_from_audio_file,
)
from .beatsaver_client import download_beatsaver_zip, search_beatsaver
from .game_manager import COLOR_THEME_PRESETS, PS5BeatSaberManager, find_ps5_game_root
from .stage_modder import STAGE_MESH_TARGETS


def _auto_default_game_dir() -> Optional[str]:
    here = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
    workspace_parent = os.path.dirname(here)
    candidates = [
        os.path.join(workspace_parent, "Beat Saber for the PS5", "PPSA15035-app 1.37", "PPSA15035-app"),
        os.path.join(workspace_parent, "Beat Saber for the PS5", "PPSA15035-app 1.12"),
        os.path.join(workspace_parent, "Beat Saber for the PS5", "PPSA15035-app"),
        os.path.join(workspace_parent, "Beat Saber for the PS5"),
        workspace_parent,
    ]
    for c in candidates:
        if os.path.exists(c):
            try:
                return find_ps5_game_root(c)
            except FileNotFoundError:
                continue
    return None


def build_parser() -> argparse.ArgumentParser:
    default_game = _auto_default_game_dir()
    p = argparse.ArgumentParser(
        prog="ps5_beatsaber_converter",
        description="Convert BeatSaver maps, auto-generate tracks from audio files, and customize PS5 Beat Saber stages.",
    )
    p.add_argument(
        "--game-dir",
        default=default_game,
        help="Path to decrypted PS5 Beat Saber folder (e.g. PPSA15035-app).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # search
    sp_search = sub.add_parser("search", help="Search BeatSaver for community custom songs.")
    sp_search.add_argument("query", help="Song or artist search text.")
    sp_search.add_argument("--sort", default="Relevance", choices=["Relevance", "Rating", "Latest"])

    # list-slots
    sp_list = sub.add_parser("list-slots", help="List available song slots (all 28 OST + DLC packs shown by default).")
    sp_list.add_argument(
        "--category",
        default="all",
        choices=["all", "dlc", "free"],
        help="Filter by pack category: 'all' (234 slots), 'dlc' (184 DLC slots), or 'free' (50 OST/Extras slots).",
    )
    sp_list.add_argument("--pack", help="Filter by pack ID (e.g. Queen, BTS, TheRollingStones, OstVol1, Extras).")

    # install-pack-batch (fill a whole DLC or OST pack from a .bplist playlist or folder!)
    sp_batch = sub.add_parser(
        "install-pack-batch",
        help="Batch-fill an entire DLC or OST Music Pack from a BeatSaver .bplist playlist or folder of .zip/music files.",
    )
    sp_batch.add_argument("--source", required=True, help="Path to a .bplist playlist file or folder of .zip/.mp3/.ogg files.")
    sp_batch.add_argument("--pack", required=True, help="Target Music Pack ID (e.g. Queen, BTS, BillieEilish, TheRollingStones).")
    sp_batch.add_argument("--stage", default="auto", help="Stage arena override ('auto', 'keep', or stage ID).")

    # list-stages
    sub.add_parser("list-stages", help="List all 37 playable PS5 Stage Environments available for songs & colors.")

    # install
    sp_inst = sub.add_parser("install", help="Convert and install a BeatSaver song into a PS5 slot.")
    src_grp = sp_inst.add_mutually_exclusive_group(required=True)
    src_grp.add_argument("--beatsaver", help="BeatSaver map ID (e.g. 30560) or beatsaver.com/maps/<id> URL.")
    src_grp.add_argument("--map-path", help="Path to a local BeatSaver .zip file or extracted map folder.")
    sp_inst.add_argument("--slot", required=True, help="Target PS5 slot level ID (e.g. SpookyBeat, FitBeat).")
    sp_inst.add_argument("--title", help="Optional override for in-game song title.")
    sp_inst.add_argument("--artist", help="Optional override for in-game artist name.")
    sp_inst.add_argument("--mapper", help="Optional override for mapper name.")
    sp_inst.add_argument(
        "--stage",
        default="auto",
        help="Stage arena override: 'auto' (match mapper stage), 'keep', or any stage from list-stages.",
    )
    sp_inst.add_argument(
        "--no-auto-diffs",
        action="store_true",
        help="Disable smart Easy/Normal/Hard generation (clones nearest mapper difficulty instead).",
    )
    sp_inst.add_argument(
        "--separate-resource",
        action="store_true",
        help="(1.12 only) Store custom audio in Media/customsongs.resource.",
    )
    sp_inst.add_argument(
        "--cover",
        action="store_true",
        help="(1.37 experimental) Paste BeatSaver cover art into PS5 BC7 SongArtworks atlas.",
    )

    # make-track (Audio File -> Beat Saber Track!)
    sp_gen = sub.add_parser(
        "make-track",
        help="Generate a full 5-difficulty Beat Saber track from any .mp3/.wav/.ogg/.flac music file.",
    )
    sp_gen.add_argument("--audio", required=True, help="Path to your music file (.mp3, .wav, .ogg, .flac, .egg).")
    sp_gen.add_argument("--slot", help="Optional PS5 slot ID to install the generated track into immediately.")
    sp_gen.add_argument("--export-zip", help="Optional path to save the generated track as a BeatSaver .zip.")
    sp_gen.add_argument("--title", help="Song title (defaults to audio filename).")
    sp_gen.add_argument("--artist", help="Artist name (defaults to audio filename).")
    sp_gen.add_argument("--bpm", type=float, help="Optional manual BPM override (auto-detected if omitted).")
    sp_gen.add_argument("--style", default="Balanced Flow", choices=list(STYLE_PRESETS))
    sp_gen.add_argument("--stage", default="DefaultEnvironment", help="Stage environment name (e.g. TheWeekndEnvironment).")

    # set-stage-colors / reset-stage-colors
    sp_col = sub.add_parser("set-stage-colors", help="Apply a custom neon saber & light color theme to a PS5 stage.")
    sp_col.add_argument("--stage", required=True, help="Stage name (e.g. DefaultEnvironment, TheWeekndEnvironment).")
    sp_col.add_argument("--theme", required=True, choices=list(COLOR_THEME_PRESETS.keys()))

    sp_rcol = sub.add_parser("reset-stage-colors", help="Restore stock saber & stage light colors.")
    sp_rcol.add_argument("--stage", help="Specific stage to reset (resets all stages if omitted).")

    # import-stage-obj / reset-stage-meshes
    sp_obj = sub.add_parser("import-stage-obj", help="Import a custom .obj 3D model into PS5 stage geometry.")
    sp_obj.add_argument("--target", default="platform", choices=list(STAGE_MESH_TARGETS.keys()))
    sp_obj.add_argument("--obj", required=True, help="Path to Wavefront .obj 3D model.")
    sp_obj.add_argument("--scale", type=float, default=1.0, help="Uniform scale multiplier (default: 1.0).")

    sub.add_parser("reset-stage-meshes", help="Restore all modified 3D stage meshes back to stock.")

    # restore / restore-all / status / verify / export-songlist / ftp-push
    sp_rest = sub.add_parser("restore", help="Restore a single song slot back to original stock.")
    sp_rest.add_argument("--slot", required=True, help="Slot level ID to restore (e.g. SpookyBeat).")

    sub.add_parser("restore-all", help="Restore all modified PS5 Beat Saber files to 100%% factory stock.")
    sub.add_parser("status", help="Show installed custom songs, stage themes, and backup state.")
    sub.add_parser("verify", help="Verify binary integrity of all installed custom songs.")

    sp_exp = sub.add_parser("export-songlist", help="Export a printable HTML list of installed custom songs.")
    sp_exp.add_argument("-o", "--output", default="PS5_Custom_Songs.html", help="Output HTML path.")

    sp_ftp = sub.add_parser("ftp-push", help="Push modified files directly to a modded PS5 running FTP.")
    sp_ftp.add_argument("--ip", required=True, help="PS5 IP address.")
    sp_ftp.add_argument("--port", type=int, default=2121, help="PS5 FTP port (default: 2121).")
    sp_ftp.add_argument("--remote-dir", required=True, help="Remote PS5 app folder containing Media/.")

    return p


def _require_manager(game_dir: Optional[str]) -> PS5BeatSaberManager:
    if not game_dir:
        print("Error: --game-dir is required (could not auto-detect PPSA*-app).", file=sys.stderr)
        sys.exit(1)
    return PS5BeatSaberManager(game_dir)


def main(argv: Optional[list[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "search":
        results = search_beatsaver(args.query, sort_order=args.sort)
        if not results:
            print("No maps found.")
            return 0
        print(f"{'ID':8s} | {'Song Title':30s} | {'Artist':22s} | {'BPM':6s} | {'Score':6s} | Difficulties")
        print("-" * 105)
        for r in results[:20]:
            print(
                f"{r.map_id:8s} | {r.song_name[:30]:30s} | {r.song_author_name[:22]:22s} | "
                f"{r.bpm:6.1f} | {r.score_pct:5.1f}% | {r.difficulties_summary}"
            )
        return 0

    if args.command == "make-track":
        if not args.slot and not args.export_zip:
            print("Error: specify --slot <PS5Slot> and/or --export-zip <out.zip>.", file=sys.stderr)
            return 1
        print(f"Analyzing rhythm & generating 5-difficulty Beat Saber charts from '{args.audio}'...")
        pkg, analysis = generate_track_from_audio_file(
            args.audio,
            song_name=args.title,
            song_author=args.artist,
            bpm_override=args.bpm,
            style=args.style,
            environment_name=args.stage,
        )
        counts = {
            k: len(c.v3_dict["colorNotes"])
            for k, c in sorted(pkg.charts["Standard"].items())
        }
        print(
            f"Generated '{pkg.song_name}' by '{pkg.song_author_name}' | "
            f"Detected BPM: {analysis.bpm} | Chorus Preview: {analysis.preview_start_seconds}s | "
            f"Notes E/N/H/Ex/Ex+: {counts[0]}/{counts[1]}/{counts[2]}/{counts[3]}/{counts[4]}"
        )
        if args.export_zip:
            saved = export_package_to_beatsaver_zip(pkg, args.export_zip)
            print(f"Exported BeatSaver .zip package to: {os.path.abspath(saved)}")
        if args.slot:
            mgr = _require_manager(args.game_dir)
            mgr.install_custom_song(
                map_source=pkg,
                target_level_id=args.slot,
                environment_override=args.stage,
                log_fn=print,
            )
        return 0

    mgr = _require_manager(args.game_dir)

    if args.command == "list-slots":
        slots = mgr.list_slots(pack_category=args.category, pack_id=args.pack)
        print(
            f"PS5 Game: {mgr.title_id} (v{mgr.content_version}, Engine: {mgr.engine_mode}) - "
            f"Showing {len(slots)} slots (Category: {args.category}):"
        )
        print(f"{'Slot ID':26s} | {'Pack':18s} | {'Stock Song':26s} | {'Status / Current Song'}")
        print("-" * 105)
        for s in slots:
            status = (
                f"CUSTOM: {s.current_song_name} - {s.current_song_author}"
                if s.is_custom
                else "Stock"
            )
            tag = "[OST] " if s.is_free_base_game else "[DLC] "
            print(f"{s.level_id:26s} | {(tag + s.pack_id)[:18]:18s} | {s.stock_song_name[:26]:26s} | {status}")
        return 0

    if args.command == "install-pack-batch":
        mgr.install_batch_into_pack(
            playlist_or_folder_or_files=args.source,
            target_pack_id=args.pack,
            environment_override=args.stage,
            log_fn=print,
        )
        return 0

    if args.command == "list-stages":
        envs = mgr.list_environments()
        print(f"Available PS5 Stage Environments ({len(envs)} arenas):")
        print(f"{'Display Name':26s} | {'Stage ID (--stage)'}")
        print("-" * 64)
        for e in envs:
            print(f"{e.display_name:26s} | {e.serialized_name}")
        return 0

    if args.command == "install":
        if args.beatsaver:
            print(f"Downloading BeatSaver map '{args.beatsaver}'...")
            src = download_beatsaver_zip(args.beatsaver)
        else:
            src = args.map_path
        mgr.install_custom_song(
            map_source=src,
            target_level_id=args.slot,
            song_name_override=args.title,
            song_author_override=args.artist,
            level_author_override=args.mapper,
            auto_generate_difficulties=not args.no_auto_diffs,
            use_separate_resource=args.separate_resource,
            environment_override=args.stage,
            install_cover_art=args.cover,
            log_fn=print,
        )
        return 0

    if args.command == "set-stage-colors":
        mgr.apply_stage_color_theme(args.stage, args.theme, log_fn=print)
        return 0

    if args.command == "reset-stage-colors":
        mgr.restore_stage_colors(args.stage, log_fn=print)
        return 0

    if args.command == "import-stage-obj":
        mgr.replace_stage_mesh_from_obj(args.target, args.obj, scale=args.scale, log_fn=print)
        return 0

    if args.command == "reset-stage-meshes":
        mgr.restore_stage_meshes(log_fn=print)
        return 0

    if args.command == "restore":
        mgr.restore_slot(args.slot, log_fn=print)
        return 0

    if args.command == "restore-all":
        mgr.restore_all(log_fn=print)
        return 0

    if args.command == "status":
        installed = mgr.state.get("installed_slots", {})
        themes = mgr.state.get("modified_stage_themes", {})
        meshes = mgr.state.get("modified_stage_meshes", {})
        print(f"Game Root      : {mgr.app_root}")
        print(f"Title ID       : {mgr.title_id} (v{mgr.content_version}) | Engine Mode: {mgr.engine_mode}")
        print(f"Total Slots    : {len(mgr.slots)} ({sum(1 for s in mgr.slots.values() if not s.is_free_base_game)} DLC + {sum(1 for s in mgr.slots.values() if s.is_free_base_game)} OST)")
        print(f"Backup Folder  : {mgr.backup_dir} ({'Ready' if mgr.has_backup() else 'Not created yet'})")
        print(f"Custom Songs   : {len(installed)} installed")
        for lid, info in sorted(installed.items()):
            st_lbl = info.get("stage_environment", "Slot Default")
            print(
                f"  - [{info['pack_name']}] {lid}: '{info['custom_song_name']}' by "
                f"'{info['custom_song_author']}' (Stage: {st_lbl})"
            )
        if themes:
            print(f"Stage Themes   : {themes}")
        if meshes:
            print(f"3D Stage Meshes: {meshes}")
        return 0

    if args.command == "verify":
        res = mgr.verify_all_installed()
        print(f"Verified {len(res)} installed custom song(s) - ALL OK:")
        for item in res:
            print(f"  OK {item['level_id']}: '{item['song_name']}' ({item['duration']}s, {item['charts_verified']} charts)")
        return 0

    if args.command == "export-songlist":
        out = mgr.export_song_list_html(args.output)
        print(f"Exported party song lineup to: {os.path.abspath(out)}")
        return 0

    if args.command == "ftp-push":
        mgr.deploy_via_ftp(args.ip, args.port, args.remote_dir, log_fn=print)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
