# -*- coding: utf-8 -*-
"""
image2jpg_cli.py — 图片批量转换命令行工具（无 GUI，可本地离线运行）。

设计目标：让 PowerShell、脚本、CI 等场景能直接以命令行完成图片格式转换，
无需联网。仅依赖 Pillow（及可选的 pillow-avif-plugin / pillow-heif）。

示例：
  python image2jpg_cli.py photo.heic -f jpg -q 90
  python image2jpg_cli.py *.png -f webp --output out/
  python image2jpg_cli.py dir/ -f jpg --resize maxedge:1920 --rename prefix:conv_
  python image2jpg_cli.py a.png b.webp -f avif --quality 100 --overwrite
  python image2jpg_cli.py dir/ -f png --json > result.json
"""
import argparse
import glob
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import convert_core as core


# ----------------------------- 参数解析辅助 -----------------------------
def parse_resize(s):
    """'percent:50' / 'maxedge:1920' / 'width:800' -> dict；'none' -> None"""
    if not s or s.lower() == "none":
        return None
    if ":" in s:
        mode, val = s.split(":", 1)
    else:
        mode, val = s, "100"
    mode = mode.lower()
    if mode not in ("percent", "maxedge", "width"):
        raise argparse.ArgumentTypeError(f"未知缩放模式: {mode}（应为 percent/maxedge/width）")
    try:
        value = float(val)
    except ValueError:
        raise argparse.ArgumentTypeError(f"缩放数值无效: {val}")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"缩放数值必须 > 0: {value}")
    return {"mode": mode, "value": value}


def parse_rename(s):
    """'prefix:IMG_' / 'suffix:_x' / 'sequence:page' / 'keep' -> dict 或 None"""
    if not s or s.lower() == "keep":
        return None
    if ":" in s:
        mode, text = s.split(":", 1)
    else:
        mode, text = s, ""
    mode = mode.lower()
    if mode not in ("prefix", "suffix", "sequence"):
        raise argparse.ArgumentTypeError(f"未知重命名模式: {mode}（应为 prefix/suffix/sequence）")
    return {"mode": mode, "text": text}


def build_parser():
    p = argparse.ArgumentParser(
        prog="image2jpg_cli",
        description="本地批量图片格式转换（支持 jpg/png/webp/bmp/tiff/gif/ico/avif/heic，免联网）。",
    )
    p.add_argument("inputs", nargs="*", help="图片文件、目录或通配符（可多个；--list-formats 时可选）")
    p.add_argument("-f", "--format", default="jpg", choices=core.OUTPUT_FORMATS,
                   help="输出格式（默认 jpg）")
    p.add_argument("-q", "--quality", type=int, default=100,
                   help="有损格式质量 1-100（默认 100；jpg/webp/avif/heic 生效）")
    p.add_argument("-o", "--output", default=None,
                   help="输出目录（默认源文件同级目录）")
    p.add_argument("-r", "--recursive", action="store_true", default=True,
                   help="递归扫描目录（默认开启）")
    p.add_argument("--no-recursive", dest="recursive", action="store_false",
                   help="仅扫描目录顶层，不进入子目录")
    p.add_argument("--resize", type=parse_resize, default=None, metavar="MODE:VALUE",
                   help="等比缩放：percent:50 | maxedge:1920 | width:800")
    p.add_argument("--rename", type=parse_rename, default=None, metavar="MODE[:TEXT]",
                   help="重命名：prefix:IMG_ | suffix:_x | sequence:page")
    p.add_argument("--overwrite", action="store_true",
                   help="覆盖已存在的输出文件（否则自动加 _converted 或序号）")
    p.add_argument("--keep-exif", action="store_true",
                   help="保留源图 EXIF 元数据（JPG/WEBP/TIFF/HEIC 输出生效；开启后不自动旋转方向）")
    p.add_argument("--open", action="store_true",
                   help="转换完成后在资源管理器打开输出目录")
    p.add_argument("--workers", type=int, default=1,
                   help="并行转换线程数（默认 1；>1 可加速大批量）")
    p.add_argument("--dry-run", action="store_true",
                   help="只列出将要执行的转换，不实际生成文件")
    p.add_argument("--quiet", action="store_true", help="静默：仅输出错误与最终汇总")
    p.add_argument("--json", action="store_true", help="以 JSON 形式输出结果汇总")
    p.add_argument("--target-size", type=int, nargs=2, metavar=("MIN", "MAX"), default=None,
                   help="自动压缩到目标大小范围 [MIN, MAX] KB：固定质量下自动缩放，使输出体积落入该范围；原图已更小则不放大")
    p.add_argument("--list-formats", action="store_true",
                   help="打印支持的输入输出格式后退出")
    p.add_argument("--version", action="version", version="image2jpg-cli 1.0")
    return p


def resolve_inputs(patterns, recursive):
    """把用户输入（文件/目录/通配符）展开为图片绝对路径列表。"""
    expanded = []
    for pat in patterns:
        if os.path.exists(pat):
            expanded.append(pat)
        elif any(c in pat for c in "*?"):
            # shell 未展开的通配符，手动 glob（顶层，不递归子目录）
            expanded.extend(glob.glob(pat))
        else:
            # 路径不存在（可能拼写错误），仍传入让后续报错更明确
            expanded.append(pat)
    return core.collect_images(expanded, recursive=recursive)


def run_cli(argv=None):
    args = build_parser().parse_args(argv)

    if args.list_formats:
        print("输入格式:", " ".join(sorted(e.lstrip('.') for e in core.SUPPORTED_EXT)))
        print("输出格式:", " ".join(core.OUTPUT_FORMATS))
        return 0

    if not args.inputs:
        sys.stderr.write("错误：至少需要指定一个输入文件/目录（或用 --list-formats 查看支持的格式）。\n")
        return 2

    # 参数规范化
    quality = max(1, min(100, args.quality))
    workers = max(1, args.workers)
    verbose = (not args.quiet) and (not args.json)  # json 模式下只输出纯 JSON

    files = resolve_inputs(args.inputs, args.recursive)
    if not files:
        sys.stderr.write("错误：未找到任何图片文件。请检查输入路径/通配符。\n")
        return 2

    use_custom = bool(args.output)

    # 预规划输出路径（sequence 重命名需要跨文件递增序号，故先主线程分配）
    plan = []
    seq = 0
    for src in files:
        rn = None
        if args.rename:
            if args.rename["mode"] == "sequence":
                seq += 1
                rn = {"mode": "sequence", "text": args.rename["text"] or "img", "index": seq}
            else:
                rn = args.rename
        try:
            dst = str(core.plan_dst(src, use_custom, args.output or "", args.format, rn))
        except Exception as e:
            sys.stderr.write(f"规划失败 {src}: {e}\n")
            return 2
        plan.append((src, dst))

    if args.dry_run:
        for src, dst in plan:
            print(f"[plan] {src} -> {dst}")
        if not args.quiet:
            print(f"\n共 {len(plan)} 项（dry-run，未实际转换）。")
        return 0

    # 覆盖模式：删除已存在的输出（排除与源文件同路径的情况）
    if args.overwrite:
        for _, dst in plan:
            try:
                if os.path.exists(dst) and not os.path.samefile(dst, _):
                    os.remove(dst)
            except OSError:
                pass

    results = []

    def do(item):
        src, dst = item
        try:
            if args.target_size:
                mn, mx = args.target_size
                out = core.convert_to_target_size(src, dst, quality, args.format, mn, mx,
                                                  keep_exif=args.keep_exif)
            else:
                out = core.convert_one(src, dst, quality, args.format, args.resize,
                                       keep_exif=args.keep_exif)
            return {"src": src, "dst": out, "ok": True, "error": None}
        except Exception as e:
            return {"src": src, "dst": dst, "ok": False, "error": str(e)}

    if workers <= 1:
        for item in plan:
            r = do(item)
            results.append(r)
            if verbose:
                if r["ok"]:
                    print(f"[OK]   {os.path.basename(r['src'])} -> {os.path.basename(r['dst'])}")
                else:
                    print(f"[FAIL] {os.path.basename(r['src'])}: {r['error']}")
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(do, it) for it in plan]
            for f in as_completed(futs):
                r = f.result()
                results.append(r)
                if verbose:
                    if r["ok"]:
                        print(f"[OK]   {os.path.basename(r['src'])} -> {os.path.basename(r['dst'])}")
                    else:
                        print(f"[FAIL] {os.path.basename(r['src'])}: {r['error']}")

    ok = sum(1 for r in results if r["ok"])
    fail = sum(1 for r in results if not r["ok"])
    total = len(results)

    if args.json:
        print(json.dumps({"total": total, "ok": ok, "fail": fail, "results": results},
                         ensure_ascii=False, indent=2))
    elif not args.quiet:
        print(f"\n完成：成功 {ok} 张，失败 {fail} 张，共 {total} 张。")

    if args.open and results:
        first_ok = next((r for r in results if r["ok"]), None)
        if first_ok:
            d = args.output if args.output else os.path.dirname(first_ok["src"])
            try:
                os.startfile(d)
            except Exception:
                pass

    return 0 if fail == 0 else 2


def main():
    sys.exit(run_cli())


if __name__ == "__main__":
    main()
