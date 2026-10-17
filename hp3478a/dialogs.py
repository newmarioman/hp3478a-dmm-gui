# -*- coding: utf-8 -*-

"""
Secondary Windows, Application Themes, Notification Sounds, and Display Information.

This module provides services for audio feedback leveraging chime, Tkinter theme 
customization, multi-monitor responsive geometry scaling using screeninfo, 
and global dialog abstractions for standard alerts and file selection wrappers.

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

import tkinter as tk
import webbrowser
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, font, messagebox, ttk

# Local Packages
from .config import AppConfig, UserSettings


class SoundNotifier:
    def __init__(self, enabled: bool = True) -> None:
        self._chime = None
        if enabled:
            try:
                import chime

                self._chime = chime
            except ImportError:
                pass

    def set_theme(self, theme: str) -> None:
        if self._chime is not None:
            try:
                self._chime.theme(theme)
            except (ValueError, OSError):
                pass

    def play(self, event: str) -> None:
        if self._chime is not None:
            try:
                getattr(self._chime, event)()
            except (OSError, RuntimeError):
                pass


class ThemeManager:
    def __init__(self, root: tk.Tk, sounds: SoundNotifier) -> None:
        self.root = root
        self.style = ttk.Style(root)
        self.sounds = sounds
        self.body_font = font.Font(root=root, family="Verdana", size=10)
        self.title_font = font.Font(root=root, family="Verdana", size=11, weight="bold")
        self.mono_font = font.Font(root=root, family="Courier", size=9)
        self._font_size = 10

    def resize_fonts(self, size: int) -> None:
        if size != self._font_size:
            self._font_size = size
            self.body_font.configure(size=size)
            self.title_font.configure(size=size + 1)
            self.mono_font.configure(size=max(8, size - 1))

    def _apply_fonts(self) -> None:
        for name in (
            "TLabel",
            "TEntry",
            "TCombobox",
            "TButton",
            "TCheckbutton",
            "TLabelframe.Label",
            "TNotebook.Tab",
        ):
            self.style.configure(name, font=self.body_font)
        self.style.configure("Title.TLabel", font=self.title_font)
        self.root.option_add("*TCombobox*Listbox.font", self.body_font)
        self._set_input_fonts(self.root)

    def _set_input_fonts(self, parent) -> None:
        # Entry's widget font overrides its style, so set the shared label font explicitly.
        if isinstance(parent, ttk.Entry):
            parent.configure(font=self.body_font)
        # Internal ttk pop-down windows are owned by Tcl, outside Python's widget registry.
        for child in tuple(parent.children.values()):
            self._set_input_fonts(child)

    def available_themes(self) -> tuple[str, ...]:
        return tuple(self.style.theme_names())

    def apply(self, settings: UserSettings) -> None:
        themes = self.available_themes()
        selected = (
            settings.theme_style
            if settings.theme_style in themes
            else ("clam" if "clam" in themes else themes[0])
        )
        self.style.theme_use(selected)
        self.style.configure("TFrame", background="#f0f0f0")
        self.style.configure("TLabel", background="#f0f0f0")
        self.style.configure("Wild.TRadiobutton", background="#f0f0f0")
        self.style.map("TNotebook.Tab", background=[("selected", "#f0f0f0")])
        self._apply_fonts()
        self.sounds.set_theme(settings.audio_theme)


class DisplayService:
    """Use monitor information, falling back to Tkinter display information."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root

    def bounds(self) -> tuple[int, int, int, int]:
        fallback = (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        try:
            from screeninfo import get_monitors
            from screeninfo.common import ScreenInfoError
        except ImportError:
            return fallback

        try:
            monitors = get_monitors()
            # Follow the active window when it is on a secondary monitor.
            if self.root.winfo_width() > 1:
                cx = self.root.winfo_rootx() + self.root.winfo_width() // 2
                cy = self.root.winfo_rooty() + self.root.winfo_height() // 2
                for monitor in monitors:
                    x, y = getattr(monitor, "x", 0), getattr(monitor, "y", 0)
                    if x <= cx < x + monitor.width and y <= cy < y + monitor.height:
                        return x, y, monitor.width, monitor.height
            for monitor in monitors:
                if monitor.is_primary:
                    return (
                        getattr(monitor, "x", 0),
                        getattr(monitor, "y", 0),
                        monitor.width,
                        monitor.height,
                    )
            if monitors:
                monitor = monitors[0]
                return (
                    getattr(monitor, "x", 0),
                    getattr(monitor, "y", 0),
                    monitor.width,
                    monitor.height,
                )
        except (ScreenInfoError, RuntimeError, OSError):
            pass
        return fallback

    def dimensions(self) -> tuple[int, int]:
        return self.bounds()[2:]

    def scale_factor(self) -> float:
        return max(1.0, self.root.winfo_fpixels("1i") / 96)

    def fit_main_window(self, width: int, height: int, maximize: bool = True) -> None:
        mx, my, screen_width, screen_height = self.bounds()
        if maximize:
            width, height = round(screen_width * 0.96), round(screen_height * 0.90)
        else:
            width, height = (
                min(width, round(screen_width * 0.9)),
                min(height, round(screen_height * 0.9)),
            )
        x, y = mx + (screen_width - width) // 2, my + (screen_height - height) // 2
        self.root.geometry(f"{width}x{height}{x:+d}{y:+d}")
        if maximize:
            try:
                self.root.state("zoomed")
            except tk.TclError:
                try:
                    self.root.attributes("-zoomed", True)
                except tk.TclError:
                    pass

    def center(self, window: tk.Toplevel) -> None:
        window.update_idletasks()
        mx, my, screen_width, screen_height = self.bounds()
        width = min(window.winfo_reqwidth(), round(screen_width * 0.9))
        height = min(window.winfo_reqheight(), round(screen_height * 0.9))
        x, y = mx + (screen_width - width) // 2, my + (screen_height - height) // 2
        window.geometry(f"{width}x{height}{x:+d}{y:+d}")


class DialogService:
    def __init__(self, root: tk.Tk, config: AppConfig, sounds: SoundNotifier) -> None:
        self.root = root
        self.config = config
        self.sounds = sounds
        self.display = DisplayService(root)
        self._logo = None

    def set_icon(self, window) -> None:
        icon = self.config.root_dir / "assets" / "HP_3478A_DMM.ico"
        if icon.is_file():
            try:
                window.iconbitmap(str(icon))
            except tk.TclError:
                pass

    def info(self, title: str, text: str) -> None:
        self.sounds.play("success")
        messagebox.showinfo(title, text, parent=self.root)

    def error(self, title: str, text: str) -> None:
        self.sounds.play("error")
        messagebox.showerror(title, text, parent=self.root)

    def warning(self, title: str, text: str) -> None:
        self.sounds.play("warning")
        messagebox.showwarning(title, text, parent=self.root)

    def confirm_exit(self) -> bool:
        self.sounds.play("warning")
        return messagebox.askokcancel("Exit", "Are you sure you want to exit?", parent=self.root)

    def ask_csv(self, directory: Path) -> Path | None:
        filename = filedialog.askopenfilename(
            parent=self.root,
            initialdir=str(directory),
            title="Select acquisition CSV",
            filetypes=(("CSV", "*.csv"),),
        )
        return Path(filename) if filename else None

    def ask_calibration_file(self, directory: Path, *, save: bool) -> Path | None:
        options = {
            "parent": self.root,
            "initialdir": str(directory),
            "filetypes": (("HP 3478A calibration backup", "*.json"),),
        }
        if save:
            filename = filedialog.asksaveasfilename(
                **options,
                title="Save complete calibration RAM",
                defaultextension=".json",
                initialfile="HP3478A_calibration.json",
            )
        else:
            filename = filedialog.askopenfilename(**options, title="Load calibration RAM backup")
        return Path(filename) if filename else None

    def confirm_calibration_restore(self, path: Path) -> bool:
        return messagebox.askyesno(
            "Restore calibration RAM",
            f"Backup: {path}\n\nOnly restore a backup made from this DMM. "
            "The current calibration RAM will be replaced. A recovery backup will be saved first. "
            "Set the front-panel CAL ENABLE switch to ON before continuing.\n\nRestore this backup?",
            parent=self.root,
            default="no",
        )

    def show_help(self) -> None:
        self.sounds.play("info")
        window = tk.Toplevel(self.root)
        window.withdraw()
        window.title("Info / Help")
        window.resizable(True, True)
        window.columnconfigure(0, weight=1)
        window.configure(background="#f0f0f0")
        self.set_icon(window)
        logo_path = self.config.root_dir / "assets" / "HP_3478A_DMM.png"
        if logo_path.is_file():
            try:
                from PIL import Image, ImageTk

                with Image.open(logo_path) as source:
                    image = source.copy()
                    image.thumbnail((200, 160), Image.Resampling.LANCZOS)
                    self._logo = ImageTk.PhotoImage(image, master=window)
                ttk.Label(window, image=self._logo).grid(row=0, column=0, padx=10, pady=8)
            except (ImportError, OSError):
                pass
        author = ttk.Label(window, text=self.config.author, foreground="blue", cursor="hand2")
        author.grid(row=1, column=0, padx=10, pady=4)
        author.bind(
            "<Button-1>",
            lambda event: webbrowser.open_new("https://www.linkedin.com/in/newmarioman/"),
        )
        ttk.Label(window, text="Electronic and Software Engineer").grid(
            row=2, column=0, padx=10, pady=4
        )
        ttk.Label(window, text="TODI (PG), ITALY").grid(row=3, column=0, padx=10, pady=4)
        title = ttk.Label(window, text=self.config.tool_name, style="Title.TLabel", wraplength=300)
        title.grid(row=4, column=0, padx=10, pady=8, sticky="ew")
        ttk.Label(window, text=f"Version: {self.config.version}").grid(
            row=5, column=0, padx=10, pady=8
        )
        window.bind(
            "<Configure>",
            lambda event: title.configure(wraplength=max(100, window.winfo_width() - 24)),
        )
        self.display.center(window)
        window.deiconify()
        window.transient(self.root)
        window.grab_set()


class ConfigurationDialog:
    """Edit a copy of the preferences; Cancel leaves active preferences unchanged."""

    def __init__(
        self,
        parent: tk.Tk,
        config: AppConfig,
        settings: UserSettings,
        themes: tuple[str, ...],
        dialogs: DialogService,
        on_apply: Callable[[UserSettings], None],
    ) -> None:
        self.config = config
        self.dialogs = dialogs
        self.on_apply = on_apply
        self.settings = replace(settings)
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title("Configuration")
        self.window.resizable(True, True)
        dialogs.set_icon(self.window)
        self.path = tk.StringVar(self.window, str(settings.program_path))
        self.theme = tk.StringVar(
            self.window, settings.theme_style if settings.theme_style in themes else themes[0]
        )
        self.audio = tk.StringVar(self.window, settings.audio_theme)
        notebook = ttk.Notebook(self.window)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)
        path_frame = ttk.Frame(notebook, padding=15)
        style_frame = ttk.Frame(notebook, padding=15)
        for frame in (path_frame, style_frame):
            frame.columnconfigure(0, weight=1)
            frame.columnconfigure(1, weight=1)
        notebook.add(path_frame, text="Folder")
        notebook.add(style_frame, text="Style")
        input_font = ttk.Style(parent).lookup("TLabel", "font")
        ttk.Label(path_frame, text="PROGRAM FOLDER PATH").grid(
            row=0, column=0, columnspan=2, pady=10
        )
        ttk.Entry(
            path_frame, textvariable=self.path, width=1, state="readonly", font=input_font
        ).grid(row=1, column=0, columnspan=2, pady=10, sticky="ew")
        ttk.Button(path_frame, text="Browse", command=self._browse).grid(row=2, column=0, pady=10)
        ttk.Button(
            path_frame, text="Set to Default", command=lambda: self.path.set(str(Path.home()))
        ).grid(row=2, column=1, pady=10)
        ttk.Label(style_frame, text="THEME").grid(row=0, column=0, pady=10)
        ttk.Combobox(
            style_frame,
            textvariable=self.theme,
            values=themes,
            width=1,
            state="readonly",
            font=input_font,
        ).grid(row=1, column=0, padx=10, sticky="ew")
        ttk.Button(
            style_frame,
            text="Set to Default",
            command=lambda: self.theme.set("vista" if "vista" in themes else themes[0]),
        ).grid(row=2, column=0, pady=10)
        ttk.Label(style_frame, text="SOUND").grid(row=0, column=1, pady=10)
        ttk.Combobox(
            style_frame,
            textvariable=self.audio,
            values=config.audio_themes,
            width=1,
            state="readonly",
            font=input_font,
        ).grid(row=1, column=1, padx=10, sticky="ew")
        ttk.Button(
            style_frame, text="Set to Default", command=lambda: self.audio.set("big-sur")
        ).grid(row=2, column=1, pady=10)
        buttons = ttk.Frame(self.window, padding=10)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="APPLY", command=self._apply).pack(side="right", padx=5)
        ttk.Button(buttons, text="CANCEL", command=self.window.destroy).pack(side="right", padx=5)
        ttk.Button(buttons, text="OK", command=lambda: self._apply(close=True)).pack(
            side="right", padx=5
        )
        dialogs.display.center(self.window)
        self.window.deiconify()
        self.window.transient(parent)
        self.window.grab_set()
        dialogs.sounds.play("info")

    def _browse(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.window, title="Select program folder", initialdir=self.path.get()
        )
        if selected:
            self.path.set(selected)

    def _apply(self, close: bool = False) -> None:
        settings = UserSettings(Path(self.path.get()), self.theme.get(), self.audio.get())
        try:
            if not settings.program_path.is_dir():
                raise ValueError("Select an existing directory.")
            self.on_apply(settings)
        except (OSError, ValueError) as exc:
            self.dialogs.error("Configuration", str(exc))
            return
        self.settings = settings
        if close:
            self.window.destroy()
