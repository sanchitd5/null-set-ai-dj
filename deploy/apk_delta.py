#!/usr/bin/env python3
"""APK delta patches for the in-app updater (stdlib only: runs on a stock python3 3.8+, nothing to install).

An APK is a zip. Between two builds most entries (libffmpeg/libpython zips, libflutter.so, assets) are
byte-identical, so a patch is mostly "copy this range of the old APK"; only changed entries are sent, and
inside those a sparse-index block matcher still finds shared runs (stored entries, the signing block).

Patch format "NSDP1" (all integers big-endian; the phone applier is lib/net/apk_patch.dart):
    8   magic  b"NSDP1\\0\\0\\0"
    32  sha256 of the OLD apk (the phone refuses to apply to anything else)
    32  sha256 of the NEW apk (the phone checks the result against it)
    8   size of the NEW apk
    ops until END:   0x01 COPY off(8) len(8)   |   0x02 ADD len(8) <len bytes>   |   0x00 END

CLI:
    apk_delta.py make OLD NEW OUT.patch     build one patch
    apk_delta.py apply OLD PATCH OUT.apk    apply (reference implementation; checks both hashes)
    apk_delta.py publish --dir D --keep N   after push_apk.sh dropped a new null-set-dj.apk + .json in D:
                                            archive it as D/history/<code>.apk, keep the last N, build
                                            D/patches/<from>.patch for every kept older APK and list them
                                            under "patches" in null-set-dj.json
"""
import argparse
import hashlib
import json
import os
import struct
import sys
import zipfile

MAGIC = b"NSDP1\0\0\0"
OP_END, OP_COPY, OP_ADD = 0, 1, 2
WIN = 32      # a match must share this many bytes
STEP = 16     # old side is indexed every STEP bytes, so every shared run >= WIN+STEP-1 bytes is found
MIN_COPY = 48


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _entry_ranges(data):
    """[(name, start, end)] of every zip entry (local header + data) in file order, plus the zip's own
    gaps (signing block, central directory) as (None, start, end)."""
    import io
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = sorted(z.infolist(), key=lambda i: i.header_offset)
    spans = []
    for i, info in enumerate(infos):
        start = info.header_offset
        end = infos[i + 1].header_offset if i + 1 < len(infos) else None
        spans.append([info.filename, start, end])
    # entry end = start of the next entry, except the last, which ends where its data ends
    last = infos[-1]
    nlen, elen = struct.unpack("<HH", data[last.header_offset + 26:last.header_offset + 30])
    spans[-1][2] = last.header_offset + 30 + nlen + elen + last.compress_size
    out, pos = [], 0
    for name, s, e in spans:
        if s > pos:
            out.append((None, pos, s))
        out.append((name, s, e))
        pos = e
    if pos < len(data):
        out.append((None, pos, len(data)))
    return out


class _Ops:
    def __init__(self):
        self.ops = []   # ("c", off, len) | ("a", bytes)

    def copy(self, off, n):
        if n <= 0:
            return
        if self.ops and self.ops[-1][0] == "c" and self.ops[-1][1] + self.ops[-1][2] == off:
            self.ops[-1] = ("c", self.ops[-1][1], self.ops[-1][2] + n)
        else:
            self.ops.append(("c", off, n))

    def add(self, b):
        if not b:
            return
        if self.ops and self.ops[-1][0] == "a":
            self.ops[-1] = ("a", self.ops[-1][1] + b)
        else:
            self.ops.append(("a", bytes(b)))


def _match_into(ops, old, o0, o1, new, n0, n1):
    """Encode new[n0:n1] using runs found in old[o0:o1] (sparse index, greedy extend both ways)."""
    n = n1 - n0
    if n <= 0:
        return
    if o1 - o0 < WIN or n < WIN:
        ops.add(new[n0:n1])
        return
    index = {}
    for p in range(o0, o1 - WIN + 1, STEP):
        index.setdefault(old[p:p + WIN], p)
    i, lit = n0, n0   # lit = start of pending literal
    limit = n1 - WIN
    while i <= limit:
        p = index.get(new[i:i + WIN])
        if p is None:
            i += 1
            continue
        # extend backwards (into the pending literal) and forwards
        b = 0
        while p - b > o0 and i - b > lit and old[p - b - 1] == new[i - b - 1]:
            b += 1
        f = WIN
        while p + f < o1 and i + f < n1 and old[p + f] == new[i + f]:
            f += 1
        if b + f < MIN_COPY:
            i += 1
            continue
        ops.add(new[lit:i - b])
        ops.copy(p - b, b + f)
        i += f
        lit = i
    ops.add(new[lit:n1])


def make_patch(old_path, new_path, out_path):
    old = open(old_path, "rb").read()
    new = open(new_path, "rb").read()
    old_entries = {n: (s, e) for n, s, e in _entry_ranges(old) if n}
    ops = _Ops()
    for name, s, e in _entry_ranges(new):
        if name is not None and name in old_entries:
            os_, oe = old_entries[name]
            if oe - os_ == e - s and old[os_:oe] == new[s:e]:
                ops.copy(os_, oe - os_)
                continue
            _match_into(ops, old, os_, oe, new, s, e)
        elif name is None:
            # signing block / central directory / end record: match against the old file's same kind of
            # tail (everything after the last old entry), which is where they live
            tail = max(e2 for n2, s2, e2 in _entry_ranges(old) if n2)
            _match_into(ops, old, tail, len(old), new, s, e)
        else:
            ops.add(new[s:e])
    with open(out_path, "wb") as f:
        f.write(MAGIC + hashlib.sha256(old).digest() + hashlib.sha256(new).digest() + struct.pack(">Q", len(new)))
        for op in ops.ops:
            if op[0] == "c":
                f.write(bytes([OP_COPY]) + struct.pack(">QQ", op[1], op[2]))
            else:
                f.write(bytes([OP_ADD]) + struct.pack(">Q", len(op[1])) + op[1])
        f.write(bytes([OP_END]))
    return os.path.getsize(out_path)


def apply_patch(old_path, patch_path, out_path):
    """Reference applier. Raises ValueError on any inconsistency; never leaves a half file at out_path."""
    out_path = os.fspath(out_path)
    tmp = out_path + ".tmp"
    try:
        with open(patch_path, "rb") as p, open(old_path, "rb") as o, open(tmp, "wb") as out:
            if p.read(8) != MAGIC:
                raise ValueError("not an NSDP1 patch")
            old_sha, new_sha = p.read(32), p.read(32)
            (new_size,) = struct.unpack(">Q", p.read(8))
            if hashlib.sha256(open(old_path, "rb").read()).digest() != old_sha:
                raise ValueError("patch was built for a different old APK")
            h, written = hashlib.sha256(), 0
            while True:
                op = p.read(1)
                if not op:
                    raise ValueError("truncated patch")
                if op[0] == OP_END:
                    break
                if op[0] == OP_COPY:
                    off, n = struct.unpack(">QQ", p.read(16))
                    o.seek(off)
                    b = o.read(n)
                    if len(b) != n:
                        raise ValueError("copy beyond the old APK")
                elif op[0] == OP_ADD:
                    (n,) = struct.unpack(">Q", p.read(8))
                    b = p.read(n)
                    if len(b) != n:
                        raise ValueError("truncated patch")
                else:
                    raise ValueError("unknown op %d" % op[0])
                out.write(b)
                h.update(b)
                written += len(b)
            if written != new_size or h.digest() != new_sha:
                raise ValueError("result does not match the published hash")
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _archive(src, dst):
    dst = os.fspath(dst)
    with open(src, "rb") as s, open(dst + ".tmp", "wb") as d:
        for b in iter(lambda: s.read(1 << 20), b""):
            d.write(b)
    os.replace(dst + ".tmp", dst)


def publish(d, keep):
    """d holds the freshly pushed null-set-dj.apk + null-set-dj.json. Returns the patch list written to the json."""
    apk, meta_path = os.path.join(d, "null-set-dj.apk"), os.path.join(d, "null-set-dj.json")
    meta = json.load(open(meta_path))
    hist, patches_dir = os.path.join(d, "history"), os.path.join(d, "patches")
    os.makedirs(hist, exist_ok=True)
    os.makedirs(patches_dir, exist_ok=True)
    code = int(meta["version_code"])
    _archive(apk, os.path.join(hist, "%d.apk" % code))
    # first run after this feature: the APK the push just replaced (.bak) is the previous build; keep it too
    try:
        prev = int(json.load(open(meta_path + ".bak"))["version_code"])
        if prev != code and not os.path.exists(os.path.join(hist, "%d.apk" % prev)) and os.path.isfile(apk + ".bak"):
            _archive(apk + ".bak", os.path.join(hist, "%d.apk" % prev))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    codes = sorted((int(f[:-4]) for f in os.listdir(hist) if f.endswith(".apk") and f[:-4].isdigit()), reverse=True)
    for c in codes[max(1, keep):]:
        os.unlink(os.path.join(hist, "%d.apk" % c))
    for f in os.listdir(patches_dir):
        os.unlink(os.path.join(patches_dir, f))   # old patches target an older "new"; rebuilt below
    out = []
    for c in codes[:max(1, keep)]:
        if c >= code:
            continue
        old = os.path.join(hist, "%d.apk" % c)
        pp = os.path.join(patches_dir, "%d.patch" % c)
        size = make_patch(old, apk, pp)
        if size >= int(meta["size"]) * 0.9:   # not worth it: the phone downloads the full APK
            os.unlink(pp)
            continue
        out.append({"from_version_code": c, "from_sha256": sha256_file(old), "sha256": meta["sha256"],
                    "patch_sha256": sha256_file(pp), "patch_size": size, "size": meta["size"]})
    meta["patches"] = out
    # plain open (umask mode, readable by the server): CT116's app dir maps new files to nobody, so root may not
    # chmod a 0600 tempfile there
    tmp = meta_path + ".tmp"
    with open(tmp, "w") as t:
        json.dump(meta, t, indent=2)
    os.replace(tmp, meta_path)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("old"), m.add_argument("new"), m.add_argument("out")
    a = sub.add_parser("apply")
    a.add_argument("old"), a.add_argument("patch"), a.add_argument("out")
    p = sub.add_parser("publish")
    p.add_argument("--dir", required=True)
    p.add_argument("--keep", type=int, default=int(os.environ.get("APK_KEEP", "3")))
    args = ap.parse_args(argv)
    if args.cmd == "make":
        print("patch %d bytes" % make_patch(args.old, args.new, args.out))
    elif args.cmd == "apply":
        apply_patch(args.old, args.patch, args.out)
        print("ok")
    else:
        for e in publish(args.dir, args.keep):
            print("patch from %d: %d bytes" % (e["from_version_code"], e["patch_size"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
