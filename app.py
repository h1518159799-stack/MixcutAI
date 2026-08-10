import queue
import random
import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from bgm_service import scan_bgm_folder
from db import APP_DIR, DB_PATH, VOICE_DIR, connect, init_db, now
from production_test import run_production_test
from subtitle_service import SubtitleError, generate_ass_for_shot
from tts_service import TtsError, audio_duration, synthesize_windows_tts
from video_engine import VideoGenerationError, generate_video, load_template_shots
from timeline_engine import build_timeline_from_shots
from video_scan import make_thumbnail, probe_video, quick_hash, scan_video_files
from runtime_paths import get_ffmpeg_path, get_ffprobe_path, get_media_runtime_status

APP_TITLE = "AI自动混剪软件 - Windows基础版"
COLORS = {
    "bg": "#0f172a",
    "panel": "#111827",
    "card": "#1f2937",
    "line": "#334155",
    "text": "#e5e7eb",
    "muted": "#94a3b8",
    "accent": "#3b82f6",
    "green": "#22c55e",
    "red": "#ef4444",
}


def fetch_all(sql, params=()):
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def fetch_one(sql, params=()):
    with connect() as conn:
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else None


def exec_sql(sql, params=()):
    with connect() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid


def upsert_shot_material(shot_id, material_id, voice_duration, start_time, end_time):
    with connect() as conn:
        old = conn.execute("SELECT id FROM generation_shot_materials WHERE shot_id=?", (shot_id,)).fetchone()
        if old:
            conn.execute(
                """UPDATE generation_shot_materials
                   SET material_id=?,voice_duration=?,material_start_time=?,material_end_time=?,updated_at=?
                   WHERE shot_id=?""",
                (material_id, voice_duration, start_time, end_time, now(), shot_id),
            )
            return old["id"]
        cur = conn.execute(
            """INSERT INTO generation_shot_materials(shot_id,material_id,voice_duration,material_start_time,material_end_time,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            (shot_id, material_id, voice_duration, start_time, end_time, now(), now()),
        )
        return cur.lastrowid


def fmt_time(seconds):
    seconds = max(0, float(seconds or 0))
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


def dur_text(row):
    v = float((row or {}).get("voice_duration") or 0)
    return f"{v:.3f}s" if v > 0 else "未生成"


class MixcutApp(tk.Tk):
    def __init__(self):
        super().__init__()
        init_db()
        self.title(APP_TITLE)
        self.geometry("1280x820")
        self.minsize(1120, 720)
        self.configure(bg=COLORS["bg"])
        self.scan_queue = queue.Queue()
        self.generation_queue = queue.Queue()
        self.test_queue = queue.Queue()
        self.image_cache = {}
        self.current_page = ""
        self.match_result_labels = {}
        self._style()
        self._shell()
        self.show("素材中心")
        self.after(200, self.poll_scan)
        self.after(500, self.poll_generation)
        self.after(500, self.poll_test_queue)

    def _style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview", background=COLORS["panel"], fieldbackground=COLORS["panel"], foreground=COLORS["text"], rowheight=30)
        style.configure("Treeview.Heading", background=COLORS["card"], foreground=COLORS["text"], relief="flat")
        style.map("Treeview", background=[("selected", COLORS["accent"])])
        style.configure("Horizontal.TProgressbar", troughcolor=COLORS["card"], background=COLORS["accent"])

    def _shell(self):
        self.side = tk.Frame(self, bg=COLORS["panel"], width=220)
        self.side.pack(side="left", fill="y")
        self.side.pack_propagate(False)
        tk.Label(self.side, text="AI MIXCUT", bg=COLORS["panel"], fg=COLORS["text"], font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w", padx=22, pady=(26, 4))
        tk.Label(self.side, text="本地自动混剪工作台", bg=COLORS["panel"], fg=COLORS["muted"]).pack(anchor="w", padx=24, pady=(0, 24))
        self.nav = {}
        for name in ["素材中心", "镜头剪辑", "时间轴预览", "任务中心", "账号档案", "生产测试", "设置"]:
            b = tk.Button(self.side, text=name, anchor="w", padx=22, pady=12, bd=0, bg=COLORS["panel"], fg=COLORS["text"], activebackground=COLORS["card"], activeforeground=COLORS["text"], font=("Microsoft YaHei UI", 12), command=lambda n=name: self.show(n))
            b.pack(fill="x", padx=12, pady=4)
            self.nav[name] = b
        tk.Label(self.side, text="第一阶段：UI + SQLite\n账号档案 + 素材扫描", bg=COLORS["panel"], fg=COLORS["muted"], justify="left").pack(side="bottom", anchor="w", padx=24, pady=22)
        self.body = tk.Frame(self, bg=COLORS["bg"])
        self.body.pack(side="left", fill="both", expand=True)

    def show(self, name):
        self.current_page = name
        for k, b in self.nav.items():
            b.configure(bg=COLORS["accent"] if k == name else COLORS["panel"])
        for w in self.body.winfo_children():
            w.destroy()
        getattr(self, f"page_{name}")()

    def title_block(self, title, desc):
        f = tk.Frame(self.body, bg=COLORS["bg"])
        f.pack(fill="x", padx=28, pady=(24, 12))
        tk.Label(f, text=title, bg=COLORS["bg"], fg=COLORS["text"], font=("Microsoft YaHei UI", 24, "bold")).pack(anchor="w")
        tk.Label(f, text=desc, bg=COLORS["bg"], fg=COLORS["muted"], font=("Microsoft YaHei UI", 11)).pack(anchor="w", pady=(4, 0))

    def btn(self, parent, text, cmd, color=None):
        return tk.Button(parent, text=text, command=cmd, bg=color or COLORS["accent"], fg="white", activebackground=color or COLORS["accent"], activeforeground="white", bd=0, padx=16, pady=8, font=("Microsoft YaHei UI", 10, "bold"))

    def card(self, parent):
        return tk.Frame(parent, bg=COLORS["panel"], highlightbackground=COLORS["line"], highlightthickness=1)

    def input(self, parent, label, row):
        tk.Label(parent, text=label, bg=COLORS["panel"], fg=COLORS["muted"]).grid(row=row * 2, column=0, sticky="w", pady=(4, 2))
        e = tk.Entry(parent, bg=COLORS["card"], fg=COLORS["text"], insertbackground=COLORS["text"], relief="flat")
        e.grid(row=row * 2 + 1, column=0, sticky="ew", ipady=7, pady=(0, 8))
        return e

    def text(self, parent, label, row):
        tk.Label(parent, text=label, bg=COLORS["panel"], fg=COLORS["muted"]).grid(row=row * 2, column=0, sticky="w", pady=(4, 2))
        t = tk.Text(parent, height=4, bg=COLORS["card"], fg=COLORS["text"], insertbackground=COLORS["text"], relief="flat", wrap="word")
        t.grid(row=row * 2 + 1, column=0, sticky="ew", pady=(0, 8))
        return t

    # 素材中心
    def page_素材中心(self):
        self.title_block("素材中心", "选择本地文件夹，扫描视频，读取视频信息并生成缩略图。")
        bar = tk.Frame(self.body, bg=COLORS["bg"])
        bar.pack(fill="x", padx=28, pady=8)
        self.btn(bar, "选择文件夹并扫描", self.choose_folder).pack(side="left")
        self.btn(bar, "刷新素材列表", self.refresh_materials, COLORS["card"]).pack(side="left", padx=10)
        tk.Label(bar, text="分类筛选", bg=COLORS["bg"], fg=COLORS["muted"]).pack(side="left", padx=(20, 8))
        self.filter = ttk.Combobox(bar, state="readonly", width=18)
        self.filter.pack(side="left")
        self.filter.bind("<<ComboboxSelected>>", lambda e: self.refresh_materials())
        self.scan_label = tk.Label(self.body, text="就绪", bg=COLORS["bg"], fg=COLORS["muted"], anchor="w")
        self.scan_label.pack(fill="x", padx=28, pady=(2, 8))
        self.progress = ttk.Progressbar(self.body, orient="horizontal", mode="determinate")
        self.progress.pack(fill="x", padx=28, pady=(0, 14))
        wrap = tk.Frame(self.body, bg=COLORS["bg"])
        wrap.pack(fill="both", expand=True, padx=28, pady=(0, 24))
        left = self.card(wrap)
        right = self.card(wrap)
        left.pack(side="left", fill="both", expand=True)
        right.pack(side="left", fill="y", padx=(14, 0))
        right.configure(width=340)
        right.pack_propagate(False)
        cols = ("id", "name", "cat", "dur", "size", "res", "status")
        self.material_tree = ttk.Treeview(left, columns=cols, show="headings")
        for c, t, w in [("id", "ID", 55), ("name", "文件名", 300), ("cat", "分类", 110), ("dur", "时长", 85), ("size", "大小", 85), ("res", "分辨率", 110), ("status", "状态", 80)]:
            self.material_tree.heading(c, text=t)
            self.material_tree.column(c, width=w, anchor="w")
        self.material_tree.pack(fill="both", expand=True, padx=12, pady=12)
        self.material_tree.bind("<<TreeviewSelect>>", self.select_material)
        tk.Label(right, text="素材详情", bg=COLORS["panel"], fg=COLORS["text"], font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w", padx=16, pady=(16, 8))
        self.preview = tk.Label(right, text="选择素材查看缩略图", bg=COLORS["card"], fg=COLORS["muted"], width=36, height=10)
        self.preview.pack(padx=16, pady=8)
        self.detail = tk.Label(right, text="", bg=COLORS["panel"], fg=COLORS["muted"], justify="left", wraplength=300)
        self.detail.pack(anchor="w", padx=16, pady=10)
        tk.Label(right, text="修改分类", bg=COLORS["panel"], fg=COLORS["text"]).pack(anchor="w", padx=16, pady=(16, 4))
        self.cat_combo = ttk.Combobox(right, state="readonly")
        self.cat_combo.pack(fill="x", padx=16)
        self.btn(right, "保存分类", self.save_material_category, COLORS["green"]).pack(fill="x", padx=16, pady=12)
        self.refresh_categories()
        self.refresh_materials()

    def refresh_categories(self):
        self.categories = fetch_all("SELECT id,name FROM material_categories ORDER BY sort_order,id")
        vals = ["全部"] + [c["name"] for c in self.categories]
        if hasattr(self, "filter"):
            self.filter["values"] = vals
            if not self.filter.get():
                self.filter.set("全部")
        if hasattr(self, "cat_combo"):
            self.cat_combo["values"] = [c["name"] for c in self.categories]

    def cat_id(self, name):
        for c in self.categories:
            if c["name"] == name:
                return c["id"]
        return None

    def refresh_materials(self):
        self.refresh_categories()
        for i in self.material_tree.get_children():
            self.material_tree.delete(i)
        sql = "SELECT m.*,c.name category_name FROM materials m LEFT JOIN material_categories c ON c.id=m.category_id"
        params = []
        if self.filter.get() and self.filter.get() != "全部":
            sql += " WHERE c.name=?"
            params.append(self.filter.get())
        sql += " ORDER BY m.id DESC"
        for m in fetch_all(sql, params):
            dur = f"{m['duration']:.1f}s" if m["duration"] else "未知"
            size = f"{m['size'] / 1024 / 1024:.1f}MB" if m["size"] else "-"
            res = f"{m['width']}x{m['height']}" if m["width"] and m["height"] else "未知"
            self.material_tree.insert("", "end", iid=str(m["id"]), values=(m["id"], m["file_name"], m["category_name"] or "未分类", dur, size, res, m["status"]))

    def choose_folder(self):
        folder = filedialog.askdirectory(title="选择视频素材文件夹")
        if not folder:
            return
        self.progress["value"] = 0
        self.scan_label.configure(text=f"开始扫描：{folder}")
        threading.Thread(target=self.scan_worker, args=(folder,), daemon=True).start()

    def scan_worker(self, folder):
        task_id = exec_sql("INSERT INTO scan_tasks(folder_path,status,created_at) VALUES(?,?,?)", (folder, "running", now()))
        try:
            files = scan_video_files(folder)
            total = len(files)
            exec_sql("UPDATE scan_tasks SET total_files=? WHERE id=?", (total, task_id))
            cat = fetch_one("SELECT id FROM material_categories ORDER BY sort_order LIMIT 1")
            default_cat = cat["id"] if cat else None
            ffmpeg = get_ffmpeg_path()
            ffprobe = get_ffprobe_path()
            added = skipped = 0
            for idx, p in enumerate(files, 1):
                if fetch_one("SELECT id FROM materials WHERE file_path=?", (str(p),)):
                    skipped += 1
                else:
                    h = quick_hash(p)
                    info = probe_video(p, ffprobe)
                    thumb = make_thumbnail(p, h, ffmpeg)
                    exec_sql(
                        """INSERT INTO materials(category_id,file_path,file_name,file_hash,duration,width,height,fps,size,thumbnail_path,status,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (default_cat, str(p), p.name, h, info["duration"], info["width"], info["height"], info["fps"], info["size"], thumb, "active", now(), now()),
                    )
                    added += 1
                exec_sql("UPDATE scan_tasks SET scanned_files=?,added_count=?,skipped_count=? WHERE id=?", (idx, added, skipped, task_id))
                self.scan_queue.put(("progress", idx, total, added, skipped, p.name))
            exec_sql("UPDATE scan_tasks SET status=?,finished_at=? WHERE id=?", ("completed", now(), task_id))
            self.scan_queue.put(("done", total, added, skipped))
        except Exception as e:
            exec_sql("UPDATE scan_tasks SET status=?,error_message=?,finished_at=? WHERE id=?", ("failed", str(e), now(), task_id))
            self.scan_queue.put(("error", str(e)))

    def poll_scan(self):
        try:
            while True:
                msg = self.scan_queue.get_nowait()
                if msg[0] == "progress":
                    _, idx, total, added, skipped, name = msg
                    self.progress["maximum"] = max(total, 1)
                    self.progress["value"] = idx
                    self.scan_label.configure(text=f"扫描中 {idx}/{total}：{name} | 新增 {added}，跳过 {skipped}")
                elif msg[0] == "done":
                    _, total, added, skipped = msg
                    self.scan_label.configure(text=f"扫描完成：共 {total} 个视频，新增 {added}，跳过 {skipped}")
                    if self.current_page == "素材中心":
                        self.refresh_materials()
                elif msg[0] == "error":
                    self.scan_label.configure(text=f"扫描失败：{msg[1]}")
                    messagebox.showerror("扫描失败", msg[1])
        except queue.Empty:
            pass
        self.after(200, self.poll_scan)

    def select_material(self, _=None):
        sel = self.material_tree.selection()
        if not sel:
            return
        m = fetch_one("SELECT m.*,c.name category_name FROM materials m LEFT JOIN material_categories c ON c.id=m.category_id WHERE m.id=?", (int(sel[0]),))
        if not m:
            return
        self.detail.configure(text=f"文件：{m['file_name']}\n路径：{m['file_path']}\n时长：{m['duration']:.2f}s\nFPS：{m['fps']}\n大小：{m['size']/1024/1024:.2f} MB\nHash：{m['file_hash'][:16]}...")
        if m["category_name"]:
            self.cat_combo.set(m["category_name"])
        if m["thumbnail_path"] and Path(m["thumbnail_path"]).exists():
            img = Image.open(m["thumbnail_path"]).resize((300, 169))
            tkimg = ImageTk.PhotoImage(img)
            self.image_cache["preview"] = tkimg
            self.preview.configure(image=tkimg, text="")
        else:
            self.preview.configure(image="", text="暂无缩略图")

    def save_material_category(self):
        sel = self.material_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先选择一个素材")
            return
        cid = self.cat_id(self.cat_combo.get())
        if cid:
            exec_sql("UPDATE materials SET category_id=?,updated_at=? WHERE id=?", (cid, now(), int(sel[0])))
            self.refresh_materials()

    # 镜头剪辑
    def page_镜头剪辑(self):
        self.title_block("镜头剪辑", "用户自定义模板与镜头编辑器。模板全部由用户创建，不添加系统模板。")
        self.selected_template_id = None
        self.shot_forms = []
        self.match_result_labels = {}

        wrap = tk.Frame(self.body, bg=COLORS["bg"])
        wrap.pack(fill="both", expand=True, padx=28, pady=12)
        left = self.card(wrap)
        mid = self.card(wrap)
        left.pack(side="left", fill="y")
        mid.pack(side="left", fill="both", expand=True, padx=(14, 0))
        left.configure(width=310)
        left.pack_propagate(False)

        tk.Label(left, text="模板列表", bg=COLORS["panel"], fg=COLORS["text"], font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w", padx=16, pady=(16, 4))
        tk.Label(left, text="新建 / 修改 / 删除 / 复制", bg=COLORS["panel"], fg=COLORS["muted"]).pack(anchor="w", padx=16, pady=(0, 10))
        form = tk.Frame(left, bg=COLORS["panel"])
        form.pack(fill="x", padx=16, pady=(0, 10))
        self.tpl_edit_id = None
        self.tpl_name = self.input(form, "模板名称", 0)
        self.tpl_desc = self.text(form, "模板描述", 1)
        ops = tk.Frame(form, bg=COLORS["panel"])
        ops.grid(row=4, column=0, sticky="ew", pady=8)
        self.btn(ops, "新建", self.clear_template_form, COLORS["card"]).pack(side="left")
        self.btn(ops, "保存模板", self.save_template, COLORS["green"]).pack(side="left", padx=8)
        form.columnconfigure(0, weight=1)

        self.template_list = tk.Listbox(left, bg=COLORS["card"], fg=COLORS["text"], selectbackground=COLORS["accent"], relief="flat", activestyle="none", font=("Microsoft YaHei UI", 11))
        self.template_list.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        self.template_list.bind("<<ListboxSelect>>", self.on_template_select)
        foot = tk.Frame(left, bg=COLORS["panel"])
        foot.pack(fill="x", padx=16, pady=(0, 16))
        self.btn(foot, "复制模板", self.duplicate_template, COLORS["card"]).pack(side="left", fill="x", expand=True)
        self.btn(foot, "删除模板", self.delete_template, COLORS["red"]).pack(side="left", fill="x", expand=True, padx=(8, 0))

        head = tk.Frame(mid, bg=COLORS["panel"])
        head.pack(fill="x", padx=16, pady=(16, 8))
        self.editor_title = tk.Label(head, text="请选择一个模板", bg=COLORS["panel"], fg=COLORS["text"], font=("Microsoft YaHei UI", 16, "bold"))
        self.editor_title.pack(side="left")
        self.btn(head, "添加镜头", self.add_shot, COLORS["accent"]).pack(side="right")
        self.btn(head, "保存全部镜头", self.save_all_shots, COLORS["green"]).pack(side="right", padx=8)
        self.btn(head, "匹配全部素材", self.match_all_shots, COLORS["card"]).pack(side="right")
        self.btn(head, "生成全部字幕", self.generate_all_subtitles, COLORS["card"]).pack(side="right", padx=8)
        self.btn(head, "生成视频", self.create_generation_task, COLORS["red"]).pack(side="right", padx=8)
        tk.Label(mid, text="每个镜头支持：名称、原始文案、AI改写文案预留、素材分类、真实语音时长、静音、字幕、配音、排序。生成视频时以 voice_duration 作为时间轴。", bg=COLORS["panel"], fg=COLORS["muted"], anchor="w").pack(fill="x", padx=16, pady=(0, 8))

        canvas_box = tk.Frame(mid, bg=COLORS["panel"])
        canvas_box.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.shot_canvas = tk.Canvas(canvas_box, bg=COLORS["panel"], highlightthickness=0)
        self.shot_scroll = ttk.Scrollbar(canvas_box, orient="vertical", command=self.shot_canvas.yview)
        self.shot_inner = tk.Frame(self.shot_canvas, bg=COLORS["panel"])
        self.shot_inner.bind("<Configure>", lambda e: self.shot_canvas.configure(scrollregion=self.shot_canvas.bbox("all")))
        self.shot_canvas.create_window((0, 0), window=self.shot_inner, anchor="nw")
        self.shot_canvas.configure(yscrollcommand=self.shot_scroll.set)
        self.shot_canvas.pack(side="left", fill="both", expand=True)
        self.shot_scroll.pack(side="right", fill="y")
        self.refresh_templates(select_first=True)

    def clear_template_form(self):
        self.tpl_edit_id = None
        self.selected_template_id = None
        self.tpl_name.delete(0, "end")
        self.tpl_desc.delete("1.0", "end")
        self.template_list.selection_clear(0, "end")
        self.editor_title.configure(text="请选择一个模板")
        self.render_shots([])

    def save_template(self):
        name = self.tpl_name.get().strip()
        desc = self.tpl_desc.get("1.0", "end").strip()
        if not name:
            messagebox.showinfo("提示", "模板名称不能为空")
            return
        if self.tpl_edit_id:
            exec_sql("UPDATE edit_templates SET name=?,description=?,updated_at=? WHERE id=?", (name, desc, now(), self.tpl_edit_id))
            tid = self.tpl_edit_id
        else:
            tid = exec_sql("INSERT INTO edit_templates(name,description,created_at,updated_at) VALUES(?,?,?,?)", (name, desc, now(), now()))
            self.tpl_edit_id = tid
        self.refresh_templates(select_id=tid)

    def refresh_templates(self, select_id=None, select_first=False):
        self.templates = fetch_all("SELECT * FROM edit_templates ORDER BY id DESC")
        self.template_list.delete(0, "end")
        for t in self.templates:
            self.template_list.insert("end", f"{t['name']}    {(t.get('created_at') or '')[:10]}")
        target = None
        if select_id:
            for i, t in enumerate(self.templates):
                if t["id"] == select_id:
                    target = i
                    break
        elif select_first and self.templates:
            target = 0
        if target is not None:
            self.template_list.selection_set(target)
            self.template_list.activate(target)
            self.load_template(self.templates[target]["id"])

    def on_template_select(self, _=None):
        sel = self.template_list.curselection()
        if sel:
            self.load_template(self.templates[sel[0]]["id"])

    def load_template(self, template_id):
        t = fetch_one("SELECT * FROM edit_templates WHERE id=?", (template_id,))
        if not t:
            return
        self.selected_template_id = template_id
        self.tpl_edit_id = template_id
        self.tpl_name.delete(0, "end")
        self.tpl_name.insert(0, t["name"])
        self.tpl_desc.delete("1.0", "end")
        self.tpl_desc.insert("1.0", t["description"] or "")
        self.editor_title.configure(text=f"镜头编辑：{t['name']}")
        self.load_shots()

    def delete_template(self):
        if not self.selected_template_id:
            messagebox.showinfo("提示", "先选择模板")
            return
        if not messagebox.askyesno("确认", "删除模板会同时删除全部镜头，确定？"):
            return
        exec_sql("DELETE FROM edit_templates WHERE id=?", (self.selected_template_id,))
        self.clear_template_form()
        self.refresh_templates(select_first=True)

    def duplicate_template(self):
        if not self.selected_template_id:
            messagebox.showinfo("提示", "先选择模板")
            return
        src = fetch_one("SELECT * FROM edit_templates WHERE id=?", (self.selected_template_id,))
        new_id = exec_sql("INSERT INTO edit_templates(name,description,output_width,output_height,created_at,updated_at) VALUES(?,?,?,?,?,?)", (src["name"] + " - 副本", src["description"], src["output_width"], src["output_height"], now(), now()))
        shots = fetch_all("SELECT * FROM template_shots WHERE template_id=? ORDER BY sort_order,id", (self.selected_template_id,))
        for s in shots:
            exec_sql(
                """INSERT INTO template_shots(template_id,shot_name,original_text,ai_text,material_category_id,duration,estimated_duration,mute_original_audio,enable_subtitle,enable_tts,voice_file_path,voice_duration,subtitle_file_path,sort_order,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id, s["shot_name"], s["original_text"], s["ai_text"], s["material_category_id"], s["duration"], s["estimated_duration"], s["mute_original_audio"], s["enable_subtitle"], s["enable_tts"], s["voice_file_path"], s["voice_duration"], s["subtitle_file_path"], s["sort_order"], now(), now()),
            )
        self.refresh_templates(select_id=new_id)

    def load_shots(self):
        if not self.selected_template_id:
            self.render_shots([])
            return
        shots = fetch_all(
            """SELECT s.*,c.name category_name FROM template_shots s
               LEFT JOIN material_categories c ON c.id=s.material_category_id
               WHERE s.template_id=? ORDER BY s.sort_order,s.id""",
            (self.selected_template_id,),
        )
        self.render_shots(shots)

    def render_shots(self, shots):
        for w in self.shot_inner.winfo_children():
            w.destroy()
        self.shot_forms = []
        if not shots:
            tip = "当前模板还没有镜头。点击右上角【添加镜头】开始。" if self.selected_template_id else "左侧选择或创建模板后编辑镜头。"
            tk.Label(self.shot_inner, text=tip, bg=COLORS["panel"], fg=COLORS["muted"], pady=40).pack(fill="x")
            return

        cats = fetch_all("SELECT id,name FROM material_categories ORDER BY sort_order,id")
        cat_names = [c["name"] for c in cats]
        for idx, s in enumerate(shots, 1):
            card = tk.Frame(self.shot_inner, bg=COLORS["card"], highlightbackground=COLORS["line"], highlightthickness=1)
            card.pack(fill="x", padx=2, pady=8)
            top = tk.Frame(card, bg=COLORS["card"])
            top.pack(fill="x", padx=14, pady=(12, 4))
            tk.Label(top, text=f"镜头{idx}", bg=COLORS["accent"], fg="white", font=("Microsoft YaHei UI", 10, "bold"), padx=12, pady=4).pack(side="left")
            tk.Label(top, text=f"排序编号：{s['sort_order']}", bg=COLORS["card"], fg=COLORS["muted"]).pack(side="left", padx=10)
            self.btn(top, "删除", lambda sid=s["id"]: self.delete_shot(sid), COLORS["red"]).pack(side="right")
            self.btn(top, "下移", lambda sid=s["id"]: self.move_shot(sid, 1), COLORS["panel"]).pack(side="right", padx=6)
            self.btn(top, "上移", lambda sid=s["id"]: self.move_shot(sid, -1), COLORS["panel"]).pack(side="right")

            grid = tk.Frame(card, bg=COLORS["card"])
            grid.pack(fill="x", padx=14, pady=(4, 12))
            for col in range(4):
                grid.columnconfigure(col, weight=1)

            name = tk.Entry(grid, bg=COLORS["panel"], fg=COLORS["text"], insertbackground=COLORS["text"], relief="flat")
            name.insert(0, s["shot_name"] or "")
            original = tk.Text(grid, height=4, bg=COLORS["panel"], fg=COLORS["text"], insertbackground=COLORS["text"], relief="flat", wrap="word")
            original.insert("1.0", s["original_text"] or "")
            ai_text = tk.Text(grid, height=4, bg=COLORS["panel"], fg=COLORS["muted"], insertbackground=COLORS["text"], relief="flat", wrap="word")
            ai_text.insert("1.0", s["ai_text"] or "")
            cat = ttk.Combobox(grid, values=cat_names, state="readonly")
            if s.get("category_name"):
                cat.set(s["category_name"])
            elif cat_names:
                cat.set(cat_names[0])
            voice_duration = float(s["voice_duration"] or 0)
            duration_info = tk.Label(
                grid,
                text=f"真实语音时长：{voice_duration:.3f} 秒" if voice_duration else "真实语音时长：未生成",
                bg=COLORS["panel"],
                fg=COLORS["text"],
                anchor="w",
                padx=8,
                pady=7,
            )
            voice_path_var = tk.StringVar(value=s["voice_file_path"] or "")
            mute_var = tk.IntVar(value=int(s["mute_original_audio"] or 0))
            sub_var = tk.IntVar(value=int(s["enable_subtitle"] if s["enable_subtitle"] is not None else 1))
            tts_var = tk.IntVar(value=int(s["enable_tts"] if s["enable_tts"] is not None else 1))

            def refresh_metric(_event=None, label=duration_info, vdur=voice_duration):
                suffix = f"真实语音时长：{vdur:.3f} 秒" if vdur else "真实语音时长：未生成，请点击【生成语音】"
                label.configure(text=suffix)

            original.bind("<KeyRelease>", refresh_metric)
            ai_text.bind("<KeyRelease>", refresh_metric)

            for label, r, c in [("名称", 0, 0), ("原始文案", 0, 1), ("AI改写文案（预留）", 0, 2), ("素材类别", 2, 0), ("语音时长", 2, 1)]:
                tk.Label(grid, text=label, bg=COLORS["card"], fg=COLORS["muted"]).grid(row=r, column=c, sticky="w", pady=(6, 2), padx=6)
            name.grid(row=1, column=0, sticky="ew", padx=6, ipady=7)
            original.grid(row=1, column=1, sticky="ew", padx=6)
            ai_text.grid(row=1, column=2, columnspan=2, sticky="ew", padx=6)
            cat.grid(row=3, column=0, sticky="ew", padx=6, ipady=4)
            duration_info.grid(row=3, column=1, sticky="ew", padx=6)
            tk.Checkbutton(grid, text="静音", variable=mute_var, bg=COLORS["card"], fg=COLORS["text"], selectcolor=COLORS["panel"], activebackground=COLORS["card"]).grid(row=3, column=2, sticky="w", padx=6)
            tk.Checkbutton(grid, text="生成字幕", variable=sub_var, bg=COLORS["card"], fg=COLORS["text"], selectcolor=COLORS["panel"], activebackground=COLORS["card"]).grid(row=3, column=2, sticky="e", padx=6)
            tk.Checkbutton(grid, text="生成配音", variable=tts_var, bg=COLORS["card"], fg=COLORS["text"], selectcolor=COLORS["panel"], activebackground=COLORS["card"]).grid(row=3, column=3, sticky="w", padx=6)
            self.btn(grid, "TTS生成语音", lambda sid=s["id"]: self.generate_shot_voice(sid), COLORS["accent"]).grid(row=4, column=0, sticky="ew", padx=6, pady=(10, 2))
            self.btn(grid, "导入语音文件", lambda sid=s["id"]: self.import_shot_voice(sid), COLORS["panel"]).grid(row=4, column=1, sticky="ew", padx=6, pady=(10, 2))
            tk.Label(grid, textvariable=voice_path_var, bg=COLORS["card"], fg=COLORS["muted"], anchor="w", wraplength=620).grid(row=4, column=2, columnspan=2, sticky="ew", padx=6, pady=(10, 2))
            self.btn(grid, "匹配素材", lambda sid=s["id"]: self.match_one_shot(sid, show_message=True), COLORS["green"]).grid(row=5, column=0, sticky="ew", padx=6, pady=(8, 2))
            match_text = self.match_summary(s["id"], s["voice_duration"], s.get("category_name") or "")
            match_var = tk.StringVar(value=match_text)
            tk.Label(grid, textvariable=match_var, bg=COLORS["card"], fg=COLORS["muted"], anchor="w", justify="left", wraplength=720).grid(row=5, column=1, columnspan=3, sticky="ew", padx=6, pady=(8, 2))
            self.match_result_labels[s["id"]] = match_var
            self.btn(grid, "生成字幕", lambda sid=s["id"]: self.generate_shot_subtitle(sid, show_message=True), COLORS["accent"]).grid(row=6, column=0, sticky="ew", padx=6, pady=(8, 2))
            subtitle_text = s["subtitle_file_path"] or "字幕：未生成"
            tk.Label(grid, text=subtitle_text, bg=COLORS["card"], fg=COLORS["muted"], anchor="w", wraplength=720).grid(row=6, column=1, columnspan=3, sticky="ew", padx=6, pady=(8, 2))
            self.shot_forms.append({"id": s["id"], "name": name, "original": original, "ai": ai_text, "cat": cat, "metric": duration_info, "voice_path": voice_path_var, "mute": mute_var, "subtitle": sub_var, "tts": tts_var})

    def add_shot(self):
        if not self.selected_template_id:
            messagebox.showinfo("提示", "先创建或选择一个模板")
            return
        r = fetch_one("SELECT COALESCE(MAX(sort_order),0)+1 next_order FROM template_shots WHERE template_id=?", (self.selected_template_id,))
        order = r["next_order"] if r else 1
        cat = fetch_one("SELECT id FROM material_categories ORDER BY sort_order,id LIMIT 1")
        exec_sql(
            """INSERT INTO template_shots(template_id,shot_name,original_text,ai_text,material_category_id,duration,estimated_duration,mute_original_audio,enable_subtitle,enable_tts,voice_file_path,voice_duration,sort_order,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.selected_template_id, f"镜头{order}", "", "", cat["id"] if cat else None, 0, 0, 1, 1, 1, "", 0, order, now(), now()),
        )
        self.load_shots()

    def save_all_shots(self):
        if not self.selected_template_id:
            return
        cats = {c["name"]: c["id"] for c in fetch_all("SELECT id,name FROM material_categories")}
        for form in self.shot_forms:
            original_text = form["original"].get("1.0", "end").strip()
            ai_text = form["ai"].get("1.0", "end").strip()
            exec_sql(
                """UPDATE template_shots SET shot_name=?,original_text=?,ai_text=?,material_category_id=?,mute_original_audio=?,enable_subtitle=?,enable_tts=?,updated_at=? WHERE id=?""",
                (
                    form["name"].get().strip() or "未命名镜头",
                    original_text,
                    ai_text,
                    cats.get(form["cat"].get()),
                    form["mute"].get(),
                    form["subtitle"].get(),
                    form["tts"].get(),
                    now(),
                    form["id"],
                ),
            )
        self.normalize_shot_order()
        self.load_shots()
        messagebox.showinfo("完成", "镜头已保存")

    def match_summary(self, shot_id, voice_duration=None, category_name=""):
        rel = fetch_one(
            """SELECT gsm.*,m.file_name,m.duration material_duration,m.width,m.height,c.name category_name
               FROM generation_shot_materials gsm
               JOIN materials m ON m.id=gsm.material_id
               LEFT JOIN material_categories c ON c.id=m.category_id
               WHERE gsm.shot_id=?""",
            (shot_id,),
        )
        if not rel:
            vd = float(voice_duration or 0)
            cat = category_name or "未分类"
            return f"匹配结果：未匹配 | 语音：{vd:.3f}秒 | 素材类别：{cat}"
        return (
            f"匹配结果：{rel['file_name']}\n"
            f"语音：{float(rel['voice_duration'] or 0):.3f}秒 | "
            f"素材类别：{rel['category_name'] or '未分类'} | "
            f"素材时长：{float(rel['material_duration'] or 0):.3f}秒 | "
            f"裁剪：{float(rel['material_start_time'] or 0):.3f}s - {float(rel['material_end_time'] or 0):.3f}s | "
            f"分辨率：{rel['width']}x{rel['height']}"
        )

    def match_all_shots(self):
        if not self.selected_template_id:
            messagebox.showinfo("提示", "先选择模板")
            return
        self.save_all_shots_silent()
        shot_ids = [s["id"] for s in fetch_all("SELECT id FROM template_shots WHERE template_id=? ORDER BY sort_order,id", (self.selected_template_id,))]
        ok = 0
        errors = []
        used = set()
        for sid in shot_ids:
            result = self.match_one_shot(sid, show_message=False, used_material_ids=used)
            if result.get("ok"):
                ok += 1
                used.add(result["material_id"])
            else:
                errors.append(result.get("message", f"镜头 {sid} 匹配失败"))
        self.load_shots()
        if errors:
            messagebox.showwarning("匹配完成但有不足", f"成功匹配 {ok} 个镜头。\n\n" + "\n".join(errors[:8]))
        else:
            messagebox.showinfo("完成", f"成功匹配 {ok} 个镜头")

    def generate_all_subtitles(self):
        if not self.selected_template_id:
            messagebox.showinfo("提示", "先选择模板")
            return
        self.save_all_shots_silent()
        shots = fetch_all("SELECT id FROM template_shots WHERE template_id=? ORDER BY sort_order,id", (self.selected_template_id,))
        ok = 0
        errors = []
        for s in shots:
            result = self.generate_shot_subtitle(s["id"], show_message=False)
            if result.get("ok"):
                ok += 1
            else:
                errors.append(result.get("message", f"镜头 {s['id']} 字幕失败"))
        self.load_shots()
        if errors:
            messagebox.showwarning("字幕完成但有失败", f"成功 {ok} 个。\n\n" + "\n".join(errors[:8]))
        else:
            messagebox.showinfo("完成", f"成功生成 {ok} 个镜头字幕")

    def generate_shot_subtitle(self, shot_id, show_message=False):
        form = next((f for f in self.shot_forms if f["id"] == shot_id), None)
        if form:
            self.save_all_shots_silent()
        shot = fetch_one("SELECT * FROM template_shots WHERE id=?", (shot_id,))
        if not shot:
            return {"ok": False, "message": "镜头不存在"}
        if not shot["enable_subtitle"]:
            msg = f"镜头【{shot['shot_name']}】未开启生成字幕"
            if show_message:
                messagebox.showinfo("提示", msg)
            return {"ok": False, "message": msg}
        if not shot["voice_file_path"]:
            msg = f"镜头【{shot['shot_name']}】没有语音文件"
            if show_message:
                messagebox.showwarning("无法生成字幕", msg)
            return {"ok": False, "message": msg}
        fallback_text = shot["ai_text"] or shot["original_text"] or ""
        try:
            subtitle_path, source = generate_ass_for_shot(shot_id, shot["voice_file_path"], fallback_text, shot["voice_duration"])
        except SubtitleError as e:
            if show_message:
                messagebox.showerror("字幕生成失败", str(e))
            return {"ok": False, "message": str(e)}
        exec_sql("UPDATE template_shots SET subtitle_file_path=?,updated_at=? WHERE id=?", (subtitle_path, now(), shot_id))
        if show_message:
            messagebox.showinfo("完成", f"字幕已生成：{subtitle_path}\n来源：{source}")
            self.load_shots()
        return {"ok": True, "path": subtitle_path, "source": source}

    def save_all_shots_silent(self):
        if not self.selected_template_id:
            return
        cats = {c["name"]: c["id"] for c in fetch_all("SELECT id,name FROM material_categories")}
        for form in self.shot_forms:
            exec_sql(
                """UPDATE template_shots SET shot_name=?,original_text=?,ai_text=?,material_category_id=?,mute_original_audio=?,enable_subtitle=?,enable_tts=?,updated_at=? WHERE id=?""",
                (
                    form["name"].get().strip() or "未命名镜头",
                    form["original"].get("1.0", "end").strip(),
                    form["ai"].get("1.0", "end").strip(),
                    cats.get(form["cat"].get()),
                    form["mute"].get(),
                    form["subtitle"].get(),
                    form["tts"].get(),
                    now(),
                    form["id"],
                ),
            )

    def match_one_shot(self, shot_id, show_message=False, used_material_ids=None):
        if used_material_ids is None:
            used_material_ids = {
                r["material_id"]
                for r in fetch_all(
                    """SELECT gsm.material_id FROM generation_shot_materials gsm
                       JOIN template_shots s ON s.id=gsm.shot_id
                       WHERE s.template_id=? AND s.id<>?""",
                    (self.selected_template_id, shot_id),
                )
            }
        else:
            used_material_ids = set(used_material_ids)
        form = next((f for f in self.shot_forms if f["id"] == shot_id), None)
        if form:
            self.save_all_shots_silent()
        shot = fetch_one(
            """SELECT s.*,c.name category_name FROM template_shots s
               LEFT JOIN material_categories c ON c.id=s.material_category_id
               WHERE s.id=?""",
            (shot_id,),
        )
        if not shot:
            return {"ok": False, "message": "镜头不存在"}
        voice_duration = float(shot["voice_duration"] or 0)
        if voice_duration <= 0:
            msg = f"镜头【{shot['shot_name']}】还没有真实语音时长，请先 TTS生成语音 或 导入语音文件。"
            if show_message:
                messagebox.showwarning("无法匹配", msg)
            return {"ok": False, "message": msg}
        if not shot["material_category_id"]:
            msg = f"镜头【{shot['shot_name']}】未选择素材分类。"
            if show_message:
                messagebox.showwarning("无法匹配", msg)
            return {"ok": False, "message": msg}

        candidates = fetch_all(
            """SELECT * FROM materials
               WHERE category_id=? AND status='active' AND duration>=?
               ORDER BY width*height DESC, duration ASC, id ASC""",
            (shot["material_category_id"], voice_duration),
        )
        fresh = [m for m in candidates if m["id"] not in used_material_ids]
        pool = fresh or candidates
        if not pool:
            longest = fetch_one(
                "SELECT MAX(duration) max_duration,COUNT(*) cnt FROM materials WHERE category_id=? AND status='active'",
                (shot["material_category_id"],),
            )
            max_d = float((longest or {}).get("max_duration") or 0)
            cnt = int((longest or {}).get("cnt") or 0)
            msg = f"素材不足：镜头【{shot['shot_name']}】需要 {voice_duration:.1f} 秒【{shot['category_name']}】素材"
            msg += f"，但是该分类最长只有 {max_d:.1f} 秒，共 {cnt} 个素材。需要补充。"
            if show_message:
                messagebox.showwarning("素材不足", msg)
            return {"ok": False, "message": msg}

        max_pixels = max((m["width"] or 0) * (m["height"] or 0) for m in pool)
        best_res = [m for m in pool if (m["width"] or 0) * (m["height"] or 0) == max_pixels]
        material = random.choice(best_res)
        max_start = max(0.0, float(material["duration"] or 0) - voice_duration)
        start = round(random.uniform(0, max_start), 3) if max_start > 0 else 0.0
        end = round(start + voice_duration, 3)
        upsert_shot_material(shot_id, material["id"], voice_duration, start, end)
        summary = self.match_summary(shot_id)
        if shot_id in self.match_result_labels:
            self.match_result_labels[shot_id].set(summary)
        if show_message:
            messagebox.showinfo("匹配成功", summary)
        return {"ok": True, "material_id": material["id"], "message": summary}

    def create_generation_task(self):
        if not self.selected_template_id:
            messagebox.showinfo("提示", "先选择模板")
            return
        self.save_all_shots_silent()
        tpl = fetch_one("SELECT * FROM edit_templates WHERE id=?", (self.selected_template_id,))
        missing = fetch_all(
            """SELECT s.shot_name
               FROM template_shots s
               LEFT JOIN generation_shot_materials gsm ON gsm.shot_id=s.id
               WHERE s.template_id=? AND (gsm.id IS NULL OR s.voice_duration<=0 OR s.voice_file_path='' OR (s.enable_subtitle=1 AND s.subtitle_file_path=''))""",
            (self.selected_template_id,),
        )
        if missing:
            names = "、".join([m["shot_name"] for m in missing[:8]])
            messagebox.showwarning("无法生成", f"以下镜头缺少语音、匹配素材或字幕：{names}\n请先生成/导入语音、匹配素材，并生成字幕。")
            return
        task_name = f"{tpl['name']} - {now()}"
        task_id = exec_sql(
            """INSERT INTO generation_tasks(template_id,task_name,status,progress,current_shot,total_shots,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (self.selected_template_id, task_name, "pending", 0, "等待开始", 0, now(), now()),
        )
        threading.Thread(target=self.generation_worker, args=(task_id, self.selected_template_id), daemon=True).start()
        messagebox.showinfo("任务已创建", f"生成任务已创建：{task_name}\n可到【任务中心】查看进度。")

    def generation_worker(self, task_id, template_id):
        try:
            settings = {r["key"]: r["value"] for r in fetch_all("SELECT * FROM app_settings")}
            path = generate_video(task_id, template_id, "", settings.get("output_dir", ""))
            self.generation_queue.put(("done", task_id, path))
        except Exception as e:
            exec_sql("UPDATE generation_tasks SET status=?,progress=?,error_message=?,current_shot=?,updated_at=? WHERE id=?", ("failed", 0, str(e), "失败", now(), task_id))
            self.generation_queue.put(("error", task_id, str(e)))

    def poll_generation(self):
        try:
            while True:
                msg = self.generation_queue.get_nowait()
                if msg[0] == "done":
                    if self.current_page == "任务中心":
                        self.refresh_tasks()
                    messagebox.showinfo("生成完成", f"视频已输出：\n{msg[2]}")
                elif msg[0] == "error":
                    if self.current_page == "任务中心":
                        self.refresh_tasks()
                    messagebox.showerror("生成失败", msg[2])
        except queue.Empty:
            pass
        if self.current_page == "任务中心" and hasattr(self, "task_tree"):
            self.refresh_tasks()
        self.after(1000, self.poll_generation)

    def generate_shot_voice(self, shot_id):
        form = next((f for f in self.shot_forms if f["id"] == shot_id), None)
        if not form:
            return
        original_text = form["original"].get("1.0", "end").strip()
        ai_text = form["ai"].get("1.0", "end").strip()
        final_text = ai_text or original_text
        if not final_text:
            messagebox.showinfo("提示", "请先输入原始文案或 AI 改写文案")
            return
        cats = {c["name"]: c["id"] for c in fetch_all("SELECT id,name FROM material_categories")}
        exec_sql(
            """UPDATE template_shots SET shot_name=?,original_text=?,ai_text=?,material_category_id=?,mute_original_audio=?,enable_subtitle=?,enable_tts=?,updated_at=? WHERE id=?""",
            (
                form["name"].get().strip() or "未命名镜头",
                original_text,
                ai_text,
                cats.get(form["cat"].get()),
                form["mute"].get(),
                form["subtitle"].get(),
                form["tts"].get(),
                now(),
                shot_id,
            ),
        )
        try:
            voice_path, duration = synthesize_windows_tts(final_text, shot_id)
        except TtsError as e:
            messagebox.showerror("TTS 生成失败", str(e))
            return
        exec_sql(
            "UPDATE template_shots SET voice_file_path=?,voice_duration=?,duration=?,estimated_duration=?,updated_at=? WHERE id=?",
            (voice_path, duration, duration, duration, now(), shot_id),
        )
        messagebox.showinfo("完成", f"语音已生成\n真实时长：{duration:.3f} 秒")
        self.load_shots()

    def import_shot_voice(self, shot_id):
        src = filedialog.askopenfilename(
            title="选择语音文件",
            filetypes=[("WAV 音频", "*.wav"), ("所有文件", "*.*")],
        )
        if not src:
            return
        duration = audio_duration(src)
        if duration <= 0:
            messagebox.showerror("导入失败", "当前只内置 WAV 时长检测；请选择有效 WAV，或后续配置 ffprobe 后扩展 MP3/AAC。")
            return
        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        dst = VOICE_DIR / f"shot_{shot_id}{Path(src).suffix.lower()}"
        if Path(src).resolve() != dst.resolve():
            shutil.copy2(src, dst)
        exec_sql(
            "UPDATE template_shots SET voice_file_path=?,voice_duration=?,duration=?,estimated_duration=?,updated_at=? WHERE id=?",
            (str(dst), duration, duration, duration, now(), shot_id),
        )
        messagebox.showinfo("完成", f"语音已导入\n真实时长：{duration:.3f} 秒")
        self.load_shots()

    def delete_shot(self, shot_id):
        exec_sql("DELETE FROM template_shots WHERE id=?", (shot_id,))
        self.normalize_shot_order()
        self.load_shots()

    def move_shot(self, shot_id, direction):
        shots = fetch_all("SELECT id FROM template_shots WHERE template_id=? ORDER BY sort_order,id", (self.selected_template_id,))
        ids = [s["id"] for s in shots]
        if shot_id not in ids:
            return
        i = ids.index(shot_id)
        j = i + direction
        if j < 0 or j >= len(ids):
            return
        ids[i], ids[j] = ids[j], ids[i]
        for order, sid in enumerate(ids, 1):
            exec_sql("UPDATE template_shots SET sort_order=?,updated_at=? WHERE id=?", (order, now(), sid))
        self.load_shots()

    def normalize_shot_order(self):
        if not self.selected_template_id:
            return
        shots = fetch_all("SELECT id FROM template_shots WHERE template_id=? ORDER BY sort_order,id", (self.selected_template_id,))
        for order, s in enumerate(shots, 1):
            exec_sql("UPDATE template_shots SET sort_order=? WHERE id=?", (order, s["id"]))

    # 时间轴预览
    def page_时间轴预览(self):
        self.title_block("镜头预览 / 时间轴", "生成前查看当前时间轴：镜头、时间、素材、语音、字幕。支持替换素材、重新匹配、删除和调整顺序。")
        self.timeline_template_id = None
        self.timeline_image_cache = {}

        wrap = tk.Frame(self.body, bg=COLORS["bg"])
        wrap.pack(fill="both", expand=True, padx=28, pady=12)
        left = self.card(wrap)
        mid = self.card(wrap)
        right = self.card(wrap)
        left.pack(side="left", fill="y")
        mid.pack(side="left", fill="both", expand=True, padx=(14, 0))
        right.pack(side="left", fill="y", padx=(14, 0))
        left.configure(width=260)
        right.configure(width=330)
        left.pack_propagate(False)
        right.pack_propagate(False)

        tk.Label(left, text="选择模板", bg=COLORS["panel"], fg=COLORS["text"], font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w", padx=16, pady=(16, 8))
        self.timeline_template_list = tk.Listbox(left, bg=COLORS["card"], fg=COLORS["text"], selectbackground=COLORS["accent"], relief="flat", activestyle="none", font=("Microsoft YaHei UI", 11))
        self.timeline_template_list.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        self.timeline_template_list.bind("<<ListboxSelect>>", self.on_timeline_template_select)
        self.btn(left, "刷新时间轴", self.refresh_timeline_view, COLORS["card"]).pack(fill="x", padx=16, pady=(0, 16))

        top = tk.Frame(mid, bg=COLORS["panel"])
        top.pack(fill="x", padx=16, pady=(16, 8))
        self.timeline_title = tk.Label(top, text="未选择模板", bg=COLORS["panel"], fg=COLORS["text"], font=("Microsoft YaHei UI", 16, "bold"))
        self.timeline_title.pack(side="left")
        self.btn(top, "生成/刷新 timeline.json", self.save_preview_timeline_json, COLORS["green"]).pack(side="right")

        self.timeline_canvas = tk.Canvas(mid, bg=COLORS["panel"], highlightthickness=0)
        self.timeline_scroll = ttk.Scrollbar(mid, orient="vertical", command=self.timeline_canvas.yview)
        self.timeline_inner = tk.Frame(self.timeline_canvas, bg=COLORS["panel"])
        self.timeline_inner.bind("<Configure>", lambda e: self.timeline_canvas.configure(scrollregion=self.timeline_canvas.bbox("all")))
        self.timeline_canvas.create_window((0, 0), window=self.timeline_inner, anchor="nw")
        self.timeline_canvas.configure(yscrollcommand=self.timeline_scroll.set)
        self.timeline_canvas.pack(side="left", fill="both", expand=True, padx=(16, 0), pady=(0, 16))
        self.timeline_scroll.pack(side="right", fill="y", pady=(0, 16))

        tk.Label(right, text="镜头详情", bg=COLORS["panel"], fg=COLORS["text"], font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w", padx=16, pady=(16, 8))
        self.timeline_detail = tk.Label(right, text="点击镜头查看详情", bg=COLORS["panel"], fg=COLORS["muted"], justify="left", wraplength=290, anchor="nw")
        self.timeline_detail.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.refresh_timeline_templates(select_first=True)

    def refresh_timeline_templates(self, select_first=False):
        self.timeline_templates = fetch_all("SELECT * FROM edit_templates ORDER BY id DESC")
        self.timeline_template_list.delete(0, "end")
        for t in self.timeline_templates:
            self.timeline_template_list.insert("end", f"{t['name']}    {(t.get('created_at') or '')[:10]}")
        if select_first and self.timeline_templates:
            self.timeline_template_list.selection_set(0)
            self.load_timeline_template(self.timeline_templates[0]["id"])

    def on_timeline_template_select(self, _=None):
        sel = self.timeline_template_list.curselection()
        if sel:
            self.load_timeline_template(self.timeline_templates[sel[0]]["id"])

    def load_timeline_template(self, template_id):
        self.timeline_template_id = template_id
        tpl = fetch_one("SELECT * FROM edit_templates WHERE id=?", (template_id,))
        self.timeline_title.configure(text=f"时间轴：{tpl['name'] if tpl else template_id}")
        self.refresh_timeline_view()

    def timeline_rows(self):
        if not self.timeline_template_id:
            return []
        return fetch_all(
            """SELECT s.*,c.name category_name,gsm.material_id,gsm.material_start_time,gsm.material_end_time,
                      m.file_name material_name,m.file_path material_path,m.duration material_duration,m.thumbnail_path,m.width,m.height
               FROM template_shots s
               LEFT JOIN material_categories c ON c.id=s.material_category_id
               LEFT JOIN generation_shot_materials gsm ON gsm.shot_id=s.id
               LEFT JOIN materials m ON m.id=gsm.material_id
               WHERE s.template_id=?
               ORDER BY s.sort_order,s.id""",
            (self.timeline_template_id,),
        )

    def refresh_timeline_view(self):
        for w in self.timeline_inner.winfo_children():
            w.destroy()
        rows = self.timeline_rows()
        if not rows:
            tk.Label(self.timeline_inner, text="当前模板没有镜头。", bg=COLORS["panel"], fg=COLORS["muted"], pady=40).pack(fill="x")
            return
        cursor = 0.0
        for idx, r in enumerate(rows, 1):
            dur = float(r["voice_duration"] or 0)
            start, end = cursor, cursor + dur
            cursor = end
            self.render_timeline_card(idx, r, start, end)
        self.render_bgm_timeline_card(cursor)

    def render_timeline_card(self, idx, r, start, end):
        card = tk.Frame(self.timeline_inner, bg=COLORS["card"], highlightbackground=COLORS["line"], highlightthickness=1)
        card.pack(fill="x", padx=2, pady=8)
        card.bind("<Button-1>", lambda e, row=r, st=start, en=end: self.show_timeline_detail(row, st, en))

        thumb = tk.Label(card, text="无缩略图", bg=COLORS["panel"], fg=COLORS["muted"], width=18, height=6)
        thumb.grid(row=0, column=0, rowspan=4, padx=12, pady=12, sticky="n")
        if r.get("thumbnail_path") and Path(r["thumbnail_path"]).exists():
            try:
                img = Image.open(r["thumbnail_path"]).resize((150, 84))
                tkimg = ImageTk.PhotoImage(img)
                self.timeline_image_cache[f"thumb_{r['id']}"] = tkimg
                thumb.configure(image=tkimg, text="")
            except Exception:
                pass

        title = f"镜头{idx}  {r['shot_name']}"
        tk.Label(card, text=title, bg=COLORS["card"], fg=COLORS["text"], font=("Microsoft YaHei UI", 13, "bold")).grid(row=0, column=1, sticky="w", padx=8, pady=(12, 2))
        tk.Label(card, text=f"时间：{fmt_time(start)} - {fmt_time(end)}    语音：{dur_text(r)}", bg=COLORS["card"], fg=COLORS["muted"]).grid(row=1, column=1, sticky="w", padx=8)
        tk.Label(card, text=f"视频：{r.get('material_name') or '未匹配'}    分类：{r.get('category_name') or '未分类'}", bg=COLORS["card"], fg=COLORS["muted"]).grid(row=2, column=1, sticky="w", padx=8)
        sub_status = "已生成" if r.get("subtitle_file_path") and Path(r["subtitle_file_path"]).exists() else "未生成"
        tk.Label(card, text=f"语音文件：{Path(r['voice_file_path']).name if r.get('voice_file_path') else '无'}    字幕：{sub_status}", bg=COLORS["card"], fg=COLORS["muted"]).grid(row=3, column=1, sticky="w", padx=8, pady=(0, 10))

        ops = tk.Frame(card, bg=COLORS["card"])
        ops.grid(row=0, column=2, rowspan=4, sticky="e", padx=10)
        self.btn(ops, "替换素材", lambda sid=r["id"]: self.open_replace_material_dialog(sid), COLORS["accent"]).pack(fill="x", pady=2)
        self.btn(ops, "重新匹配", lambda sid=r["id"]: self.timeline_rematch_shot(sid), COLORS["green"]).pack(fill="x", pady=2)
        self.btn(ops, "上移", lambda sid=r["id"]: self.timeline_move_shot(sid, -1), COLORS["panel"]).pack(fill="x", pady=2)
        self.btn(ops, "下移", lambda sid=r["id"]: self.timeline_move_shot(sid, 1), COLORS["panel"]).pack(fill="x", pady=2)
        self.btn(ops, "删除镜头", lambda sid=r["id"]: self.timeline_delete_shot(sid), COLORS["red"]).pack(fill="x", pady=2)
        card.columnconfigure(1, weight=1)

    def render_bgm_timeline_card(self, total_duration):
        bgm = self.get_default_bgm_row()
        card = tk.Frame(self.timeline_inner, bg="#172033", highlightbackground=COLORS["line"], highlightthickness=1)
        card.pack(fill="x", padx=2, pady=8)
        tk.Label(card, text="BGM轨", bg="#172033", fg=COLORS["text"], font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w", padx=14, pady=(12, 4))
        if bgm:
            msg = (
                f"时间：00:00 - {fmt_time(total_duration)}    layer：5    volume：{float(bgm['volume_default'] or 0.15):.2f}\n"
                f"音乐：{bgm['name']}    时长：{float(bgm['duration'] or 0):.1f}s\n"
                f"路径：{bgm['file_path']}"
            )
        else:
            msg = "未选择/未扫描 BGM。到【设置】选择 BGM目录 并扫描后，生成视频会自动使用最近一条BGM。"
        tk.Label(card, text=msg, bg="#172033", fg=COLORS["muted"], justify="left", anchor="w", wraplength=860).pack(fill="x", padx=14, pady=(0, 12))

    def get_default_bgm_row(self):
        setting = fetch_one("SELECT value FROM app_settings WHERE key='default_bgm_id'")
        bgm_id = (setting or {}).get("value") or ""
        if str(bgm_id).strip():
            row = fetch_one("SELECT * FROM bgm_library WHERE id=?", (bgm_id,))
            if row:
                return row
        return fetch_one("SELECT * FROM bgm_library ORDER BY id DESC LIMIT 1")

    def show_timeline_detail(self, r, start, end):
        text = (
            f"镜头：{r['shot_name']}\n"
            f"排序：{r['sort_order']}\n"
            f"时间：{fmt_time(start)} - {fmt_time(end)}\n"
            f"voice_duration：{float(r['voice_duration'] or 0):.3f}s\n\n"
            f"素材分类：{r.get('category_name') or '未分类'}\n"
            f"当前素材：{r.get('material_name') or '未匹配'}\n"
            f"素材路径：{r.get('material_path') or '-'}\n"
            f"素材时长：{float(r.get('material_duration') or 0):.3f}s\n"
            f"裁剪区间：{float(r.get('material_start_time') or 0):.3f}s - {float(r.get('material_end_time') or 0):.3f}s\n\n"
            f"语音：{r.get('voice_file_path') or '-'}\n"
            f"字幕：{r.get('subtitle_file_path') or '-'}"
        )
        self.timeline_detail.configure(text=text)

    def save_preview_timeline_json(self):
        if not self.timeline_template_id:
            messagebox.showinfo("提示", "先选择模板")
            return
        try:
            shots = load_template_shots(self.timeline_template_id)
            timeline = build_timeline_from_shots(shots)
            out = APP_DIR / "outputs" / f"preview_template_{self.timeline_template_id}_timeline.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(__import__("json").dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
            messagebox.showinfo("完成", f"timeline.json 已生成：\n{out.resolve()}")
        except Exception as e:
            messagebox.showerror("生成失败", str(e))

    def open_replace_material_dialog(self, shot_id):
        row = fetch_one("SELECT * FROM template_shots WHERE id=?", (shot_id,))
        if not row:
            return
        mats = fetch_all(
            """SELECT * FROM materials WHERE category_id=? AND status='active'
               ORDER BY (duration>=?) DESC,width*height DESC,duration ASC""",
            (row["material_category_id"], float(row["voice_duration"] or 0)),
        )
        if not mats:
            messagebox.showwarning("无素材", "当前分类没有可用素材")
            return
        win = tk.Toplevel(self)
        win.title("替换镜头素材")
        win.geometry("760x420")
        win.configure(bg=COLORS["bg"])
        tree = ttk.Treeview(win, columns=("id", "name", "duration", "res", "path"), show="headings")
        for c, t, w in [("id", "ID", 60), ("name", "文件名", 180), ("duration", "时长", 80), ("res", "分辨率", 100), ("path", "路径", 320)]:
            tree.heading(c, text=t)
            tree.column(c, width=w)
        tree.pack(fill="both", expand=True, padx=12, pady=12)
        for m in mats:
            tree.insert("", "end", iid=str(m["id"]), values=(m["id"], m["file_name"], f"{float(m['duration'] or 0):.1f}s", f"{m['width']}x{m['height']}", m["file_path"]))

        def choose():
            sel = tree.selection()
            if not sel:
                return
            self.timeline_set_material(shot_id, int(sel[0]))
            win.destroy()

        self.btn(win, "确认替换", choose, COLORS["green"]).pack(pady=(0, 12))

    def timeline_set_material(self, shot_id, material_id):
        shot = fetch_one("SELECT * FROM template_shots WHERE id=?", (shot_id,))
        mat = fetch_one("SELECT * FROM materials WHERE id=?", (material_id,))
        vd = float(shot["voice_duration"] or 0)
        if vd <= 0:
            messagebox.showwarning("无法替换", "镜头没有 voice_duration")
            return
        if float(mat["duration"] or 0) < vd:
            if not messagebox.askyesno("素材较短", f"素材只有 {float(mat['duration'] or 0):.1f}s，镜头需要 {vd:.1f}s。仍然使用？"):
                return
        max_start = max(0.0, float(mat["duration"] or 0) - vd)
        start = round(random.uniform(0, max_start), 3) if max_start > 0 else 0.0
        end = round(start + vd, 3)
        upsert_shot_material(shot_id, material_id, vd, start, end)
        self.refresh_timeline_view()

    def timeline_rematch_shot(self, shot_id):
        shot = fetch_one("SELECT * FROM template_shots WHERE id=?", (shot_id,))
        if not shot or float(shot["voice_duration"] or 0) <= 0:
            messagebox.showwarning("无法匹配", "镜头没有真实语音时长")
            return
        candidates = fetch_all(
            "SELECT * FROM materials WHERE category_id=? AND status='active' AND duration>=? ORDER BY width*height DESC,duration ASC",
            (shot["material_category_id"], float(shot["voice_duration"] or 0)),
        )
        if not candidates:
            longest = fetch_one("SELECT MAX(duration) max_duration,COUNT(*) cnt FROM materials WHERE category_id=? AND status='active'", (shot["material_category_id"],))
            messagebox.showwarning("素材不足", f"需要 {float(shot['voice_duration'] or 0):.1f}s 素材，但最长只有 {float((longest or {}).get('max_duration') or 0):.1f}s。")
            return
        max_pixels = max((m["width"] or 0) * (m["height"] or 0) for m in candidates)
        pool = [m for m in candidates if (m["width"] or 0) * (m["height"] or 0) == max_pixels]
        self.timeline_set_material(shot_id, random.choice(pool)["id"])

    def timeline_delete_shot(self, shot_id):
        if not messagebox.askyesno("确认", "删除这个镜头？"):
            return
        exec_sql("DELETE FROM template_shots WHERE id=?", (shot_id,))
        self.timeline_normalize_order()
        self.refresh_timeline_view()

    def timeline_move_shot(self, shot_id, direction):
        shots = fetch_all("SELECT id FROM template_shots WHERE template_id=? ORDER BY sort_order,id", (self.timeline_template_id,))
        ids = [s["id"] for s in shots]
        if shot_id not in ids:
            return
        i, j = ids.index(shot_id), ids.index(shot_id) + direction
        if j < 0 or j >= len(ids):
            return
        ids[i], ids[j] = ids[j], ids[i]
        for order, sid in enumerate(ids, 1):
            exec_sql("UPDATE template_shots SET sort_order=?,updated_at=? WHERE id=?", (order, now(), sid))
        self.refresh_timeline_view()

    def timeline_normalize_order(self):
        shots = fetch_all("SELECT id FROM template_shots WHERE template_id=? ORDER BY sort_order,id", (self.timeline_template_id,))
        for order, s in enumerate(shots, 1):
            exec_sql("UPDATE template_shots SET sort_order=? WHERE id=?", (order, s["id"]))

    # 任务中心
    def page_任务中心(self):
        self.title_block("任务中心", "生成任务状态看板：pending / running / processing_shot / merging / completed / failed。")
        bar = tk.Frame(self.body, bg=COLORS["bg"])
        bar.pack(fill="x", padx=28, pady=(0, 8))
        self.btn(bar, "刷新任务", self.refresh_tasks, COLORS["card"]).pack(side="left")
        box = self.card(self.body)
        box.pack(fill="both", expand=True, padx=28, pady=14)
        self.task_tree = ttk.Treeview(box, columns=("id", "name", "status", "progress", "current", "output", "error", "created"), show="headings")
        for c, t, w in [
            ("id", "ID", 55),
            ("name", "任务名", 230),
            ("status", "状态", 120),
            ("progress", "进度", 70),
            ("current", "当前处理镜头", 170),
            ("output", "输出文件", 300),
            ("error", "错误", 220),
            ("created", "创建时间", 150),
        ]:
            self.task_tree.heading(c, text=t)
            self.task_tree.column(c, width=w, anchor="w")
        self.task_tree.pack(fill="both", expand=True, padx=16, pady=16)
        self.refresh_tasks()

    def refresh_tasks(self):
        if not hasattr(self, "task_tree"):
            return
        for i in self.task_tree.get_children():
            self.task_tree.delete(i)
        for task in fetch_all("SELECT * FROM generation_tasks ORDER BY id DESC"):
            self.task_tree.insert(
                "",
                "end",
                iid=str(task["id"]),
                values=(
                    task["id"],
                    task["task_name"],
                    task["status"],
                    f"{task['progress']}%",
                    task.get("current_shot") or "",
                    task.get("output_path") or "",
                    (task.get("error_message") or "")[:120],
                    task["created_at"],
                ),
            )

    # 账号档案
    def page_账号档案(self):
        self.title_block("账号档案", "保存公司、行业、产品、品牌和人设信息。")
        wrap = tk.Frame(self.body, bg=COLORS["bg"])
        wrap.pack(fill="both", expand=True, padx=28, pady=12)
        left = self.card(wrap)
        right = self.card(wrap)
        left.pack(side="left", fill="both", expand=True)
        right.pack(side="left", fill="both", expand=True, padx=(14, 0))
        tk.Label(left, text="档案列表", bg=COLORS["panel"], fg=COLORS["text"], font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w", padx=16, pady=16)
        self.profile_tree = ttk.Treeview(left, columns=("id", "company", "industry", "product"), show="headings")
        for c, t, w in [("id", "ID", 50), ("company", "公司", 180), ("industry", "行业", 120), ("product", "产品", 220)]:
            self.profile_tree.heading(c, text=t)
            self.profile_tree.column(c, width=w)
        self.profile_tree.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.profile_tree.bind("<<TreeviewSelect>>", self.load_profile)
        self.btn(left, "删除选中档案", self.delete_profile, COLORS["red"]).pack(anchor="e", padx=16, pady=(0, 16))
        tk.Label(right, text="编辑档案", bg=COLORS["panel"], fg=COLORS["text"], font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w", padx=16, pady=16)
        form = tk.Frame(right, bg=COLORS["panel"])
        form.pack(fill="both", expand=True, padx=16)
        self.profile_id = None
        self.p_company = self.input(form, "公司名称", 0)
        self.p_industry = self.input(form, "行业", 1)
        self.p_product = self.text(form, "产品信息", 2)
        self.p_brand = self.text(form, "品牌介绍", 3)
        self.p_persona = self.text(form, "人设信息", 4)
        buttons = tk.Frame(form, bg=COLORS["panel"])
        buttons.grid(row=10, column=0, sticky="ew", pady=14)
        self.btn(buttons, "新建", self.clear_profile, COLORS["card"]).pack(side="left")
        self.btn(buttons, "保存档案", self.save_profile, COLORS["green"]).pack(side="left", padx=10)
        form.columnconfigure(0, weight=1)
        self.refresh_profiles()

    def refresh_profiles(self):
        for i in self.profile_tree.get_children():
            self.profile_tree.delete(i)
        for p in fetch_all("SELECT * FROM account_profiles ORDER BY id DESC"):
            self.profile_tree.insert("", "end", iid=str(p["id"]), values=(p["id"], p["company_name"], p["industry"], p["product_info"][:40]))

    def clear_profile(self):
        self.profile_id = None
        for e in [self.p_company, self.p_industry]:
            e.delete(0, "end")
        for t in [self.p_product, self.p_brand, self.p_persona]:
            t.delete("1.0", "end")

    def load_profile(self, _=None):
        sel = self.profile_tree.selection()
        if not sel:
            return
        p = fetch_one("SELECT * FROM account_profiles WHERE id=?", (int(sel[0]),))
        self.clear_profile()
        self.profile_id = p["id"]
        self.p_company.insert(0, p["company_name"])
        self.p_industry.insert(0, p["industry"])
        self.p_product.insert("1.0", p["product_info"])
        self.p_brand.insert("1.0", p["brand_intro"])
        self.p_persona.insert("1.0", p["persona_info"])

    def save_profile(self):
        company = self.p_company.get().strip()
        if not company:
            messagebox.showinfo("提示", "公司名称不能为空")
            return
        vals = (company, self.p_industry.get().strip(), self.p_product.get("1.0", "end").strip(), self.p_brand.get("1.0", "end").strip(), self.p_persona.get("1.0", "end").strip())
        if self.profile_id:
            exec_sql("UPDATE account_profiles SET company_name=?,industry=?,product_info=?,brand_intro=?,persona_info=?,updated_at=? WHERE id=?", vals + (now(), self.profile_id))
        else:
            self.profile_id = exec_sql("INSERT INTO account_profiles(company_name,industry,product_info,brand_intro,persona_info,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", vals + (now(), now()))
        self.refresh_profiles()
        messagebox.showinfo("完成", "账号档案已保存")

    def delete_profile(self):
        sel = self.profile_tree.selection()
        if sel and messagebox.askyesno("确认", "删除选中的账号档案？"):
            exec_sql("DELETE FROM account_profiles WHERE id=?", (int(sel[0]),))
            self.clear_profile()
            self.refresh_profiles()

    # 生产测试
    def page_生产测试(self):
        self.title_block("生产测试", "一键跑通：测试账号 → 测试素材 → 测试模板 → 语音 → 字幕 → 素材匹配 → FFmpeg最终MP4。")
        top = tk.Frame(self.body, bg=COLORS["bg"])
        top.pack(fill="x", padx=28, pady=(0, 10))
        self.btn(top, "开始完整生产测试", self.start_production_test, COLORS["red"]).pack(side="left")
        self.btn(top, "清空日志显示", self.clear_test_log, COLORS["card"]).pack(side="left", padx=10)
        hint = (
            "重点检查：FFmpeg/FFprobe、字幕烧录、音视频同步、镜头拼接、输出MP4可播放。\n"
            "测试会自动生成3段竖屏测试素材和3段测试语音；如果 FFmpeg 未配置，会在第一步失败并写入报告。"
        )
        tk.Label(self.body, text=hint, bg=COLORS["bg"], fg=COLORS["muted"], justify="left", anchor="w").pack(fill="x", padx=28, pady=(0, 10))
        box = self.card(self.body)
        box.pack(fill="both", expand=True, padx=28, pady=(0, 24))
        self.test_status = tk.Label(box, text="状态：等待测试", bg=COLORS["panel"], fg=COLORS["text"], anchor="w")
        self.test_status.pack(fill="x", padx=16, pady=(16, 8))
        self.test_log = tk.Text(box, bg=COLORS["card"], fg=COLORS["text"], insertbackground=COLORS["text"], relief="flat", wrap="word")
        self.test_log.pack(fill="both", expand=True, padx=16, pady=(0, 16))

    def clear_test_log(self):
        if hasattr(self, "test_log"):
            self.test_log.delete("1.0", "end")
            self.test_status.configure(text="状态：等待测试")

    def append_test_log(self, msg):
        if hasattr(self, "test_log"):
            self.test_log.insert("end", msg + "\n")
            self.test_log.see("end")

    def start_production_test(self):
        if hasattr(self, "test_status"):
            self.test_status.configure(text="状态：测试运行中")
        self.append_test_log("========== 开始完整生产测试 ==========")

        def emit(msg):
            self.test_queue.put(("log", msg))

        def worker():
            result = run_production_test(emit=emit)
            self.test_queue.put(("done", result))

        threading.Thread(target=worker, daemon=True).start()

    def poll_test_queue(self):
        try:
            while True:
                msg = self.test_queue.get_nowait()
                if msg[0] == "log":
                    self.append_test_log(msg[1])
                elif msg[0] == "done":
                    result = msg[1]
                    if hasattr(self, "test_status"):
                        if result.get("ok"):
                            self.test_status.configure(text=f"状态：测试成功 | 输出：{result.get('output_path')}")
                            messagebox.showinfo("生产测试成功", f"输出视频：\n{result.get('output_path')}\n\n报告：\n{result.get('report')}")
                        else:
                            self.test_status.configure(text=f"状态：测试失败 | {result.get('error')}")
                            messagebox.showerror("生产测试失败", f"错误：\n{result.get('error')}\n\n报告：\n{result.get('report')}")
                    self.append_test_log("========== 测试结束 ==========")
        except queue.Empty:
            pass
        self.after(500, self.poll_test_queue)

    # 设置
    def page_设置(self):
        self.title_block("设置", "配置输出目录和媒体资源，视频处理组件已随软件内置。")
        box = self.card(self.body)
        box.pack(fill="both", expand=True, padx=28, pady=14)
        form = tk.Frame(box, bg=COLORS["panel"])
        form.pack(fill="x", padx=18, pady=18)
        data = {r["key"]: r["value"] for r in fetch_all("SELECT * FROM app_settings")}
        self.s_output = self.input(form, "默认输出目录", 0)
        self.s_bgm_dir = self.input(form, "BGM目录", 1)
        self.s_output.insert(0, data.get("output_dir", ""))
        self.s_bgm_dir.insert(0, data.get("bgm_dir", ""))
        status = get_media_runtime_status()
        state = "正常" if status["ready"] else "异常"
        detail = f"FFmpeg {status['ffmpeg'].get('version') or '未检测'} · FFprobe {status['ffprobe'].get('version') or '未检测'}"
        tk.Label(form, text=f"视频处理引擎：{state}  {detail}\n视频处理组件已内置，无需配置。", bg=COLORS["panel"], fg=COLORS["muted"], justify="left").grid(row=4,column=0,sticky="w",pady=12)
        btns = tk.Frame(form, bg=COLORS["panel"])
        btns.grid(row=5, column=0, sticky="w", pady=12)
        self.btn(btns, "选择BGM目录", self.choose_bgm_dir, COLORS["card"]).pack(side="left")
        self.btn(btns, "扫描BGM", self.scan_bgm_from_settings, COLORS["accent"]).pack(side="left", padx=8)
        self.btn(btns, "保存设置", self.save_settings, COLORS["green"]).pack(side="left")
        form.columnconfigure(0, weight=1)
        tk.Label(box, text=f"数据库：{DB_PATH}", bg=COLORS["panel"], fg=COLORS["muted"], justify="left").pack(anchor="w", padx=18, pady=8)
        tk.Label(box, text="BGM库", bg=COLORS["panel"], fg=COLORS["text"], font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w", padx=18, pady=(12, 4))
        self.bgm_tree = ttk.Treeview(box, columns=("id", "name", "duration", "volume", "path"), show="headings", height=8)
        for c, t, w in [("id", "ID", 50), ("name", "名称", 180), ("duration", "时长", 90), ("volume", "默认音量", 90), ("path", "路径", 520)]:
            self.bgm_tree.heading(c, text=t)
            self.bgm_tree.column(c, width=w, anchor="w")
        self.bgm_tree.pack(fill="x", padx=18, pady=(0, 12))
        bgm_ops = tk.Frame(box, bg=COLORS["panel"])
        bgm_ops.pack(fill="x", padx=18, pady=(0, 12))
        self.btn(bgm_ops, "设为默认BGM", self.set_default_bgm, COLORS["green"]).pack(side="left")
        self.refresh_bgm_tree()

    def save_settings(self):
        for k, v in {"output_dir": self.s_output.get().strip(), "bgm_dir": self.s_bgm_dir.get().strip()}.items():
            exec_sql("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (k, v, now()))
        messagebox.showinfo("完成", "设置已保存")

    def choose_bgm_dir(self):
        folder = filedialog.askdirectory(title="选择BGM目录")
        if folder:
            self.s_bgm_dir.delete(0, "end")
            self.s_bgm_dir.insert(0, folder)

    def scan_bgm_from_settings(self):
        folder = self.s_bgm_dir.get().strip()
        if not folder:
            messagebox.showinfo("提示", "先选择BGM目录")
            return
        self.save_settings()
        result = scan_bgm_folder(folder)
        self.refresh_bgm_tree()
        messagebox.showinfo("扫描完成", f"共 {result['total']} 个音频，新增 {result['added']}，更新 {result['updated']}")

    def refresh_bgm_tree(self):
        if not hasattr(self, "bgm_tree"):
            return
        for i in self.bgm_tree.get_children():
            self.bgm_tree.delete(i)
        for b in fetch_all("SELECT * FROM bgm_library ORDER BY id DESC"):
            self.bgm_tree.insert("", "end", iid=str(b["id"]), values=(b["id"], b["name"], f"{float(b['duration'] or 0):.1f}s", b["volume_default"], b["file_path"]))

    def set_default_bgm(self):
        sel = self.bgm_tree.selection() if hasattr(self, "bgm_tree") else []
        if not sel:
            messagebox.showinfo("提示", "先选择一条BGM")
            return
        exec_sql("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", ("default_bgm_id", str(sel[0]), now()))
        messagebox.showinfo("完成", f"默认BGM已设置为 ID={sel[0]}")


if __name__ == "__main__":
    MixcutApp().mainloop()
