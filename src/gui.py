"""
Photo Gallery GUI

Dark-mode photo gallery with CLIP semantic search, inspired by iPhone Photos.
Uses a raw tk.Canvas with virtual scrolling for performance with thousands of images.
"""

import json
import sys
import threading
import queue
import tkinter as tk
from collections import OrderedDict
from tkinter import filedialog
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from datetime import datetime

import customtkinter as ctk
from PIL import Image, ImageTk

from .ios_backup import (
    IMAGE_EXTENSIONS,
    iOSBackupDecryptor,
    check_encryption_status,
    get_backup_device_name,
    run_extraction,
)
from .semantic import forensic_image_open

# Register HEIC support
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

# Colors — greyscale dark mode
BG_DARK = "#141414"
BG_CARD = "#1e1e1e"
BG_SEARCH = "#2b2b2b"
BG_HOVER = "#3a3a3a"
BG_BTN = "#4a4a4a"
BG_BTN_HOVER = "#5a5a5a"
BG_BTN_ALT = "#333333"
BG_BTN_ALT_HOVER = "#444444"
TEXT_PRIMARY = "#e0e0e0"
TEXT_MUTED = "#888888"
TEXT_DIM = "#666666"
ERROR_COLOR = "#cc4444"
SUCCESS_COLOR = "#aaaaaa"
ACCENT_COLOR = "#2a6dd4"
ACCENT_HOVER = "#3578e0"

SEARCH_PRESETS = {
    "Weapons": "gun firearm knife weapon ammunition rifle pistol sword handgun",
    "Drugs": "drugs narcotics pills marijuana cocaine powder substance paraphernalia",
    "Currency": "cash money currency banknotes bills coins credit card",
    "Documents": "document identification passport drivers license ID card paperwork",
    "Vehicles": "car vehicle automobile truck license plate motorcycle",
    "Screenshots": "screenshot phone screen computer screen text message notification",
    "Faces": "person face portrait selfie headshot people",
    "Locations": "map location address building house street sign landmark",
}

THUMB_SIZE = 150
CELL_SIZE = THUMB_SIZE + 8  # thumb + padding


class _ExtractionCancelled(Exception):
    """Raised internally when the user cancels an extraction."""
    pass


def _count_images(path: Path) -> int:
    """Count image files in a directory."""
    return sum(1 for p in path.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
               and not p.parent.name.startswith("."))


# ---------------------------------------------------------------------------
# Main app — single window that switches between selector and gallery views
# ---------------------------------------------------------------------------

class App(ctk.CTk):
    def __init__(self, base_output: str):
        super().__init__()

        self.base_output = Path(base_output)
        self.base_output.mkdir(parents=True, exist_ok=True)

        self.title("Semantic Search for iOS Photos")
        self.geometry("1100x750")
        self.minsize(600, 400)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.configure(fg_color=BG_DARK)

        # Container for swapping views
        self._current_view: Optional[ctk.CTkFrame] = None

        self.show_selector()

    def _clear_view(self):
        if self._current_view is not None:
            self._current_view.destroy()
            self._current_view = None

    def show_selector(self):
        self._clear_view()
        self.title("Semantic Search for iOS Photos")
        view = SelectorView(self, self.base_output)
        view.pack(fill="both", expand=True)
        self._current_view = view

    def show_gallery(self, image_dir: str, index_dir: str):
        self._clear_view()
        self.title("Photos")
        view = GalleryView(self, image_dir, index_dir)
        view.pack(fill="both", expand=True)
        self._current_view = view


# ---------------------------------------------------------------------------
# Selector view — lists existing phone dirs + new extraction
# ---------------------------------------------------------------------------

class SelectorView(ctk.CTkFrame):
    def __init__(self, master: App, base_output: Path):
        super().__init__(master, fg_color=BG_DARK)
        self.app = master
        self.base_output = base_output

        self._selected_path: Optional[Path] = None
        self._password: Optional[str] = None
        self._is_encrypted = False
        self._cancel_event: Optional[threading.Event] = None
        self._extraction_output: Optional[Path] = None

        # Container that gets swapped between screens
        self._container = ctk.CTkFrame(self, fg_color=BG_DARK)
        self._container.pack(fill="both", expand=True)

        self._show_screen("home")

    # ------------------------------------------------------------------
    # Screen switching
    # ------------------------------------------------------------------

    def _show_screen(self, name: str):
        for child in self._container.winfo_children():
            child.destroy()
        if name == "home":
            self._build_home()
        elif name == "existing":
            self._build_existing()
        elif name == "new_selected":
            self._build_new_selected_screen()

    # ------------------------------------------------------------------
    # Helper: back button
    # ------------------------------------------------------------------

    def _add_back_button(self, parent):
        self._back_btn = ctk.CTkButton(
            parent, text="\u2190  Back",
            font=ctk.CTkFont(size=14),
            fg_color="transparent", hover_color=BG_HOVER,
            text_color=TEXT_MUTED, height=34, anchor="w",
            command=lambda: self._show_screen("home"),
        )
        self._back_btn.pack(anchor="nw", padx=20, pady=(16, 0))

    # ------------------------------------------------------------------
    # Screen 1: Home — two centered buttons
    # ------------------------------------------------------------------

    def _build_home(self):
        center = ctk.CTkFrame(self._container, fg_color=BG_DARK)
        center.place(relx=0.5, rely=0.5, anchor="center")

        ctk.CTkButton(
            center, text="Existing Extractions",
            font=ctk.CTkFont(size=14),
            fg_color=BG_BTN, hover_color=BG_BTN_HOVER,
            text_color=TEXT_PRIMARY,
            height=48, corner_radius=10, width=220,
            command=lambda: self._show_screen("existing"),
        ).pack(pady=(0, 12))

        ctk.CTkButton(
            center, text="New Extraction",
            font=ctk.CTkFont(size=14),
            fg_color=BG_BTN, hover_color=BG_BTN_HOVER,
            text_color=TEXT_PRIMARY,
            height=48, corner_radius=10, width=220,
            command=self._browse_backup,
        ).pack()

    # ------------------------------------------------------------------
    # Screen 2: Existing Extractions
    # ------------------------------------------------------------------

    def _build_existing(self):
        self._add_back_button(self._container)

        # Show loading state while scanning in background
        self._existing_loading = ctk.CTkLabel(
            self._container, text="Scanning...",
            font=ctk.CTkFont(size=14), text_color=TEXT_MUTED,
        )
        self._existing_loading.place(relx=0.5, rely=0.5, anchor="center")

        def scan():
            existing = self._find_existing_phones()
            self.after(0, lambda: self._populate_existing(existing))

        threading.Thread(target=scan, daemon=True).start()

    def _populate_existing(self, existing):
        self._existing_loading.destroy()

        if not existing:
            msg = ctk.CTkFrame(self._container, fg_color=BG_DARK)
            msg.place(relx=0.5, rely=0.5, anchor="center")
            ctk.CTkLabel(
                msg, text="No Existing Extractions",
                font=ctk.CTkFont(size=16),
                text_color=TEXT_MUTED,
            ).pack()
            return

        # Centered list — scrollable only when content exceeds max height
        ROW_HEIGHT = 54  # 48px row + 6px vertical padding
        MAX_VISIBLE_ROWS = 8
        need_scroll = len(existing) > MAX_VISIBLE_ROWS

        if need_scroll:
            list_wrapper = ctk.CTkFrame(self._container, fg_color=BG_DARK, width=500)
            list_wrapper.place(relx=0.5, rely=0.5, anchor="center",
                               relheight=0.75)
            parent = ctk.CTkScrollableFrame(
                list_wrapper, fg_color=BG_DARK,
                scrollbar_button_color=BG_HOVER,
                scrollbar_button_hover_color="#555555",
            )
            parent.pack(fill="both", expand=True)
        else:
            content_height = len(existing) * ROW_HEIGHT
            parent = ctk.CTkFrame(
                self._container, fg_color=BG_DARK,
                width=500, height=content_height,
            )
            parent.place(relx=0.5, rely=0.5, anchor="center")
            parent.pack_propagate(False)

        for phone_dir, count in existing:
            row = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=10, height=48)
            row.pack(fill="x", pady=3)
            row.pack_propagate(False)

            ctk.CTkButton(
                row,
                text=f"  {phone_dir.name}   \u2014   {count} photos",
                font=ctk.CTkFont(size=14), anchor="w",
                fg_color=BG_CARD, hover_color=BG_HOVER,
                text_color=TEXT_PRIMARY,
                height=48, corner_radius=10,
                command=lambda d=phone_dir: self._open_phone(d),
            ).pack(side="left", fill="both", expand=True)

            ctk.CTkButton(
                row, text="X",
                font=ctk.CTkFont(size=13, weight="bold"),
                fg_color=BG_CARD, hover_color=ERROR_COLOR,
                text_color=TEXT_MUTED,
                width=40, height=48, corner_radius=10,
                command=lambda d=phone_dir: self._confirm_delete(d),
            ).pack(side="right")

    # ------------------------------------------------------------------
    # Screen 3: New Extraction (post-browse, backup selected)
    # ------------------------------------------------------------------

    def _build_new_selected_screen(self):
        """Show extraction UI after a backup has been selected via browse."""
        self._add_back_button(self._container)

        self._new_center = ctk.CTkFrame(self._container, fg_color=BG_DARK)
        self._new_center.place(relx=0.5, rely=0.5, anchor="center")

        # Path display
        self._path_var = tk.StringVar(value=str(self._selected_path))
        ctk.CTkLabel(
            self._new_center, textvariable=self._path_var,
            font=ctk.CTkFont(size=12),
            text_color=TEXT_MUTED, wraplength=400,
        ).pack(pady=(0, 8))

        # Status (device name + encryption)
        self.status_label = ctk.CTkLabel(
            self._new_center, text="",
            font=ctk.CTkFont(size=12),
            text_color=SUCCESS_COLOR, height=24,
        )
        self.status_label.pack(pady=(0, 8))

        device_name = get_backup_device_name(self._selected_path)
        enc_text = " (encrypted)" if self._is_encrypted else ""
        self.status_label.configure(text=f"{device_name}{enc_text}")

        # Password field (encrypted only)
        if self._is_encrypted:
            self._password_frame = ctk.CTkFrame(self._new_center, fg_color=BG_DARK)
            self._password_frame.pack(pady=(0, 8))

            self._password_entry = ctk.CTkEntry(
                self._password_frame, show="*",
                placeholder_text="Backup password",
                height=38, font=ctk.CTkFont(size=14),
                corner_radius=10, width=220,
                fg_color=BG_SEARCH, border_color="#3a3a3a",
                text_color=TEXT_PRIMARY,
                placeholder_text_color=TEXT_DIM,
                border_width=1,
            )
            self._password_entry.pack()
            self._password_entry.bind("<Return>", lambda e: self._on_extract())
            self._password_entry.focus_set()

            self._password_error = ctk.CTkLabel(
                self._password_frame, text="",
                font=ctk.CTkFont(size=11),
                text_color=ERROR_COLOR, height=20,
            )

        # Extract button
        self._extract_btn = ctk.CTkButton(
            self._new_center, text="Extract",
            font=ctk.CTkFont(size=14),
            fg_color=BG_BTN, hover_color=BG_BTN_HOVER,
            text_color=TEXT_PRIMARY,
            height=48, corner_radius=10, width=220,
            command=self._on_extract,
        )
        self._extract_btn.pack(pady=(0, 8))

    # ------------------------------------------------------------------
    # Data helpers (unchanged logic)
    # ------------------------------------------------------------------

    def _find_existing_phones(self) -> list:
        """Find subdirectories of base_output that contain images."""
        results = []
        if not self.base_output.exists():
            return results
        for sub in sorted(self.base_output.iterdir()):
            if sub.is_dir():
                count = _count_images(sub)
                if count > 0:
                    results.append((sub, count))
        return results

    def _open_phone(self, phone_dir: Path):
        index_dir = str(phone_dir / ".search_index")
        self.app.show_gallery(str(phone_dir), index_dir)

    def _confirm_delete(self, phone_dir: Path):
        # Replace container content with inline confirmation
        for child in self._container.winfo_children():
            child.destroy()

        self._add_back_button(self._container)

        confirm_frame = ctk.CTkFrame(self._container, fg_color=BG_DARK)
        confirm_frame.place(relx=0.5, rely=0.5, anchor="center")

        ctk.CTkLabel(
            confirm_frame, text=f"Delete \"{phone_dir.name}\"?",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=TEXT_PRIMARY,
        ).pack(pady=(0, 8))

        ctk.CTkLabel(
            confirm_frame, text="This will permanently delete all extracted images.",
            font=ctk.CTkFont(size=13),
            text_color=TEXT_MUTED,
        ).pack(pady=(0, 16))

        btn_row = ctk.CTkFrame(confirm_frame, fg_color=BG_DARK)
        btn_row.pack()

        ctk.CTkButton(
            btn_row, text="Cancel",
            command=lambda: self._show_screen("existing"),
            fg_color=BG_BTN, hover_color=BG_BTN_HOVER,
            text_color=TEXT_PRIMARY,
            height=36, corner_radius=10, width=120,
        ).pack(side="left", padx=8)

        def do_delete():
            import shutil
            shutil.rmtree(phone_dir, ignore_errors=True)
            self._show_screen("existing")

        ctk.CTkButton(
            btn_row, text="Delete", command=do_delete,
            fg_color=ERROR_COLOR, hover_color="#aa3333",
            text_color=TEXT_PRIMARY,
            height=36, corner_radius=10, width=120,
        ).pack(side="left", padx=8)

    # ------------------------------------------------------------------
    # Browse + extraction logic
    # ------------------------------------------------------------------

    def _browse_backup(self):
        path = filedialog.askdirectory(title="Select iOS Backup Folder")
        if not path:
            return

        backup_path = Path(path)
        if not (backup_path / "Manifest.plist").exists():
            # Show error inline on home screen
            for child in self._container.winfo_children():
                child.destroy()
            self._add_back_button(self._container)
            err_center = ctk.CTkFrame(self._container, fg_color=BG_DARK)
            err_center.place(relx=0.5, rely=0.5, anchor="center")
            ctk.CTkLabel(
                err_center, text="Invalid: no Manifest.plist found",
                font=ctk.CTkFont(size=13), text_color=ERROR_COLOR,
            ).pack(pady=(0, 12))
            ctk.CTkButton(
                err_center, text="Browse Again",
                font=ctk.CTkFont(size=14),
                fg_color=BG_BTN, hover_color=BG_BTN_HOVER,
                text_color=TEXT_PRIMARY,
                height=48, corner_radius=10, width=220,
                command=self._browse_backup,
            ).pack()
            return

        self._selected_path = backup_path
        self._is_encrypted = check_encryption_status(backup_path)
        self._password = None

        self._show_screen("new_selected")

    def _on_extract(self):
        self._extract_btn.configure(state="disabled", text="Working...")

        if self._is_encrypted:
            password = self._password_entry.get()
            if not password:
                self._password_error.configure(text="Password cannot be empty")
                self._password_error.pack(pady=(2, 0))
                self._extract_btn.configure(state="normal", text="Extract")
                return

            def verify():
                decryptor = iOSBackupDecryptor(str(self._selected_path))
                result = decryptor.decrypt_with_password(password)
                if not result.success:
                    self.after(0, lambda: self._password_verify_failed())
                else:
                    self.after(0, lambda: self._password_verify_ok(password))

            self.status_label.configure(text="Verifying password...", text_color=TEXT_MUTED)
            threading.Thread(target=verify, daemon=True).start()
        else:
            self._start_extraction(None)

    def _password_verify_failed(self):
        self._password_error.configure(text="Incorrect password. Try again.")
        self._password_error.pack(pady=(2, 0))
        self._password_entry.delete(0, "end")
        self._password_entry.focus_set()
        self._extract_btn.configure(state="normal", text="Extract")
        self.status_label.configure(text="", text_color=TEXT_MUTED)

    def _password_verify_ok(self, password: str):
        self._password_error.pack_forget()
        self._start_extraction(password)

    def _start_extraction(self, password: Optional[str]):
        self._extract_btn.configure(state="disabled", text="Extracting...")
        self.status_label.configure(text="Starting extraction...", text_color=TEXT_MUTED)

        # Swap back button to Stop
        if hasattr(self, '_back_btn') and self._back_btn.winfo_exists():
            self._back_btn.configure(text="Stop", command=self._stop_extraction)

        cancel = threading.Event()
        self._cancel_event = cancel

        def run():
            output_path = None
            try:
                backup_path = self._selected_path
                device_name = get_backup_device_name(backup_path)
                output_path = self.base_output / device_name
                output_path.mkdir(parents=True, exist_ok=True)
                self._extraction_output = output_path
                index_dir = str(output_path / ".search_index")

                def check_cancel():
                    if cancel.is_set():
                        raise _ExtractionCancelled()

                def extract_progress(current, total, filename):
                    check_cancel()
                    self.after(0, lambda c=current, t=total: self.status_label.configure(
                        text=f"Extracting {c}/{t}..."
                    ))

                def meta_progress(current, total):
                    check_cancel()
                    self.after(0, lambda c=current, t=total: self.status_label.configure(
                        text=f"Extracting metadata {c}/{t}..."
                    ))

                def idx_progress(current, total):
                    check_cancel()
                    self.after(0, lambda c=current, t=total: self.status_label.configure(
                        text=f"Indexing {c}/{t}..."
                    ))

                def status_update(msg):
                    check_cancel()
                    self.after(0, lambda: self.status_label.configure(text=msg))

                manifest = run_extraction(
                    backup_path,
                    output_path,
                    password=password,
                    extract_progress=extract_progress,
                    metadata_progress=meta_progress,
                    index_progress=idx_progress,
                    status_update=status_update,
                )

                if cancel.is_set():
                    raise _ExtractionCancelled()

                if manifest is None:
                    self.after(0, lambda: self._extraction_error("No images found in backup."))
                    return

                self.after(0, lambda: self.app.show_gallery(str(output_path), index_dir))

            except _ExtractionCancelled:
                # Clean up partial output
                if output_path and output_path.exists():
                    import shutil
                    shutil.rmtree(output_path, ignore_errors=True)
            except Exception as exc:
                if cancel.is_set():
                    # Cancelled during error — still clean up
                    if output_path and output_path.exists():
                        import shutil
                        shutil.rmtree(output_path, ignore_errors=True)
                    return
                msg = str(exc)
                self.after(0, lambda: self._extraction_error(msg))

        threading.Thread(target=run, daemon=True).start()

    def _stop_extraction(self):
        """Cancel the running extraction and return to home."""
        if self._cancel_event:
            self._cancel_event.set()
            self._cancel_event = None
        self._extraction_output = None
        self._show_screen("home")

    def _extraction_error(self, message: str):
        self.status_label.configure(text=f"Error: {message}", text_color=ERROR_COLOR)
        self._extract_btn.configure(state="normal", text="Extract")


# ---------------------------------------------------------------------------
# Gallery view — photo grid with search, back button
# ---------------------------------------------------------------------------

class GalleryView(ctk.CTkFrame):
    def __init__(self, master: App, image_dir: str, index_dir: str):
        super().__init__(master, fg_color=BG_DARK)
        self.app = master
        self.image_dir = Path(image_dir)
        self.index_dir = index_dir
        self.all_image_paths: List[str] = []
        self.display_paths: List[str] = []
        self.thumb_cache: OrderedDict = OrderedDict()
        self._thumb_cache_max = 512
        self.thumb_queue = queue.Queue()
        self._search_after_id = None
        self._search_index = None
        self._columns = 5
        self._rendered_indices: set = set()
        self._loading_paths: set = set()
        self._file_manifest: Optional[dict] = None
        self._active_preset: Optional[str] = None
        self._preset_buttons: dict = {}
        self._threshold_value: float = 0.20
        self._poll_id = None
        self._date_filter_frame: Optional[ctk.CTkFrame] = None
        self._sort_order: Optional[str] = None  # None, "newest", or "oldest"
        self._scroll_after_id = None
        self._load_generation = 0
        self._thumb_executor = ThreadPoolExecutor(max_workers=8)

        # Thumbnail cache dir
        self.thumb_dir = self.image_dir / ".thumbnails"
        self.thumb_dir.mkdir(exist_ok=True)

        self._build_ui()
        self._update_status("Loading photos...")

        # Load manifest
        manifest_path = self.image_dir / "file_manifest.json"
        if manifest_path.exists():
            try:
                with open(manifest_path) as f:
                    self._file_manifest = json.load(f)
            except Exception:
                pass

        # Load search index in background
        threading.Thread(target=self._load_search_index, daemon=True).start()

        # Collect image paths in background to avoid blocking UI
        def _collect_and_display():
            self._collect_image_paths()
            paths = list(self.all_image_paths)
            def apply():
                self.display_paths = self._maybe_sort(paths)
                self._path_idx_cache = {p: i for i, p in enumerate(self.display_paths)}
                self._update_status(f"{len(self.all_image_paths)} photos")
                self._full_layout()
            self.after(0, apply)

        threading.Thread(target=_collect_and_display, daemon=True).start()

        # Initial layout after window is mapped
        self.after(100, self._full_layout)

    def _build_ui(self):
        # Top bar
        top_frame = ctk.CTkFrame(self, fg_color=BG_DARK, height=50)
        top_frame.pack(fill="x", padx=20, pady=(16, 0))
        top_frame.pack_propagate(False)

        back_btn = ctk.CTkButton(
            top_frame, text="< Back",
            font=ctk.CTkFont(size=14),
            fg_color="transparent", hover_color=BG_HOVER,
            text_color=TEXT_MUTED, width=70, height=34,
            command=self._go_back,
        )
        back_btn.pack(side="left", pady=8)

        title = ctk.CTkLabel(
            top_frame, text=self.image_dir.name,
            font=ctk.CTkFont(size=28, weight="bold"),
            text_color=TEXT_PRIMARY,
        )
        title.pack(side="left", padx=12, pady=8)

        # Threshold slider (right side of header)
        self._threshold_label = ctk.CTkLabel(
            top_frame, text="0.20",
            font=ctk.CTkFont(size=11), text_color=TEXT_MUTED, width=32,
        )
        self._threshold_label.pack(side="right", padx=(0, 4), pady=8)

        self._threshold_slider = ctk.CTkSlider(
            top_frame, from_=0.15, to=0.40,
            number_of_steps=25, width=100, height=16,
            button_color=ACCENT_COLOR, button_hover_color=ACCENT_HOVER,
            progress_color=BG_BTN, fg_color=BG_SEARCH,
            command=self._on_threshold_change,
        )
        self._threshold_slider.set(0.20)
        self._threshold_slider.pack(side="right", padx=2, pady=8)

        ctk.CTkLabel(
            top_frame, text="Threshold:",
            font=ctk.CTkFont(size=11), text_color=TEXT_MUTED,
        ).pack(side="right", padx=(16, 0), pady=8)

        # Search bar
        search_frame = ctk.CTkFrame(self, fg_color=BG_DARK, height=50)
        self._search_frame = search_frame
        search_frame.pack(fill="x", padx=20, pady=(12, 0))
        search_frame.pack_propagate(False)

        self.search_entry = ctk.CTkEntry(
            search_frame,
            placeholder_text="Search photos...",
            height=38,
            font=ctk.CTkFont(size=14),
            corner_radius=19,
            fg_color=BG_SEARCH,
            border_color="#3a3a3a",
            text_color=TEXT_PRIMARY,
            placeholder_text_color=TEXT_DIM,
            border_width=1,
        )
        self.search_entry.pack(fill="x", pady=6)
        self.search_entry.bind("<KeyRelease>", self._on_search_key)

        # Controls row — presets, date range, threshold
        controls_frame = ctk.CTkFrame(self, fg_color=BG_DARK)
        controls_frame.pack(fill="x", padx=20, pady=(4, 4))

        # Preset filter chips
        for name in list(SEARCH_PRESETS):
            btn = ctk.CTkButton(
                controls_frame, text=name,
                font=ctk.CTkFont(size=12),
                fg_color=BG_BTN, hover_color=BG_BTN_HOVER,
                text_color=TEXT_PRIMARY,
                height=28, corner_radius=14, width=0,
                command=lambda n=name: self._on_preset_click(n),
            )
            btn.pack(side="left", padx=(0, 4), pady=4)
            self._preset_buttons[name] = btn

        # Separator
        ctk.CTkLabel(
            controls_frame, text="|",
            font=ctk.CTkFont(size=12), text_color=TEXT_DIM,
        ).pack(side="left", padx=(8, 8), pady=4)

        # Date range controls
        ctk.CTkLabel(
            controls_frame, text="From:",
            font=ctk.CTkFont(size=11), text_color=TEXT_MUTED,
        ).pack(side="left", padx=(0, 4), pady=4)

        self._date_from_entry = ctk.CTkEntry(
            controls_frame,
            placeholder_text="YYYY-MM-DD",
            height=28, width=105,
            font=ctk.CTkFont(size=11),
            corner_radius=8,
            fg_color=BG_SEARCH, border_color="#3a3a3a",
            text_color=TEXT_PRIMARY,
            placeholder_text_color=TEXT_MUTED,
            border_width=1,
        )
        self._date_from_entry.pack(side="left", padx=(0, 6), pady=4)
        self._date_from_entry.bind("<KeyRelease>", lambda e: self._auto_format_date(self._date_from_entry))

        ctk.CTkLabel(
            controls_frame, text="To:",
            font=ctk.CTkFont(size=11), text_color=TEXT_MUTED,
        ).pack(side="left", padx=(0, 4), pady=4)

        self._date_to_entry = ctk.CTkEntry(
            controls_frame,
            placeholder_text="YYYY-MM-DD",
            height=28, width=105,
            font=ctk.CTkFont(size=11),
            corner_radius=8,
            fg_color=BG_SEARCH, border_color="#3a3a3a",
            text_color=TEXT_PRIMARY,
            placeholder_text_color=TEXT_MUTED,
            border_width=1,
        )
        self._date_to_entry.pack(side="left", padx=(0, 6), pady=4)
        self._date_to_entry.bind("<KeyRelease>", lambda e: self._auto_format_date(self._date_to_entry))

        self._date_apply_btn = ctk.CTkButton(
            controls_frame, text="Apply",
            font=ctk.CTkFont(size=11),
            fg_color=BG_BTN, hover_color=BG_BTN_HOVER,
            text_color=TEXT_PRIMARY,
            height=28, corner_radius=8, width=50,
            command=self._apply_date_filter,
        )
        self._date_apply_btn.pack(side="left", padx=(0, 4), pady=4)

        self._date_clear_btn = ctk.CTkButton(
            controls_frame, text="Clear",
            font=ctk.CTkFont(size=11),
            fg_color=BG_BTN_ALT, hover_color=BG_BTN_ALT_HOVER,
            text_color=TEXT_MUTED,
            height=28, corner_radius=8, width=50,
            command=self._clear_date_filter,
        )
        self._date_clear_btn.pack(side="left", pady=4)

        # Sort toggle (right side of controls row)
        self._sort_oldest_btn = ctk.CTkButton(
            controls_frame, text="Oldest",
            font=ctk.CTkFont(size=11),
            fg_color=BG_BTN_ALT, hover_color=BG_BTN_ALT_HOVER,
            text_color=TEXT_MUTED,
            height=28, corner_radius=8, width=55,
            command=lambda: self._set_sort_order("oldest"),
        )
        self._sort_oldest_btn.pack(side="right", padx=(0, 4), pady=4)

        self._sort_newest_btn = ctk.CTkButton(
            controls_frame, text="Newest",
            font=ctk.CTkFont(size=11),
            fg_color=BG_BTN_ALT, hover_color=BG_BTN_ALT_HOVER,
            text_color=TEXT_MUTED,
            height=28, corner_radius=8, width=60,
            command=lambda: self._set_sort_order("newest"),
        )
        self._sort_newest_btn.pack(side="right", padx=(0, 4), pady=4)

        ctk.CTkLabel(
            controls_frame, text="Sort:",
            font=ctk.CTkFont(size=11), text_color=TEXT_MUTED,
        ).pack(side="right", padx=(8, 4), pady=4)

        # Canvas with scrollbar for virtual scrolling
        canvas_frame = ctk.CTkFrame(self, fg_color=BG_DARK)
        canvas_frame.pack(fill="both", expand=True, padx=16, pady=(0, 0))

        self.canvas = tk.Canvas(
            canvas_frame, bg=BG_DARK, highlightthickness=0,
            borderwidth=0, relief="flat",
        )
        self.scrollbar = ctk.CTkScrollbar(canvas_frame, command=self.canvas.yview)

        def _on_scroll_change(*args):
            self.scrollbar.set(*args)
            if self._scroll_after_id is not None:
                self.after_cancel(self._scroll_after_id)
            self._scroll_after_id = self.after(40, self._render_visible)

        self.canvas.configure(yscrollcommand=_on_scroll_change)
        # Pixel-based scroll increment for smooth cross-platform scrolling
        self.canvas.configure(yscrollincrement=4)

        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        # Mouse wheel scrolling (platform-specific bindings)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", self._on_mousewheel)
        self.canvas.bind("<Button-5>", self._on_mousewheel)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Button-1>", self._on_canvas_click)

        # Status bar
        self.status_label = ctk.CTkLabel(
            self, text="", font=ctk.CTkFont(size=12),
            text_color=TEXT_MUTED, height=30,
        )
        self.status_label.pack(fill="x", padx=20, pady=(4, 10))

    def _go_back(self):
        self._thumb_executor.shutdown(wait=False)
        self.app.show_selector()

    # --- LRU thumbnail cache ---

    def _cache_get(self, path: str) -> Optional[ImageTk.PhotoImage]:
        """Get thumbnail from cache, updating LRU order."""
        if path in self.thumb_cache:
            self.thumb_cache.move_to_end(path)
            return self.thumb_cache[path]
        return None

    def _cache_put(self, path: str, photo: ImageTk.PhotoImage):
        """Insert thumbnail into cache, evicting oldest if over limit."""
        self.thumb_cache[path] = photo
        self.thumb_cache.move_to_end(path)
        while len(self.thumb_cache) > self._thumb_cache_max:
            self.thumb_cache.popitem(last=False)

    def _collect_image_paths(self):
        image_dir = Path(self.image_dir)
        self.all_image_paths = sorted(
            str(p) for p in image_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
            and not p.parent.name.startswith(".")
        )

    def _load_search_index(self):
        try:
            from .semantic import SemanticIndex
            self._search_index = SemanticIndex(self.index_dir)
        except Exception as exc:
            print(f"Could not load search index: {exc}")

    # --- Virtual scrolling ---

    def _full_layout(self):
        """Recalculate columns and update scroll region."""
        canvas_width = self.canvas.winfo_width()
        if canvas_width < 10:
            self.after(100, self._full_layout)
            return

        self._columns = max(2, canvas_width // CELL_SIZE)
        total_rows = (len(self.display_paths) + self._columns - 1) // self._columns
        total_height = total_rows * CELL_SIZE

        self.canvas.configure(scrollregion=(0, 0, canvas_width, total_height))
        self._render_visible()

    def _render_visible(self):
        """Only render thumbnails currently in the viewport."""
        if not self.display_paths:
            self.canvas.delete("thumb")
            self._rendered_indices.clear()
            return

        canvas_height = self.canvas.winfo_height()
        if canvas_height < 10:
            return

        y_top = self.canvas.canvasy(0)
        y_bottom = y_top + canvas_height

        first_row = max(0, int(y_top // CELL_SIZE) - 4)
        last_row = int(y_bottom // CELL_SIZE) + 4

        first_idx = first_row * self._columns
        last_idx = min((last_row + 1) * self._columns, len(self.display_paths))

        needed_indices = set(range(first_idx, last_idx))

        to_remove = self._rendered_indices - needed_indices
        for idx in to_remove:
            tag = f"t{idx}"
            self.canvas.delete(tag)
        self._rendered_indices -= to_remove

        to_add = needed_indices - self._rendered_indices
        paths_to_load = []

        for idx in to_add:
            if idx >= len(self.display_paths):
                continue

            path = self.display_paths[idx]
            row = idx // self._columns
            col = idx % self._columns
            x = col * CELL_SIZE + CELL_SIZE // 2
            y = row * CELL_SIZE + CELL_SIZE // 2
            tag = f"t{idx}"

            photo = self._cache_get(path)
            if photo is not None:
                self.canvas.create_image(x, y, image=photo, anchor="center", tags=("thumb", tag))
            else:
                half = THUMB_SIZE // 2
                self.canvas.create_rectangle(
                    x - half, y - half, x + half, y + half,
                    fill=BG_CARD, outline="", tags=("thumb", tag),
                )
                if path not in self._loading_paths:
                    paths_to_load.append(path)
                    self._loading_paths.add(path)

            self._rendered_indices.add(idx)

        if paths_to_load:
            gen = self._load_generation
            self._load_thumbnails_batch(paths_to_load, gen)
            self._start_polling()

    def _on_mousewheel(self, event):
        if event.num == 4:
            delta = -5
        elif event.num == 5:
            delta = 5
        elif sys.platform == "darwin":
            delta = -event.delta
        else:
            delta = int(-event.delta / 24)

        self.canvas.yview_scroll(delta, "units")
        if self._scroll_after_id is not None:
            self.after_cancel(self._scroll_after_id)
        self._scroll_after_id = self.after(40, self._render_visible)

    def _on_canvas_configure(self, event):
        new_cols = max(2, event.width // CELL_SIZE)
        if new_cols != self._columns:
            self._columns = new_cols
            self.canvas.delete("thumb")
            self._rendered_indices.clear()
            self._full_layout()
        else:
            self._render_visible()

    def _on_canvas_click(self, event):
        canvas_x = self.canvas.canvasx(event.x)
        canvas_y = self.canvas.canvasy(event.y)

        col = int(canvas_x // CELL_SIZE)
        row = int(canvas_y // CELL_SIZE)
        idx = row * self._columns + col

        if 0 <= idx < len(self.display_paths):
            self._on_thumbnail_click(self.display_paths[idx])

    # --- Thumbnail loading ---

    def _get_thumbnail_path(self, image_path: str) -> Path:
        file_id = Path(image_path).stem
        return self.thumb_dir / f"{file_id}.jpg"

    def _generate_thumbnail(self, image_path: str) -> Optional[Image.Image]:
        cached_path = self._get_thumbnail_path(image_path)

        if cached_path.exists():
            try:
                return Image.open(cached_path)
            except Exception:
                pass

        try:
            try:
                img = Image.open(image_path)
                img.draft("RGB", (THUMB_SIZE * 2, THUMB_SIZE * 2))
                img = img.convert("RGB")
            except Exception:
                img = forensic_image_open(image_path).convert("RGB")
            w, h = img.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            img = img.crop((left, top, left + side, top + side))
            img = img.resize((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
            img.save(cached_path, "JPEG", quality=80)
            return img
        except Exception:
            return None

    def _load_thumbnails_batch(self, paths: List[str], generation: int):
        def worker(path):
            if self._load_generation != generation:
                return
            img = self._generate_thumbnail(path)
            if img and self._load_generation == generation:
                self.thumb_queue.put((path, img, generation))

        for path in paths:
            self._thumb_executor.submit(worker, path)

    def _start_polling(self):
        """Start the thumbnail poll loop if not already running."""
        if self._poll_id is None:
            self._poll_id = self.after(50, self._poll_thumbnails)

    def _poll_thumbnails(self):
        """Poll queue and update canvas items for loaded thumbnails."""
        self._poll_id = None
        count = 0
        while count < 15:
            try:
                path, pil_img, gen = self.thumb_queue.get_nowait()
                if gen != self._load_generation:
                    continue
                photo = ImageTk.PhotoImage(pil_img)
                self._cache_put(path, photo)
                self._loading_paths.discard(path)

                idx = self._path_idx_cache.get(path)
                if idx is not None:
                    if idx in self._rendered_indices:
                        tag = f"t{idx}"
                        self.canvas.delete(tag)
                        row = idx // self._columns
                        col = idx % self._columns
                        x = col * CELL_SIZE + CELL_SIZE // 2
                        y = row * CELL_SIZE + CELL_SIZE // 2
                        self.canvas.create_image(
                            x, y, image=photo, anchor="center", tags=("thumb", tag),
                        )
                count += 1
            except queue.Empty:
                break

        if self._loading_paths:
            self._poll_id = self.after(50, self._poll_thumbnails)

    # --- Search ---

    def _on_preset_click(self, preset_name: str):
        if self._active_preset == preset_name:
            # Deselect — reset to all photos
            self._active_preset = None
            self.search_entry.delete(0, "end")
            self._update_preset_styles()
            self.display_paths = self._maybe_sort(list(self.all_image_paths))
            self._update_status(f"{len(self.all_image_paths)} photos")
            self._refresh_grid()
        else:
            # Activate preset
            self._active_preset = preset_name
            self.search_entry.delete(0, "end")
            self.search_entry.insert(0, SEARCH_PRESETS[preset_name])
            self._update_preset_styles()
            self._perform_search()

    def _update_preset_styles(self):
        for name, btn in self._preset_buttons.items():
            if name == self._active_preset:
                btn.configure(fg_color=ACCENT_COLOR, hover_color=ACCENT_HOVER)
            else:
                btn.configure(fg_color=BG_BTN, hover_color=BG_BTN_HOVER)

    def _set_sort_order(self, order: str):
        """Set sort order to 'newest', 'oldest', or toggle off if already active."""
        if self._sort_order == order:
            self._sort_order = None
        else:
            self._sort_order = order
        self._update_sort_styles()
        self._apply_sort_and_refresh()

    def _update_sort_styles(self):
        for btn, key in [(self._sort_newest_btn, "newest"), (self._sort_oldest_btn, "oldest")]:
            if self._sort_order == key:
                btn.configure(fg_color=ACCENT_COLOR, hover_color=ACCENT_HOVER, text_color=TEXT_PRIMARY)
            else:
                btn.configure(fg_color=BG_BTN_ALT, hover_color=BG_BTN_ALT_HOVER, text_color=TEXT_MUTED)

    def _sort_paths_by_date(self, paths: list, newest_first: bool = True) -> list:
        """Sort paths by date_created from photo_metadata. Paths without dates go to the end."""
        if not self._file_manifest:
            return paths

        sentinel = datetime.min if newest_first else datetime.max

        def _date_key(path):
            file_id = Path(path).stem
            meta = self._file_manifest.get(file_id, {})
            date_str = meta.get("photo_metadata", {}).get("date_created")
            if date_str:
                try:
                    dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    return dt.replace(tzinfo=None)
                except (ValueError, TypeError):
                    pass
            return sentinel

        return sorted(paths, key=_date_key, reverse=newest_first)

    def _maybe_sort(self, paths: list) -> list:
        """Apply current sort order to paths if one is active."""
        if self._sort_order:
            return self._sort_paths_by_date(paths, newest_first=(self._sort_order == "newest"))
        return paths

    def _apply_sort_and_refresh(self):
        """Re-sort current display_paths and refresh the grid."""
        self.display_paths = self._maybe_sort(self.display_paths)
        self._refresh_grid()

    def _on_threshold_change(self, value: float):
        self._threshold_value = round(value, 2)
        self._threshold_label.configure(text=f"{self._threshold_value:.2f}")
        # Re-trigger search if there's an active query
        query = self.search_entry.get().strip()
        if query:
            if self._search_after_id:
                self.after_cancel(self._search_after_id)
            self._search_after_id = self.after(300, self._perform_search)

    @staticmethod
    def _auto_format_date(entry):
        """Auto-insert hyphens and enforce YYYY-MM-DD format as user types."""
        raw = entry.get()
        # Strip non-digit characters, then rebuild with hyphens
        digits = "".join(c for c in raw if c.isdigit())
        digits = digits[:8]  # max 8 digits (YYYYMMDD)
        formatted = ""
        for i, d in enumerate(digits):
            if i == 4 or i == 6:
                formatted += "-"
            formatted += d
        if formatted != raw:
            entry.delete(0, "end")
            entry.insert(0, formatted)

    def _apply_date_filter(self):
        """Re-run the current search/display with date filtering applied."""
        query = self.search_entry.get().strip()
        if query:
            self._perform_search()
        else:
            # Filter all images by date
            filtered = self._filter_paths_by_date(list(self.all_image_paths))
            self.display_paths = self._maybe_sort(filtered)
            self._update_status(f"{len(filtered)} photos (date filtered)")
            self._refresh_grid()

    def _clear_date_filter(self):
        """Clear date filter fields and re-display."""
        self._date_from_entry.delete(0, "end")
        self._date_to_entry.delete(0, "end")
        query = self.search_entry.get().strip()
        if query:
            self._perform_search()
        else:
            self.display_paths = self._maybe_sort(list(self.all_image_paths))
            self._update_status(f"{len(self.all_image_paths)} photos")
            self._refresh_grid()

    def _filter_paths_by_date(self, paths: list) -> list:
        """Filter image paths by the date range set in the filter fields.

        Only filters images that have photo_metadata with date_created.
        Images without metadata are excluded when a date filter is active.
        """
        date_from = self._date_from_entry.get().strip() if self._date_from_entry else ""
        date_to = self._date_to_entry.get().strip() if self._date_to_entry else ""

        if not date_from and not date_to:
            return paths

        # Validate date strings
        try:
            from_dt = datetime.strptime(date_from, "%Y-%m-%d") if date_from else None
        except ValueError:
            from_dt = None
        try:
            to_dt = datetime.strptime(date_to, "%Y-%m-%d") if date_to else None
        except ValueError:
            to_dt = None

        if from_dt is None and to_dt is None:
            return paths

        if not self._file_manifest:
            return paths

        filtered = []
        for path in paths:
            file_id = Path(path).stem
            meta = self._file_manifest.get(file_id, {})
            photo_meta = meta.get("photo_metadata", {})
            date_str = photo_meta.get("date_created")

            if not date_str:
                continue

            try:
                # Parse ISO 8601 date — take just the date portion
                img_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                img_date_naive = img_date.replace(tzinfo=None)

                if from_dt and img_date_naive < from_dt:
                    continue
                if to_dt and img_date_naive > to_dt.replace(hour=23, minute=59, second=59):
                    continue

                filtered.append(path)
            except (ValueError, TypeError):
                continue

        return filtered

    def _on_search_key(self, event):
        if self._active_preset:
            self._active_preset = None
            self._update_preset_styles()
        if self._search_after_id:
            self.after_cancel(self._search_after_id)
        self._search_after_id = self.after(300, self._perform_search)

    def _perform_search(self):
        query = self.search_entry.get().strip()

        if not query:
            self.display_paths = self._maybe_sort(list(self.all_image_paths))
            self._update_status(f"{len(self.all_image_paths)} photos")
            self._refresh_grid()
            return

        if not self._search_index:
            self._update_status("Search index not loaded yet...")
            return

        self._update_status("Searching...")

        threshold = self._threshold_value

        def do_search():
            try:
                results = self._search_index.search(query, threshold=threshold)
                matched_paths = [r.file_path for r in results]
                self.after(0, lambda: self._show_search_results(query, matched_paths))
            except Exception as exc:
                msg = str(exc)
                self.after(0, lambda: self._update_status(f"Search error: {msg}"))

        threading.Thread(target=do_search, daemon=True).start()

    def _show_search_results(self, query: str, paths: List[str]):
        filtered = self._maybe_sort(self._filter_paths_by_date(paths))
        self.display_paths = filtered
        if len(filtered) != len(paths):
            self._update_status(f"{len(filtered)} results for \"{query}\" (date filtered from {len(paths)})")
        else:
            self._update_status(f"{len(filtered)} results for \"{query}\"")
        self._refresh_grid()

    def _refresh_grid(self):
        self._load_generation += 1
        self._path_idx_cache = {p: i for i, p in enumerate(self.display_paths)}
        self._loading_paths.clear()
        self._drain_stale_queue()
        self.canvas.delete("thumb")
        self._rendered_indices.clear()
        self._full_layout()
        self.canvas.yview_moveto(0)

    def _drain_stale_queue(self):
        """Discard all pending items in thumb_queue."""
        while True:
            try:
                self.thumb_queue.get_nowait()
            except queue.Empty:
                break

    def _update_status(self, text: str):
        self.status_label.configure(text=text)

    # --- Preview ---

    def _on_thumbnail_click(self, image_path: str):
        preview = ctk.CTkToplevel(self)
        preview.title(Path(image_path).name)
        preview.geometry("900x700")
        preview.configure(fg_color="#0a0a0a")
        preview.transient(self.app)
        preview.focus_set()

        def _safe_grab(event=None):
            try:
                preview.grab_set()
            except Exception:
                pass

        preview.after(50, _safe_grab)
        preview.bind("<Escape>", lambda e: preview.destroy())

        # Get metadata
        photo_meta = {}
        rel_path = ""
        if self._file_manifest:
            file_id = Path(image_path).stem
            meta = self._file_manifest.get(file_id, {})
            rel_path = meta.get("relative_path", "")
            photo_meta = meta.get("photo_metadata", {})

        # Main horizontal container
        content_frame = ctk.CTkFrame(preview, fg_color="#0a0a0a")
        content_frame.pack(fill="both", expand=True)

        # Sidebar (hidden by default)
        sidebar = ctk.CTkFrame(content_frame, fg_color=BG_CARD, width=280)
        sidebar_visible = [False]

        # Image area (left side)
        image_frame = ctk.CTkFrame(content_frame, fg_color="#0a0a0a")
        image_frame.pack(side="left", fill="both", expand=True)

        # Info button in top-right corner of image area
        btn_bar = ctk.CTkFrame(image_frame, fg_color="#0a0a0a", height=36)
        btn_bar.pack(fill="x", padx=8, pady=(8, 0))
        btn_bar.pack_propagate(False)

        if rel_path:
            ctk.CTkLabel(
                btn_bar, text=rel_path,
                font=ctk.CTkFont(size=11), text_color=TEXT_MUTED,
                anchor="w",
            ).pack(side="left", padx=4)

        def toggle_sidebar():
            if sidebar_visible[0]:
                sidebar.pack_forget()
                preview.geometry("900x700")
                info_btn.configure(fg_color=BG_BTN)
                sidebar_visible[0] = False
            else:
                sidebar.pack(side="right", fill="y", padx=(0, 0))
                preview.geometry("1180x700")
                info_btn.configure(fg_color=ACCENT_COLOR)
                sidebar_visible[0] = True

        info_btn = ctk.CTkButton(
            btn_bar, text="\u2139  Info",
            font=ctk.CTkFont(size=12),
            fg_color=BG_BTN, hover_color=BG_BTN_HOVER,
            text_color=TEXT_PRIMARY,
            width=70, height=28, corner_radius=14,
            command=toggle_sidebar,
        )
        info_btn.pack(side="right", padx=4)

        # Populate sidebar content
        self._build_info_sidebar(sidebar, photo_meta, rel_path)

        # Image label
        label = ctk.CTkLabel(image_frame, text="Loading...", text_color=TEXT_MUTED, fg_color="#0a0a0a")
        label.pack(expand=True, fill="both", padx=20, pady=(4, 20))

        def load():
            try:
                img = forensic_image_open(image_path).convert("RGB")
                max_w, max_h = 860, 620
                img.thumbnail((max_w, max_h), Image.LANCZOS)

                def show(img=img):
                    if not preview.winfo_exists():
                        return
                    photo = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
                    label.configure(image=photo, text="")
                    label._preview_photo = photo

                self.after(0, show)
            except Exception as exc:
                def show_error(exc=exc):
                    if not preview.winfo_exists():
                        return
                    label.configure(text=f"Cannot open image:\n{exc}")
                self.after(0, show_error)

        threading.Thread(target=load, daemon=True).start()

    def _build_info_sidebar(self, sidebar, photo_meta: dict, rel_path: str):
        """Build the scrollable metadata sidebar content."""
        # Scrollable inner frame
        canvas = tk.Canvas(sidebar, bg=BG_CARD, highlightthickness=0, width=264)
        scrollbar = ctk.CTkScrollbar(sidebar, command=canvas.yview)
        inner = ctk.CTkFrame(canvas, fg_color=BG_CARD)

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw", width=264)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scrollbar.pack(side="right", fill="y", pady=8)

        # Mouse wheel scrolling
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _on_button4(event):
            canvas.yview_scroll(-3, "units")

        def _on_button5(event):
            canvas.yview_scroll(3, "units")

        for widget in (canvas, inner):
            widget.bind("<MouseWheel>", _on_mousewheel)
            widget.bind("<Button-4>", _on_button4)
            widget.bind("<Button-5>", _on_button5)

        if not photo_meta:
            ctk.CTkLabel(
                inner, text="No metadata available",
                font=ctk.CTkFont(size=12), text_color=TEXT_DIM,
            ).pack(padx=8, pady=20)
            return

        def add_section(title):
            ctk.CTkLabel(
                inner, text=title,
                font=ctk.CTkFont(size=13, weight="bold"), text_color=TEXT_PRIMARY,
                anchor="w",
            ).pack(fill="x", padx=8, pady=(12, 4))
            # Separator line
            sep = ctk.CTkFrame(inner, fg_color=BG_HOVER, height=1)
            sep.pack(fill="x", padx=8, pady=(0, 6))

        def add_field(label_text, value):
            if value is None:
                return
            row = ctk.CTkFrame(inner, fg_color=BG_CARD)
            row.pack(fill="x", padx=8, pady=1)
            ctk.CTkLabel(
                row, text=label_text,
                font=ctk.CTkFont(size=11), text_color=TEXT_DIM,
                width=100, anchor="w",
            ).pack(side="left")
            ctk.CTkLabel(
                row, text=str(value),
                font=ctk.CTkFont(size=11), text_color=TEXT_PRIMARY,
                anchor="w", wraplength=150,
            ).pack(side="left", fill="x", expand=True)

        # --- General ---
        add_section("General")
        add_field("Filename", photo_meta.get("original_filename"))
        fs = photo_meta.get("original_file_size")
        if fs is not None:
            if fs >= 1_048_576:
                add_field("File size", f"{fs / 1_048_576:.1f} MB")
            elif fs >= 1024:
                add_field("File size", f"{fs / 1024:.0f} KB")
            else:
                add_field("File size", f"{fs} B")
        add_field("Type", photo_meta.get("uniform_type"))
        add_field("Created", photo_meta.get("date_created"))
        add_field("Modified", photo_meta.get("date_modified"))
        add_field("Added", photo_meta.get("date_added"))
        add_field("Last shared", photo_meta.get("last_shared_date"))
        add_field("Media type", photo_meta.get("media_type"))
        dur = photo_meta.get("duration")
        if dur is not None:
            mins, secs = divmod(dur, 60)
            add_field("Duration", f"{int(mins)}:{secs:05.2f}")
        if rel_path:
            add_field("File path", rel_path)

        # --- Location ---
        lat = photo_meta.get("latitude")
        lon = photo_meta.get("longitude")
        if lat is not None or lon is not None:
            add_section("Location")
            add_field("Latitude", f"{lat:.6f}" if lat is not None else None)
            add_field("Longitude", f"{lon:.6f}" if lon is not None else None)

        # --- Camera ---
        camera_fields = ["camera_make", "camera_model", "lens_model", "iso",
                         "aperture", "focal_length", "focal_length_35mm",
                         "shutter_speed", "flash_fired", "metering_mode", "white_balance"]
        if any(photo_meta.get(f) is not None for f in camera_fields):
            add_section("Camera")
            make = photo_meta.get("camera_make")
            model = photo_meta.get("camera_model")
            if make and model:
                add_field("Device", f"{make} {model}")
            elif model:
                add_field("Device", model)
            elif make:
                add_field("Device", make)
            add_field("Lens", photo_meta.get("lens_model"))
            iso = photo_meta.get("iso")
            if iso is not None:
                add_field("ISO", str(int(iso)))
            ap = photo_meta.get("aperture")
            if ap is not None:
                add_field("Aperture", f"f/{ap:.1f}")
            fl = photo_meta.get("focal_length")
            if fl is not None:
                add_field("Focal length", f"{fl:.1f}mm")
            fl35 = photo_meta.get("focal_length_35mm")
            if fl35 is not None:
                add_field("35mm equiv", f"{fl35:.0f}mm")
            ss = photo_meta.get("shutter_speed")
            if ss is not None:
                if ss >= 1:
                    add_field("Shutter", f"{ss:.1f}s")
                else:
                    add_field("Shutter", f"1/{int(round(1/ss))}s")
            flash = photo_meta.get("flash_fired")
            if flash is not None:
                add_field("Flash", "Yes" if flash else "No")
            mm = photo_meta.get("metering_mode")
            if mm is not None:
                metering_names = {0: "Unknown", 1: "Average", 2: "Center-weighted",
                                  3: "Spot", 4: "Multi-spot", 5: "Pattern", 6: "Partial"}
                add_field("Metering", metering_names.get(mm, str(mm)))
            wb = photo_meta.get("white_balance")
            if wb is not None:
                add_field("White bal.", "Auto" if wb == 0 else "Manual" if wb == 1 else str(wb))

        # --- Dimensions ---
        dim_fields = ["width", "height", "original_width", "original_height", "color_space", "hdr_gain"]
        if any(photo_meta.get(f) is not None for f in dim_fields):
            add_section("Dimensions")
            w, h = photo_meta.get("width"), photo_meta.get("height")
            if w is not None and h is not None:
                add_field("Size", f"{w} \u00d7 {h}")
            ow, oh = photo_meta.get("original_width"), photo_meta.get("original_height")
            if ow is not None and oh is not None:
                add_field("Original", f"{ow} \u00d7 {oh}")
            cs = photo_meta.get("color_space")
            if cs is not None:
                cs_names = {1: "sRGB", 2: "Adobe RGB", 65535: "Uncalibrated"}
                add_field("Color space", cs_names.get(cs, str(cs)))
            hdr = photo_meta.get("hdr_gain")
            if hdr is not None:
                add_field("HDR gain", f"{hdr:.2f}")

        # --- Status ---
        status_fields = ["favorite", "hidden", "trashed", "view_count", "play_count"]
        if any(photo_meta.get(f) is not None for f in status_fields):
            add_section("Status")
            fav = photo_meta.get("favorite")
            if fav is not None:
                add_field("Favorite", "Yes" if fav else "No")
            hid = photo_meta.get("hidden")
            if hid is not None:
                add_field("Hidden", "Yes" if hid else "No")
            tr = photo_meta.get("trashed")
            if tr is not None:
                add_field("Trashed", "Yes" if tr else "No")
            add_field("Views", photo_meta.get("view_count"))
            add_field("Plays", photo_meta.get("play_count"))
            add_field("Source", photo_meta.get("source_db"))



def launch(base_output: str = "output_images"):
    app = App(base_output)
    app.mainloop()
