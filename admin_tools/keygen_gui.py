"""Native GUI for issuing machine-bound Mixcut AI licenses."""

from __future__ import annotations

import sys
import tkinter as tk
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from issue_license import issue


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


PRIVATE_KEY = application_dir() / "keys" / "license_private_key.pem"
TYPE_MAP = {"1天卡（24小时）": "day1", "7天卡（7×24小时）": "day7", "月卡（30天）": "month", "年卡（365天）": "year", "永久授权": "permanent"}


class KeygenApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Mixcut AI · 授权密钥生成器")
        root.geometry("760x610")
        root.minsize(680, 560)
        root.configure(bg="#f3f6fb")

        self.machine_var = tk.StringVar()
        self.type_var = tk.StringVar(value="月卡（30天）")
        self.expiry_var = tk.StringVar()
        self.status_var = tk.StringVar(value="等待生成授权")
        self.build_ui()
        self.update_expiry()

    def build_ui(self) -> None:
        header = tk.Frame(self.root, bg="#1f5fd1", height=100)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="Mixcut AI 授权密钥生成器", bg="#1f5fd1", fg="white",
                 font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w", padx=34, pady=(20, 2))
        tk.Label(header, text="输入客户机器码，选择授权类型，一键生成签名密钥",
                 bg="#1f5fd1", fg="#dbe8ff", font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=35)

        card = tk.Frame(self.root, bg="white", highlightthickness=1, highlightbackground="#dce5f2")
        card.pack(fill="both", expand=True, padx=28, pady=22)

        form = tk.Frame(card, bg="white")
        form.pack(fill="x", padx=28, pady=(24, 10))
        form.grid_columnconfigure(1, weight=1)

        self.label(form, "客户机器码", 0)
        machine = tk.Entry(form, textvariable=self.machine_var, font=("Consolas", 11), relief="solid", bd=1)
        machine.grid(row=0, column=1, columnspan=2, sticky="ew", ipady=9)
        tk.Button(form, text="粘贴", command=self.paste_machine, relief="flat", bg="#edf3ff", fg="#2867d8",
                  font=("Microsoft YaHei UI", 9), cursor="hand2").grid(row=0, column=3, padx=(10, 0), ipadx=12, ipady=6)

        self.label(form, "授权类型", 1)
        type_box = ttk.Combobox(form, textvariable=self.type_var, values=list(TYPE_MAP), state="readonly",
                                font=("Microsoft YaHei UI", 10))
        type_box.grid(row=1, column=1, sticky="ew", pady=16, ipady=5)
        type_box.bind("<<ComboboxSelected>>", lambda _event: self.update_expiry())

        tk.Label(form, text="到期日期", bg="white", fg="#44516a", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=1, column=2, sticky="e", padx=(22, 10))
        self.expiry_entry = tk.Entry(form, textvariable=self.expiry_var, font=("Consolas", 10), relief="solid", bd=1, width=18)
        self.expiry_entry.grid(row=1, column=3, sticky="ew", ipady=7)

        tk.Label(card, text="生成结果", bg="white", fg="#44516a", font=("Microsoft YaHei UI", 10, "bold")).pack(
            anchor="w", padx=28, pady=(4, 7))
        self.output = ScrolledText(card, height=10, wrap="word", font=("Consolas", 9), relief="solid", bd=1,
                                   bg="#f8faff", fg="#23314d")
        self.output.pack(fill="both", expand=True, padx=28)

        tk.Label(card, textvariable=self.status_var, bg="white", fg="#79869b",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=28, pady=(9, 6))

        actions = tk.Frame(card, bg="white")
        actions.pack(fill="x", padx=28, pady=(0, 22))
        tk.Button(actions, text="保存到文件", command=self.save_token, relief="flat", bg="#edf3ff", fg="#2867d8",
                  font=("Microsoft YaHei UI", 10), cursor="hand2").pack(side="left", ipadx=15, ipady=8)
        tk.Button(actions, text="复制密钥", command=self.copy_token, relief="flat", bg="#e8f7ef", fg="#16834a",
                  font=("Microsoft YaHei UI", 10), cursor="hand2").pack(side="right", padx=(10, 0), ipadx=18, ipady=8)
        tk.Button(actions, text="生成并复制", command=self.generate, relief="flat", bg="#3478f6", fg="white",
                  activebackground="#2869df", activeforeground="white", font=("Microsoft YaHei UI", 10, "bold"),
                  cursor="hand2").pack(side="right", ipadx=22, ipady=8)

    @staticmethod
    def label(parent: tk.Widget, text: str, row: int) -> None:
        tk.Label(parent, text=text, bg="white", fg="#44516a", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=row, column=0, sticky="w", padx=(0, 14))

    def update_expiry(self) -> None:
        license_type = TYPE_MAP[self.type_var.get()]
        if license_type == "permanent":
            self.expiry_var.set("永久")
            self.expiry_entry.configure(state="disabled")
        else:
            if license_type in {"day1", "day7"}:
                days = 1 if license_type == "day1" else 7
                self.expiry_entry.configure(state="normal")
                self.expiry_var.set((datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=days)).isoformat().replace("+00:00", "Z"))
                return
            days = 365 if license_type == "year" else 30
            self.expiry_entry.configure(state="normal")
            self.expiry_var.set((date.today() + timedelta(days=days)).isoformat())

    def paste_machine(self) -> None:
        try:
            self.machine_var.set(self.root.clipboard_get().strip())
        except tk.TclError:
            self.status_var.set("剪贴板中没有文本")

    def current_token(self) -> str:
        return self.output.get("1.0", "end").strip()

    def generate(self) -> None:
        machine = self.machine_var.get().strip().upper()
        license_type = TYPE_MAP[self.type_var.get()]
        # Short cards are activated by issuance here; calculate from the exact click time.
        expiry = "" if license_type == "permanent" or license_type in {"day1", "day7"} else self.expiry_var.get().strip()
        if not PRIVATE_KEY.is_file():
            messagebox.showerror("缺少私钥", f"未找到管理员私钥：\n{PRIVATE_KEY}", parent=self.root)
            return
        try:
            token = issue(machine, license_type, expiry, PRIVATE_KEY)
        except Exception as exc:
            messagebox.showerror("生成失败", str(exc), parent=self.root)
            return
        self.output.delete("1.0", "end")
        self.output.insert("1.0", token)
        self.root.clipboard_clear(); self.root.clipboard_append(token)
        self.status_var.set(f"已生成{self.type_var.get()}，密钥已复制到剪贴板")

    def copy_token(self) -> None:
        token = self.current_token()
        if not token:
            self.status_var.set("请先生成授权密钥")
            return
        self.root.clipboard_clear(); self.root.clipboard_append(token)
        self.status_var.set("密钥已复制到剪贴板")

    def save_token(self) -> None:
        token = self.current_token()
        if not token:
            self.status_var.set("请先生成授权密钥")
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="保存授权密钥", defaultextension=".license",
                                            filetypes=[("Mixcut License", "*.license"), ("Text", "*.txt")])
        if path:
            Path(path).write_text(token, encoding="utf-8")
            self.status_var.set(f"已保存到：{path}")


def main() -> int:
    root = tk.Tk()
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    KeygenApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
