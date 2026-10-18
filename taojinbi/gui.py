from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
import winsound
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from .adb import Adb, Control, ROOT, default_adb
from .model import Stopped
from .runner import Runner


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("淘金币 · ADB 浏览助手")
        root.geometry("980x800")
        root.minsize(860, 800)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("Treeview", rowheight=34)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 19, "bold"))
        style.configure("Accent.TButton", padding=(20, 8))
        self.messages: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.control = Control()
        self.tasks = {}
        self.selected: set[str] = set()
        self.has_scanned = False
        self.run_directory = ROOT / "artifacts"
        self.completed = 0
        self.adb_path = tk.StringVar(value=default_adb())
        self.serial = tk.StringVar()
        self.status = tk.StringVar(value="连接手机并打开淘金币任务列表，然后点击“检查连接”。")
        self.counter = tk.StringVar(value="本次确认完成 0 项")
        self.limit = tk.StringVar(value="0")
        self.all_browse = tk.BooleanVar(value=True)
        self.notify_sound = tk.BooleanVar(value=True)
        self.notify_toast = tk.BooleanVar(value=True)
        self.toast_window: tk.Toplevel | None = None
        try:
            settings = json.loads((ROOT / ".local-settings.json").read_text(encoding="utf-8"))
            self.adb_path.set(settings.get("adb", default_adb()))
            self.serial.set(settings.get("serial", ""))
            self.notify_sound.set(bool(settings.get("beep", True)))
            self.notify_toast.set(bool(settings.get("toast", True)))
        except (OSError, ValueError):
            pass
        self.build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.drain)

    def build(self):
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="淘金币浏览助手", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="本地执行  ·  任务进度核验  ·  验证与异常停机").pack(anchor="w", pady=(5, 14))
        connection = ttk.Frame(outer)
        connection.pack(fill="x")
        ttk.Label(connection, text="ADB 路径").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.path_entry = ttk.Entry(connection, textvariable=self.adb_path)
        self.path_entry.grid(row=0, column=1, columnspan=3, sticky="ew")
        self.browse_button = ttk.Button(connection, text="选择…", command=self.browse)
        self.browse_button.grid(row=0, column=4, padx=(8, 0))
        ttk.Label(connection, text="设备").grid(row=1, column=0, sticky="w", pady=10)
        self.device_box = ttk.Combobox(connection, textvariable=self.serial, state="readonly", width=26)
        self.device_box.grid(row=1, column=1, sticky="w")
        self.connect_button = ttk.Button(connection, text="检查连接", command=lambda: self.begin("devices"))
        self.connect_button.grid(row=1, column=2, padx=8)
        self.scan_button = ttk.Button(connection, text="扫描任务", command=lambda: self.begin("scan"))
        self.scan_button.grid(row=1, column=3, sticky="w")
        connection.columnconfigure(1, weight=1)
        table_frame = ttk.Frame(outer)
        self.auto_check = ttk.Checkbutton(outer, text="自动处理全部可执行任务（站内浏览 + 跳转其他 APP；跟随换名）", variable=self.all_browse)
        self.auto_check.pack(anchor="w", pady=(0, 4))
        table_frame.pack(fill="both", expand=True, pady=(6, 10))
        self.table = ttk.Treeview(table_frame, columns=("check", "title", "progress", "detail", "support"),
                                  show="headings", selectmode="browse", height=6)
        for name, label, width in (("check", "执行", 50), ("title", "任务", 230),
                                    ("progress", "进度", 70), ("detail", "要求", 230), ("support", "状态", 125)):
            self.table.heading(name, text=label)
            self.table.column(name, width=width, stretch=name in ("title", "detail"))
        self.table.tag_configure("unavailable", foreground="#808080")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.bind("<ButtonRelease-1>", self.toggle)
        controls = ttk.Frame(outer)
        controls.pack(fill="x")
        self.start_button = ttk.Button(controls, text="开始执行", style="Accent.TButton", command=lambda: self.begin("run"))
        self.start_button.pack(side="left")
        self.pause_button = ttk.Button(controls, text="暂停", command=self.pause, state="disabled")
        self.pause_button.pack(side="left", padx=8)
        self.stop_button = ttk.Button(controls, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left")
        ttk.Label(controls, text="本次上限（0=全部）").pack(side="left", padx=(20, 5))
        self.limit_box = ttk.Spinbox(controls, from_=0, to=50, textvariable=self.limit, width=5)
        self.limit_box.pack(side="left")
        ttk.Label(controls, textvariable=self.counter).pack(side="right")
        notify = ttk.Frame(outer)
        notify.pack(fill="x", pady=(10, 0))
        ttk.Label(notify, text="完成提醒").pack(side="left")
        ttk.Checkbutton(notify, text="响铃", variable=self.notify_sound).pack(side="left", padx=(10, 14))
        ttk.Checkbutton(notify, text="置顶提示（3 秒后自动消失）",
                        variable=self.notify_toast).pack(side="left")
        ttk.Label(outer, textvariable=self.status, wraplength=900).pack(anchor="w", fill="x", pady=(12, 6))
        self.logbox = tk.Text(outer, height=5, font=("Microsoft YaHei UI", 9), state="disabled", wrap="word")
        self.logbox.pack(fill="x")
        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(6, 0))
        ttk.Label(footer, text="保持手机亮屏和淘宝前台；调试时请勿同时手动操作。").pack(side="left")
        ttk.Button(footer, text="打开本地记录", command=self.open_records).pack(side="right")

    @staticmethod
    def available(task):
        return task.supported and task.remaining > 0

    def browse(self):
        path = filedialog.askopenfilename(title="选择 adb.exe", filetypes=[("ADB", "adb.exe")])
        if path:
            self.adb_path.set(path)

    def busy(self, value: bool, running: bool = False):
        for widget in (self.connect_button, self.scan_button, self.start_button,
                       self.browse_button, self.path_entry, self.limit_box, self.auto_check):
            widget.configure(state="disabled" if value else "normal")
        self.device_box.configure(state="disabled" if value else "readonly")
        self.pause_button.configure(state="normal" if running else "disabled", text="暂停")
        self.stop_button.configure(state="normal" if value else "disabled")

    def begin(self, mode: str):
        if self.worker and self.worker.is_alive():
            return
        try:
            limit = int(self.limit.get())
            if limit < 0 or limit > 50:
                raise ValueError()
        except ValueError:
            messagebox.showerror("执行上限", "请填写0至50的整数，0表示执行全部剩余任务。")
            return
        path, serial = self.adb_path.get().strip(), self.serial.get().strip()
        if mode != "devices" and not serial:
            messagebox.showinfo("设备", "请先检查连接并选择设备。")
            return
        titles = [title for title, task in self.tasks.items() if title in self.selected and self.available(task)]
        all_browse = self.all_browse.get()
        if mode == "run" and not titles and not all_browse:
            messagebox.showinfo("任务", "请先扫描任务并勾选可执行项目。")
            return
        self.control = Control()
        control = self.control
        self.busy(True, mode == "run")
        self.status.set("正在检查连接…" if mode == "devices" else "正在读取手机…")
        def work():
            try:
                adb = Adb(path, "" if mode == "devices" else serial, control)
                if mode == "devices":
                    devices = adb.devices()
                    if not devices:
                        raise RuntimeError("没有已授权设备，请检查 USB 连接及手机上的调试授权。")
                    self.messages.put(("devices", devices))
                else:
                    runner = Runner(adb, lambda kind, value: self.messages.put((kind, value)))
                    self.messages.put(("directory", runner.directory))
                    if mode == "scan":
                        runner.scan()
                        self.messages.put(("state", "扫描完成，可以开始执行。"))
                    else:
                        self.messages.put(("reset_count", None))
                        runner.run(titles, limit=limit or None, all_browse=all_browse)
            except Stopped:
                self.messages.put(("state", "已停止"))
            except Exception as exc:
                self.messages.put(("error", str(exc)))
            finally:
                self.messages.put(("idle", None))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def render_tasks(self, tasks):
        previous = list(self.tasks.values())
        if not self.has_scanned:
            self.selected = {t.title for t in tasks if self.available(t)}
            self.has_scanned = True
        else:
            # A renamed slot keeps the user's choice; match it by its position in the list.
            current = {t.title for t in tasks}
            for index, old in enumerate(previous):
                if old.title in self.selected and old.title not in current and index < len(tasks):
                    renamed = tasks[index]
                    if self.available(renamed):
                        self.selected.add(renamed.title)
        self.tasks = {t.title: t for t in tasks}
        self.table.delete(*self.table.get_children())
        for task in tasks:
            available = self.available(task)
            checked = "☑" if available and task.title in self.selected else "☐" if available else "—"
            if available:
                state = "跳转或站外" if task.kind == "open" else "站内浏览"
            else:
                state = "已完成" if not task.remaining else task.skip_reason
            self.table.insert("", "end", iid=task.title,
                              values=(checked, task.title, f"{task.done}/{task.total}", task.detail, state),
                              tags=() if available else ("unavailable",))

    def toggle(self, event):
        if self.worker and self.worker.is_alive():
            return
        item = self.table.identify_row(event.y)
        if not item or not self.available(self.tasks[item]):
            return
        if item in self.selected:
            self.selected.remove(item)
        else:
            self.selected.add(item)
        self.render_tasks(list(self.tasks.values()))

    def pause(self):
        if self.control.paused.is_set():
            self.control.resume()
            self.pause_button.configure(text="暂停")
            self.status.set("已继续；下一步将重新读取页面。")
        else:
            self.control.pause()
            self.pause_button.configure(text="继续")
            self.status.set("已暂停，不再发送新操作；已发出的手势可能完成。")

    def stop(self):
        self.control.stop()
        self.status.set("正在停止，不再发送新操作…")

    def add_log(self, message):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", f"{datetime.now():%H:%M:%S}  {message}\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def notify(self, title):
        """Both reminders are opt-out; nothing here is forced on the user."""
        if self.notify_sound.get():
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except RuntimeError:
                pass
        if self.notify_toast.get():
            self.show_toast(title)

    def show_toast(self, title):
        self.close_toast()
        window = tk.Toplevel(self.root)
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        frame = ttk.Frame(window, padding=(16, 12), relief="solid", borderwidth=1)
        frame.pack()
        ttk.Label(frame, text=f"已完成：{title}", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        ttk.Label(frame, text=f"本次累计 {self.completed} 项").pack(anchor="w")
        window.update_idletasks()
        x = self.root.winfo_rootx() + self.root.winfo_width() - window.winfo_width() - 24
        y = self.root.winfo_rooty() + self.root.winfo_height() - window.winfo_height() - 24
        window.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.toast_window = window
        window.after(3000, self.close_toast)

    def close_toast(self):
        window, self.toast_window = self.toast_window, None
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass

    def drain(self):
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "devices":
                    self.device_box.configure(values=value)
                    if self.serial.get() not in value:
                        self.serial.set(value[0])
                    self.status.set(f"已连接 {len(value)} 台设备，请扫描任务。")
                elif kind == "tasks":
                    self.render_tasks(value)
                elif kind == "state":
                    if not self.control.paused.is_set():
                        self.status.set(value)
                elif kind == "log":
                    self.add_log(value)
                elif kind == "directory":
                    self.run_directory = value
                elif kind == "reset_count":
                    self.completed = 0
                    self.counter.set("本次确认完成 0 项")
                elif kind == "result":
                    self.completed += 1
                    self.counter.set(f"本次确认完成 {self.completed} 项")
                    self.notify(value.title)
                elif kind == "task_failed":
                    self.add_log(f"任务未确认完成：{value.title}（{value.reason}）")
                elif kind == "error":
                    self.status.set(f"已停止：{value}")
                    self.add_log(value)
                elif kind == "idle":
                    self.busy(False)
        except queue.Empty:
            pass
        self.root.after(100, self.drain)

    def open_records(self):
        self.run_directory.mkdir(parents=True, exist_ok=True)
        os.startfile(self.run_directory)

    def close(self):
        self.control.stop()
        self.close_toast()
        (ROOT / ".local-settings.json").write_text(json.dumps(
            {"adb": self.adb_path.get(), "serial": self.serial.get(),
             "beep": self.notify_sound.get(), "toast": self.notify_toast.get()},
            ensure_ascii=False), encoding="utf-8")
        # Give the worker's polling loop time to reap the active adb process.
        if self.worker and self.worker.is_alive():
            self.status.set("正在停止并关闭…")
            self.root.after(200, self.close)
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()
