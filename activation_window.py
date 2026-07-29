"""Native MixcutAI activation dialog."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox

from license_module import activate_license, check_license, machine_id
from runtime_paths import RESOURCE_DIR


def friendly_activation_error(exc: Exception) -> str:
    text = str(exc or "").strip()
    lowered = text.lower()
    mappings = (
        (("卡密不存在", "not found", "invalid key"), "卡密不存在，请检查后重新输入"),
        (("已过期", "expired"), "卡密已过期，请联系管理员续期"),
        (("封禁", "frozen", "disabled"), "卡密已被封禁，请联系管理员"),
        (("撤销", "revoked"), "卡密已被撤销，请联系管理员"),
        (("其他机器", "其他设备", "machine", "device"), "卡密已绑定其他设备，请联系管理员解绑"),
        (("timeout", "timed out", "超时"), "授权服务器连接超时，请检查网络后重试"),
        (("ssl", "certificate", "https"), "HTTPS连接失败，请检查系统时间和网络"),
        (("无法连接", "temporarily unavailable", "connection"), "授权服务器暂时不可用，请稍后重试"),
    )
    for needles, message in mappings:
        if any(needle in lowered for needle in needles):
            return message
    return text or "激活失败，请检查卡密和网络"


def require_activation() -> bool:
    if check_license().valid:
        return True

    activated = {"value": False}
    root = tk.Tk()
    root.title("MixcutAI 软件激活")
    try:
        root.iconbitmap(str(RESOURCE_DIR / "brand" / "mixcut.ico"))
    except Exception:
        pass
    root.geometry("560x430")
    root.resizable(False, False)
    root.configure(bg="#090D16")

    card = tk.Frame(root, bg="#111827", highlightthickness=1, highlightbackground="#263044")
    card.pack(fill="both", expand=True, padx=18, pady=18)
    tk.Label(card, text="MIXCUT", bg="#111827", fg="#E6EDF7", font=("Segoe UI", 24, "bold")).pack(pady=(24, 0))
    tk.Label(card, text="STUDIO  /  PROFESSIONAL VIDEO WORKSTATION", bg="#111827", fg="#5B8CFF", font=("Segoe UI", 8)).pack(pady=(1, 22))

    current_machine = machine_id()
    tk.Label(card, text="本机机器码", bg="#111827", fg="#98A6BB", anchor="w", font=("Microsoft YaHei UI", 10)).pack(fill="x", padx=38, pady=(0, 6))
    machine_value = tk.StringVar(value=current_machine)
    tk.Entry(card, textvariable=machine_value, state="readonly", relief="flat", font=("Consolas", 10), readonlybackground="#0D1422", fg="#B9C6D8").pack(fill="x", padx=38, ipady=9)

    tk.Label(card, text="授权卡密", bg="#111827", fg="#98A6BB", anchor="w", font=("Microsoft YaHei UI", 10)).pack(fill="x", padx=38, pady=(16, 6))
    key_value = tk.StringVar()
    entry = tk.Entry(card, textvariable=key_value, relief="flat", font=("Consolas", 11), bg="#0D1422", fg="#E6EDF7", insertbackground="white")
    entry.pack(fill="x", padx=38, ipady=9)
    status_value = tk.StringVar(value="请输入卡密并点击激活")
    status = tk.Label(card, textvariable=status_value, bg="#111827", fg="#66758B", font=("Microsoft YaHei UI", 9), wraplength=450)
    status.pack(pady=10)

    def copy_machine() -> None:
        root.clipboard_clear()
        root.clipboard_append(current_machine)
        status.configure(fg="#67D391")
        status_value.set("机器码已复制")

    def activate() -> None:
        value = key_value.get().strip()
        if not value:
            status.configure(fg="#F87171")
            status_value.set("请输入卡密")
            return
        try:
            state = activate_license(value)
        except Exception as exc:
            status.configure(fg="#F87171")
            status_value.set(friendly_activation_error(exc))
            return
        activated["value"] = True
        messagebox.showinfo("激活成功", state.message, parent=root)
        root.destroy()

    bar = tk.Frame(card, bg="#111827")
    bar.pack(fill="x", padx=38)
    tk.Button(bar, text="复制机器码", command=copy_machine, relief="flat", bg="#192334", fg="#CAD5E4", font=("Microsoft YaHei UI", 10), cursor="hand2").pack(side="left", ipadx=12, ipady=7)
    tk.Button(bar, text="激活软件", command=activate, relief="flat", bg="#5B8CFF", fg="white", font=("Microsoft YaHei UI", 10, "bold"), cursor="hand2").pack(side="right", ipadx=28, ipady=7)

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    entry.focus_set()
    root.mainloop()
    return bool(activated["value"])


def show_expiry_reminder(reminder_days: int = 7) -> None:
    state = check_license()
    if state.valid and state.days_left is not None and state.days_left <= reminder_days:
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("授权即将到期", f"授权将在 {state.days_left} 天后到期，请及时续期", parent=root)
        root.destroy()
