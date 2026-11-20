#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Res Manager  (RES file manager + built-in IGI 2 sound player)

 - Drag & drop: drop a .res to open it, drop files/folders to add them
 - Tick boxes (or Ctrl/Shift-select) to act on many items at once
 - Smart destination folder when adding files (no more asking every time)
 - Play IGI 2 (ILSF) .wav files right inside the RES tree
"""

import os
import sys
import time
import wave
import shutil
import struct
import tempfile
import posixpath
import subprocess
from collections import Counter

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:  # app still works without drag & drop
    HAS_DND = False

# ─────────────────────────────────────────────────────────────────────────────
#  Paths
# ─────────────────────────────────────────────────────────────────────────────


def app_dir():
    """Folder of the .exe (frozen) or of this script."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name):
    """Path of a file bundled inside the exe (--add-data) or next to the script."""
    return os.path.join(getattr(sys, "_MEIPASS", app_dir()), name)


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ─────────────────────────────────────────────────────────────────────────────
#  IGI 2 sound (ILSF) support
# ─────────────────────────────────────────────────────────────────────────────

_STEP = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
    253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
    3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767,
]
_INDEX = [-1, -1, -1, -1, 2, 4, 6, 8]
PACK_NAMES = {0: "RAW", 1: "RAW_RESIDENT", 2: "ADPCM", 3: "ADPCM_RESIDENT"}


def _adpcm_decode(data, channels=1):
    if not data or channels < 1:
        return b""
    has_term = data[-1] == 0xAB
    body = data[:-1] if has_term else data
    states = [[0, 0] for _ in range(channels)]  # [predictor, step_index]
    out = []
    n = 0
    for byte in body:
        for code in ((byte >> 4) & 0x0F, byte & 0x0F):
            st = states[n % channels]
            pred, idx = st
            step = _STEP[idx]
            diff = step >> 3
            if code & 1:
                diff += step >> 2
            if code & 2:
                diff += step >> 1
            if code & 4:
                diff += step
            pred = pred - diff if code & 8 else pred + diff
            pred = max(-32768, min(32767, pred))
            out.append(pred)
            idx = max(0, min(88, idx + _INDEX[code & 7]))
            st[0], st[1] = pred, idx
            n += 1
    if not has_term and out:
        out.pop()
    return struct.pack("<%dh" % len(out), *out) if out else b""


class ILSFHeader:
    SIZE = 20

    def __init__(self, sound_pack, sample_width, channels, framerate, sample_count):
        self.sound_pack = sound_pack
        self.sample_width = sample_width
        self.channels = channels
        self.framerate = framerate
        self.sample_count = sample_count

    @classmethod
    def parse(cls, data):
        if len(data) < cls.SIZE:
            raise ValueError("File too short for an ILSF header")
        sig, sp, sw, ch, _unk, fr, sc = struct.unpack_from("<4s4H2I", data, 0)
        if sig != b"ILSF":
            raise ValueError("Not an ILSF file")
        return cls(sp, sw, ch, fr, sc)

    def describe(self):
        dur = self.sample_count / self.framerate if self.framerate else 0
        return "ILSF %s, %d Hz, %d ch, %.2f s" % (
            PACK_NAMES.get(self.sound_pack, "?"), self.framerate, self.channels, dur)


def ilsf_info(path):
    """One-line description of a .wav (ILSF or standard)."""
    with open(path, "rb") as f:
        head = f.read(ILSFHeader.SIZE)
    if head[:4] == b"RIFF":
        return "Standard WAV"
    return ILSFHeader.parse(head).describe()


def convert_ilsf_to_wav(src, dst):
    """Write a standard WAV from an ILSF file (standard WAVs are copied as-is)."""
    with open(src, "rb") as f:
        data = f.read()
    if data[:4] == b"RIFF":
        with open(dst, "wb") as f:
            f.write(data)
        return
    hdr = ILSFHeader.parse(data)
    body = data[ILSFHeader.SIZE:]
    if hdr.sound_pack in (0, 1):
        pcm = body
    elif hdr.sound_pack in (2, 3):
        pcm = _adpcm_decode(body, hdr.channels)
    else:
        raise ValueError("Unsupported sound_pack: %s" % hdr.sound_pack)
    width = hdr.sample_width // 8 if hdr.sample_width >= 8 else hdr.sample_width
    with wave.open(dst, "wb") as wf:
        wf.setnchannels(hdr.channels)
        wf.setsampwidth(width if width >= 1 else 2)
        wf.setframerate(hdr.framerate)
        wf.writeframes(pcm)


class AudioPlayer:
    """Plays ILSF/standard wav through winsound (Windows)."""

    def __init__(self):
        try:
            import winsound
            self.ws = winsound
        except ImportError:
            self.ws = None
        self._tmp = None
        self._end = 0.0
        self._stale = []

    @property
    def is_playing(self):
        return self._tmp is not None and time.monotonic() < self._end

    def _remove(self, path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            self._stale.append(path)

    def cleanup(self):
        if self._tmp and not self.is_playing:
            self._remove(self._tmp)
            self._tmp = None
        for p in self._stale[:]:
            self._stale.remove(p)
            self._remove(p)

    def play(self, src):
        self.stop()
        if not self.ws:
            raise RuntimeError("Playback is only supported on Windows.")
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            convert_ilsf_to_wav(src, tmp)
            with wave.open(tmp, "rb") as w:
                dur = w.getnframes() / float(w.getframerate() or 1)
            self.ws.PlaySound(tmp, self.ws.SND_FILENAME | self.ws.SND_ASYNC)
        except Exception:
            self._remove(tmp)
            raise
        self._tmp = tmp
        self._end = time.monotonic() + dur + 0.15

    def stop(self):
        if self.ws:
            try:
                self.ws.PlaySound(None, self.ws.SND_PURGE)
            except Exception:
                pass
        self._end = 0.0
        self.cleanup()


# ─────────────────────────────────────────────────────────────────────────────
#  UI helpers
# ─────────────────────────────────────────────────────────────────────────────

BG = "#1e1e1e"
PANEL = "#2d2d2d"
FG = "#ffffff"
SEL = "#0d47a1"
ROOT_LABEL = "(root)"


class FolderDialog(tk.Toplevel):
    """Ask once where a batch of files should go (pick existing or type new)."""

    def __init__(self, parent, folders, count, default=""):
        super().__init__(parent)
        self.title("Destination folder")
        self.configure(bg=BG)
        self.transient(parent)
        self.resizable(False, False)
        self.result = None

        ttk.Label(self, text="Where should the %d file(s) go?" % count).pack(
            anchor="w", padx=15, pady=(15, 2))
        ttk.Label(self, text="Pick a folder from the list, or type a new path (e.g. sounds/new).",
                  foreground="#aaaaaa").pack(anchor="w", padx=15, pady=(0, 8))

        self.var = tk.StringVar(value=default or ROOT_LABEL)
        self.combo = ttk.Combobox(self, textvariable=self.var, width=55,
                                  values=[ROOT_LABEL] + folders)
        self.combo.pack(padx=15, fill="x")

        row = ttk.Frame(self)
        row.pack(pady=15)
        ttk.Button(row, text="OK", command=self.ok).pack(side="left", padx=5)
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="left", padx=5)

        self.bind("<Return>", self.ok)
        self.bind("<Escape>", lambda e: self.destroy())
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
        self.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        self.grab_set()
        self.combo.focus_set()
        self.wait_window(self)

    def ok(self, _event=None):
        value = self.var.get().strip().replace("\\", "/")
        if value == ROOT_LABEL:
            value = ""
        parts = [p for p in value.split("/") if p]
        if any(p in (".", "..") or ":" in p for p in parts):
            messagebox.showerror("Invalid folder", "Please use a simple relative path.", parent=self)
            return
        self.result = "/".join(parts)
        self.destroy()


def make_checkbox_image(checked):
    img = tk.PhotoImage(width=16, height=16)
    img.put(PANEL, to=(0, 0, 16, 16))
    border = "#2e7dd7" if checked else "#9e9e9e"
    for i in range(2, 14):
        for xy in ((i, 2), (i, 13), (2, i), (13, i)):
            img.put(border, to=xy)
    if checked:
        img.put("#2e7dd7", to=(3, 3, 13, 13))
        for xy in ((4, 8), (5, 9), (6, 10), (7, 9), (8, 8), (9, 7), (10, 6), (11, 5),
                   (4, 7), (5, 8), (6, 9), (7, 8), (8, 7), (9, 6), (10, 5)):
            img.put("#ffffff", to=xy)
    return img


# ─────────────────────────────────────────────────────────────────────────────
#  Main application
# ─────────────────────────────────────────────────────────────────────────────

class ResManager:
    def __init__(self, root):
        self.root = root
        self.current_res_file = None
        self.dirty = False
        self.checked = set()
        self.dirs = set()
        self.files = set()
        self.last_dest = ""

        base = app_dir()
        self.work_dir = base if os.access(base, os.W_OK) else os.path.join(
            tempfile.gettempdir(), "ResManager")
        os.makedirs(self.work_dir, exist_ok=True)
        self.extract_dir = os.path.join(self.work_dir, "temp_extract")
        self.gconv_path = self.find_gconv()

        self.player = AudioPlayer()
        self._was_playing = False

        self.setup_dark_theme()
        self.img_off = make_checkbox_image(False)
        self.img_on = make_checkbox_image(True)
        self.setup_ui()
        self.setup_drag_drop()
        self.update_title()
        self.poll_audio()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        try:
            root.iconbitmap(resource_path("Icon.ico"))
        except tk.TclError:
            pass

    # ── setup ────────────────────────────────────────────────────────────────

    def setup_dark_theme(self):
        self.root.title("RES File Manager")
        self.root.geometry("980x680")
        self.root.minsize(700, 450)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("TButton", background=PANEL, foreground=FG, padding=(8, 3))
        style.map("TButton", background=[("active", "#3d3d3d")])
        style.configure("Treeview", background=PANEL, foreground=FG,
                        fieldbackground=PANEL, borderwidth=0, rowheight=22)
        style.configure("Treeview.Heading", background="#404040", foreground=FG)
        style.map("Treeview", background=[("selected", SEL)],
                  foreground=[("selected", FG)])
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=FG)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)])
        self.root.option_add("*TCombobox*Listbox.background", PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.configure(bg=BG)

    def setup_ui(self):
        r = self.root
        menu_kw = dict(tearoff=0, bg=PANEL, fg=FG)

        menubar = tk.Menu(r, **menu_kw)
        r.config(menu=menubar)

        m = tk.Menu(menubar, **menu_kw)
        menubar.add_cascade(label="File", menu=m)
        m.add_command(label="Open RES File...  (Ctrl+O)", command=self.open_res)
        m.add_command(label="Save  (Ctrl+S)", command=self.save_res)
        m.add_separator()
        m.add_command(label="Exit", command=self.on_close)

        m = tk.Menu(menubar, **menu_kw)
        menubar.add_cascade(label="Edit", menu=m)
        m.add_command(label="Add Files...", command=self.add_files_dialog)
        m.add_command(label="Add Folder...", command=self.add_folder_dialog)
        m.add_command(label="Delete  (Del)", command=self.delete_items)
        m.add_command(label="Extract...", command=self.extract_items)
        m.add_separator()
        m.add_command(label="Check All", command=self.check_all)
        m.add_command(label="Uncheck All", command=self.uncheck_all)

        m = tk.Menu(menubar, **menu_kw)
        menubar.add_cascade(label="Sound", menu=m)
        m.add_command(label="Play / Stop selected WAV  (Enter)", command=self.toggle_play)
        m.add_command(label="Extract as standard WAV...", command=self.export_wavs)

        # toolbar
        tb = ttk.Frame(r)
        tb.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)

        def btn(text, cmd):
            ttk.Button(tb, text=text, command=cmd).pack(side=tk.LEFT, padx=2)

        def sep():
            ttk.Separator(tb, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=6)

        btn("Open", self.open_res)
        btn("Save", self.save_res)
        sep()
        btn("Add Files", self.add_files_dialog)
        btn("Add Folder", self.add_folder_dialog)
        btn("Delete", self.delete_items)
        btn("Extract", self.extract_items)
        sep()
        self.play_btn = ttk.Button(tb, text="\u25b6 Play", command=self.toggle_play)
        self.play_btn.pack(side=tk.LEFT, padx=2)
        sep()
        btn("Check All", self.check_all)
        btn("Uncheck All", self.uncheck_all)
        btn("Refresh", self.display_tree)

        # current file + counter
        pf = ttk.Frame(r)
        pf.pack(fill=tk.X, padx=5, pady=(0, 3))
        ttk.Label(pf, text="Current File:").pack(side=tk.LEFT)
        self.path_label = ttk.Label(pf, text="No file opened", foreground="orange")
        self.path_label.pack(side=tk.LEFT, padx=10)
        self.count_label = ttk.Label(pf, text="Checked: 0")
        self.count_label.pack(side=tk.RIGHT, padx=5)

        # status bar (packed before the tree so it always stays visible)
        self.status_bar = ttk.Label(r, text="Ready", relief=tk.SUNKEN, anchor="w")
        self.status_bar.pack(fill=tk.X, side=tk.BOTTOM)

        # tree
        tf = ttk.Frame(r)
        tf.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        sb = ttk.Scrollbar(tf)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree = ttk.Treeview(tf, columns=("size",), selectmode="extended",
                                 yscrollcommand=sb.set)
        sb.config(command=self.tree.yview)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.heading("#0", text="Files inside RES  (click a box to tick, or use Ctrl/Shift)")
        self.tree.heading("size", text="Size")
        self.tree.column("#0", width=640, stretch=True)
        self.tree.column("size", width=100, anchor="e", stretch=False)

        t = self.tree
        t.bind("<Button-1>", self.on_click)
        t.bind("<Double-1>", self.on_double_click)
        t.bind("<Button-3>", self.context_menu)
        t.bind("<space>", self.on_space)
        t.bind("<Delete>", lambda e: self.delete_items())
        t.bind("<Return>", lambda e: self.toggle_play())
        t.bind("<Control-a>", self.select_all_rows)
        t.bind("<<TreeviewSelect>>", self.on_select)
        r.bind("<Control-o>", lambda e: self.open_res())
        r.bind("<Control-s>", lambda e: self.save_res())

    def setup_drag_drop(self):
        if not HAS_DND:
            self.set_status("Drag & drop disabled (pip install tkinterdnd2)")
            return
        for w in (self.root, self.tree):
            w.drop_target_register(DND_FILES)
            w.dnd_bind("<<Drop>>", self.on_drop)

    # ── small helpers ────────────────────────────────────────────────────────

    def set_status(self, text):
        self.status_bar.config(text=text)

    def update_title(self):
        name = os.path.basename(self.current_res_file) if self.current_res_file else ""
        title = "RES File Manager"
        if name:
            title += " - " + name
        if self.dirty:
            title += " *"
        self.root.title(title)

    def set_dirty(self, value=True):
        self.dirty = value
        self.update_title()

    def abs_path(self, rel):
        return os.path.join(self.extract_dir, *rel.split("/")) if rel else self.extract_dir

    def iter_items(self, parent=""):
        for child in self.tree.get_children(parent):
            yield child
            yield from self.iter_items(child)

    def descendants(self, rel):
        prefix = rel + "/"
        return [x for x in (self.dirs | self.files) if x.startswith(prefix)]

    def files_under(self, rel):
        if rel in self.files:
            return [rel]
        prefix = rel + "/"
        return sorted(f for f in self.files if f.startswith(prefix))

    @staticmethod
    def is_wav(rel):
        return rel.lower().endswith(".wav")

    # ── gconv ────────────────────────────────────────────────────────────────

    def find_gconv(self):
        base = app_dir()
        for p in (os.path.join(base, "gconv", "gconv.exe"),
                  os.path.join(base, "gconv.exe"),
                  resource_path(os.path.join("gconv", "gconv.exe")),
                  os.path.join(os.getcwd(), "gconv", "gconv.exe")):
            if os.path.isfile(p):
                return p
        return None

    def ensure_gconv(self):
        if self.gconv_path and os.path.isfile(self.gconv_path):
            return True
        messagebox.showwarning(
            "gconv.exe not found",
            "gconv.exe was not found next to the program (gconv\\gconv.exe).\n"
            "Please locate it.")
        p = filedialog.askopenfilename(title="Locate gconv.exe",
                                       filetypes=[("gconv.exe", "gconv.exe"), ("Executables", "*.exe")])
        if p:
            self.gconv_path = p
            return True
        return False

    def run_gconv(self, qsc_name, input_path, output_path):
        cmd = [self.gconv_path, qsc_name,
               "-InputPath=%s" % input_path, "-OutputPath=%s" % output_path]
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                              stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)

    # ── open / extract / save ────────────────────────────────────────────────

    def confirm_discard(self):
        if not self.dirty:
            return True
        ans = messagebox.askyesnocancel("Unsaved changes",
                                        "Save changes to the current RES file first?")
        if ans is None:
            return False
        if ans:
            return self.save_res()
        return True

    def open_res(self):
        path = filedialog.askopenfilename(
            filetypes=[("RES Files", "*.res"), ("All Files", "*.*")])
        if path:
            self.load_res(path)

    def load_res(self, path):
        if not self.confirm_discard():
            return
        self.stop_audio()
        self.current_res_file = path
        self.checked.clear()
        self.path_label.config(text=path, foreground="lime")
        if not self.extract_and_show():
            self.current_res_file = None
            self.path_label.config(text="No file opened", foreground="orange")
        self.set_dirty(False)

    def extract_and_show(self):
        if not self.ensure_gconv():
            return False
        qsc = os.path.join(self.work_dir, "temp_extract.qsc")
        try:
            self.set_status("Extracting...")
            self.root.config(cursor="watch")
            self.root.update()
            shutil.rmtree(self.extract_dir, ignore_errors=True)
            os.makedirs(self.extract_dir, exist_ok=True)
            with open(qsc, "w") as f:
                f.write('ExtractResource("%s");' % os.path.basename(self.current_res_file))
            res = self.run_gconv(qsc, os.path.dirname(self.current_res_file), self.extract_dir)
            if res.returncode != 0:
                messagebox.showerror("Extraction Error", (res.stderr or res.stdout or "gconv failed")[:1500])
                self.set_status("Extraction failed")
                return False
            self.display_tree()
            self.set_status("Ready  -  drop files here to add them")
            return True
        except Exception as e:
            messagebox.showerror("Error", str(e))
            self.set_status("Error")
            return False
        finally:
            self.root.config(cursor="")
            if os.path.exists(qsc):
                os.remove(qsc)

    def generate_qsc(self):
        lines = ['BeginResource("%s");' % os.path.basename(self.current_res_file)]
        for folder, dirs, files in os.walk(self.extract_dir):
            dirs.sort()
            for name in sorted(files):
                rel = os.path.relpath(os.path.join(folder, name), self.extract_dir).replace("\\", "/")
                lines.append('AddResource("%s", "LOCAL:%s");' % (rel, rel))
        lines.append("EndResource();")
        return "\n".join(lines) + "\n"

    def save_res(self):
        if not self.current_res_file:
            messagebox.showerror("Error", "No file is open")
            return False
        if not self.ensure_gconv():
            return False
        qsc = os.path.join(self.work_dir, "Resource.qsc")
        try:
            self.set_status("Saving...")
            self.root.config(cursor="watch")
            self.root.update()
            bak = self.current_res_file + ".bak"
            if os.path.isfile(self.current_res_file) and not os.path.exists(bak):
                shutil.copy2(self.current_res_file, bak)
            with open(qsc, "w") as f:
                f.write(self.generate_qsc())
            res = self.run_gconv(qsc, self.extract_dir, os.path.dirname(self.current_res_file))
            if res.returncode != 0:
                messagebox.showerror("Save Error", (res.stderr or res.stdout or "gconv failed")[:1500])
                self.set_status("Save failed")
                return False
            self.set_dirty(False)
            self.set_status("Saved")
            messagebox.showinfo("Success", "File saved successfully")
            return True
        except Exception as e:
            messagebox.showerror("Error", str(e))
            self.set_status("Save failed")
            return False
        finally:
            self.root.config(cursor="")
            if os.path.exists(qsc):
                os.remove(qsc)

    # ── tree ─────────────────────────────────────────────────────────────────

    def display_tree(self):
        opened = {i for i in self.iter_items() if self.tree.item(i, "open")}
        self.tree.delete(*self.tree.get_children())
        self.dirs, self.files = set(), set()
        self._fill("")
        self.checked &= (self.dirs | self.files)
        for i in opened:
            if self.tree.exists(i):
                self.tree.item(i, open=True)
        self.update_counter()

    def _fill(self, rel):
        path = self.abs_path(rel)
        try:
            entries = sorted(os.listdir(path), key=lambda n: (not os.path.isdir(os.path.join(path, n)), n.lower()))
        except OSError:
            return
        for name in entries:
            child_rel = rel + "/" + name if rel else name
            full = os.path.join(path, name)
            img = self.img_on if child_rel in self.checked else self.img_off
            if os.path.isdir(full):
                self.dirs.add(child_rel)
                self.tree.insert(rel, "end", iid=child_rel, text=" \U0001F4C1 " + name, image=img)
                self._fill(child_rel)
            else:
                self.files.add(child_rel)
                size = "%.2f KB" % (os.path.getsize(full) / 1024)
                self.tree.insert(rel, "end", iid=child_rel, text=" \U0001F4C4 " + name,
                                 image=img, values=(size,))

    # ── checkboxes / selection ───────────────────────────────────────────────

    def update_counter(self):
        n = sum(1 for c in self.checked if c in self.files)
        self.count_label.config(text="Checked: %d" % n)

    def set_check(self, items, state):
        changed = []
        for it in items:
            changed.append(it)
            if it in self.dirs:
                changed.extend(self.descendants(it))
        for it in changed:
            if state:
                self.checked.add(it)
            else:
                self.checked.discard(it)
            if self.tree.exists(it):
                self.tree.item(it, image=self.img_on if state else self.img_off)
        self.update_counter()

    def check_all(self):
        self.set_check(list(self.tree.get_children("")), True)

    def uncheck_all(self):
        self.set_check(list(self.tree.get_children("")), False)
        self.checked.clear()
        self.update_counter()

    def on_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "tree":
            return None
        try:
            element = self.tree.identify_element(event.x, event.y)
        except (AttributeError, tk.TclError):
            return None
        if "image" in element:
            item = self.tree.identify_row(event.y)
            if item:
                self.set_check([item], item not in self.checked)
                return "break"
        return None

    def on_space(self, _event):
        sel = list(self.tree.selection())
        if sel:
            self.set_check(sel, not all(i in self.checked for i in sel))
        return "break"

    def select_all_rows(self, _event):
        self.tree.selection_set(list(self.iter_items()))
        return "break"

    def get_targets(self):
        """Checked items if any are ticked, otherwise the selected rows."""
        items = sorted(self.checked) if self.checked else list(self.tree.selection())
        chosen = set(i for i in items if i in self.files or i in self.dirs)
        result = []
        for it in sorted(chosen):
            parts = it.split("/")
            if any("/".join(parts[:k]) in chosen for k in range(1, len(parts))):
                continue  # a parent folder is already included
            result.append(it)
        return result

    # ── adding files ─────────────────────────────────────────────────────────

    def on_drop(self, event):
        paths = list(self.root.tk.splitlist(event.data))
        if len(paths) == 1 and paths[0].lower().endswith(".res") and os.path.isfile(paths[0]):
            self.load_res(paths[0])
            return event.action
        if not self.current_res_file:
            messagebox.showerror("Error", "Please open a RES file first (you can drop a .res here).")
            return event.action
        self.add_paths(paths, self.folder_at_pointer(event.x_root, event.y_root))
        return event.action

    def folder_at_pointer(self, xr, yr):
        """Folder under the mouse when something is dropped on the tree.
        Returns None (no hint) or a rel folder path ('' = root)."""
        tx, ty = self.tree.winfo_rootx(), self.tree.winfo_rooty()
        if not (tx <= xr < tx + self.tree.winfo_width() and ty <= yr < ty + self.tree.winfo_height()):
            return None
        item = self.tree.identify_row(yr - ty)
        if not item:
            return None
        return item if item in self.dirs else posixpath.dirname(item)

    def selected_folder(self):
        sel = self.tree.selection()
        if len(sel) == 1 and sel[0] in self.dirs:
            return sel[0]
        return None

    def auto_folder(self, ext):
        counts = Counter(posixpath.dirname(f) for f in self.files
                         if posixpath.splitext(f)[1].lower() == ext)
        if counts:
            return counts.most_common(1)[0][0]
        return {".tex": "textures", ".mef": "models"}.get(ext)

    def add_files_dialog(self):
        if not self.current_res_file:
            messagebox.showerror("Error", "Please open a RES file first")
            return
        paths = filedialog.askopenfilenames(title="Select files to add")
        if paths:
            self.add_paths(list(paths), self.selected_folder())

    def add_folder_dialog(self):
        if not self.current_res_file:
            messagebox.showerror("Error", "Please open a RES file first")
            return
        path = filedialog.askdirectory(title="Select folder to add")
        if path:
            self.add_paths([path], self.selected_folder())

    def add_paths(self, paths, hint=None):
        # 1) expand dropped folders into (source, sub-folder) jobs
        jobs = []
        for p in paths:
            if os.path.isfile(p):
                jobs.append((p, "", False))
            elif os.path.isdir(p):
                top = os.path.basename(os.path.normpath(p))
                for folder, _dirs, names in os.walk(p):
                    sub = os.path.relpath(folder, p).replace("\\", "/")
                    sub = top if sub == "." else top + "/" + sub
                    for n in names:
                        jobs.append((os.path.join(folder, n), sub, True))
        if not jobs:
            return

        # 2) decide the destination folder of every job
        plan, unknown = [], []
        for src, sub, from_folder in jobs:
            name = os.path.basename(src)
            if from_folder:
                base = hint if hint is not None else ""
            elif hint is not None:
                base = hint
            else:
                base = self.auto_folder(os.path.splitext(name)[1].lower())
            plan.append([src, base, sub, name])
            if base is None:
                unknown.append(plan[-1])

        # 3) ask only once, and only if some files have no obvious home
        if unknown:
            dlg = FolderDialog(self.root, sorted(self.dirs), len(unknown),
                               default=self.selected_folder() or self.last_dest)
            if dlg.result is None:
                return
            self.last_dest = dlg.result
            for item in unknown:
                item[1] = dlg.result

        targets = []
        for src, base, sub, name in plan:
            rel = "/".join(x for x in (base, sub, name) if x)
            targets.append((src, rel))

        # 4) existing files
        existing = [rel for _s, rel in targets if rel in self.files]
        skip_existing = False
        if existing:
            sample = "\n".join(existing[:6]) + ("\n..." if len(existing) > 6 else "")
            ans = messagebox.askyesnocancel(
                "Files already exist",
                "%d file(s) already exist in the RES:\n\n%s\n\nYes = overwrite, No = skip them, Cancel = abort."
                % (len(existing), sample))
            if ans is None:
                return
            skip_existing = not ans

        # 5) copy
        added, errors = [], []
        for src, rel in targets:
            if skip_existing and rel in self.files:
                continue
            dest = self.abs_path(rel)
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
                added.append(rel)
            except Exception as e:
                errors.append("%s: %s" % (os.path.basename(src), e))

        if added:
            self.set_dirty(True)
            self.display_tree()
            parents = set()
            for a in added:
                parts = a.split("/")
                for k in range(1, len(parts)):
                    parents.add("/".join(parts[:k]))
            for p in parents:
                if self.tree.exists(p):
                    self.tree.item(p, open=True)
            self.tree.selection_set(added[:300])
            self.tree.see(added[0])
            folder = posixpath.dirname(added[0]) or ROOT_LABEL
            self.set_status("Added %d file(s)  ->  %s   (remember to Save)" % (len(added), folder))
        if errors:
            messagebox.showerror("Some files failed", "\n".join(errors[:10]))

    # ── delete / extract ─────────────────────────────────────────────────────

    def delete_items(self):
        items = self.get_targets()
        if not items:
            messagebox.showwarning("Selection", "Please select or tick something first")
            return
        sample = "\n".join(items[:8]) + ("\n..." if len(items) > 8 else "")
        if not messagebox.askyesno("Confirm delete",
                                   "Delete %d item(s) (folders include their contents)?\n\n%s" % (len(items), sample)):
            return
        errors = []
        for it in items:
            p = self.abs_path(it)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                elif os.path.exists(p):
                    os.remove(p)
            except Exception as e:
                errors.append("%s: %s" % (it, e))
        self.set_dirty(True)
        self.display_tree()
        self.set_status("Deleted %d item(s)   (remember to Save)" % (len(items) - len(errors)))
        if errors:
            messagebox.showerror("Some items failed", "\n".join(errors[:10]))

    def extract_items(self):
        items = self.get_targets()
        if not items:
            messagebox.showwarning("Selection", "Please select or tick something first")
            return
        dest = filedialog.askdirectory(title="Select save location")
        if not dest:
            return
        errors = []
        for it in items:
            src = self.abs_path(it)
            try:
                if os.path.isdir(src):
                    shutil.copytree(src, os.path.join(dest, posixpath.basename(it)), dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dest)
            except Exception as e:
                errors.append("%s: %s" % (it, e))
        self.set_status("Extracted %d item(s) to %s" % (len(items) - len(errors), dest))
        if errors:
            messagebox.showerror("Some items failed", "\n".join(errors[:10]))

    # ── sound ────────────────────────────────────────────────────────────────

    def on_select(self, _event=None):
        sel = self.tree.selection()
        if len(sel) == 1 and sel[0] in self.files and self.is_wav(sel[0]):
            try:
                self.set_status("%s  -  %s   (Enter / double-click to play)"
                                % (posixpath.basename(sel[0]), ilsf_info(self.abs_path(sel[0]))))
            except Exception:
                pass

    def first_wav(self):
        for it in list(self.tree.selection()) + sorted(self.checked):
            if it in self.files and self.is_wav(it):
                return it
        return None

    def toggle_play(self):
        if self.player.is_playing:
            self.stop_audio()
            return
        item = self.first_wav()
        if not item:
            messagebox.showinfo("Play", "Select a .wav file in the list first.")
            return
        self.play_item(item)

    def play_item(self, rel):
        try:
            self.player.play(self.abs_path(rel))
        except Exception as e:
            messagebox.showerror("Playback failed", str(e))
            return
        self.play_btn.config(text="\u25a0 Stop")
        self._was_playing = True
        self.set_status("Playing: " + posixpath.basename(rel))

    def stop_audio(self):
        self.player.stop()
        self.play_btn.config(text="\u25b6 Play")
        self._was_playing = False

    def on_double_click(self, event):
        item = self.tree.identify_row(event.y)
        if item and item in self.files and self.is_wav(item):
            self.play_item(item)
            return "break"
        return None

    def poll_audio(self):
        playing = self.player.is_playing
        if self._was_playing and not playing:
            self.player.cleanup()
            self.play_btn.config(text="\u25b6 Play")
            self.set_status("Finished")
        self._was_playing = playing
        self.root.after(200, self.poll_audio)

    def export_wavs(self):
        wavs = []
        for it in self.get_targets():
            wavs.extend(f for f in self.files_under(it) if self.is_wav(f))
        if not wavs:
            messagebox.showinfo("Extract WAV", "Select or tick one or more .wav files first.")
            return
        dest = filedialog.askdirectory(title="Save standard WAV files to...")
        if not dest:
            return
        ok, errors = 0, []
        for rel in wavs:
            name = posixpath.basename(rel)
            out = os.path.join(dest, name)
            n = 2
            while os.path.exists(out):
                out = os.path.join(dest, "%s_%d.wav" % (posixpath.splitext(name)[0], n))
                n += 1
            try:
                convert_ilsf_to_wav(self.abs_path(rel), out)
                ok += 1
            except Exception as e:
                errors.append("%s: %s" % (name, e))
        self.set_status("Converted %d/%d WAV file(s) to %s" % (ok, len(wavs), dest))
        if errors:
            messagebox.showerror("Some files failed", "\n".join(errors[:10]))

    # ── context menu / exit ──────────────────────────────────────────────────

    def context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if item and item not in self.tree.selection():
            self.tree.selection_set(item)
        menu = tk.Menu(self.root, tearoff=0, bg=PANEL, fg=FG)
        if item and item in self.files and self.is_wav(item):
            menu.add_command(label="Play", command=lambda: self.play_item(item))
            menu.add_separator()
        menu.add_command(label="Extract...", command=self.extract_items)
        menu.add_command(label="Extract as standard WAV...", command=self.export_wavs)
        menu.add_command(label="Delete", command=self.delete_items)
        menu.add_separator()
        menu.add_command(label="Check selected", command=lambda: self.set_check(list(self.tree.selection()), True))
        menu.add_command(label="Uncheck selected", command=lambda: self.set_check(list(self.tree.selection()), False))
        menu.add_command(label="Check all", command=self.check_all)
        menu.add_command(label="Uncheck all", command=self.uncheck_all)
        menu.post(event.x_root, event.y_root)

    def on_close(self):
        if not self.confirm_discard():
            return
        self.stop_audio()
        shutil.rmtree(self.extract_dir, ignore_errors=True)
        self.root.destroy()


def main():
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    app = ResManager(root)
    # allow: drag a .res onto the exe, or "Open with"
    if len(sys.argv) > 1 and sys.argv[1].lower().endswith(".res") and os.path.isfile(sys.argv[1]):
        root.after(150, lambda: app.load_res(sys.argv[1]))
    root.mainloop()


if __name__ == "__main__":
    main()
