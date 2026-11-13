"""
Native Windows Desktop GUI for the PS5 Beat Saber (PSVR2) Custom Song Converter.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional

from .beatmap_converter import CustomSongPackage, load_custom_map
from .beatsaver_client import (
    BeatSaverSearchResult,
    download_beatsaver_zip,
    search_beatsaver,
)
from .cli import _auto_default_game_dir
from .game_manager import PS5BeatSaberManager, SongSlotInfo


BG_DARK = "#0d1017"
BG_PANEL = "#151a26"
BG_INPUT = "#1d2436"
FG_TEXT = "#eef2ff"
FG_MUTED = "#94a3b8"
ACCENT_BLUE = "#00a8ff"
ACCENT_RED = "#ff2a54"
ACCENT_GREEN = "#10b981"


def _enable_windows_dpi_awareness() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


class ConverterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PS5 Beat Saber (PSVR2) — Custom Song Converter")
        self.root.geometry("1260x840")
        self.root.minsize(1040, 720)
        self.root.configure(bg=BG_DARK)

        self.msg_queue: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        self.busy = False

        self.manager: Optional[PS5BeatSaberManager] = None
        self.loaded_pkg: Optional[CustomSongPackage] = None
        self.loaded_source_path: Optional[str] = None
        self.search_results: Dict[str, BeatSaverSearchResult] = {}
        self.displayed_slots: Dict[str, SongSlotInfo] = {}

        self._setup_styles()
        self._build_ui()

        default_dir = _auto_default_game_dir()
        if default_dir:
            self.game_dir_var.set(default_dir)
            self._load_game_folder(default_dir)

        self.root.after(80, self._poll_queue)

    def _setup_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")

        style.configure(".", background=BG_DARK, foreground=FG_TEXT, font=("Segoe UI", 10))
        style.configure("TFrame", background=BG_DARK)
        style.configure("Card.TFrame", background=BG_PANEL)
        style.configure("TLabel", background=BG_DARK, foreground=FG_TEXT)
        style.configure("Card.TLabel", background=BG_PANEL, foreground=FG_TEXT)
        style.configure("Muted.TLabel", background=BG_PANEL, foreground=FG_MUTED, font=("Segoe UI", 9))
        style.configure("Header.TLabel", background=BG_DARK, foreground=ACCENT_BLUE, font=("Segoe UI Semibold", 15))
        style.configure("SubHeader.TLabel", background=BG_PANEL, foreground=ACCENT_BLUE, font=("Segoe UI Semibold", 11))

        style.configure(
            "TNotebook", background=BG_DARK, borderwidth=0
        )
        style.configure(
            "TNotebook.Tab",
            background=BG_PANEL,
            foreground=FG_MUTED,
            padding=(16, 8),
            font=("Segoe UI Semibold", 10),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", ACCENT_BLUE)],
            foreground=[("selected", "#ffffff")],
        )

        style.configure(
            "TEntry",
            fieldbackground=BG_INPUT,
            foreground=FG_TEXT,
            insertcolor=FG_TEXT,
            borderwidth=1,
            padding=5,
        )
        style.configure(
            "TCombobox",
            fieldbackground=BG_INPUT,
            background=BG_PANEL,
            foreground=FG_TEXT,
            arrowcolor=FG_TEXT,
            padding=4,
        )
        style.map("TCombobox", fieldbackground=[("readonly", BG_INPUT)], foreground=[("readonly", FG_TEXT)])

        style.configure(
            "TCheckbutton",
            background=BG_PANEL,
            foreground=FG_TEXT,
            font=("Segoe UI", 10),
        )
        style.map("TCheckbutton", background=[("active", BG_PANEL)])

        style.configure(
            "TButton",
            background=BG_INPUT,
            foreground=FG_TEXT,
            padding=(12, 6),
            font=("Segoe UI Semibold", 9),
            borderwidth=0,
        )
        style.map("TButton", background=[("active", "#2a3550"), ("disabled", "#151924")])

        style.configure(
            "Primary.TButton",
            background=ACCENT_BLUE,
            foreground="#ffffff",
            padding=(16, 9),
            font=("Segoe UI Bold", 10),
        )
        style.map("Primary.TButton", background=[("active", "#0088d1"), ("disabled", "#1e3a52")])

        style.configure(
            "Danger.TButton",
            background=ACCENT_RED,
            foreground="#ffffff",
            padding=(12, 7),
            font=("Segoe UI Semibold", 9),
        )
        style.map("Danger.TButton", background=[("active", "#d91b42"), ("disabled", "#4a1c28")])

        style.configure(
            "Treeview",
            background=BG_INPUT,
            fieldbackground=BG_INPUT,
            foreground=FG_TEXT,
            rowheight=26,
            borderwidth=0,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Treeview.Heading",
            background="#232c42",
            foreground=ACCENT_BLUE,
            font=("Segoe UI Semibold", 9),
            padding=5,
        )
        style.map("Treeview", background=[("selected", "#006699")], foreground=[("selected", "#ffffff")])

    def _build_ui(self) -> None:
        # Top Header & Game Dump Selector
        top = ttk.Frame(self.root, padding=(14, 10))
        top.pack(fill="x")

        title_row = ttk.Frame(top)
        title_row.pack(fill="x", pady=(0, 6))
        ttk.Label(
            title_row,
            text="BEAT SABER PS5 (PSVR2) — CUSTOM SONG CONVERTER",
            style="Header.TLabel",
        ).pack(side="left")
        self.game_badge_var = tk.StringVar(value="No PS5 game folder loaded")
        ttk.Label(title_row, textvariable=self.game_badge_var, foreground=ACCENT_GREEN, font=("Segoe UI Semibold", 10)).pack(
            side="right"
        )

        path_row = ttk.Frame(top)
        path_row.pack(fill="x")
        ttk.Label(path_row, text="PS5 Game Dump Folder:").pack(side="left", padx=(0, 8))
        self.game_dir_var = tk.StringVar()
        ttk.Entry(path_row, textvariable=self.game_dir_var).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(path_row, text="Browse...", command=self._on_browse_game_dir).pack(side="left", padx=(0, 6))
        ttk.Button(path_row, text="Reload", command=lambda: self._load_game_folder(self.game_dir_var.get())).pack(
            side="left"
        )

        # Notebook Tabs
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=14, pady=(4, 6))

        self.tab_convert = ttk.Frame(self.notebook, padding=8)
        self.tab_manage = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.tab_convert, text="  1. Add / Convert Custom Song  ")
        self.notebook.add(self.tab_manage, text="  2. Installed Songs, Restore & PS5 FTP  ")

        self._build_convert_tab()
        self._build_manage_tab()

        # Bottom Log Console
        log_frame = ttk.Frame(self.root, style="Card.TFrame", padding=(10, 6))
        log_frame.pack(fill="x", padx=14, pady=(0, 10))
        hdr = ttk.Frame(log_frame, style="Card.TFrame")
        hdr.pack(fill="x")
        ttk.Label(hdr, text="Activity Log", style="Muted.TLabel").pack(side="left")
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(hdr, textvariable=self.status_var, style="Muted.TLabel").pack(side="right")

        self.log_text = tk.Text(
            log_frame,
            height=5,
            bg="#090b10",
            fg="#cbd5e1",
            font=("Consolas", 9),
            relief="flat",
            state="disabled",
        )
        self.log_text.pack(fill="x", pady=(4, 0))

    def _build_convert_tab(self) -> None:
        paned = tk.PanedWindow(self.tab_convert, orient="horizontal", bg=BG_DARK, sashwidth=6, bd=0)
        paned.pack(fill="both", expand=True)

        # LEFT PANE: Custom Song Source (BeatSaver Search / ID / Local Zip)
        left = ttk.Frame(paned, style="Card.TFrame", padding=12)
        paned.add(left, width=590)

        ttk.Label(left, text="Step 1 — Select Custom Song (BeatSaver or Local File)", style="SubHeader.TLabel").pack(
            anchor="w", pady=(0, 8)
        )

        # Search BeatSaver row
        srow = ttk.Frame(left, style="Card.TFrame")
        srow.pack(fill="x", pady=(0, 6))
        self.search_var = tk.StringVar()
        s_entry = ttk.Entry(srow, textvariable=self.search_var)
        s_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        s_entry.bind("<Return>", lambda _e: self._on_search_beatsaver())

        self.sort_var = tk.StringVar(value="Relevance")
        ttk.Combobox(
            srow, textvariable=self.sort_var, values=["Relevance", "Rating", "Latest"], width=10, state="readonly"
        ).pack(side="left", padx=(0, 6))
        ttk.Button(srow, text="Search BeatSaver", command=self._on_search_beatsaver).pack(side="left")

        # Search results Treeview
        tree_wrap = ttk.Frame(left, style="Card.TFrame")
        tree_wrap.pack(fill="both", expand=True, pady=(0, 8))
        cols = ("id", "song", "artist", "bpm", "score", "diffs")
        self.search_tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", height=9)
        self.search_tree.heading("id", text="ID")
        self.search_tree.heading("song", text="Song Title")
        self.search_tree.heading("artist", text="Artist")
        self.search_tree.heading("bpm", text="BPM")
        self.search_tree.heading("score", text="Rating")
        self.search_tree.heading("diffs", text="Difficulties")
        self.search_tree.column("id", width=62, anchor="w")
        self.search_tree.column("song", width=165, anchor="w")
        self.search_tree.column("artist", width=115, anchor="w")
        self.search_tree.column("bpm", width=52, anchor="e")
        self.search_tree.column("score", width=58, anchor="e")
        self.search_tree.column("diffs", width=120, anchor="w")
        vsb1 = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.search_tree.yview)
        self.search_tree.configure(yscrollcommand=vsb1.set)
        self.search_tree.pack(side="left", fill="both", expand=True)
        vsb1.pack(side="right", fill="y")
        self.search_tree.bind("<Double-1>", lambda _e: self._on_download_selected_search())

        dl_row = ttk.Frame(left, style="Card.TFrame")
        dl_row.pack(fill="x", pady=(0, 8))
        ttk.Button(
            dl_row, text="Download & Load Highlighted Song", command=self._on_download_selected_search
        ).pack(side="left", padx=(0, 8))

        ttk.Label(dl_row, text="or Map ID/URL:", style="Card.TLabel").pack(side="left", padx=(4, 4))
        self.direct_id_var = tk.StringVar()
        ttk.Entry(dl_row, textvariable=self.direct_id_var, width=14).pack(side="left", padx=(0, 4))
        ttk.Button(dl_row, text="Fetch", command=self._on_fetch_direct_id).pack(side="left")

        local_row = ttk.Frame(left, style="Card.TFrame")
        local_row.pack(fill="x", pady=(0, 10))
        ttk.Button(local_row, text="Load Local BeatSaver .zip...", command=self._on_browse_zip).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(local_row, text="Load Extracted Map Folder...", command=self._on_browse_map_dir).pack(side="left")

        # Loaded Map Summary Card
        info_box = tk.Frame(left, bg="#0f1420", highlightthickness=1, highlightbackground="#26324d", padx=10, pady=8)
        info_box.pack(fill="x")
        self.loaded_summary_var = tk.StringVar(
            value="No custom song loaded yet.\nSearch BeatSaver above or pick a local BeatSaver .zip file."
        )
        tk.Label(
            info_box,
            textvariable=self.loaded_summary_var,
            bg="#0f1420",
            fg=ACCENT_GREEN,
            justify="left",
            anchor="w",
            font=("Segoe UI Semibold", 9),
        ).pack(fill="x")

        # RIGHT PANE: Target PS5 Song Slot + Options + Install Button
        right = ttk.Frame(paned, style="Card.TFrame", padding=12)
        paned.add(right, width=620)

        ttk.Label(right, text="Step 2 — Pick Target PS5 Song Slot to Replace", style="SubHeader.TLabel").pack(
            anchor="w", pady=(0, 6)
        )

        frow = ttk.Frame(right, style="Card.TFrame")
        frow.pack(fill="x", pady=(0, 6))
        self.free_only_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frow,
            text="Free Base-Game Packs Only (50 Unlocked Songs)",
            variable=self.free_only_var,
            command=self._refresh_slot_tree,
        ).pack(side="left", padx=(0, 10))

        ttk.Label(frow, text="Pack:", style="Card.TLabel").pack(side="left", padx=(0, 4))
        self.pack_filter_var = tk.StringVar(value="All Packs")
        self.pack_combo = ttk.Combobox(frow, textvariable=self.pack_filter_var, width=16, state="readonly")
        self.pack_combo.pack(side="left", padx=(0, 8))
        self.pack_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_slot_tree())

        self.slot_search_var = tk.StringVar()
        self.slot_search_var.trace_add("write", lambda *_: self._refresh_slot_tree())
        ttk.Entry(frow, textvariable=self.slot_search_var, width=14).pack(side="left", fill="x", expand=True)

        slot_wrap = ttk.Frame(right, style="Card.TFrame")
        slot_wrap.pack(fill="both", expand=True, pady=(0, 8))
        scols = ("pack", "stock", "modes", "status")
        self.slot_tree = ttk.Treeview(slot_wrap, columns=scols, show="headings", height=9)
        self.slot_tree.heading("pack", text="Music Pack")
        self.slot_tree.heading("stock", text="Original Slot")
        self.slot_tree.heading("modes", text="Modes")
        self.slot_tree.heading("status", text="Current In-Game Song")
        self.slot_tree.column("pack", width=155, anchor="w")
        self.slot_tree.column("stock", width=145, anchor="w")
        self.slot_tree.column("modes", width=120, anchor="w")
        self.slot_tree.column("status", width=175, anchor="w")
        vsb2 = ttk.Scrollbar(slot_wrap, orient="vertical", command=self.slot_tree.yview)
        self.slot_tree.configure(yscrollcommand=vsb2.set)
        self.slot_tree.pack(side="left", fill="both", expand=True)
        vsb2.pack(side="right", fill="y")

        # Step 3 — Metadata & Conversion Options
        ttk.Label(right, text="Step 3 — In-Game Display & Party Playability Options", style="SubHeader.TLabel").pack(
            anchor="w", pady=(4, 6)
        )

        meta_grid = ttk.Frame(right, style="Card.TFrame")
        meta_grid.pack(fill="x", pady=(0, 6))
        ttk.Label(meta_grid, text="Song Title:", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=2)
        self.override_title_var = tk.StringVar()
        ttk.Entry(meta_grid, textvariable=self.override_title_var, width=28).grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=2)

        ttk.Label(meta_grid, text="Artist:", style="Card.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 6), pady=2)
        self.override_artist_var = tk.StringVar()
        ttk.Entry(meta_grid, textvariable=self.override_artist_var, width=22).grid(row=0, column=3, sticky="ew", pady=2)
        meta_grid.columnconfigure(1, weight=1)
        meta_grid.columnconfigure(3, weight=1)

        self.auto_diffs_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            right,
            text="Auto-generate missing Easy / Normal / Hard / Expert / Expert+ charts (great for friends & family)",
            variable=self.auto_diffs_var,
        ).pack(anchor="w", pady=1)

        self.auto_modes_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            right,
            text="Auto-generate OneSaber / NoArrows / 90° / 360° charts on multi-mode slots",
            variable=self.auto_modes_var,
        ).pack(anchor="w", pady=(1, 8))

        self.install_btn = ttk.Button(
            right,
            text="⚡ Convert & Install Custom Song into PS5 Beat Saber",
            style="Primary.TButton",
            command=self._on_install_clicked,
        )
        self.install_btn.pack(fill="x", pady=(2, 0))

    def _build_manage_tab(self) -> None:
        top_card = ttk.Frame(self.tab_manage, style="Card.TFrame", padding=12)
        top_card.pack(fill="both", expand=True, pady=(0, 8))

        hdr = ttk.Frame(top_card, style="Card.TFrame")
        hdr.pack(fill="x", pady=(0, 8))
        ttk.Label(hdr, text="Installed Custom Songs on PS5 Dump", style="SubHeader.TLabel").pack(side="left")

        btn_bar = ttk.Frame(hdr, style="Card.TFrame")
        btn_bar.pack(side="right")
        ttk.Button(btn_bar, text="Verify All Files", command=self._on_verify_clicked).pack(side="left", padx=4)
        ttk.Button(btn_bar, text="Export Printable Party Song List", command=self._on_export_html).pack(
            side="left", padx=4
        )
        ttk.Button(btn_bar, text="Restore Selected Slot", command=self._on_restore_selected).pack(side="left", padx=4)
        ttk.Button(
            btn_bar, text="Restore ALL to Original Stock", style="Danger.TButton", command=self._on_restore_all
        ).pack(side="left", padx=(4, 0))

        icols = ("slot", "pack", "custom_title", "custom_artist", "bpm", "len", "stock_title")
        self.installed_tree = ttk.Treeview(top_card, columns=icols, show="headings", height=11)
        self.installed_tree.heading("slot", text="Slot ID")
        self.installed_tree.heading("pack", text="Music Pack")
        self.installed_tree.heading("custom_title", text="Custom Song Title")
        self.installed_tree.heading("custom_artist", text="Artist")
        self.installed_tree.heading("bpm", text="BPM")
        self.installed_tree.heading("len", text="Length")
        self.installed_tree.heading("stock_title", text="Replaced Stock Song")
        self.installed_tree.column("slot", width=140)
        self.installed_tree.column("pack", width=170)
        self.installed_tree.column("custom_title", width=230)
        self.installed_tree.column("custom_artist", width=180)
        self.installed_tree.column("bpm", width=70, anchor="e")
        self.installed_tree.column("len", width=75, anchor="e")
        self.installed_tree.column("stock_title", width=190)
        self.installed_tree.pack(fill="both", expand=True)

        # Optional PS5 FTP Sync Card
        ftp_card = ttk.Frame(self.tab_manage, style="Card.TFrame", padding=12)
        ftp_card.pack(fill="x")
        ttk.Label(
            ftp_card,
            text="Optional — Direct FTP Push to Modded PS5 (etaHEN / ftpsrv — sends only modified delta, skips 2.9 GB copy)",
            style="SubHeader.TLabel",
        ).pack(anchor="w", pady=(0, 6))

        frow = ttk.Frame(ftp_card, style="Card.TFrame")
        frow.pack(fill="x")
        ttk.Label(frow, text="PS5 IP:", style="Card.TLabel").pack(side="left", padx=(0, 4))
        self.ftp_ip_var = tk.StringVar(value="192.168.1.100")
        ttk.Entry(frow, textvariable=self.ftp_ip_var, width=15).pack(side="left", padx=(0, 10))

        ttk.Label(frow, text="Port:", style="Card.TLabel").pack(side="left", padx=(0, 4))
        self.ftp_port_var = tk.StringVar(value="2121")
        ttk.Entry(frow, textvariable=self.ftp_port_var, width=7).pack(side="left", padx=(0, 10))

        ttk.Label(frow, text="PS5 App Path:", style="Card.TLabel").pack(side="left", padx=(0, 4))
        self.ftp_dir_var = tk.StringVar(value="/mnt/ext0/PPSA15035-app")
        ttk.Entry(frow, textvariable=self.ftp_dir_var).pack(side="left", fill="x", expand=True, padx=(0, 10))

        ttk.Button(frow, text="Push Updates to PS5 via FTP", style="Primary.TButton", command=self._on_ftp_push).pack(
            side="left"
        )

    # ==========================================================================
    # Async Worker & Logging Helpers
    # ==========================================================================

    def log(self, msg: str) -> None:
        self.msg_queue.put(("log", msg))

    def _append_log_ui(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.status_var.set(msg[:95])

    def _poll_queue(self) -> None:
        while True:
            try:
                kind, payload = self.msg_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._append_log_ui(str(payload))
            elif kind == "done":
                self.busy = False
                self.install_btn.configure(state="normal")
                cb = payload  # type: ignore[assignment]
                if callable(cb):
                    cb()
            elif kind == "error":
                self.busy = False
                self.install_btn.configure(state="normal")
                self._append_log_ui(f"ERROR: {payload}")
                messagebox.showerror("Error", str(payload))
        self.root.after(80, self._poll_queue)

    def _run_bg(self, work_fn: Callable[[], object], on_done: Callable[[object], None]) -> None:
        if self.busy:
            messagebox.showinfo("Busy", "Please wait for the current operation to finish.")
            return
        self.busy = True
        self.install_btn.configure(state="disabled")

        def _target() -> None:
            try:
                res = work_fn()
                self.msg_queue.put(("done", lambda: on_done(res)))
            except Exception as exc:
                self.msg_queue.put(("error", str(exc)))

        threading.Thread(target=_target, daemon=True).start()

    # ==========================================================================
    # Game Folder & Slot Table Management
    # ==========================================================================

    def _on_browse_game_dir(self) -> None:
        chosen = filedialog.askdirectory(title="Select PS5 Beat Saber Folder (e.g. PPSA15035-app)")
        if chosen:
            self.game_dir_var.set(chosen)
            self._load_game_folder(chosen)

    def _load_game_folder(self, folder: str) -> None:
        if not folder.strip():
            return
        try:
            self.manager = PS5BeatSaberManager(folder)
        except Exception as exc:
            self.game_badge_var.set("Invalid PS5 Beat Saber Folder")
            messagebox.showerror("Could Not Load PS5 Dump", str(exc))
            return

        self.game_dir_var.set(self.manager.app_root)
        self._refresh_all_views()
        self._append_log_ui(
            f"Loaded PS5 Beat Saber {self.manager.title_id} (v{self.manager.content_version}) at {self.manager.app_root}"
        )

    def _refresh_all_views(self) -> None:
        if not self.manager:
            return
        inst_cnt = len(self.manager.state.get("installed_slots", {}))
        free_cnt = sum(1 for s in self.manager.slots.values() if s.is_free_base_game)
        self.game_badge_var.set(
            f"✔ {self.manager.title_id} v{self.manager.content_version}  |  {free_cnt} Free Slots  |  {inst_cnt} Custom Installed"
        )

        # Populate Pack Filter Dropdown
        packs = sorted(
            {(s.pack_id, s.pack_name, s.is_free_base_game) for s in self.manager.slots.values()},
            key=lambda x: (0 if x[2] else 1, x[1]),
        )
        pack_labels = ["All Packs"] + [p[0] for p in packs]
        self.pack_combo["values"] = pack_labels
        if self.pack_filter_var.get() not in pack_labels:
            self.pack_filter_var.set("All Packs")

        self._refresh_slot_tree()
        self._refresh_installed_tree()

    def _refresh_slot_tree(self) -> None:
        self.slot_tree.delete(*self.slot_tree.get_children())
        self.displayed_slots.clear()
        if not self.manager:
            return

        pfilter = self.pack_filter_var.get()
        pack_id = None if pfilter == "All Packs" else pfilter
        slots = self.manager.list_slots(free_only=self.free_only_var.get(), pack_id=pack_id)
        q = self.slot_search_var.get().strip().lower()

        for s in slots:
            if q and not (
                q in s.stock_song_name.lower()
                or q in s.current_song_name.lower()
                or q in s.pack_name.lower()
                or q in s.level_id.lower()
            ):
                continue
            status = f"★ {s.current_song_name} ({s.current_song_author})" if s.is_custom else "Stock"
            pack_disp = s.pack_name if s.is_free_base_game else f"[DLC] {s.pack_name}"
            modes_disp = ", ".join(m.replace("Degree", "°") for m in s.modes)
            self.slot_tree.insert(
                "", "end", iid=s.level_id, values=(pack_disp, s.stock_song_name, modes_disp, status)
            )
            self.displayed_slots[s.level_id] = s

    def _refresh_installed_tree(self) -> None:
        self.installed_tree.delete(*self.installed_tree.get_children())
        if not self.manager:
            return
        installed = list(self.manager.state.get("installed_slots", {}).values())
        installed.sort(key=lambda x: (x["pack_name"], x["custom_song_name"]))
        for item in installed:
            mins, secs = divmod(int(round(item.get("duration_seconds", 0))), 60)
            self.installed_tree.insert(
                "",
                "end",
                iid=item["level_id"],
                values=(
                    item["level_id"],
                    item["pack_name"],
                    item["custom_song_name"],
                    item["custom_song_author"],
                    item["bpm"],
                    f"{mins}:{secs:02d}",
                    item["stock_song_name"],
                ),
            )

    # ==========================================================================
    # Song Selection & Conversion Actions
    # ==========================================================================

    def _set_loaded_package(self, pkg: CustomSongPackage, source_path: str) -> None:
        self.loaded_pkg = pkg
        self.loaded_source_path = source_path
        self.override_title_var.set(pkg.song_name)
        self.override_artist_var.set(pkg.song_author_name)
        self.loaded_summary_var.set(
            f"✔ Loaded: {pkg.song_name} — {pkg.song_author_name} (Mapped by {pkg.level_author_name or 'Community'})\n"
            f"   BPM: {pkg.bpm:.1f}   |   Native Charts: {pkg.native_summary()}"
        )
        self._append_log_ui(f"Ready to convert '{pkg.song_name}' by '{pkg.song_author_name}'. Pick a PS5 slot on the right!")

    def _on_search_beatsaver(self) -> None:
        q = self.search_var.get().strip()
        if not q:
            return
        sort_by = self.sort_var.get()
        self.log(f"Searching BeatSaver for '{q}'...")

        def _work() -> List[BeatSaverSearchResult]:
            return search_beatsaver(q, sort_order=sort_by)

        def _done(res: object) -> None:
            items: List[BeatSaverSearchResult] = res  # type: ignore[assignment]
            self.search_tree.delete(*self.search_tree.get_children())
            self.search_results.clear()
            for r in items:
                self.search_results[r.map_id] = r
                self.search_tree.insert(
                    "",
                    "end",
                    iid=r.map_id,
                    values=(
                        r.map_id,
                        r.song_name,
                        r.song_author_name,
                        f"{r.bpm:.0f}",
                        f"{r.score_pct:.0f}%",
                        r.difficulties_summary,
                    ),
                )
            self._append_log_ui(f"Found {len(items)} BeatSaver maps for '{q}'.")

        self._run_bg(_work, _done)

    def _on_download_selected_search(self) -> None:
        sel = self.search_tree.selection()
        if not sel:
            messagebox.showinfo("Select a Song", "Highlight a BeatSaver result in the table first.")
            return
        item = self.search_results[sel[0]]
        self._download_and_load(item)

    def _on_fetch_direct_id(self) -> None:
        val = self.direct_id_var.get().strip()
        if not val:
            return
        self._download_and_load(val)

    def _download_and_load(self, target: str | BeatSaverSearchResult) -> None:
        label = target.song_name if isinstance(target, BeatSaverSearchResult) else target
        self.log(f"Downloading '{label}' from BeatSaver...")

        def _work() -> tuple[CustomSongPackage, str]:
            zpath = download_beatsaver_zip(target)
            pkg = load_custom_map(zpath)
            return pkg, zpath

        def _done(res: object) -> None:
            pkg, zpath = res  # type: ignore[misc]
            self._set_loaded_package(pkg, zpath)

        self._run_bg(_work, _done)

    def _on_browse_zip(self) -> None:
        path = filedialog.askopenfilename(
            title="Select BeatSaver Custom Song .zip",
            filetypes=[("BeatSaver Map Zip", "*.zip"), ("All Files", "*.*")],
        )
        if not path:
            return
        try:
            pkg = load_custom_map(path)
            self._set_loaded_package(pkg, path)
        except Exception as exc:
            messagebox.showerror("Invalid Map Zip", str(exc))

    def _on_browse_map_dir(self) -> None:
        folder = filedialog.askdirectory(title="Select Extracted BeatSaver Map Folder (containing Info.dat)")
        if not folder:
            return
        try:
            pkg = load_custom_map(folder)
            self._set_loaded_package(pkg, folder)
        except Exception as exc:
            messagebox.showerror("Invalid Map Folder", str(exc))

    def _on_install_clicked(self) -> None:
        if not self.manager:
            messagebox.showwarning("No PS5 Game Loaded", "Select your PS5 Beat Saber dump folder first.")
            return
        if not self.loaded_pkg:
            messagebox.showwarning("No Custom Song Loaded", "Load a BeatSaver map or local .zip on the left first.")
            return
        sel = self.slot_tree.selection()
        if not sel:
            messagebox.showwarning("No PS5 Slot Selected", "Click a target PS5 song slot in Step 2 on the right.")
            return

        slot_id = sel[0]
        slot = self.manager.slots[slot_id]
        title_ov = self.override_title_var.get().strip() or self.loaded_pkg.song_name
        artist_ov = self.override_artist_var.get().strip() or self.loaded_pkg.song_author_name
        auto_diffs = self.auto_diffs_var.get()
        auto_modes = self.auto_modes_var.get()
        pkg = self.loaded_pkg

        if not slot.is_free_base_game:
            ok = messagebox.askyesno(
                "DLC Slot Selected",
                f"'{slot.stock_song_name}' belongs to the '{slot.pack_name}' DLC pack.\n\n"
                "It will only be playable if your PS5 already owns that DLC pack.\n"
                "(Tip: The 50 songs in OST Vol. 1–6, Extras, and Camellia are unlocked on every PS5!)\n\n"
                "Continue replacing this DLC slot?",
            )
            if not ok:
                return

        def _work() -> SongSlotInfo:
            assert self.manager is not None
            return self.manager.install_custom_song(
                map_source=pkg,
                target_level_id=slot_id,
                song_name_override=title_ov,
                song_author_override=artist_ov,
                auto_generate_difficulties=auto_diffs,
                auto_generate_modes=auto_modes,
                log_fn=self.log,
            )

        def _done(res: object) -> None:
            updated: SongSlotInfo = res  # type: ignore[assignment]
            self._refresh_all_views()
            self.slot_tree.selection_set(updated.level_id)
            messagebox.showinfo(
                "Custom Song Installed!",
                f"Successfully converted & installed:\n\n"
                f"  Song: {updated.current_song_name}\n"
                f"  Artist: {updated.current_song_author}\n"
                f"  Pack: {updated.pack_name} (replaces '{updated.stock_song_name}')\n\n"
                f"All binary checks (FSB5 audio, UTF-16LE V3.3.0 charts, and 8-byte Unity alignment) passed!",
            )

        self._run_bg(_work, _done)

    # ==========================================================================
    # Manage / Restore / Export / FTP Actions
    # ==========================================================================

    def _on_restore_selected(self) -> None:
        if not self.manager:
            return
        sel = self.installed_tree.selection()
        if not sel:
            messagebox.showinfo("Select a Song", "Select an installed custom song to restore.")
            return
        lid = sel[0]
        slot = self.manager.slots[lid]
        if not messagebox.askyesno(
            "Restore Slot",
            f"Restore '{slot.stock_song_name}' ({slot.pack_name}) back to its original Beat Saber track?",
        ):
            return

        self._run_bg(
            lambda: self.manager.restore_slot(lid, log_fn=self.log),  # type: ignore[union-attr]
            lambda _: self._refresh_all_views(),
        )

    def _on_restore_all(self) -> None:
        if not self.manager:
            return
        if not messagebox.askyesno(
            "Restore All Songs",
            "Restore ALL modified PS5 Beat Saber files back to 100% original factory stock?",
        ):
            return
        self._run_bg(
            lambda: self.manager.restore_all(log_fn=self.log),  # type: ignore[union-attr]
            lambda _: self._refresh_all_views(),
        )

    def _on_verify_clicked(self) -> None:
        if not self.manager:
            return

        def _work() -> List[dict]:
            assert self.manager is not None
            return self.manager.verify_all_installed()

        def _done(res: object) -> None:
            items: List[dict] = res  # type: ignore[assignment]
            messagebox.showinfo(
                "Verification Passed",
                f"Verified {len(items)} installed custom song(s).\n"
                "All Unity SerializedFiles, FSB5 PCM16 streams, and V3.3.0 beatmaps are 100% valid.",
            )

        self._run_bg(_work, _done)

    def _on_export_html(self) -> None:
        if not self.manager:
            return
        out = filedialog.asksaveasfilename(
            title="Save Printable Party Song List",
            defaultextension=".html",
            initialfile="PS5_Beat_Saber_Custom_Songs.html",
            filetypes=[("HTML Page", "*.html")],
        )
        if not out:
            return
        saved = self.manager.export_song_list_html(out)
        self._append_log_ui(f"Exported printable song list to {saved}")
        webbrowser.open(f"file:///{os.path.abspath(saved).replace(os.sep, '/')}")

    def _on_ftp_push(self) -> None:
        if not self.manager:
            return
        ip = self.ftp_ip_var.get().strip()
        port = int(self.ftp_port_var.get().strip() or "2121")
        rdir = self.ftp_dir_var.get().strip()
        if not ip or not rdir:
            messagebox.showwarning("Missing FTP Settings", "Enter your PS5 IP address and remote app path.")
            return

        self._run_bg(
            lambda: self.manager.deploy_via_ftp(ip, port, rdir, log_fn=self.log),  # type: ignore[union-attr]
            lambda _: messagebox.showinfo("FTP Push Complete", f"Successfully pushed updated files to {ip}:{port}!"),
        )


def launch_gui() -> None:
    _enable_windows_dpi_awareness()
    root = tk.Tk()
    ConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    launch_gui()
