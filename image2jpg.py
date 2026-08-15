# -*- coding: utf-8 -*-
"""
image2jpg.py — 本地图片批量格式转换桌面工具。

功能：
  - 支持拖入（Windows 原生拖放）或按钮添加文件/文件夹
  - 支持主流输入格式：jpg/jpeg/png/bmp/gif/webp/tiff/ico/ppm 等
  - 可批量转换为任意输出格式：JPG（默认）/ PNG / WEBP / BMP / TIFF / GIF / ICO / AVIF
  - 一键批量转换，可选输出到指定文件夹
  - 可选等比缩放（百分比 / 最长边 / 宽度）与重命名（前缀 / 后缀 / 序号）
  - 透明通道：JPG/BMP/GIF 自动填白底；PNG/WEBP/TIFF/ICO/AVIF 保留透明
  - EXIF 方向自动校正；JPG/WEBP/AVIF 质量可调（默认 100%）

运行（需本机完整 Python + Pillow，见 README）：
  python image2jpg.py
"""
import os
import ctypes
import threading
from pathlib import Path
from tkinter import Tk, ttk, filedialog, messagebox, StringVar, BooleanVar, IntVar
from ctypes import wintypes

import convert_core as core

# ----------------------------- Windows 拖放支持 -----------------------------
WM_DROPFILES = 0x0233
GWL_WNDPROC = -4

# GUI 中文选项 -> 核心逻辑内部 key 的映射
RESIZE_MODE_MAP = {"百分比": "percent", "最长边": "maxedge", "宽度": "width"}
RENAME_MODE_MAP = {"保留原名": "keep", "加前缀": "prefix", "加后缀": "suffix", "序号": "sequence"}


class FileDropper:
    """通过 ctypes 子类化 Tk 窗口，支持把文件拖入窗口（仅 Windows）。"""

    def __init__(self, hwnd, callback):
        self.callback = callback
        self.hwnd = hwnd
        shell32 = ctypes.windll.shell32
        user32 = ctypes.windll.user32

        self.DragQueryFileW = shell32.DragQueryFileW
        self.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
        self.DragQueryFileW.restype = wintypes.UINT
        self.DragFinish = shell32.DragFinish
        self.DragFinish.argtypes = [wintypes.HANDLE]
        self.DragAcceptFiles = shell32.DragAcceptFiles
        self.DragAcceptFiles.argtypes = [wintypes.HANDLE, wintypes.BOOL]

        self.SetWindowLongPtrW = user32.SetWindowLongPtrW
        self.SetWindowLongPtrW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
        self.SetWindowLongPtrW.restype = ctypes.c_void_p
        self.CallWindowProcW = user32.CallWindowProcW
        self.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HANDLE, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        self.CallWindowProcW.restype = ctypes.c_long

        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p)
        self.wndproc = WNDPROC(self._proc)

        self.DragAcceptFiles(self.hwnd, True)
        self.old_wndproc = self.SetWindowLongPtrW(self.hwnd, GWL_WNDPROC, self.wndproc)

    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_DROPFILES:
            hdrop = wintypes.HANDLE(wparam)
            count = self.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
            paths = []
            for i in range(count):
                length = self.DragQueryFileW(hdrop, i, None, 0)
                buf = ctypes.create_unicode_buffer(length + 1)
                self.DragQueryFileW(hdrop, i, buf, length + 1)
                paths.append(buf.value)
            self.DragFinish(hdrop)
            self.callback(paths)
            return 1
        return self.CallWindowProcW(self.old_wndproc, hwnd, msg, wparam, lparam)


# ----------------------------- 主界面 -----------------------------
class App:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("图片批量格式转换")
        self.root.geometry("820x680")

        self.files = []          # 待转换的绝对路径列表
        self.row_map = {}        # path -> tree iid
        self.converting = False

        self._build_ui()
        self._enable_drop()
        self._on_fmt_change()    # 初始化格式相关 UI 状态

    # ---------------- UI 构建 ----------------
    def _build_ui(self):
        # 顶部工具栏
        frm_top = ttk.Frame(self.root, padding=8)
        frm_top.pack(fill="x")
        ttk.Button(frm_top, text="添加文件", command=self.add_files).pack(side="left", padx=4)
        ttk.Button(frm_top, text="添加文件夹", command=self.add_folder).pack(side="left", padx=4)
        ttk.Button(frm_top, text="清空列表", command=self.clear_list).pack(side="left", padx=4)

        ttk.Label(frm_top, text="输出格式:").pack(side="left", padx=(18, 4))
        self.fmt_var = StringVar(value="jpg")
        self.fmt_combo = ttk.Combobox(
            frm_top, textvariable=self.fmt_var, values=core.OUTPUT_FORMATS,
            state="readonly", width=8,
        )
        self.fmt_combo.pack(side="left", padx=2)
        self.fmt_combo.bind("<<ComboboxSelected>>", self._on_fmt_change)

        ttk.Label(frm_top, text="质量:").pack(side="left", padx=(12, 4))
        self.quality = IntVar(value=100)
        self.scale = ttk.Scale(
            frm_top, from_=10, to=100, variable=self.quality,
            orient="horizontal", length=120,
            command=lambda e: self.q_label.configure(text=str(self.quality.get())),
        )
        self.scale.pack(side="left")
        self.q_label = ttk.Label(frm_top, text="100", width=5)
        self.q_label.pack(side="left", padx=2)

        # 文件列表
        frm_list = ttk.Frame(self.root, padding=(8, 4, 8, 0))
        frm_list.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frm_list, columns=("name", "size", "status"), show="headings")
        self.tree.heading("name", text="文件名")
        self.tree.heading("size", text="大小")
        self.tree.heading("status", text="状态")
        self.tree.column("name", width=400)
        self.tree.column("size", width=100)
        self.tree.column("status", width=200)
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(frm_list, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)

        ttk.Label(self.root, text="提示：可直接把图片或文件夹拖入窗口", foreground="gray").pack(pady=(0, 6))

        # 缩放 + 重命名设置
        frm_opt = ttk.LabelFrame(self.root, text="缩放与重命名", padding=(8, 4, 8, 8))
        frm_opt.pack(fill="x", padx=8, pady=(0, 4))

        # 缩放
        self.resize_on = BooleanVar(value=False)
        ttk.Checkbutton(frm_opt, text="启用缩放", variable=self.resize_on,
                        command=self._toggle_resize).pack(side="left", padx=4)
        ttk.Label(frm_opt, text="模式:").pack(side="left")
        self.resize_mode = StringVar(value="百分比")
        self.resize_combo = ttk.Combobox(
            frm_opt, textvariable=self.resize_mode,
            values=list(RESIZE_MODE_MAP.keys()), state="readonly", width=8,
        )
        self.resize_combo.pack(side="left", padx=2)
        self.resize_val = StringVar(value="100")
        self.resize_entry = ttk.Entry(frm_opt, textvariable=self.resize_val, width=8, state="disabled")
        self.resize_entry.pack(side="left", padx=2)
        ttk.Label(frm_opt, text="% 或 px（等比）").pack(side="left")

        ttk.Label(frm_opt, text="   重命名:").pack(side="left", padx=(12, 4))
        self.rename_mode = StringVar(value="保留原名")
        self.rename_combo = ttk.Combobox(
            frm_opt, textvariable=self.rename_mode,
            values=list(RENAME_MODE_MAP.keys()), state="readonly", width=10,
        )
        self.rename_combo.pack(side="left", padx=2)
        self.rename_text = StringVar(value="img")
        self.rename_entry = ttk.Entry(frm_opt, textvariable=self.rename_text, width=12)
        self.rename_entry.pack(side="left", padx=2)

        # 输出设置
        frm_out = ttk.Frame(self.root, padding=(8, 0, 8, 0))
        frm_out.pack(fill="x")
        self.use_custom_dir = BooleanVar(value=False)
        ttk.Checkbutton(frm_out, text="输出到指定文件夹", variable=self.use_custom_dir,
                        command=self._toggle_out_dir).pack(side="left", padx=4)
        self.out_dir_var = StringVar()
        self.out_entry = ttk.Entry(frm_out, textvariable=self.out_dir_var, state="disabled")
        self.out_entry.pack(side="left", padx=4, fill="x", expand=True)
        self.out_btn = ttk.Button(frm_out, text="选择目录", command=self.choose_out_dir, state="disabled")
        self.out_btn.pack(side="left", padx=4)

        # 额外选项：EXIF / 自动打开目录
        frm_tog = ttk.Frame(self.root, padding=(8, 2, 8, 2))
        frm_tog.pack(fill="x")
        self.keep_exif = BooleanVar(value=False)
        ttk.Checkbutton(frm_tog, text="保留EXIF元数据", variable=self.keep_exif).pack(side="left", padx=4)
        self.open_dir = BooleanVar(value=False)
        ttk.Checkbutton(frm_tog, text="转换完成后打开输出目录", variable=self.open_dir).pack(side="left", padx=4)

        # 底部：进度 + 转换按钮
        frm_bottom = ttk.Frame(self.root, padding=8)
        frm_bottom.pack(fill="x")
        self.progress = ttk.Progressbar(frm_bottom, orient="horizontal", mode="determinate")
        self.progress.pack(fill="x", pady=4)
        self.status = StringVar(value="就绪")
        ttk.Label(frm_bottom, textvariable=self.status).pack(anchor="w")
        self.convert_btn = ttk.Button(frm_bottom, text="一键转换为 JPG", command=self.start_convert)
        self.convert_btn.pack(pady=8)

    def _enable_drop(self):
        try:
            hwnd = self.root.winfo_id()
            self.dropper = FileDropper(hwnd, self.on_drop)
        except Exception as e:
            print("拖放不可用：", e)

    # ---------------- 格式 / 缩放 切换 ----------------
    def _on_fmt_change(self, *args):
        fmt = self.fmt_var.get()
        if fmt in core.LOSSY_FORMATS:
            self.scale.configure(state="normal")
            self.q_label.configure(state="normal", text=str(self.quality.get()))
        else:
            self.scale.configure(state="disabled")
            self.q_label.configure(text="无损")
        self.convert_btn.configure(text=f"一键转换为 {fmt.upper()}")

    def _toggle_resize(self):
        state = "normal" if self.resize_on.get() else "disabled"
        self.resize_combo.configure(state=state)
        self.resize_entry.configure(state=state)

    # ---------------- 列表操作 ----------------
    def _add_to_list(self, path: str) -> int:
        ap = os.path.abspath(path)
        if ap in self.files:
            return 0
        self.files.append(ap)
        size = core.human_size(os.path.getsize(ap))
        iid = self.tree.insert("", "end", values=(os.path.basename(ap), size, "等待"))
        self.row_map[ap] = iid
        return 1

    def _collect(self, paths):
        added = 0
        for ap in core.collect_images(paths):
            added += self._add_to_list(ap)
        if added:
            self.status.set(f"已添加 {added} 张，列表共 {len(self.files)} 张")

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择图片",
            filetypes=[("图片文件", "*.jpg;*.jpeg;*.png;*.bmp;*.gif;*.webp;*.tif;*.tiff;*.ico;*.ppm;*.pgm;*.tga;*.avif;*.heic;*.heif"),
                       ("所有文件", "*.*")],
        )
        if paths:
            self._collect(list(paths))

    def add_folder(self):
        d = filedialog.askdirectory(title="选择文件夹（将递归扫描图片）")
        if d:
            self._collect([d])

    def on_drop(self, paths):
        self._collect(list(paths))

    def clear_list(self):
        self.files.clear()
        self.row_map.clear()
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.status.set("已清空列表")

    # ---------------- 输出目录 ----------------
    def _toggle_out_dir(self):
        state = "normal" if self.use_custom_dir.get() else "disabled"
        self.out_entry.configure(state=state)
        self.out_btn.configure(state=state)

    def choose_out_dir(self):
        d = filedialog.askdirectory(title="选择输出文件夹")
        if d:
            self.out_dir_var.set(d)

    # ---------------- 转换 ----------------
    def start_convert(self):
        if self.converting:
            return
        if not self.files:
            messagebox.showinfo("提示", "请先添加图片（拖入或点击按钮）。")
            return
        fmt = self.fmt_var.get()
        quality = self.quality.get() if fmt in core.LOSSY_FORMATS else 100
        use_custom = self.use_custom_dir.get()
        out_dir = self.out_dir_var.get().strip()
        if use_custom and not out_dir:
            messagebox.showwarning("提示", "请先选择输出文件夹。")
            return

        # 记录转换完成后要打开的目录
        if use_custom and out_dir:
            self._open_target = out_dir
        else:
            self._open_target = os.path.dirname(self.files[0]) if self.files else None

        # 缩放参数
        resize = None
        if self.resize_on.get():
            rmode = RESIZE_MODE_MAP.get(self.resize_mode.get(), "percent")
            try:
                rval = float(self.resize_val.get())
            except ValueError:
                rval = 100
            resize = {"mode": rmode, "value": rval}

        # 重命名参数
        rn_mode = RENAME_MODE_MAP.get(self.rename_mode.get(), "keep")
        rn_text = self.rename_text.get().strip()

        self.converting = True
        self.convert_btn.configure(state="disabled")
        t = threading.Thread(
            target=self._worker,
            args=(fmt, quality, use_custom, out_dir, resize, rn_mode, rn_text),
            daemon=True,
        )
        t.start()

    def _worker(self, fmt, quality, use_custom, out_dir, resize, rn_mode, rn_text):
        total = len(self.files)
        ok = fail = 0
        seq = 0
        self.root.after(0, lambda: self.progress.configure(maximum=total, value=0))
        for idx, src in enumerate(self.files, 1):
            try:
                rename = None
                if rn_mode != "keep":
                    seq += 1
                    rename = {"mode": rn_mode, "text": rn_text or "img", "index": seq}
                dst = core.plan_dst(src, use_custom, out_dir, fmt, rename)
                result = core.convert_one(
                    src, str(dst), quality, fmt, resize,
                    keep_exif=self.keep_exif.get(),
                )
                stat = "完成 → " + os.path.basename(result)
                ok += 1
            except Exception as e:
                stat = "失败: " + str(e)[:50]
                fail += 1
            self.root.after(0, self._apply_status, src, stat, idx, total)
        self.root.after(0, self._finish, ok, fail)

    def _apply_status(self, src, stat, idx, total):
        iid = self.row_map.get(src)
        if iid:
            vals = list(self.tree.item(iid, "values"))
            vals[2] = stat
            self.tree.item(iid, values=vals)
        self.progress.configure(value=idx)
        self.status.set(f"转换中 {idx}/{total}")

    def _finish(self, ok, fail):
        self.converting = False
        self.convert_btn.configure(state="normal")
        self.status.set(f"完成：成功 {ok} 张，失败 {fail} 张")
        if getattr(self, "open_dir", None) and self.open_dir.get() and getattr(self, "_open_target", None):
            self._open_dir(self._open_target)
        messagebox.showinfo("完成", f"转换完成\n成功 {ok} 张，失败 {fail} 张")

    @staticmethod
    def _open_dir(path):
        try:
            os.startfile(path)
        except Exception:
            pass


# ----------------------------- 启动 -----------------------------
def main_gui():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # 高 DPI 清晰显示
    except Exception:
        pass
    root = Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main_gui()
