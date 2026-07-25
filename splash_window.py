"""Minimal native splash for MIXCUT Studio startup."""
from __future__ import annotations

import tkinter as tk
from runtime_paths import RESOURCE_DIR


class SplashWindow:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.configure(bg="#090D16")
        self.image = tk.PhotoImage(file=str(RESOURCE_DIR / "brand" / "splash.png"))
        tk.Label(self.root, image=self.image, bg="#090D16", bd=0).pack()
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - self.image.width()) // 2
        y = (self.root.winfo_screenheight() - self.image.height()) // 2
        self.root.geometry(f"+{x}+{y}")
        self.root.update()

    def close(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass
