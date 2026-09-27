# -*- coding: utf-8 -*-
"""从官方 v0.1.11 的 jev-chat-windows.exe 生成补丁包（写进 patch/）。

补的是什么：把 5 个模块的新字节码塞进 exe 里的 PYZ，再把新的 main 脚本塞进 CArchive。
输出的是给 patch/apply.ps1 用的几段二进制 + manifest.json，不是重新打包一个 exe 出来。

用法（必须 Python 3.11——exe 里的字节码是 3.11 的，别的版本编译出来的塞不进去）：
    python tools/build_patch.py --exe "C:\\path\\jev-chat-windows.exe"
    python tools/build_patch.py --exe ... --patched-exe dist\\patched.exe   # 顺手导出打好的 exe

PYZ / CArchive 的格式照 PyInstaller 自己的 PyInstaller/archive/readers.py 和 writers.py 来。
"""
import argparse
import hashlib
import json
import marshal
import os
import struct
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")   # 改动过的源文件，路径跟上游一致

# 补丁覆盖的模块：PYZ 里的名字 → src/ 下的源码路径
MODULES = {
    "core.providers": "core/providers.py",
    "core.jev_client": "core/jev_client.py",
    "core.engine": "core/engine.py",
    "app.settings": "app/settings.py",
    "app.overlay": "app/overlay.py",
}
MAIN_SOURCE = "main.py"
MAIN_ENTRY = "main"          # CArchive 里的条目名没有 .py
PYZ_NAME = "PYZ.pyz"

COOKIE_MAGIC = b"MEI\014\013\012\013\016"
COOKIE_FORMAT = "!8sIIII64s"
COOKIE_LENGTH = struct.calcsize(COOKIE_FORMAT)
TOC_FORMAT = "!IIIIBc"
TOC_LENGTH = struct.calcsize(TOC_FORMAT)
PYZ_TOC_ENTRY_TYPE = {0: "module", 1: "package", 2: "data", 3: "namespace"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_carchive(raw: bytes):
    """exe → (archive_start, entries, pylib_name)，entries 按 TOC 顺序。"""
    cookie_start = raw.rfind(COOKIE_MAGIC)
    if cookie_start < 0:
        raise SystemExit("no PyInstaller CArchive cookie in this exe")
    magic, archive_length, toc_offset, toc_length, pyvers, pylib = struct.unpack(
        COOKIE_FORMAT, raw[cookie_start:cookie_start + COOKIE_LENGTH])
    if pyvers != 311:
        raise SystemExit(f"this exe was built with Python {pyvers}; only 3.11 builds are supported")
    if cookie_start + COOKIE_LENGTH != len(raw):
        raise SystemExit("unexpected trailing bytes after the archive cookie")
    start = cookie_start + COOKIE_LENGTH - archive_length
    blob = raw[start + toc_offset:start + toc_offset + toc_length]
    entries, pos = [], 0
    while pos < len(blob):
        entry_length, off, data_length, raw_length, compressed, typecode = \
            struct.unpack(TOC_FORMAT, blob[pos:pos + TOC_LENGTH])
        pos += TOC_LENGTH
        name_length = entry_length - TOC_LENGTH
        name = blob[pos:pos + name_length].rstrip(b"\0").decode("utf-8")
        pos += name_length
        entries.append({"name": name, "offset": off, "data_length": data_length,
                        "raw_length": raw_length, "compressed": int(compressed),
                        "typecode": typecode.decode("ascii")})
    return start, entries, pylib.rstrip(b"\0").decode("ascii")


def serialize_carchive_toc(entries) -> bytes:
    """跟 PyInstaller 的 CArchiveWriter._serialize_toc 一样：名字补零到 16 字节对齐。"""
    out = []
    for e in entries:
        name = e["name"].encode("utf-8")
        name_length = len(name) + 1
        entry_length = TOC_LENGTH + name_length
        if entry_length % 16:
            name_length += 16 - (entry_length % 16)
        out.append(struct.pack(
            TOC_FORMAT + f"{name_length}s", TOC_LENGTH + name_length, e["offset"],
            e["data_length"], e["raw_length"], e["compressed"],
            e["typecode"].encode("ascii"), name))
    return b"".join(out)


def read_pyz(raw: bytes, archive_start: int, entry: dict):
    """CArchive 里的 PYZ 条目 → (pyz 字节, 数据起点, TOC 偏移, TOC) 。"""
    if entry["compressed"]:
        raise SystemExit("PYZ entry is stored compressed; unsupported layout")
    start = archive_start + entry["offset"]
    blob = raw[start:start + entry["data_length"]]
    if blob[:4] != b"PYZ\0":
        raise SystemExit("PYZ entry does not start with PYZ magic")
    toc_offset = struct.unpack("!i", blob[8:12])[0]
    if not 12 <= toc_offset <= len(blob):
        raise SystemExit(f"PYZ TOC offset out of range: {toc_offset} len={len(blob)}")
    toc = marshal.loads(blob[toc_offset:])
    data_start = min(off for _, (_, off, length) in toc if length)
    return blob, data_start, toc_offset, toc


def compile_source(rel_path: str, co_filename: str) -> bytes:
    """源码 → marshal 后的代码对象（跟 PyInstaller 存进归档的形状一致）。"""
    with open(os.path.join(SRC, rel_path), encoding="utf-8") as f:
        source = f.read()
    code = compile(source, co_filename, "exec", dont_inherit=True, optimize=0)
    return marshal.dumps(code)


def build(exe_path: str, patched_exe: str | None):
    if sys.version_info[:2] != (3, 11):
        raise SystemExit("run this with Python 3.11 (the exe ships 3.11 bytecode)")
    raw = open(exe_path, "rb").read()
    archive_start, entries, pylib_name = read_carchive(raw)
    by_name = {e["name"]: e for e in entries}
    for needed in (MAIN_ENTRY, PYZ_NAME):
        if needed not in by_name:
            raise SystemExit(f"CArchive has no {needed!r} entry")
    main_entry, pyz_entry = by_name[MAIN_ENTRY], by_name[PYZ_NAME]
    if entries[-1] is not pyz_entry:
        raise SystemExit("PYZ is not the last CArchive entry; unsupported layout")
    if main_entry["typecode"] != "s" or pyz_entry["typecode"] != "z":
        raise SystemExit("main / PYZ entries have unexpected type codes")

    pyz_blob, pyz_data_start, pyz_toc_offset, pyz_toc = read_pyz(raw, archive_start, pyz_entry)
    known = {name for name, _ in pyz_toc}
    missing = sorted(set(MODULES) - known)
    if missing:
        raise SystemExit(f"modules missing from PYZ: {missing}")

    # PYZ：老的数据区原样留着（老的 5 份字节码变成死数据），新字节码追加在 TOC 前面
    new_code = {name: compile_source(rel, os.path.join(*name.split(".")) + ".py")
                for name, rel in MODULES.items()}
    appended, new_toc, cursor = [], [], pyz_toc_offset
    for name, (typecode, offset, length) in pyz_toc:
        if name in new_code:
            blob = zlib.compress(new_code[name], 6)
            appended.append(blob)
            new_toc.append((name, (typecode, cursor, len(blob))))
            cursor += len(blob)
        else:
            new_toc.append((name, (typecode, offset, length)))
    # 新的 TOC 挪到追加区后面，头里的 toc_offset 要跟着改
    new_toc_offset = pyz_toc_offset + sum(len(b) for b in appended)
    new_toc = marshal.dumps(new_toc)
    new_pyz = (pyz_blob[:8] + struct.pack("!i", new_toc_offset)
               + pyz_blob[12:pyz_data_start] + pyz_blob[pyz_data_start:pyz_toc_offset]
               + b"".join(appended) + new_toc)

    # CArchive：只换 main 和 PYZ 两块，其余的字节原样搬过去，只改它们的偏移
    new_main_raw = compile_source(MAIN_SOURCE, "main.py")   # 未压缩长度要单独留着：
    new_main = zlib.compress(new_main_raw, 6)              # 引导程序按这一项申请解压缓冲区
    main_delta = len(new_main) - main_entry["data_length"]
    prefix = raw[archive_start:archive_start + main_entry["offset"]]
    middle = raw[archive_start + main_entry["offset"] + main_entry["data_length"]:
                 archive_start + pyz_entry["offset"]]
    moved = []
    for e in entries:
        if e is main_entry:
            moved.append(dict(e, data_length=len(new_main), raw_length=len(new_main_raw)))
        elif e is pyz_entry:
            moved.append(dict(e, offset=e["offset"] + main_delta, data_length=len(new_pyz),
                              raw_length=len(new_pyz)))
        elif e["offset"] > main_entry["offset"]:
            moved.append(dict(e, offset=e["offset"] + main_delta))
        else:
            moved.append(dict(e))
    toc_offset = len(prefix) + len(new_main) + len(middle) + len(new_pyz)
    toc_bytes = serialize_carchive_toc(moved)
    archive_length = toc_offset + len(toc_bytes) + COOKIE_LENGTH
    cookie = struct.pack(COOKIE_FORMAT, COOKIE_MAGIC, archive_length, toc_offset,
                         len(toc_bytes), 311, pylib_name.encode("ascii").ljust(64, b"\0"))
    new_archive = prefix + new_main + middle + new_pyz + toc_bytes + cookie
    patched = raw[:archive_start] + new_archive

    # 拿新 exe 自己验一遍：TOC 能解回来、5 个模块指到的是新字节码、地址都落在文件里
    check_start, check_entries, _ = read_carchive(patched)
    check_by_name = {e["name"]: e for e in check_entries}
    for name, offset in ((MAIN_ENTRY, check_by_name[MAIN_ENTRY]["offset"]),):
        assert offset + check_by_name[name]["data_length"] <= len(patched)
    pyz_c = check_by_name[PYZ_NAME]
    blob_c, _, toc_offset_c, toc_c = read_pyz(patched, check_start, pyz_c)
    map_c = dict(toc_c)
    for name in MODULES:
        typecode, offset, length = map_c[name]
        assert offset + length <= len(blob_c), name
        code = marshal.loads(zlib.decompress(blob_c[offset:offset + length]))
        assert code.co_filename == os.path.join(*name.split(".")) + ".py", (name, code.co_filename)
    main_c = check_by_name[MAIN_ENTRY]
    assert main_c["raw_length"] == len(new_main_raw), main_c
    assert zlib.decompress(patched[check_start + main_c["offset"]:
                                   check_start + main_c["offset"] + main_c["data_length"]]) \
        == new_main_raw
    assert marshal.loads(new_main_raw).co_filename == "main.py"
    for e in check_entries:
        assert e["offset"] + e["data_length"] <= len(patched), e

    payload = {
        "main.bin": new_main,
        "pyz-header.bin": new_pyz[:pyz_data_start],
        "pyz-append.bin": b"".join(appended),
        "pyz-toc.bin": new_toc,
        "carchive-toc.bin": toc_bytes,
        "carchive-cookie.bin": cookie,
    }
    manifest = {
        "app": "jev-chat-windows",
        "app_version": "v0.1.11",
        "patch": "判断模型支持自定义 OpenAI 兼容来源",
        "built_by": sys.version.split()[0],
        "original": {"sha256": sha256(raw), "size": len(raw)},
        "patched": {"sha256": sha256(patched), "size": len(patched)},
        # apply.ps1 拼文件要用：CArchive 在 exe 里的起点。下面几个 offset 都是相对的。
        "archive_start": archive_start,
        "main_entry": {"offset": main_entry["offset"], "length": main_entry["data_length"],
                       "file": "main.bin"},
        "pyz": {"offset": pyz_entry["offset"], "length": pyz_entry["data_length"],
                "data_start": pyz_data_start, "toc_offset": pyz_toc_offset,
                "header_file": "pyz-header.bin", "header_length": pyz_data_start,
                "new_toc_offset": new_toc_offset,
                "append_file": "pyz-append.bin", "toc_file": "pyz-toc.bin"},
        "carchive": {"toc_file": "carchive-toc.bin", "cookie_file": "carchive-cookie.bin"},
        "modules": sorted(MODULES),
    }
    return payload, manifest, patched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", help="官方 v0.1.11 的 jev-chat-windows.exe")
    ap.add_argument("--out", default=os.path.join(ROOT, "patch"), help="补丁输出目录")
    ap.add_argument("--patched-exe", help="顺便把打好的 exe 写到这个路径，方便自己先跑")
    args = ap.parse_args()
    if not args.exe:
        ap.error("--exe is required: path to the official v0.1.11 jev-chat-windows.exe")

    payload, manifest, patched = build(args.exe, args.patched_exe)
    os.makedirs(args.out, exist_ok=True)
    for name, blob in payload.items():
        with open(os.path.join(args.out, name), "wb") as f:
            f.write(blob)
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
    if args.patched_exe:
        os.makedirs(os.path.dirname(os.path.abspath(args.patched_exe)), exist_ok=True)
        with open(args.patched_exe, "wb") as f:
            f.write(patched)
    print(f"source exe : {manifest['original']['sha256']}  {manifest['original']['size']} B")
    print(f"patched exe: {manifest['patched']['sha256']}  {manifest['patched']['size']} B")
    print(f"payload    : {sum(len(b) for b in payload.values())} B -> {args.out}")


if __name__ == "__main__":
    main()