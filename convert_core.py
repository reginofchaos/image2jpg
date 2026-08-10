# -*- coding: utf-8 -*-
"""
convert_core.py — 图片批量转换核心逻辑（不依赖 GUI / tkinter）。
仅依赖 Pillow（+ 可选 pillow-avif-plugin 以支持 AVIF 输出）。

支持把图片批量转换为任意主流格式：jpg / png / webp / bmp / tiff / gif / ico / avif / heic。
"""
import os
from pathlib import Path
from PIL import Image, ImageOps, ImageFile

# 允许读取被截断的图片（避免偶发损坏导致整批失败）
ImageFile.LOAD_TRUNCATED_IMAGES = True

# 可选 AVIF 编解码（pip install pillow-avif-plugin 后生效）
try:
    import pillow_avif  # 注册 AVIF 到 Pillow
except ImportError:
    pillow_avif = None

# 可选 HEIC/HEIF 编解码（pip install pillow-heif 后生效，同时注册 HEIF 与 AVIF）
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HAVE_HEIF = True
except ImportError:
    HAVE_HEIF = False

# 支持的输入格式（Pillow 能解码的主流位图）
SUPPORTED_EXT = {
    ".jpg", ".jpeg", ".jpe", ".png", ".bmp", ".gif", ".webp",
    ".tif", ".tiff", ".ico", ".ppm", ".pgm", ".pbm", ".pnm",
    ".tga", ".dds", ".pcx", ".sgi", ".xbm", ".eps", ".im", ".heic", ".heif",
}

# 可选输出格式（顺序即下拉顺序，第一个为默认）
OUTPUT_FORMATS = ["jpg", "png", "webp", "bmp", "tiff", "gif", "ico", "avif", "heic"]

# 有损格式：质量滑块对其生效
LOSSY_FORMATS = {"jpg", "webp", "avif", "heic"}

# 支持透明通道的目标格式（gif/bmp 不支持透明，自动填白底）
ALPHA_FORMATS = {"png", "webp", "tiff", "ico", "avif", "heic"}


def human_size(num: int) -> str:
    """把字节数转成人类可读字符串。"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def make_unique(src: str, dst: str) -> Path:
    """
    计算最终输出路径：
    - 若输出路径与源文件本身是同一文件（例如原图就是目标格式且输出到同目录），
      则改名为 `<原名>_converted.<ext>`，避免覆盖原图。
    - 若目标已存在但不是源文件本身（通常是上次转换的残留产物），直接覆盖。
    """
    d = Path(dst)
    try:
        if Path(src).resolve() == d.resolve():
            d = d.with_name(d.stem + "_converted" + d.suffix)
    except OSError:
        pass
    return d


def plan_dst(src: str, use_custom_dir: bool, out_dir: str, fmt: str, rename=None) -> Path:
    """
    根据设置计算输出路径（不含去重处理）。
    rename: None 或 {"mode": "keep"|"prefix"|"suffix"|"sequence",
                     "text": str, "index": int}
    """
    p = Path(src)
    ext = (fmt or "jpg").lower()
    stem = p.stem
    if rename:
        mode = rename.get("mode", "keep")
        text = (rename.get("text") or "").strip()
        index = rename.get("index", 0)
        if mode == "prefix" and text:
            stem = text + p.stem
        elif mode == "suffix" and text:
            stem = p.stem + text
        elif mode == "sequence":
            stem = f"{text}{index:03d}"
    filename = stem + "." + ext
    if use_custom_dir and out_dir and out_dir.strip():
        base = Path(out_dir.strip())
        base.mkdir(parents=True, exist_ok=True)
        return base / filename
    return p.with_name(filename)


def _apply_resize(im: Image.Image, resize) -> Image.Image:
    """按设置等比缩放。resize=None 或 {"mode":"none"} 时原样返回。"""
    if not resize or resize.get("mode", "none") == "none":
        return im
    w, h = im.size
    mode = resize.get("mode")
    try:
        v = float(resize.get("value", 100))
    except (TypeError, ValueError):
        return im
    if mode == "percent":
        nw, nh = max(1, round(w * v / 100.0)), max(1, round(h * v / 100.0))
    elif mode == "maxedge":
        if max(w, h) <= 0:
            return im
        scale = v / max(w, h)
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    elif mode == "width":
        if w <= 0:
            return im
        scale = v / w
        nw, nh = max(1, round(v)), max(1, round(h * scale))
    else:
        return im
    if (nw, nh) != (w, h):
        im = im.resize((nw, nh), Image.LANCZOS)
    return im


def convert_one(src: str, dst: str, quality: int = 100, fmt: str = "jpg",
                resize=None, keep_exif: bool = False) -> str:
    """
    把单张图片转换为指定格式并返回最终保存路径。
    - keep_exif=False（默认）：自动按 EXIF 方向校正旋转，且不保留 EXIF 元数据
    - keep_exif=True：原样保留像素与 EXIF（含方向标签），并把 EXIF 写入支持的目标格式
    - 动图（GIF/WEBP）只取第一帧
    - 等比缩放（resize 参数）
    - 目标格式不支持透明时（jpg/bmp/gif），透明区域以白色背景合成
    - quality 仅对有损格式（jpg/webp/avif/heic）生效；无损格式忽略质量参数
    """
    fmt = (fmt or "jpg").lower()
    if fmt == "avif" and pillow_avif is None:
        raise RuntimeError("未安装 pillow-avif-plugin，无法输出 AVIF（请运行 pip install pillow-avif-plugin）")
    if fmt == "heic" and not HAVE_HEIF:
        raise RuntimeError("未安装 pillow-heif，无法输出 HEIC（请运行 pip install pillow-heif）")
    with Image.open(src) as im:
        exif_data = im.info.get("exif") if keep_exif else None
        if keep_exif:
            # 保留 EXIF：不自动旋转，原样保留方向标签（查看器会据此自动转正）
            im = im.copy()
        else:
            im = ImageOps.exif_transpose(im)  # 修正手机照片的旋转
        # 动图取第一帧
        try:
            if getattr(im, "is_animated", False):
                im.seek(0)
        except Exception:
            pass

        # 缩放（在透明处理前，保持宽高比）
        im = _apply_resize(im, resize)

        has_alpha = im.mode in ("RGBA", "LA") or (
            im.mode == "P" and "transparency" in im.info
        )

        # 目标不支持透明 → 填白底转 RGB
        if has_alpha and fmt not in ALPHA_FORMATS:
            bg = Image.new("RGB", im.size, (255, 255, 255))
            if im.mode in ("RGBA", "LA"):
                bg.paste(im, mask=im.split()[-1])  # 用 alpha 通道做蒙版
                im = bg
            else:  # P + 透明度
                im = im.convert("RGBA")
                bg.paste(im, mask=im.split()[-1])
                im = bg
        # 目标支持透明 → 保留透明通道
        elif has_alpha and fmt in ALPHA_FORMATS:
            if im.mode in ("P", "LA"):
                im = im.convert("RGBA")
        elif im.mode != "RGB":
            im = im.convert("RGB")

        out = make_unique(src, dst)

        if fmt == "jpg":
            save_fmt = "JPEG"
        elif fmt == "heic":
            save_fmt = "HEIF"  # pillow-heif 注册的是 HEIF（扩展名仍用 .heic）
        else:
            save_fmt = fmt.upper()
        kwargs = {}
        # 是否把原始 EXIF 写回（仅 keep_exif 模式 + 目标格式支持 EXIF）
        EXIF_FMTS = {"jpg", "webp", "tiff", "heic"}
        embed_exif = bool(keep_exif and exif_data and fmt in EXIF_FMTS)

        if fmt in LOSSY_FORMATS:
            kwargs["quality"] = quality
            if fmt == "jpg":
                # 高质量时关闭色度子采样，避免色彩模糊
                kwargs["subsampling"] = "4:4:4" if quality >= 95 else "4:2:2"
                kwargs["optimize"] = True
                if embed_exif:
                    kwargs["exif"] = exif_data
            elif fmt == "webp":
                kwargs["method"] = 6
                if embed_exif:
                    kwargs["exif"] = exif_data
            # avif 仅 quality 生效（已由上面统一设置）
        elif fmt == "png":
            kwargs["optimize"] = True
        elif fmt in ("tiff", "heic"):
            if embed_exif:
                kwargs["exif"] = exif_data
        # bmp / gif / ico：不支持 EXIF，忽略

        try:
            im.save(str(out), save_fmt, **kwargs)
        except Exception:
            # EXIF 数据异常时降级为不带 EXIF 重试，避免单张失败拖累整批
            if "exif" in kwargs:
                kwargs.pop("exif", None)
                im.save(str(out), save_fmt, **kwargs)
            else:
                raise
    return str(out)


def collect_images(paths, recursive=True) -> list:
    """
    从给定的文件/文件夹路径列表中收集图片文件，返回去重后的绝对路径列表。
    文件夹默认递归扫描子目录；传 recursive=False 仅扫描顶层。
    """
    result = []
    seen = set()
    for p in paths:
        p = Path(p)
        if p.is_dir():
            iterator = p.rglob("*") if recursive else p.glob("*")
            for f in sorted(iterator):
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXT:
                    ap = os.path.abspath(f)
                    if ap not in seen:
                        seen.add(ap)
                        result.append(ap)
        elif p.is_file() and p.suffix.lower() in SUPPORTED_EXT:
            ap = os.path.abspath(p)
            if ap not in seen:
                seen.add(ap)
                result.append(ap)
    return result
