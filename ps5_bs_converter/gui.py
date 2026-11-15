"""
Modern Neon Studio Desktop GUI for PS5 Beat Saber (PSVR2) Custom Song Converter (1.12 & 1.37).
"""

from __future__ import annotations

import io
import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import colorchooser, filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageTk

from .auto_mapper import (
    STYLE_PRESETS,
    AudioBeatAnalysis,
    export_package_to_beatsaver_zip,
    generate_track_from_audio_file,
    parse_title_artist_from_filename,
)
from .beatmap_converter import CustomSongPackage, load_custom_map
from .beatsaver_client import (
    BeatSaverSearchResult,
    download_beatsaver_zip,
    search_beatsaver,
)
from .cli import _auto_default_game_dir
from .game_manager import (
    COLOR_THEME_PRESETS,
    PS5BeatSaberManager,
    SongSlotInfo,
    find_ps5_game_root,
)
from .stage_modder import STAGE_MESH_TARGETS


# Beat Saber Dual-Neon Palette
BG_ROOT = "#070a10"
BG_SIDEBAR = "#0b101b"
BG_CARD = "#111827"
BG_CARD_ALT = "#172033"
BG_INPUT = "#1b253b"
BORDER_SUBTLE = "#24324d"
FG_PRIMARY = "#f1f5f9"
FG_SECONDARY = "#94a3b8"
FG_DIM = "#64748b"
SABER_BLUE = "#00b4ff"
SABER_BLUE_HOVER = "#0090d4"
SABER_RED = "#ff244c"
SABER_RED_HOVER = "#d9193c"
ACCENT_EMERALD = "#10b981"
ACCENT_AMBER = "#f59e0b"


def _enable_windows_dpi_awareness() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def _discover_nearby_dumps() -> List[str]:
    here = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
    ws_parent = os.path.dirname(here)
    bs_folder = os.path.join(ws_parent, "Beat Saber for the PS5")
    found: List[str] = []
    seen_norm = set()
    if os.path.isdir(bs_folder):
        for entry in sorted(os.listdir(bs_folder)):
            if entry.startswith(("_", "[")):
                continue
            sub = os.path.join(bs_folder, entry)
            if os.path.isdir(sub):
                try:
                    root = find_ps5_game_root(sub)
                    nk = os.path.normcase(os.path.abspath(root))
                    if nk not in seen_norm:
                        seen_norm.add(nk)
                        found.append(root)
                except FileNotFoundError:
                    continue
    return found


def _read_clipboard_file_path(root: tk.Tk) -> Optional[str]:
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            CF_HDROP = 15
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
            shell32.DragQueryFileW.restype = wintypes.UINT

            if user32.IsClipboardFormatAvailable(CF_HDROP) and user32.OpenClipboard(None):
                try:
                    hdrop = user32.GetClipboardData(CF_HDROP)
                    if hdrop:
                        count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
                        if count > 0:
                            length = shell32.DragQueryFileW(hdrop, 0, None, 0)
                            buf = ctypes.create_unicode_buffer(length + 1)
                            shell32.DragQueryFileW(hdrop, 0, buf, length + 1)
                            if os.path.isfile(buf.value):
                                return buf.value
                finally:
                    user32.CloseClipboard()
        except Exception:
            pass

    try:
        txt = root.clipboard_get().strip().strip('"').strip("'")
        if txt and os.path.exists(txt):
            return txt
    except Exception:
        pass
    return None


def _hex_to_rgba(hex_color: str) -> Tuple[float, float, float, float]:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (1.0, 1.0, 1.0, 1.0)
    return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0, 1.0)


def _rgba_to_hex(rgba: Tuple[float, float, float, float]) -> str:
    r = max(0, min(255, int(round(rgba[0] * 255))))
    g = max(0, min(255, int(round(rgba[1] * 255))))
    b = max(0, min(255, int(round(rgba[2] * 255))))
    return f"#{r:02x}{g:02x}{b:02x}"


def _make_default_cover_photo(size: int = 72) -> ImageTk.PhotoImage:
    img = Image.new("RGBA", (size, size), (13, 18, 32, 255))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((1, 1, size - 2, size - 2), radius=10, outline=(36, 50, 77, 255), width=2)
    # Draw stylized Red & Blue Beat Saber cubes/slashes
    draw.rounded_rectangle((12, 18, 34, 40), radius=5, fill=(255, 36, 76, 230))
    draw.rounded_rectangle((38, 32, 60, 54), radius=5, fill=(0, 180, 255, 230))
    draw.line((10, 58, 62, 14), fill=(255, 255, 255, 210), width=2)
    return ImageTk.PhotoImage(img)


class ConverterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PS5 Beat Saber Studio — Custom Songs, Auto-Mapper & Stage Modder (v1.12 + v1.37)")
        self.root.geometry("1340x880")
        self.root.minsize(1120, 760)
        self.root.configure(bg=BG_ROOT)

        self.msg_queue: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        self.busy = False

        self.manager: Optional[PS5BeatSaberManager] = None
        self.loaded_pkg: Optional[CustomSongPackage] = None
        self.loaded_source_path: Optional[str] = None
        self.generated_pkg: Optional[CustomSongPackage] = None
        self.search_results: Dict[str, BeatSaverSearchResult] = {}
        self.displayed_slots: Dict[str, SongSlotInfo] = {}
        self.stage_label_to_id: Dict[str, str] = {}
        self._cover_photo_ref: Optional[ImageTk.PhotoImage] = None
        self._nav_buttons: Dict[str, Tuple[tk.Frame, tk.Frame, tk.Label]] = {}
        self._pages: Dict[str, tk.Frame] = {}

        self._setup_styles()
        self._build_shell()

        self.root.bind("<Control-v>", self._on_global_paste)

        default_dir = _auto_default_game_dir()
        if default_dir:
            self.game_dir_var.set(default_dir)
            self._load_game_folder(default_dir)

        self.root.after(80, self._poll_queue)

    # ==========================================================================
    # Theme & Custom Styling
    # ==========================================================================

    def _setup_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")

        style.configure(".", background=BG_ROOT, foreground=FG_PRIMARY, font=("Segoe UI", 10))
        style.configure("TFrame", background=BG_ROOT)
        style.configure("Card.TFrame", background=BG_CARD)
        style.configure("TLabel", background=BG_ROOT, foreground=FG_PRIMARY)
        style.configure("Card.TLabel", background=BG_CARD, foreground=FG_PRIMARY)
        style.configure("Muted.TLabel", background=BG_CARD, foreground=FG_SECONDARY, font=("Segoe UI", 9))
        style.configure("Section.TLabel", background=BG_CARD, foreground=SABER_BLUE, font=("Segoe UI Semibold", 11))

        style.configure(
            "TEntry",
            fieldbackground=BG_INPUT,
            foreground=FG_PRIMARY,
            insertcolor=FG_PRIMARY,
            bordercolor=BORDER_SUBTLE,
            lightcolor=BORDER_SUBTLE,
            darkcolor=BORDER_SUBTLE,
            borderwidth=1,
            padding=6,
        )
        style.configure(
            "TCombobox",
            fieldbackground=BG_INPUT,
            background=BG_CARD_ALT,
            foreground=FG_PRIMARY,
            arrowcolor=SABER_BLUE,
            bordercolor=BORDER_SUBTLE,
            padding=5,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", BG_INPUT)],
            foreground=[("readonly", FG_PRIMARY)],
            selectbackground=[("readonly", BG_INPUT)],
            selectforeground=[("readonly", FG_PRIMARY)],
        )

        style.configure("TCheckbutton", background=BG_CARD, foreground=FG_PRIMARY, font=("Segoe UI", 10))
        style.map("TCheckbutton", background=[("active", BG_CARD)])

        style.configure(
            "TButton",
            background=BG_CARD_ALT,
            foreground=FG_PRIMARY,
            bordercolor=BORDER_SUBTLE,
            borderwidth=1,
            padding=(12, 6),
            font=("Segoe UI Semibold", 9),
        )
        style.map(
            "TButton",
            background=[("active", "#23314d"), ("disabled", "#0f1522")],
            foreground=[("disabled", FG_DIM)],
        )

        style.configure(
            "Primary.TButton",
            background=SABER_BLUE,
            foreground="#ffffff",
            borderwidth=0,
            padding=(18, 10),
            font=("Segoe UI Bold", 10),
        )
        style.map(
            "Primary.TButton",
            background=[("active", SABER_BLUE_HOVER), ("disabled", "#163854")],
            foreground=[("disabled", "#7ca2bf")],
        )

        style.configure(
            "Danger.TButton",
            background=SABER_RED,
            foreground="#ffffff",
            borderwidth=0,
            padding=(13, 7),
            font=("Segoe UI Semibold", 9),
        )
        style.map("Danger.TButton", background=[("active", SABER_RED_HOVER), ("disabled", "#4a1625")])

        style.configure(
            "Treeview",
            background="#121929",
            fieldbackground="#121929",
            foreground=FG_PRIMARY,
            rowheight=28,
            borderwidth=0,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Treeview.Heading",
            background="#1b263d",
            foreground=SABER_BLUE,
            borderwidth=0,
            font=("Segoe UI Semibold", 9),
            padding=(8, 6),
        )
        style.map(
            "Treeview",
            background=[("selected", "#0284c7")],
            foreground=[("selected", "#ffffff")],
        )
        style.map("Treeview.Heading", background=[("active", "#243352")])

        style.configure(
            "Neon.Horizontal.TProgressbar",
            troughcolor=BG_ROOT,
            background=SABER_BLUE,
            bordercolor=BG_ROOT,
            lightcolor=SABER_BLUE,
            darkcolor=SABER_BLUE,
            thickness=4,
        )

    def _make_card(self, parent: tk.Widget, padx: int = 14, pady: int = 12) -> tk.Frame:
        card = tk.Frame(
            parent,
            bg=BG_CARD,
            highlightthickness=1,
            highlightbackground=BORDER_SUBTLE,
            padx=padx,
            pady=pady,
        )
        return card

    # ==========================================================================
    # App Shell: Dual-Neon Top Banner + Sidebar Rail + Content Workspace
    # ==========================================================================

    def _build_shell(self) -> None:
        # 1. Dual-Saber Top Accent Stripe (Red Left -> Cyan Right)
        accent_canvas = tk.Canvas(self.root, height=4, bg=BG_ROOT, highlightthickness=0)
        accent_canvas.pack(fill="x")

        def _draw_top_stripe(event: tk.Event) -> None:
            accent_canvas.delete("all")
            w = event.width
            mid = w // 2
            accent_canvas.create_rectangle(0, 0, mid, 4, fill=SABER_RED, outline="")
            accent_canvas.create_rectangle(mid, 0, w, 4, fill=SABER_BLUE, outline="")

        accent_canvas.bind("<Configure>", _draw_top_stripe)

        # 2. Top Studio Header Bar + KPI Pills + Quick Dump Selector
        header = tk.Frame(self.root, bg=BG_SIDEBAR, highlightthickness=1, highlightbackground="#172033", padx=18, pady=10)
        header.pack(fill="x")

        top_line = tk.Frame(header, bg=BG_SIDEBAR)
        top_line.pack(fill="x", pady=(0, 8))

        brand = tk.Frame(top_line, bg=BG_SIDEBAR)
        brand.pack(side="left")
        tk.Label(
            brand,
            text="BEAT SABER",
            bg=BG_SIDEBAR,
            fg=SABER_RED,
            font=("Segoe UI Black", 15),
        ).pack(side="left")
        tk.Label(
            brand,
            text=" PS5 STUDIO",
            bg=BG_SIDEBAR,
            fg=SABER_BLUE,
            font=("Segoe UI Black", 15),
        ).pack(side="left")
        tk.Label(
            brand,
            text="   PSVR2 Custom Song, Auto-Mapper & Stage Modder",
            bg=BG_SIDEBAR,
            fg=FG_SECONDARY,
            font=("Segoe UI", 10),
        ).pack(side="left", pady=(3, 0))

        # KPI Status Pills on the Right
        pills = tk.Frame(top_line, bg=BG_SIDEBAR)
        pills.pack(side="right")
        self.pill_engine_var = tk.StringVar(value="ENGINE: —")
        self.pill_slots_var = tk.StringVar(value="SLOTS: —")
        self.pill_installed_var = tk.StringVar(value="CUSTOM: 0")

        for var, fg_col, border_col in [
            (self.pill_engine_var, SABER_BLUE, "#0369a1"),
            (self.pill_slots_var, FG_PRIMARY, BORDER_SUBTLE),
            (self.pill_installed_var, ACCENT_EMERALD, "#047857"),
        ]:
            lbl = tk.Label(
                pills,
                textvariable=var,
                bg="#111a2e",
                fg=fg_col,
                font=("Segoe UI Semibold", 9),
                padx=10,
                pady=3,
                highlightthickness=1,
                highlightbackground=border_col,
            )
            lbl.pack(side="left", padx=(8, 0))

        # Game Dump Selector Row
        dump_row = tk.Frame(header, bg=BG_SIDEBAR)
        dump_row.pack(fill="x")
        tk.Label(
            dump_row,
            text="Active PS5 Game Copy:",
            bg=BG_SIDEBAR,
            fg=FG_SECONDARY,
            font=("Segoe UI Semibold", 9),
        ).pack(side="left", padx=(0, 8))

        nearby = _discover_nearby_dumps()
        self.game_dir_var = tk.StringVar()
        self.dump_combo = ttk.Combobox(dump_row, textvariable=self.game_dir_var, values=nearby)
        self.dump_combo.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.dump_combo.bind("<<ComboboxSelected>>", lambda _e: self._load_game_folder(self.game_dir_var.get()))

        ttk.Button(dump_row, text="Browse Folder...", command=self._on_browse_game_dir).pack(side="left", padx=(0, 6))
        ttk.Button(dump_row, text="Reload", command=lambda: self._load_game_folder(self.game_dir_var.get())).pack(
            side="left"
        )

        # 3. Indeterminate Progress Bar (Active During Background Tasks)
        self.progress = ttk.Progressbar(self.root, style="Neon.Horizontal.TProgressbar", mode="indeterminate")
        self.progress.pack(fill="x")

        # 4. Main Body: Left Sidebar Navigation Rail + Right Page Stack
        body = tk.Frame(self.root, bg=BG_ROOT)
        body.pack(fill="both", expand=True)

        sidebar = tk.Frame(body, bg=BG_SIDEBAR, width=225, highlightthickness=1, highlightbackground="#172033")
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        tk.Label(
            sidebar,
            text="WORKSPACE",
            bg=BG_SIDEBAR,
            fg=FG_DIM,
            font=("Segoe UI Bold", 8),
            anchor="w",
            padx=18,
            pady=12,
        ).pack(fill="x")

        nav_defs = [
            ("convert", "⚔   Install Songs"),
            ("maketrack", "🎹   Auto-Map Audio"),
            ("stages", "🌌   Stages & Colors"),
            ("manage", "📦   Library & PS5 FTP"),
        ]
        for key, label_txt in nav_defs:
            self._create_nav_item(sidebar, key, label_txt)

        # Quick Tip Card at bottom of Sidebar
        tip_box = tk.Frame(sidebar, bg="#0f172a", highlightthickness=1, highlightbackground="#1e293b", padx=12, pady=10)
        tip_box.pack(side="bottom", fill="x", padx=12, pady=14)
        tk.Label(
            tip_box,
            text="💡 Quick Paste Shortcut",
            bg="#0f172a",
            fg=SABER_BLUE,
            font=("Segoe UI Semibold", 9),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            tip_box,
            text="Copy any .mp3/.wav/.ogg or BeatSaver .zip in Explorer and press Ctrl+V anywhere!",
            bg="#0f172a",
            fg=FG_SECONDARY,
            font=("Segoe UI", 8),
            justify="left",
            wraplength=175,
            anchor="w",
        ).pack(fill="x", pady=(4, 0))

        # Content Stack + Bottom Log Drawer
        right_shell = tk.Frame(body, bg=BG_ROOT)
        right_shell.pack(side="left", fill="both", expand=True, padx=14, pady=12)

        self.page_container = tk.Frame(right_shell, bg=BG_ROOT)
        self.page_container.pack(fill="both", expand=True)

        for key, _ in nav_defs:
            frame = tk.Frame(self.page_container, bg=BG_ROOT)
            frame.place(relx=0, rely=0, relwidth=1, relheight=1)
            self._pages[key] = frame

        self._build_convert_page(self._pages["convert"])
        self._build_maketrack_page(self._pages["maketrack"])
        self._build_stages_page(self._pages["stages"])
        self._build_manage_page(self._pages["manage"])

        # Bottom Sleek Activity Log Drawer
        log_card = self._make_card(right_shell, padx=12, pady=8)
        log_card.pack(fill="x", pady=(10, 0))
        lhdr = tk.Frame(log_card, bg=BG_CARD)
        lhdr.pack(fill="x")
        tk.Label(lhdr, text="CONSOLE OUTPUT", bg=BG_CARD, fg=SABER_BLUE, font=("Segoe UI Bold", 8)).pack(side="left")
        self.status_var = tk.StringVar(value="Ready.")
        tk.Label(lhdr, textvariable=self.status_var, bg=BG_CARD, fg=FG_SECONDARY, font=("Segoe UI", 9)).pack(
            side="right"
        )

        self.log_text = tk.Text(
            log_card,
            height=4,
            bg="#080c14",
            fg="#cbd5e1",
            font=("Consolas", 9),
            relief="flat",
            padx=8,
            pady=6,
            state="disabled",
        )
        self.log_text.pack(fill="x", pady=(6, 0))

        self._select_page("convert")

    def _create_nav_item(self, parent: tk.Frame, key: str, text: str) -> None:
        row = tk.Frame(parent, bg=BG_SIDEBAR, height=44, cursor="hand2")
        row.pack(fill="x", pady=2)
        row.pack_propagate(False)

        bar = tk.Frame(row, bg=BG_SIDEBAR, width=4)
        bar.pack(side="left", fill="y")

        lbl = tk.Label(
            row,
            text=text,
            bg=BG_SIDEBAR,
            fg=FG_SECONDARY,
            font=("Segoe UI Semibold", 10),
            anchor="w",
            padx=14,
        )
        lbl.pack(side="left", fill="both", expand=True)

        for widget in (row, bar, lbl):
            widget.bind("<Button-1>", lambda _e, k=key: self._select_page(k))

        self._nav_buttons[key] = (row, bar, lbl)

    def _select_page(self, key: str) -> None:
        for k, (row, bar, lbl) in self._nav_buttons.items():
            if k == key:
                row.configure(bg="#131d31")
                bar.configure(bg=SABER_BLUE)
                lbl.configure(bg="#131d31", fg="#ffffff")
            else:
                row.configure(bg=BG_SIDEBAR)
                bar.configure(bg=BG_SIDEBAR)
                lbl.configure(bg=BG_SIDEBAR, fg=FG_SECONDARY)
        self._pages[key].tkraise()

    def _style_treeview_tags(self, tree: ttk.Treeview) -> None:
        tree.tag_configure("even", background="#111827")
        tree.tag_configure("odd", background="#151f32")
        tree.tag_configure("custom", background="#0e2923", foreground="#6ee7b7")

    # ==========================================================================
    # Page 1: Install Songs (BeatSaver Search + Album Cover Preview + Slot Browser)
    # ==========================================================================

    def _build_convert_page(self, page: tk.Frame) -> None:
        paned = tk.PanedWindow(page, orient="horizontal", bg=BG_ROOT, sashwidth=8, bd=0)
        paned.pack(fill="both", expand=True)

        # LEFT CARD: Song Source & Live Cover Preview
        left = self._make_card(paned)
        paned.add(left, width=510)

        ttk.Label(left, text="STEP 1 · CHOOSE CUSTOM SONG", style="Section.TLabel").pack(anchor="w", pady=(0, 8))

        srow = tk.Frame(left, bg=BG_CARD)
        srow.pack(fill="x", pady=(0, 8))
        self.search_var = tk.StringVar()
        s_entry = ttk.Entry(srow, textvariable=self.search_var)
        s_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        s_entry.bind("<Return>", lambda _e: self._on_search_beatsaver())

        self.sort_var = tk.StringVar(value="Relevance")
        ttk.Combobox(
            srow, textvariable=self.sort_var, values=["Relevance", "Rating", "Latest"], width=10, state="readonly"
        ).pack(side="left", padx=(0, 6))
        ttk.Button(srow, text="Search BeatSaver", command=self._on_search_beatsaver).pack(side="left")

        tree_wrap = tk.Frame(left, bg=BG_CARD)
        tree_wrap.pack(fill="both", expand=True, pady=(0, 8))
        cols = ("id", "song", "artist", "bpm", "score", "diffs")
        self.search_tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", height=9)
        self._style_treeview_tags(self.search_tree)
        self.search_tree.heading("id", text="ID")
        self.search_tree.heading("song", text="Song Title")
        self.search_tree.heading("artist", text="Artist")
        self.search_tree.heading("bpm", text="BPM")
        self.search_tree.heading("score", text="Score")
        self.search_tree.heading("diffs", text="Difficulties")
        self.search_tree.column("id", width=58, anchor="w")
        self.search_tree.column("song", width=155, anchor="w")
        self.search_tree.column("artist", width=110, anchor="w")
        self.search_tree.column("bpm", width=48, anchor="e")
        self.search_tree.column("score", width=52, anchor="e")
        self.search_tree.column("diffs", width=115, anchor="w")
        vsb1 = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.search_tree.yview)
        self.search_tree.configure(yscrollcommand=vsb1.set)
        self.search_tree.pack(side="left", fill="both", expand=True)
        vsb1.pack(side="right", fill="y")
        self.search_tree.bind("<Double-1>", lambda _e: self._on_download_selected_search())

        dl_row = tk.Frame(left, bg=BG_CARD)
        dl_row.pack(fill="x", pady=(0, 8))
        ttk.Button(dl_row, text="⬇ Load Highlighted", command=self._on_download_selected_search).pack(
            side="left", padx=(0, 8)
        )
        tk.Label(dl_row, text="ID / URL:", bg=BG_CARD, fg=FG_SECONDARY, font=("Segoe UI", 9)).pack(
            side="left", padx=(2, 4)
        )
        self.direct_id_var = tk.StringVar()
        ttk.Entry(dl_row, textvariable=self.direct_id_var, width=13).pack(side="left", padx=(0, 4))
        ttk.Button(dl_row, text="Fetch", command=self._on_fetch_direct_id).pack(side="left")

        local_row = tk.Frame(left, bg=BG_CARD)
        local_row.pack(fill="x", pady=(0, 10))
        ttk.Button(local_row, text="📂 BeatSaver .zip...", command=self._on_browse_zip).pack(side="left", padx=(0, 6))
        ttk.Button(local_row, text="📁 Map Folder...", command=self._on_browse_map_dir).pack(side="left", padx=(0, 6))
        ttk.Button(local_row, text="📋 Paste (Ctrl+V)", command=self._on_paste_button).pack(side="left")

        # Sleek Loaded Song Hero Card with Live Cover Art Thumbnail!
        hero = tk.Frame(left, bg="#0b111e", highlightthickness=1, highlightbackground="#1e3a5f", padx=10, pady=10)
        hero.pack(fill="x")
        self._default_cover_photo = _make_default_cover_photo(68)
        self.cover_label = tk.Label(hero, image=self._default_cover_photo, bg="#0b111e")
        self.cover_label.pack(side="left", padx=(0, 12))

        self.loaded_summary_var = tk.StringVar(
            value="No custom song loaded yet.\nSearch BeatSaver above, open a .zip, or auto-map an MP3 in the sidebar!"
        )
        tk.Label(
            hero,
            textvariable=self.loaded_summary_var,
            bg="#0b111e",
            fg=FG_PRIMARY,
            justify="left",
            anchor="w",
            wraplength=380,
            font=("Segoe UI Semibold", 9),
        ).pack(side="left", fill="both", expand=True)

        # RIGHT CARD: Slot Browser + Metadata + Convert Action
        right = self._make_card(paned)
        paned.add(right, width=560)

        ttk.Label(right, text="STEP 2 · PICK TARGET PS5 SONG SLOT", style="Section.TLabel").pack(
            anchor="w", pady=(0, 8)
        )

        frow = tk.Frame(right, bg=BG_CARD)
        frow.pack(fill="x", pady=(0, 8))
        self.pack_cat_map = {
            "All Packs (OST + DLC)": "all",
            "DLC Music Packs Only": "dlc",
            "Base OST & Extras Only": "free",
        }
        self.pack_cat_var = tk.StringVar(value="All Packs (OST + DLC)")
        cat_cb = ttk.Combobox(
            frow,
            textvariable=self.pack_cat_var,
            values=list(self.pack_cat_map.keys()),
            width=20,
            state="readonly",
        )
        cat_cb.pack(side="left", padx=(0, 6))
        cat_cb.bind("<<ComboboxSelected>>", lambda _e: self._refresh_slot_tree())

        self.pack_filter_var = tk.StringVar(value="All Packs")
        self.pack_combo = ttk.Combobox(frow, textvariable=self.pack_filter_var, width=14, state="readonly")
        self.pack_combo.pack(side="left", padx=(0, 6))
        self.pack_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_slot_tree())

        self.slot_search_var = tk.StringVar()
        self.slot_search_var.trace_add("write", lambda *_: self._refresh_slot_tree())
        ttk.Entry(frow, textvariable=self.slot_search_var, width=12).pack(
            side="left", fill="x", expand=True, padx=(0, 6)
        )

        ttk.Button(frow, text="📦 Batch Fill Pack...", command=self._on_batch_fill_pack).pack(side="left")

        slot_wrap = tk.Frame(right, bg=BG_CARD)
        slot_wrap.pack(fill="both", expand=True, pady=(0, 10))
        scols = ("pack", "stock", "modes", "status")
        self.slot_tree = ttk.Treeview(slot_wrap, columns=scols, show="headings", height=8)
        self._style_treeview_tags(self.slot_tree)
        self.slot_tree.heading("pack", text="Music Pack")
        self.slot_tree.heading("stock", text="Original Slot")
        self.slot_tree.heading("modes", text="Modes")
        self.slot_tree.heading("status", text="In-Game Song")
        self.slot_tree.column("pack", width=155, anchor="w")
        self.slot_tree.column("stock", width=140, anchor="w")
        self.slot_tree.column("modes", width=95, anchor="w")
        self.slot_tree.column("status", width=185, anchor="w")
        vsb2 = ttk.Scrollbar(slot_wrap, orient="vertical", command=self.slot_tree.yview)
        self.slot_tree.configure(yscrollcommand=vsb2.set)
        self.slot_tree.pack(side="left", fill="both", expand=True)
        vsb2.pack(side="right", fill="y")

        ttk.Label(right, text="STEP 3 · DISPLAY, STAGE ARENA & INSTALL", style="Section.TLabel").pack(
            anchor="w", pady=(2, 6)
        )

        meta_grid = tk.Frame(right, bg=BG_CARD)
        meta_grid.pack(fill="x", pady=(0, 6))
        ttk.Label(meta_grid, text="Title:", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
        self.override_title_var = tk.StringVar()
        ttk.Entry(meta_grid, textvariable=self.override_title_var, width=22).grid(
            row=0, column=1, sticky="ew", padx=(0, 10), pady=3
        )

        ttk.Label(meta_grid, text="Artist:", style="Card.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 6), pady=3)
        self.override_artist_var = tk.StringVar()
        ttk.Entry(meta_grid, textvariable=self.override_artist_var, width=18).grid(row=0, column=3, sticky="ew", pady=3)

        ttk.Label(meta_grid, text="Stage:", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
        self.install_stage_var = tk.StringVar(value="Auto (Match Mapper's Stage)")
        self.install_stage_combo = ttk.Combobox(meta_grid, textvariable=self.install_stage_var, state="readonly")
        self.install_stage_combo.grid(row=1, column=1, columnspan=3, sticky="ew", pady=3)

        meta_grid.columnconfigure(1, weight=1)
        meta_grid.columnconfigure(3, weight=1)

        self.auto_diffs_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            right,
            text="Auto-fill all 5 difficulties (Easy → Expert+) for friends & family",
            variable=self.auto_diffs_var,
        ).pack(anchor="w", pady=1)

        self.auto_modes_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            right,
            text="Auto-generate extra modes (1.12) / Paint BC7 Cover Art Thumbnail (1.37 experimental)",
            variable=self.auto_modes_var,
        ).pack(anchor="w", pady=(1, 10))

        self.install_btn = ttk.Button(
            right,
            text="⚡  CONVERT & INSTALL INTO PS5 BEAT SABER",
            style="Primary.TButton",
            command=self._on_install_clicked,
        )
        self.install_btn.pack(fill="x")

    # ==========================================================================
    # Page 2: Make Your Own Track (Offline DSP Audio-to-Beatmap Studio)
    # ==========================================================================

    def _build_maketrack_page(self, page: tk.Frame) -> None:
        card = self._make_card(page, padx=20, pady=18)
        card.pack(fill="both", expand=True)

        ttk.Label(
            card,
            text="OFFLINE AUDIO-TO-BEATMAP STUDIO · TURN ANY MUSIC FILE INTO A PLAYABLE TRACK",
            style="Section.TLabel",
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            card,
            text="Copy any .mp3, .wav, .ogg, .flac, or .egg file in Explorer and press Ctrl+V (or browse below). "
            "Multi-band spectral flux detects kick/snare/melody transients, locks BPM & beat phase, and generates Easy → Expert+ plus a synced lightshow.",
            style="Muted.TLabel",
            wraplength=980,
        ).pack(anchor="w", pady=(0, 16))

        arow = tk.Frame(card, bg=BG_CARD)
        arow.pack(fill="x", pady=(0, 14))
        ttk.Label(arow, text="Music File:", style="Card.TLabel").pack(side="left", padx=(0, 8))
        self.gen_audio_path_var = tk.StringVar()
        ttk.Entry(arow, textvariable=self.gen_audio_path_var).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(arow, text="📂 Browse Audio...", command=self._on_browse_gen_audio).pack(side="left", padx=(0, 6))
        ttk.Button(arow, text="📋 Paste File (Ctrl+V)", command=self._on_paste_button).pack(side="left")

        form = tk.Frame(card, bg=BG_CARD)
        form.pack(fill="x", pady=(0, 16))

        ttk.Label(form, text="Song Title:", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=6)
        self.gen_title_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.gen_title_var, width=32).grid(
            row=0, column=1, sticky="ew", padx=(0, 18), pady=6
        )

        ttk.Label(form, text="Artist Name:", style="Card.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 8), pady=6)
        self.gen_artist_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.gen_artist_var, width=28).grid(row=0, column=3, sticky="ew", pady=6)

        ttk.Label(form, text="Mapping Style:", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=6
        )
        self.gen_style_var = tk.StringVar(value=STYLE_PRESETS[0])
        ttk.Combobox(form, textvariable=self.gen_style_var, values=list(STYLE_PRESETS), state="readonly").grid(
            row=1, column=1, sticky="ew", padx=(0, 18), pady=6
        )

        ttk.Label(form, text="BPM Override (optional):", style="Card.TLabel").grid(
            row=1, column=2, sticky="w", padx=(0, 8), pady=6
        )
        self.gen_bpm_var = tk.StringVar(value="")
        ttk.Entry(form, textvariable=self.gen_bpm_var, width=14).grid(row=1, column=3, sticky="w", pady=6)

        ttk.Label(form, text="Default Stage Arena:", style="Card.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=6
        )
        self.gen_stage_var = tk.StringVar(value="DefaultEnvironment")
        self.gen_stage_combo = ttk.Combobox(form, textvariable=self.gen_stage_var, state="readonly")
        self.gen_stage_combo.grid(row=2, column=1, sticky="ew", padx=(0, 18), pady=6)

        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)

        btn_row = tk.Frame(card, bg=BG_CARD)
        btn_row.pack(fill="x", pady=(4, 16))
        ttk.Button(
            btn_row,
            text="🎵  GENERATE 5-DIFFICULTY TRACK & SEND TO INSTALLER",
            style="Primary.TButton",
            command=self._on_generate_track_clicked,
        ).pack(side="left", padx=(0, 10))
        ttk.Button(
            btn_row,
            text="💾 Export as BeatSaver .zip...",
            command=self._on_export_generated_zip,
        ).pack(side="left")

        res_box = tk.Frame(card, bg="#0b111e", highlightthickness=1, highlightbackground="#1e3a5f", padx=16, pady=14)
        res_box.pack(fill="x")
        self.gen_result_var = tk.StringVar(
            value="Ready to map.\nSelect any music file above and click 'Generate 5-Difficulty Track'!"
        )
        tk.Label(
            res_box,
            textvariable=self.gen_result_var,
            bg="#0b111e",
            fg=ACCENT_EMERALD,
            justify="left",
            anchor="w",
            font=("Segoe UI Semibold", 10),
        ).pack(fill="x")

    # ==========================================================================
    # Page 3: Stages, Neon Color Swatches & 3D .obj Geometry
    # ==========================================================================

    def _build_stages_page(self, page: tk.Frame) -> None:
        col_card = self._make_card(page, padx=16, pady=14)
        col_card.pack(fill="x", pady=(0, 12))

        ttk.Label(
            col_card,
            text="STAGE & SABER NEON COLOR CUSTOMIZER (1.12 ColorSchemeSO PALETTES)",
            style="Section.TLabel",
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            col_card,
            text="Preview and apply vibrant custom Left/Right Saber, Neon Stage Light, and Wall palettes.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 10))

        crow1 = tk.Frame(col_card, bg=BG_CARD)
        crow1.pack(fill="x", pady=(0, 10))
        ttk.Label(crow1, text="Target Stage:", style="Card.TLabel").pack(side="left", padx=(0, 6))
        self.color_stage_var = tk.StringVar()
        self.color_stage_combo = ttk.Combobox(crow1, textvariable=self.color_stage_var, width=26, state="readonly")
        self.color_stage_combo.pack(side="left", padx=(0, 12))

        ttk.Label(crow1, text="Theme:", style="Card.TLabel").pack(side="left", padx=(0, 6))
        self.color_preset_var = tk.StringVar(value=next(iter(COLOR_THEME_PRESETS.keys())))
        preset_cb = ttk.Combobox(
            crow1,
            textvariable=self.color_preset_var,
            values=list(COLOR_THEME_PRESETS.keys()) + ["Custom Hex Colors Below"],
            width=30,
            state="readonly",
        )
        preset_cb.pack(side="left", padx=(0, 12))
        preset_cb.bind("<<ComboboxSelected>>", lambda _e: self._sync_swatch_preview())

        ttk.Button(crow1, text="Apply Neon Theme", style="Primary.TButton", command=self._on_apply_stage_colors).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(crow1, text="Reset Colors", command=self._on_reset_stage_colors).pack(side="left")

        # Live Visual Color Swatch Bar!
        self.swatch_canvas = tk.Canvas(col_card, height=34, bg="#0b111e", highlightthickness=1, highlightbackground=BORDER_SUBTLE)
        self.swatch_canvas.pack(fill="x", pady=(0, 10))

        crow2 = tk.Frame(col_card, bg=BG_CARD)
        crow2.pack(fill="x")
        self.hex_saber_l = tk.StringVar(value="#ff1e7a")
        self.hex_saber_r = tk.StringVar(value="#00f0ff")
        self.hex_env_0 = tk.StringVar(value="#ff198c")
        self.hex_env_1 = tk.StringVar(value="#00d8ff")
        self.hex_wall = tk.StringVar(value="#f20d73")

        for lbl, var in [
            ("Left Saber:", self.hex_saber_l),
            ("Right Saber:", self.hex_saber_r),
            ("Light A:", self.hex_env_0),
            ("Light B:", self.hex_env_1),
            ("Walls:", self.hex_wall),
        ]:
            ttk.Label(crow2, text=lbl, style="Card.TLabel").pack(side="left", padx=(4, 3))
            ent = ttk.Entry(crow2, textvariable=var, width=8)
            ent.pack(side="left", padx=(0, 3))
            var.trace_add("write", lambda *_: self._draw_swatch_bar())
            ttk.Button(crow2, text="🎨", command=lambda v=var: self._pick_color(v)).pack(side="left", padx=(0, 8))

        self._sync_swatch_preview()
        self.swatch_canvas.bind("<Configure>", lambda _e: self._draw_swatch_bar())

        # Bottom Card: 3D Stage .obj Mesh Injector
        mesh_card = self._make_card(page, padx=16, pady=14)
        mesh_card.pack(fill="both", expand=True)

        ttk.Label(
            mesh_card,
            text="CUSTOM 3D STAGE GEOMETRY INJECTOR (.obj → PS5 UNSWIZZLED MESH + NATIVE SHADERS)",
            style="Section.TLabel",
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            mesh_card,
            text="Import Wavefront .obj 3D models into PS5 stage architecture while inheriting native PS5 metallic/mirror/glow materials.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 12))

        mrow = tk.Frame(mesh_card, bg=BG_CARD)
        mrow.pack(fill="x", pady=(0, 10))
        ttk.Label(mrow, text="Mesh Target:", style="Card.TLabel").pack(side="left", padx=(0, 6))
        self.mesh_target_labels = {f"{k} — {v[3]}": k for k, v in STAGE_MESH_TARGETS.items()}
        self.mesh_target_var = tk.StringVar(value=next(iter(self.mesh_target_labels.keys())))
        ttk.Combobox(
            mrow, textvariable=self.mesh_target_var, values=list(self.mesh_target_labels.keys()), width=42, state="readonly"
        ).pack(side="left", padx=(0, 14))

        ttk.Label(mrow, text="Scale:", style="Card.TLabel").pack(side="left", padx=(0, 6))
        self.mesh_scale_var = tk.StringVar(value="1.0")
        ttk.Entry(mrow, textvariable=self.mesh_scale_var, width=8).pack(side="left")

        mrow2 = tk.Frame(mesh_card, bg=BG_CARD)
        mrow2.pack(fill="x")
        ttk.Label(mrow2, text="3D .obj File:", style="Card.TLabel").pack(side="left", padx=(0, 6))
        self.obj_path_var = tk.StringVar()
        ttk.Entry(mrow2, textvariable=self.obj_path_var).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(mrow2, text="Browse .obj...", command=self._on_browse_obj).pack(side="left", padx=(0, 8))
        ttk.Button(mrow2, text="Inject 3D Model", style="Primary.TButton", command=self._on_inject_obj).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(mrow2, text="Restore Stock Meshes", command=self._on_reset_meshes).pack(side="left")

    def _sync_swatch_preview(self) -> None:
        preset = self.color_preset_var.get()
        if preset in COLOR_THEME_PRESETS:
            pal = COLOR_THEME_PRESETS[preset]
            self.hex_saber_l.set(_rgba_to_hex(pal["saber_a"]))
            self.hex_saber_r.set(_rgba_to_hex(pal["saber_b"]))
            self.hex_env_0.set(_rgba_to_hex(pal["env_color_0"]))
            self.hex_env_1.set(_rgba_to_hex(pal["env_color_1"]))
            self.hex_wall.set(_rgba_to_hex(pal["obstacles_color"]))
        self._draw_swatch_bar()

    def _draw_swatch_bar(self) -> None:
        if not hasattr(self, "swatch_canvas"):
            return
        c = self.swatch_canvas
        c.delete("all")
        w = max(500, c.winfo_width())
        items = [
            ("LEFT SABER", self.hex_saber_l.get()),
            ("RIGHT SABER", self.hex_saber_r.get()),
            ("STAGE LIGHT A", self.hex_env_0.get()),
            ("STAGE LIGHT B", self.hex_env_1.get()),
            ("OBSTACLE WALLS", self.hex_wall.get()),
        ]
        seg_w = w / len(items)
        for idx, (label, hex_col) in enumerate(items):
            x0 = int(idx * seg_w) + 6
            x1 = int((idx + 1) * seg_w) - 6
            col = hex_col if (hex_col.startswith("#") and len(hex_col) == 7) else "#334155"
            try:
                c.create_rectangle(x0, 6, x0 + 26, 28, fill=col, outline="#ffffff")
            except tk.TclError:
                c.create_rectangle(x0, 6, x0 + 26, 28, fill="#334155", outline="#ffffff")
            c.create_text(x0 + 34, 17, text=f"{label} ({col})", anchor="w", fill=FG_PRIMARY, font=("Segoe UI Semibold", 8))

    # ==========================================================================
    # Page 4: Library, Surgical Restore & SHA-1 Verified FTP Sync
    # ==========================================================================

    def _build_manage_page(self, page: tk.Frame) -> None:
        top_card = self._make_card(page, padx=14, pady=12)
        top_card.pack(fill="both", expand=True, pady=(0, 10))

        hdr = tk.Frame(top_card, bg=BG_CARD)
        hdr.pack(fill="x", pady=(0, 10))
        ttk.Label(hdr, text="INSTALLED CUSTOM SONG LIBRARY", style="Section.TLabel").pack(side="left")

        btn_bar = tk.Frame(hdr, bg=BG_CARD)
        btn_bar.pack(side="right")
        ttk.Button(btn_bar, text="✔ Verify Integrity", command=self._on_verify_clicked).pack(side="left", padx=4)
        ttk.Button(btn_bar, text="🖨 Export Party Songbook (HTML)", command=self._on_export_html).pack(
            side="left", padx=4
        )
        ttk.Button(btn_bar, text="↩ Restore Selected Slot", command=self._on_restore_selected).pack(side="left", padx=4)
        ttk.Button(
            btn_bar, text="⚠ Factory Reset All", style="Danger.TButton", command=self._on_restore_all
        ).pack(side="left", padx=(4, 0))

        icols = ("slot", "pack", "custom_title", "custom_artist", "bpm", "len", "stage", "stock_title")
        self.installed_tree = ttk.Treeview(top_card, columns=icols, show="headings", height=11)
        self._style_treeview_tags(self.installed_tree)
        self.installed_tree.heading("slot", text="Slot ID")
        self.installed_tree.heading("pack", text="Music Pack")
        self.installed_tree.heading("custom_title", text="Custom Song Title")
        self.installed_tree.heading("custom_artist", text="Artist")
        self.installed_tree.heading("bpm", text="BPM")
        self.installed_tree.heading("len", text="Length")
        self.installed_tree.heading("stage", text="Stage Arena")
        self.installed_tree.heading("stock_title", text="Replaced Stock Song")
        self.installed_tree.column("slot", width=120)
        self.installed_tree.column("pack", width=150)
        self.installed_tree.column("custom_title", width=210)
        self.installed_tree.column("custom_artist", width=150)
        self.installed_tree.column("bpm", width=60, anchor="e")
        self.installed_tree.column("len", width=65, anchor="e")
        self.installed_tree.column("stage", width=145)
        self.installed_tree.column("stock_title", width=160)
        self.installed_tree.pack(fill="both", expand=True)

        ftp_card = self._make_card(page, padx=14, pady=12)
        ftp_card.pack(fill="x")
        ttk.Label(
            ftp_card,
            text="DIRECT PS5 FTP PUSH (etaHEN / ftpsrv · 1.12 Delta Tail Push & 1.37 SHA-1 Verified Push w/ Index Last)",
            style="Section.TLabel",
        ).pack(anchor="w", pady=(0, 8))

        frow = tk.Frame(ftp_card, bg=BG_CARD)
        frow.pack(fill="x")
        ttk.Label(frow, text="PS5 IP:", style="Card.TLabel").pack(side="left", padx=(0, 4))
        self.ftp_ip_var = tk.StringVar(value="192.168.1.100")
        ttk.Entry(frow, textvariable=self.ftp_ip_var, width=15).pack(side="left", padx=(0, 10))

        ttk.Label(frow, text="Port:", style="Card.TLabel").pack(side="left", padx=(0, 4))
        self.ftp_port_var = tk.StringVar(value="2121")
        ttk.Entry(frow, textvariable=self.ftp_port_var, width=7).pack(side="left", padx=(0, 10))

        ttk.Label(frow, text="PS5 Mount Path:", style="Card.TLabel").pack(side="left", padx=(0, 4))
        self.ftp_dir_var = tk.StringVar(value="/mnt/usb0/BeatSaber-Custom-PPSA15035")
        ttk.Entry(frow, textvariable=self.ftp_dir_var).pack(side="left", fill="x", expand=True, padx=(0, 10))

        ttk.Button(frow, text="🚀 Push Changes to PS5", style="Primary.TButton", command=self._on_ftp_push).pack(
            side="left"
        )

    # ==========================================================================
    # Clipboard Paste & Cover Preview Helpers
    # ==========================================================================

    def _update_cover_preview(self, cover_bytes: Optional[bytes]) -> None:
        if cover_bytes:
            try:
                img = Image.open(io.BytesIO(cover_bytes)).convert("RGBA").resize((68, 68), Image.Resampling.LANCZOS)
                self._cover_photo_ref = ImageTk.PhotoImage(img)
                self.cover_label.configure(image=self._cover_photo_ref)
                return
            except Exception:
                pass
        self.cover_label.configure(image=self._default_cover_photo)

    def _on_global_paste(self, _event: tk.Event) -> Optional[str]:
        focused = self.root.focus_get()
        pasted_path = _read_clipboard_file_path(self.root)
        if not pasted_path:
            return None
        if isinstance(focused, ttk.Entry) and not os.path.exists(pasted_path):
            return None
        self._handle_pasted_path(pasted_path)
        return "break"

    def _on_paste_button(self) -> None:
        pasted_path = _read_clipboard_file_path(self.root)
        if not pasted_path:
            messagebox.showinfo(
                "Clipboard Empty",
                "Copy a music file (.mp3, .wav, .ogg, .flac) or BeatSaver .zip in Explorer (Ctrl+C), then click Paste!",
            )
            return
        self._handle_pasted_path(pasted_path)

    def _handle_pasted_path(self, path: str) -> None:
        low = path.lower()
        if low.endswith((".mp3", ".wav", ".ogg", ".flac", ".egg")):
            self._populate_maketrack_audio(path)
            self._select_page("maketrack")
            self._append_log_ui(f"Pasted music file '{os.path.basename(path)}' into Auto-Map Studio!")
        elif low.endswith(".zip") or os.path.isdir(path):
            try:
                pkg = load_custom_map(path)
                self._set_loaded_package(pkg, path)
                self._select_page("convert")
            except Exception as exc:
                messagebox.showerror("Invalid Map Package", str(exc))

    # ==========================================================================
    # Async Worker & Progress Bar
    # ==========================================================================

    def log(self, msg: str) -> None:
        self.msg_queue.put(("log", msg))

    def _append_log_ui(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.status_var.set(msg[:105])

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
                self.progress.stop()
                self.install_btn.configure(state="normal")
                cb = payload  # type: ignore[assignment]
                if callable(cb):
                    cb()
            elif kind == "error":
                self.busy = False
                self.progress.stop()
                self.install_btn.configure(state="normal")
                self._append_log_ui(f"ERROR: {payload}")
                messagebox.showerror("Error", str(payload))
        self.root.after(80, self._poll_queue)

    def _run_bg(self, work_fn: Callable[[], object], on_done: Callable[[object], None]) -> None:
        if self.busy:
            messagebox.showinfo("Busy", "Please wait for the current task to complete.")
            return
        self.busy = True
        self.install_btn.configure(state="disabled")
        self.progress.start(12)

        def _target() -> None:
            try:
                res = work_fn()
                self.msg_queue.put(("done", lambda: on_done(res)))
            except Exception as exc:
                self.msg_queue.put(("error", str(exc)))

        threading.Thread(target=_target, daemon=True).start()

    # ==========================================================================
    # Game Dump Loading & Views
    # ==========================================================================

    def _on_browse_game_dir(self) -> None:
        chosen = filedialog.askdirectory(title="Select PS5 Beat Saber Folder (1.12 or 1.37)")
        if chosen:
            self.game_dir_var.set(chosen)
            self._load_game_folder(chosen)

    def _load_game_folder(self, folder: str) -> None:
        if not folder.strip():
            return
        try:
            self.manager = PS5BeatSaberManager(folder)
        except Exception as exc:
            self.pill_engine_var.set("ENGINE: INVALID")
            messagebox.showerror("Could Not Load PS5 Dump", str(exc))
            return

        self.game_dir_var.set(self.manager.app_root)
        self._refresh_all_views()
        mode_desc = (
            "1.37 Addressables + Vorbis FSB5 + AMPR Index"
            if self.manager.engine_mode == "bundle_v137"
            else "1.12 SharedAssets + PCM16 FSB5"
        )
        self._append_log_ui(
            f"Loaded {self.manager.title_id} v{self.manager.content_version} [{mode_desc}] — "
            f"{len(self.manager.slots)} playable slots ready."
        )

    def _refresh_all_views(self) -> None:
        if not self.manager:
            return
        inst_cnt = len(self.manager.state.get("installed_slots", {}))
        total_cnt = len(self.manager.slots)
        envs = self.manager.list_environments()

        eng_short = "1.37 ADDRESSABLES" if self.manager.engine_mode == "bundle_v137" else "1.12 SHAREDASSETS"
        self.pill_engine_var.set(f"⚡ {eng_short} ({self.manager.content_version})")
        self.pill_slots_var.set(f"🎵 {total_cnt} SLOTS · {len(envs)} STAGES")
        self.pill_installed_var.set(f"★ {inst_cnt} INSTALLED")

        self.stage_label_to_id = {
            "Auto (Match Mapper's Stage)": "auto",
            "Keep Original Slot Stage": "keep",
        }
        stage_labels = ["Auto (Match Mapper's Stage)", "Keep Original Slot Stage"]
        raw_stage_labels = []
        for e in envs:
            lbl = f"{e.display_name} ({e.serialized_name})"
            self.stage_label_to_id[lbl] = e.serialized_name
            stage_labels.append(lbl)
            raw_stage_labels.append(lbl)

        self.install_stage_combo["values"] = stage_labels
        if self.install_stage_var.get() not in self.stage_label_to_id:
            self.install_stage_var.set("Auto (Match Mapper's Stage)")

        self.gen_stage_combo["values"] = raw_stage_labels
        if self.gen_stage_var.get() not in self.stage_label_to_id and raw_stage_labels:
            default_lbl = next((x for x in raw_stage_labels if "DefaultEnvironment" in x), raw_stage_labels[0])
            self.gen_stage_var.set(default_lbl)

        self.color_stage_combo["values"] = raw_stage_labels
        if self.color_stage_var.get() not in self.stage_label_to_id and raw_stage_labels:
            default_lbl = next((x for x in raw_stage_labels if "DefaultEnvironment" in x), raw_stage_labels[0])
            self.color_stage_var.set(default_lbl)

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
        cat = self.pack_cat_map.get(self.pack_cat_var.get(), "all")
        slots = self.manager.list_slots(pack_category=cat, pack_id=pack_id)
        q = self.slot_search_var.get().strip().lower()

        row_idx = 0
        for s in slots:
            if q and not (
                q in s.stock_song_name.lower()
                or q in s.current_song_name.lower()
                or q in s.pack_name.lower()
                or q in s.level_id.lower()
            ):
                continue
            status = f"★ {s.current_song_name} — {s.current_song_author}" if s.is_custom else "Stock"
            pack_disp = f"[OST] {s.pack_name}" if s.is_free_base_game else f"[DLC] {s.pack_name}"
            modes_disp = ", ".join(m.replace("Degree", "°") for m in s.modes)
            tag = "custom" if s.is_custom else ("even" if row_idx % 2 == 0 else "odd")
            self.slot_tree.insert(
                "", "end", iid=s.level_id, values=(pack_disp, s.stock_song_name, modes_disp, status), tags=(tag,)
            )
            self.displayed_slots[s.level_id] = s
            row_idx += 1

    def _refresh_installed_tree(self) -> None:
        self.installed_tree.delete(*self.installed_tree.get_children())
        if not self.manager:
            return
        installed = list(self.manager.state.get("installed_slots", {}).values())
        installed.sort(key=lambda x: (x["pack_name"], x["custom_song_name"]))
        for idx, item in enumerate(installed):
            mins, secs = divmod(int(round(item.get("duration_seconds", 0))), 60)
            tag = "even" if idx % 2 == 0 else "odd"
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
                    item.get("stage_environment", "Slot Default"),
                    item["stock_song_name"],
                ),
                tags=(tag,),
            )

    # ==========================================================================
    # Page 1 Actions
    # ==========================================================================

    def _set_loaded_package(self, pkg: CustomSongPackage, source_path: str) -> None:
        self.loaded_pkg = pkg
        self.loaded_source_path = source_path
        self.override_title_var.set(pkg.song_name)
        self.override_artist_var.set(pkg.song_author_name)
        self._update_cover_preview(pkg.cover_bytes)
        env_hint = f"  ·  Stage: {pkg.environment_name}" if pkg.environment_name else ""
        cov_hint = "  ·  Cover Art ✔" if pkg.cover_bytes else ""
        self.loaded_summary_var.set(
            f"{pkg.song_name} — {pkg.song_author_name}\n"
            f"Mapper: {pkg.level_author_name or 'Community'}  ·  {pkg.bpm:.1f} BPM{env_hint}{cov_hint}\n"
            f"Charts: {pkg.native_summary()}"
        )
        self._append_log_ui(f"Loaded '{pkg.song_name}' by '{pkg.song_author_name}'. Select a PS5 slot on the right!")

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
            for idx, r in enumerate(items):
                self.search_results[r.map_id] = r
                tag = "even" if idx % 2 == 0 else "odd"
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
                    tags=(tag,),
                )
            self._append_log_ui(f"Found {len(items)} BeatSaver maps for '{q}'.")

        self._run_bg(_work, _done)

    def _on_download_selected_search(self) -> None:
        sel = self.search_tree.selection()
        if not sel:
            messagebox.showinfo("Select a Song", "Highlight a BeatSaver result in the table first.")
            return
        self._download_and_load(self.search_results[sel[0]])

    def _on_fetch_direct_id(self) -> None:
        val = self.direct_id_var.get().strip()
        if val:
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
        if path:
            try:
                self._set_loaded_package(load_custom_map(path), path)
            except Exception as exc:
                messagebox.showerror("Invalid Map Zip", str(exc))

    def _on_browse_map_dir(self) -> None:
        folder = filedialog.askdirectory(title="Select Extracted BeatSaver Map Folder")
        if folder:
            try:
                self._set_loaded_package(load_custom_map(folder), folder)
            except Exception as exc:
                messagebox.showerror("Invalid Map Folder", str(exc))

    def _on_install_clicked(self) -> None:
        if not self.manager:
            messagebox.showwarning("No PS5 Game Loaded", "Select your PS5 Beat Saber dump folder first.")
            return
        if not self.loaded_pkg:
            messagebox.showwarning("No Custom Song Loaded", "Load a BeatSaver map or generate a track first.")
            return
        sel = self.slot_tree.selection()
        if not sel:
            messagebox.showwarning("No PS5 Slot Selected", "Click a target PS5 song slot in Step 2 on the right.")
            return

        slot_id = sel[0]
        title_ov = self.override_title_var.get().strip() or self.loaded_pkg.song_name
        artist_ov = self.override_artist_var.get().strip() or self.loaded_pkg.song_author_name
        stage_choice = self.stage_label_to_id.get(self.install_stage_var.get(), "auto")
        auto_diffs = self.auto_diffs_var.get()
        extra_opt = self.auto_modes_var.get()
        pkg = self.loaded_pkg

        def _work() -> SongSlotInfo:
            assert self.manager is not None
            return self.manager.install_custom_song(
                map_source=pkg,
                target_level_id=slot_id,
                song_name_override=title_ov,
                song_author_override=artist_ov,
                auto_generate_difficulties=auto_diffs,
                auto_generate_modes=extra_opt,
                environment_override=stage_choice,
                install_cover_art=(extra_opt and self.manager.engine_mode == "bundle_v137"),
                log_fn=self.log,
            )

        def _done(res: object) -> None:
            updated: SongSlotInfo = res  # type: ignore[assignment]
            self._refresh_all_views()
            self.slot_tree.selection_set(updated.level_id)
            st_lbl = (updated.installed_info or {}).get("stage_environment", "Slot Default")
            messagebox.showinfo(
                "Custom Song Installed!",
                f"Successfully converted & installed:\n\n"
                f"  Song: {updated.current_song_name}\n"
                f"  Artist: {updated.current_song_author}\n"
                f"  Pack: {updated.pack_name} (replaces '{updated.stock_song_name}')\n"
                f"  Stage Arena: {st_lbl}",
            )

        self._run_bg(_work, _done)

    def _on_batch_fill_pack(self) -> None:
        if not self.manager:
            return
        pack_id = self.pack_filter_var.get()
        if pack_id == "All Packs":
            sel = self.slot_tree.selection()
            if sel and sel[0] in self.manager.slots:
                pack_id = self.manager.slots[sel[0]].pack_id
            else:
                messagebox.showinfo(
                    "Choose a Target Pack",
                    "Select a specific Music Pack in the Pack dropdown first (or highlight a song from that pack)!",
                )
                return

        choice = messagebox.askyesnocancel(
            f"Batch Fill '{pack_id}' Pack",
            f"Fill song slots in pack '{pack_id}':\n\n"
            "• YES = BeatSaver Playlist (.bplist) file\n"
            "• NO = Folder of .zip maps or .mp3/.wav/.ogg music files\n"
            "• CANCEL = Abort",
        )
        if choice is None:
            return
        if choice:
            src_path = filedialog.askopenfilename(
                title=f"Select BeatSaver Playlist (.bplist) for '{pack_id}'",
                filetypes=[("BeatSaver Playlist", "*.bplist *.json"), ("All Files", "*.*")],
            )
        else:
            src_path = filedialog.askdirectory(
                title=f"Select Folder of .zip Maps or Music Files for '{pack_id}'"
            )
        if not src_path:
            return

        stage_choice = self.stage_label_to_id.get(self.install_stage_var.get(), "auto")
        auto_diffs = self.auto_diffs_var.get()
        auto_modes = self.auto_modes_var.get()

        def _work() -> List[SongSlotInfo]:
            assert self.manager is not None
            return self.manager.install_batch_into_pack(
                playlist_or_folder_or_files=src_path,
                target_pack_id=pack_id,
                environment_override=stage_choice,
                auto_generate_difficulties=auto_diffs,
                auto_generate_modes=auto_modes,
                log_fn=self.log,
            )

        def _done(res: object) -> None:
            installed: List[SongSlotInfo] = res  # type: ignore[assignment]
            self._refresh_all_views()
            messagebox.showinfo(
                "Batch Pack Install Complete!",
                f"Installed {len(installed)} custom song(s) into pack '{pack_id}'!",
            )

        self._run_bg(_work, _done)

    # ==========================================================================
    # Page 2 Actions: Auto-Mapper
    # ==========================================================================

    def _populate_maketrack_audio(self, path: str) -> None:
        self.gen_audio_path_var.set(path)
        title, artist = parse_title_artist_from_filename(path)
        self.gen_title_var.set(title)
        self.gen_artist_var.set(artist)

    def _on_browse_gen_audio(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Music File to Auto-Map",
            filetypes=[("Audio Files", "*.mp3 *.wav *.ogg *.flac *.egg"), ("All Files", "*.*")],
        )
        if path:
            self._populate_maketrack_audio(path)

    def _on_generate_track_clicked(self) -> None:
        audio_path = self.gen_audio_path_var.get().strip().strip('"')
        if not audio_path or not os.path.isfile(audio_path):
            messagebox.showwarning("Select a Music File", "Pick or paste a valid .mp3, .wav, .ogg, or .flac file first.")
            return

        title = self.gen_title_var.get().strip() or None
        artist = self.gen_artist_var.get().strip() or None
        style = self.gen_style_var.get()
        stage_id = self.stage_label_to_id.get(self.gen_stage_var.get(), self.gen_stage_var.get())
        bpm_txt = self.gen_bpm_var.get().strip()
        bpm_ov = float(bpm_txt) if bpm_txt else None

        self.log(f"Analyzing '{os.path.basename(audio_path)}' & generating 5-difficulty choreography...")

        def _work() -> Tuple[CustomSongPackage, AudioBeatAnalysis]:
            return generate_track_from_audio_file(
                audio_path,
                song_name=title,
                song_author=artist,
                bpm_override=bpm_ov,
                style=style,
                environment_name=stage_id,
            )

        def _done(res: object) -> None:
            pkg, analysis = res  # type: ignore[misc]
            self.generated_pkg = pkg
            std = pkg.charts["Standard"]
            counts = "  /  ".join(f"{['E','N','H','Ex','Ex+'][d]}: {len(std[d].v3_dict['colorNotes'])}" for d in range(5))
            self.gen_result_var.set(
                f"✔ Generated '{pkg.song_name}' — {pkg.song_author_name} ({style})\n"
                f"   Detected Tempo: {analysis.bpm:.1f} BPM   ·   Length: {analysis.duration_seconds:.1f}s   ·   "
                f"Chorus Preview: {analysis.preview_start_seconds:.1f}s\n"
                f"   Notes →  {counts}"
            )
            self._set_loaded_package(pkg, audio_path)
            self.install_stage_var.set("Auto (Match Mapper's Stage)")
            self._select_page("convert")

        self._run_bg(_work, _done)

    def _on_export_generated_zip(self) -> None:
        pkg = self.generated_pkg or self.loaded_pkg
        if not pkg:
            messagebox.showinfo("No Track Ready", "Generate a track first.")
            return
        out = filedialog.asksaveasfilename(
            title="Save Generated Beat Saber Track as .zip",
            defaultextension=".zip",
            initialfile=f"{pkg.song_name}.zip",
            filetypes=[("BeatSaver Map Zip", "*.zip")],
        )
        if out:
            saved = export_package_to_beatsaver_zip(pkg, out)
            self._append_log_ui(f"Saved BeatSaver .zip package to {saved}")

    # ==========================================================================
    # Page 3 Actions: Stage Colors & 3D .obj Mesh Modding
    # ==========================================================================

    def _pick_color(self, target_var: tk.StringVar) -> None:
        _, hex_val = colorchooser.askcolor(color=target_var.get(), title="Choose Neon Color")
        if hex_val:
            target_var.set(hex_val)
            self.color_preset_var.set("Custom Hex Colors Below")

    def _on_apply_stage_colors(self) -> None:
        if not self.manager:
            return
        if self.manager.engine_mode != "assets_v134":
            messagebox.showinfo("1.12 Feature", "Direct resources.assets ColorSchemeSO editing applies to 1.12 dumps.")
            return
        stage_id = self.stage_label_to_id.get(self.color_stage_var.get(), "")
        if not stage_id or stage_id in ("auto", "keep"):
            messagebox.showwarning("Select Stage", "Pick a target PS5 stage first.")
            return
        preset = self.color_preset_var.get()
        if preset == "Custom Hex Colors Below":
            theme_arg: str | Dict[str, Tuple[float, float, float, float]] = {
                "saber_a": _hex_to_rgba(self.hex_saber_l.get()),
                "saber_b": _hex_to_rgba(self.hex_saber_r.get()),
                "env_color_0": _hex_to_rgba(self.hex_env_0.get()),
                "env_color_1": _hex_to_rgba(self.hex_env_1.get()),
                "env_color_0_boost": _hex_to_rgba(self.hex_env_1.get()),
                "env_color_1_boost": _hex_to_rgba(self.hex_env_0.get()),
                "obstacles_color": _hex_to_rgba(self.hex_wall.get()),
            }
        else:
            theme_arg = preset

        self._run_bg(
            lambda: self.manager.apply_stage_color_theme(stage_id, theme_arg, log_fn=self.log),  # type: ignore[union-attr]
            lambda stage_disp: messagebox.showinfo(
                "Stage Colors Updated!",
                f"Applied custom neon saber & stage light colors to '{stage_disp}'!",
            ),
        )

    def _on_reset_stage_colors(self) -> None:
        if not self.manager or self.manager.engine_mode != "assets_v134":
            return
        self._run_bg(
            lambda: self.manager.restore_stage_colors(None, log_fn=self.log),  # type: ignore[union-attr]
            lambda _: messagebox.showinfo("Stage Colors Restored", "All stage & saber colors restored to stock!"),
        )

    def _on_browse_obj(self) -> None:
        p = filedialog.askopenfilename(
            title="Select Wavefront .obj 3D Model",
            filetypes=[("Wavefront OBJ", "*.obj"), ("All Files", "*.*")],
        )
        if p:
            self.obj_path_var.set(p)

    def _on_inject_obj(self) -> None:
        if not self.manager:
            return
        if self.manager.engine_mode != "assets_v134":
            messagebox.showinfo("1.12 Feature", "sharedassets3.assets 3D mesh replacement applies to 1.12 dumps.")
            return
        obj_p = self.obj_path_var.get().strip().strip('"')
        if not obj_p or not os.path.isfile(obj_p):
            messagebox.showwarning("Select .obj Model", "Pick a valid .obj 3D model file first.")
            return
        tkey = self.mesh_target_labels[self.mesh_target_var.get()]
        scale = float(self.mesh_scale_var.get().strip() or "1.0")

        self._run_bg(
            lambda: self.manager.replace_stage_mesh_from_obj(tkey, obj_p, scale=scale, log_fn=self.log),  # type: ignore[union-attr]
            lambda desc: messagebox.showinfo(
                "3D Stage Mesh Injected!",
                f"Successfully injected '{os.path.basename(obj_p)}' into PS5 '{desc}'!",
            ),
        )

    def _on_reset_meshes(self) -> None:
        if not self.manager or self.manager.engine_mode != "assets_v134":
            return
        self._run_bg(
            lambda: self.manager.restore_stage_meshes(log_fn=self.log),  # type: ignore[union-attr]
            lambda _: messagebox.showinfo("3D Meshes Restored", "All PS5 stage 3D meshes restored to stock!"),
        )

    # ==========================================================================
    # Page 4 Actions: Library, Restore & FTP
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
            "Factory Reset All",
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
                f"Verified {len(items)} installed custom song(s) — 100% binary & schema checks passed!",
            )

        self._run_bg(_work, _done)

    def _on_export_html(self) -> None:
        if not self.manager:
            return
        out = filedialog.asksaveasfilename(
            title="Save Printable Party Songbook",
            defaultextension=".html",
            initialfile="PS5_Beat_Saber_Custom_Songs.html",
            filetypes=[("HTML Page", "*.html")],
        )
        if out:
            saved = self.manager.export_song_list_html(out)
            self._append_log_ui(f"Exported party songbook to {saved}")
            webbrowser.open(f"file:///{os.path.abspath(saved).replace(os.sep, '/')}")

    def _on_ftp_push(self) -> None:
        if not self.manager:
            return
        ip = self.ftp_ip_var.get().strip()
        port = int(self.ftp_port_var.get().strip() or "2121")
        rdir = self.ftp_dir_var.get().strip()
        if not ip or not rdir:
            messagebox.showwarning("Missing FTP Settings", "Enter your PS5 IP address and remote mount path.")
            return
        self._run_bg(
            lambda: self.manager.deploy_via_ftp(ip, port, rdir, log_fn=self.log),  # type: ignore[union-attr]
            lambda _: messagebox.showinfo("FTP Push Complete", f"Successfully pushed & verified files on {ip}:{port}!"),
        )


def launch_gui() -> None:
    _enable_windows_dpi_awareness()
    root = tk.Tk()
    ConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    launch_gui()
