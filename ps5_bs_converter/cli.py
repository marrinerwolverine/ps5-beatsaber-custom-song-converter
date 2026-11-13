"""
Command-Line Interface for PS5 Beat Saber (PSVR2) Custom Song Converter.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

from .beatsaver_client import download_beatsaver_zip, search_beatsaver
from .game_manager import PS5BeatSaberManager, find_ps5_game_root


def _auto_default_game_dir() -> Optional[str]:
    here = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
    workspace_parent = os.path.dirname(here)
    candidates = [
        os.path.join(workspace_parent, "Beat Saber for the PS5", "PPSA15035-app"),
        os.path.join(workspace_parent, "Beat Saber for the PS5"),
        workspace_parent,
    ]
    for c in candidates:
        try:
            return find_ps5_game_root(c)
        except FileNotFoundError:
            continue
    return None


def build_parser() -> argparse.ArgumentParser:
    default_game = _auto_default_game_dir()
    p = argparse.ArgumentParser(
        prog="ps5_beatsaber_converter",
        description="Convert BeatSaver custom songs into native PS5 / PSVR2 Beat Saber format.",
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
    sp_list = sub.add_parser("list-slots", help="List available song slots in the PS5 game dump.")
    sp_list.add_argument(
        "--all-packs",
        action="store_true",
        help="Include DLC packs in addition to the 50 free Always-Owned base-game songs.",
    )
    sp_list.add_argument("--pack", help="Filter by pack ID (e.g. OstVol1, Extras, Camellia).")

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
        "--no-auto-diffs",
        action="store_true",
        help="Disable smart Easy/Normal/Hard generation (clones nearest mapper difficulty instead).",
    )
    sp_inst.add_argument(
        "--separate-resource",
        action="store_true",
        help="Store custom audio in Media/customsongs.resource instead of appending to sharedassets*.resource.",
    )

    # restore
    sp_rest = sub.add_parser("restore", help="Restore a single song slot back to original stock.")
    sp_rest.add_argument("--slot", required=True, help="Slot level ID to restore (e.g. SpookyBeat).")

    # restore-all
    sub.add_parser("restore-all", help="Restore all modified PS5 Beat Saber files to 100%% factory stock.")

    # status
    sub.add_parser("status", help="Show installed custom songs and backup state.")

    # verify
    sub.add_parser("verify", help="Verify binary integrity of all installed custom songs.")

    # export-songlist
    sp_exp = sub.add_parser("export-songlist", help="Export a printable HTML list of installed custom songs.")
    sp_exp.add_argument("-o", "--output", default="PS5_Custom_Songs.html", help="Output HTML path.")

    # ftp-push
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

    mgr = _require_manager(args.game_dir)

    if args.command == "list-slots":
        slots = mgr.list_slots(free_only=not args.all_packs, pack_id=args.pack)
        print(
            f"PS5 Game: {mgr.title_id} (v{mgr.content_version}) - Showing {len(slots)} slots "
            f"({'All Packs' if args.all_packs else 'Free Always-Owned Packs'}):"
        )
        print(f"{'Slot ID':26s} | {'Pack':16s} | {'Stock Song':26s} | {'Status / Current Song'}")
        print("-" * 105)
        for s in slots:
            status = (
                f"CUSTOM: {s.current_song_name} - {s.current_song_author}"
                if s.is_custom
                else "Stock"
            )
            tag = "" if s.is_free_base_game else "[DLC] "
            print(f"{s.level_id:26s} | {(tag + s.pack_id)[:16]:16s} | {s.stock_song_name[:26]:26s} | {status}")
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
            log_fn=print,
        )
        return 0

    if args.command == "restore":
        mgr.restore_slot(args.slot, log_fn=print)
        return 0

    if args.command == "restore-all":
        mgr.restore_all(log_fn=print)
        return 0

    if args.command == "status":
        installed = mgr.state.get("installed_slots", {})
        print(f"Game Root      : {mgr.app_root}")
        print(f"Title ID       : {mgr.title_id} (v{mgr.content_version})")
        print(f"Backup Folder  : {mgr.backup_dir} ({'Ready' if mgr.has_backup() else 'Not created yet'})")
        print(f"Custom Songs   : {len(installed)} installed")
        for lid, info in sorted(installed.items()):
            print(
                f"  - [{info['pack_name']}] {lid}: '{info['custom_song_name']}' by "
                f"'{info['custom_song_author']}' (replaces '{info['stock_song_name']}')"
            )
        return 0

    if args.command == "verify":
        res = mgr.verify_all_installed()
        print(f"Verified {len(res)} installed custom song(s) — ALL OK:")
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
