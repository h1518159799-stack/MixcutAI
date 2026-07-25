"""Native MIXCUT Studio activation dialog."""
from __future__ import annotations
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from license_module import activate_license, check_license, machine_id
from runtime_paths import RESOURCE_DIR

def require_activation() -> bool:
    if check_license().valid: return True
    activated={"value":False}; root=tk.Tk(); root.title("MIXCUT Studio ? ????")
    try: root.iconbitmap(str(RESOURCE_DIR/"brand"/"mixcut.ico"))
    except Exception: pass
    root.geometry("560x430"); root.resizable(False,False); root.configure(bg="#090D16")
    card=tk.Frame(root,bg="#111827",highlightthickness=1,highlightbackground="#263044"); card.pack(fill="both",expand=True,padx=18,pady=18)
    tk.Label(card,text="MIXCUT",bg="#111827",fg="#E6EDF7",font=("Segoe UI",24,"bold")).pack(pady=(24,0))
    tk.Label(card,text="STUDIO  /  PROFESSIONAL VIDEO WORKSTATION",bg="#111827",fg="#5B8CFF",font=("Segoe UI",8)).pack(pady=(1,22))
    machine=machine_id(); tk.Label(card,text="?????",bg="#111827",fg="#98A6BB",anchor="w",font=("Microsoft YaHei UI",10)).pack(fill="x",padx=38,pady=(0,6))
    mv=tk.StringVar(value=machine); tk.Entry(card,textvariable=mv,state="readonly",relief="flat",font=("Consolas",10),readonlybackground="#0D1422",fg="#B9C6D8").pack(fill="x",padx=38,ipady=9)
    tk.Label(card,text="????",bg="#111827",fg="#98A6BB",anchor="w",font=("Microsoft YaHei UI",10)).pack(fill="x",padx=38,pady=(16,6))
    kv=tk.StringVar(); entry=tk.Entry(card,textvariable=kv,relief="flat",font=("Consolas",11),bg="#0D1422",fg="#E6EDF7",insertbackground="white"); entry.pack(fill="x",padx=38,ipady=9)
    status_var=tk.StringVar(value="????????????"); status=tk.Label(card,textvariable=status_var,bg="#111827",fg="#66758B",font=("Microsoft YaHei UI",9)); status.pack(pady=10)
    def copy_machine(): root.clipboard_clear(); root.clipboard_append(machine); status_var.set("??????")
    def activate():
        try: state=activate_license(kv.get())
        except Exception as exc: status.configure(fg="#F87171"); status_var.set(str(exc)); return
        activated["value"]=True; messagebox.showinfo("????",state.message,parent=root); root.destroy()
    bar=tk.Frame(card,bg="#111827"); bar.pack(fill="x",padx=38)
    tk.Button(bar,text="?????",command=copy_machine,relief="flat",bg="#192334",fg="#CAD5E4",font=("Microsoft YaHei UI",10),cursor="hand2").pack(side="left",ipadx=12,ipady=7)
    tk.Button(bar,text="????",command=activate,relief="flat",bg="#5B8CFF",fg="white",font=("Microsoft YaHei UI",10,"bold"),cursor="hand2").pack(side="right",ipadx=28,ipady=7)
    root.protocol("WM_DELETE_WINDOW",root.destroy); entry.focus_set(); root.mainloop(); return bool(activated["value"])

def show_expiry_reminder(reminder_days:int=7)->None:
    state=check_license()
    if state.valid and state.days_left is not None and state.days_left<=reminder_days:
        root=tk.Tk(); root.withdraw(); messagebox.showwarning("??????",f"?????? {state.days_left} ????????",parent=root); root.destroy()
