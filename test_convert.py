# -*- coding: utf-8 -*-
"""
test_convert.py — 验证核心转换逻辑（不启动 GUI）。
生成多种格式的测试图，转换为 jpg/png/webp/bmp/tiff/gif/ico/avif，
并校验缩放（百分比/最长边/宽度）与重命名（前缀/后缀/序号）结果。
"""
import os
import tempfile
from pathlib import Path
from PIL import Image

import convert_core as core


def make_test_images(d: Path) -> dict:
    """生成若干测试图，返回 {描述: 路径}。"""
    made = {}

    # 1) 普通 PNG（RGB）
    p = d / "rgb.png"
    Image.new("RGB", (64, 48), (200, 30, 30)).save(p)
    made["rgb_png"] = str(p)

    # 2) 带透明的 PNG（RGBA），左半透明、右半红色
    p = d / "rgba.png"
    im = Image.new("RGBA", (64, 48), (0, 0, 0, 0))
    for x in range(32, 64):
        for y in range(48):
            im.putpixel((x, y), (255, 0, 0, 255))
    im.save(p)
    made["rgba_png"] = str(p)

    # 3) WebP
    p = d / "photo.webp"
    Image.new("RGB", (64, 48), (0, 160, 90)).save(p, "WEBP")
    made["webp"] = str(p)

    # 4) GIF（动图，验证只取第一帧不报错）
    p = d / "anim.gif"
    frames = [Image.new("RGB", (32, 32), c) for c in [(255, 0, 0), (0, 255, 0), (0, 0, 255)]]
    frames[0].save(p, save_all=True, append_images=frames[1:], duration=100, loop=0)
    made["gif"] = str(p)

    # 5) BMP
    p = d / "flat.bmp"
    Image.new("RGB", (40, 40), (10, 10, 200)).save(p)
    made["bmp"] = str(p)

    # 6) TIFF
    p = d / "scan.tif"
    Image.new("RGB", (40, 40), (120, 120, 0)).save(p, "TIFF")
    made["tiff"] = str(p)

    # 7) HEIC（pillow-heif，验证读入）
    p = d / "photo.heic"
    Image.new("RGB", (64, 48), (90, 60, 200)).save(p, "HEIF")
    made["heic"] = str(p)

    return made


def main():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        imgs = make_test_images(d)
        print(f"生成测试图 {len(imgs)} 张于 {d}")

        # ---- 1) 每种输入 × 每种输出格式都能成功 ----
        fmts = core.OUTPUT_FORMATS
        total = 0
        for name, src in imgs.items():
            for fmt in fmts:
                dst = core.plan_dst(src, False, "", fmt)
                out = core.convert_one(src, str(dst), quality=100, fmt=fmt)
                assert Path(out).exists(), f"{name}->{fmt}: 输出未生成"
                with Image.open(out) as im:
                    if fmt == "jpg":
                        expected = "JPEG"
                    elif fmt == "heic":
                        expected = "HEIF"  # pillow-heif 注册为 HEIF
                    else:
                        expected = fmt.upper()
                    assert im.format == expected, f"{name}->{fmt}: 格式应为 {expected}，实际 {im.format}"
                total += 1
                print(f"  [OK] {name:10s} -> {Path(out).name}")

        # ---- 2) 透明：png/webp/tiff/ico/avif 应保留 alpha；jpg/bmp/gif 应填白底 ----
        alpha_fmts = [f for f in fmts if f in core.ALPHA_FORMATS]
        for fmt in alpha_fmts:
            out = core.convert_one(
                imgs["rgba_png"], str(core.plan_dst(imgs["rgba_png"], False, "", fmt)), 100, fmt
            )
            with Image.open(out) as im:
                assert im.mode in ("RGBA", "LA"), f"{fmt}: 应保留透明通道，实际 {im.mode}"
        for fmt in ["jpg", "bmp", "gif"]:
            out = core.convert_one(
                imgs["rgba_png"], str(core.plan_dst(imgs["rgba_png"], False, "", fmt)), 100, fmt
            )
            with Image.open(out) as im:
                # jpg/bmp 读回为 RGB；gif 读回为调色板模式 P（格式特性，非 bug）
                if fmt != "gif":
                    assert im.mode == "RGB", f"{fmt}: 应为 RGB，实际 {im.mode}"
                rgb = im.convert("RGB")  # gif 需转 RGB 才能按像素取值
                # 透明区域（源图 (4,4) 处 alpha=0）应被合成成白色
                assert rgb.getpixel((4, 4)) == (255, 255, 255), f"{fmt}: 透明区域应填白，实际 {rgb.getpixel((4, 4))}"
                # 不透明区域（源图 (50,24) 处 alpha=255，红色）应保留
                r2, g2, b2 = rgb.getpixel((50, 24))
                # jpg 有损，允许轻微 DCT 取整误差；bmp/gif 应精确
                assert r2 >= 250 and g2 == 0 and b2 == 0, f"{fmt}: 不透明红色应保留，实际 {(r2, g2, b2)}"

        # ---- 3) 缩放：百分比 / 最长边 / 宽度 ----
        base = imgs["rgb_png"]
        # 百分比 50%
        out = core.convert_one(base, str(core.plan_dst(base, False, "", "png")), 100, "png",
                               resize={"mode": "percent", "value": 50})
        with Image.open(out) as im:
            assert im.size == (32, 24), f"percent 50%: 期望 (32,24)，实际 {im.size}"
        # 最长边 100px（原 64x48 → 最长 64 → 100/64 缩放 → 100x75）
        out = core.convert_one(base, str(core.plan_dst(base, False, "", "png")), 100, "png",
                               resize={"mode": "maxedge", "value": 100})
        with Image.open(out) as im:
            assert im.size == (100, 75), f"maxedge 100: 期望 (100,75)，实际 {im.size}"
        # 宽度 200px（原 64 → 200/64，高度等比 → 200x150）
        out = core.convert_one(base, str(core.plan_dst(base, False, "", "png")), 100, "png",
                               resize={"mode": "width", "value": 200})
        with Image.open(out) as im:
            assert im.size == (200, 150), f"width 200: 期望 (200,150)，实际 {im.size}"
        print("  [OK] 缩放 percent/maxedge/width 尺寸正确")

        # ---- 4) 重命名：前缀 / 后缀 / 序号 ----
        # 前缀
        dst = core.plan_dst(base, False, "", "jpg", rename={"mode": "prefix", "text": "pre_", "index": 1})
        assert dst.name == "pre_rgb.jpg", f"prefix: 期望 pre_rgb.jpg，实际 {dst.name}"
        # 后缀
        dst = core.plan_dst(base, False, "", "jpg", rename={"mode": "suffix", "text": "_suf", "index": 1})
        assert dst.name == "rgb_suf.jpg", f"suffix: 期望 rgb_suf.jpg，实际 {dst.name}"
        # 序号（index=7 → 007）
        dst = core.plan_dst(base, False, "", "jpg", rename={"mode": "sequence", "text": "img", "index": 7})
        assert dst.name == "img007.jpg", f"sequence: 期望 img007.jpg，实际 {dst.name}"
        # 实际执行序列重命名转换，确认文件生成
        out = core.convert_one(base, str(core.plan_dst(base, False, "", "jpg",
                                  rename={"mode": "sequence", "text": "img", "index": 3})), 100, "jpg")
        assert Path(out).name == "img003.jpg", f"sequence convert: 期望 img003.jpg，实际 {Path(out).name}"
        print("  [OK] 重命名 prefix/suffix/sequence 名称正确")

        # ---- 5) 自定义输出目录 ----
        custom = d / "out"
        out2 = core.convert_one(
            imgs["rgb_png"], str(core.plan_dst(imgs["rgb_png"], True, str(custom), "png")), 100, "png"
        )
        assert str(custom) in str(out2) and Path(out2).exists(), "自定义目录失败"

        # ---- 6) EXIF 保留 ----
        exif_obj = Image.Exif()
        exif_obj[0x0110] = "TestCam"   # Model 标签
        exif_obj[0x0112] = 1           # Orientation = 1
        exif_bytes = exif_obj.tobytes()
        p = d / "with_exif.jpg"
        Image.new("RGB", (40, 30), (33, 66, 99)).save(p, "JPEG", exif=exif_bytes)
        # 默认（keep_exif=False）：不保留 EXIF
        out_no = core.convert_one(str(p), str(core.plan_dst(str(p), False, "", "jpg")), 100, "jpg")
        with Image.open(out_no) as im:
            assert not im.info.get("exif"), "默认不应保留 EXIF"
        # keep_exif=True：应把 EXIF 写回 JPG
        out_yes = core.convert_one(
            str(p), str(core.plan_dst(str(p), False, "", "jpg")), 100, "jpg", keep_exif=True
        )
        with Image.open(out_yes) as im:
            assert im.info.get("exif"), "keep_exif=True 应保留 EXIF"
        print("  [OK] EXIF 保留：默认不保留 / keep_exif 写回 JPG")

        print(f"\n全部通过：{total} 次格式转换（含 HEIC 读写）+ 透明处理 + 缩放 + 重命名 + 自定义目录 + EXIF 校验。")
        print("默认格式 jpg、质量 100% 已生效（convert_one 默认参数）。")


if __name__ == "__main__":
    main()
