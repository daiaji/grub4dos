#!/usr/bin/env python3
"""squeeze.py — exact Python transcription of the pre_stage2 head/tail
zero-squeeze from stage2/Makefile.am (%.exec -> % rule).

The shell rules do (F = len(raw)-3MB, S = stripped leading-zero count):
  1. fullsize = raw[3MB:]                      (dd bs=65536 skip=48)
  2. tail = fullsize with line-1 leading NULs stripped (sed 1s/^\\x00*//)
     -> len(tail) = F - S
  3. if sed stripped nothing (T >= F): cmp-vs-/dev/zero finds the first
     differing (= first nonzero) byte N of fullsize; when N > 2000,
     tail = fullsize[N-1:] (S = N-1)
  4. head = pre_stage2[0 : F - T] = pre_stage2[0 : S]  (dd bs=expr $1-$6)
     final = head + tail = raw[:S] + raw[3MB+S:]

Net effect: cut the byte window [S, 3MB+S) out of the raw binary — exactly
the ".space 0x300000 / !!!! insert 3M !!!!" hole that the runtime re-creates
by copying the 32-bit part above 3MB (asm.S "move 32-bit code to above 3M").
"""
import sys

MB = 3 * 1024 * 1024


def squeeze(raw: bytes) -> bytes:
    if len(raw) < MB:
        raise SystemExit("squeeze: raw binary < 3MB (%d) - layout needs "
                         "checking" % len(raw))
    full = raw[MB:]
    # sed '1s/^\x00*//' : strip leading NULs of line 1 only (up to first \n)
    nl = full.find(b"\n")
    line1 = full if nl < 0 else full[:nl]
    s = len(line1) - len(line1.lstrip(b"\x00"))
    if s == 0:
        # cmp vs /dev/zero: first differing byte = first nonzero byte
        nz = next((i for i, b in enumerate(full) if b), None)
        if nz is not None and nz > 2000:
            s = nz - 1
        # else: tail unchanged, s stays 0
    head = raw[:s]
    tail = full[s:]
    return head + tail


def main():
    raw = open(sys.argv[1], "rb").read()
    out = squeeze(raw)
    # the trailing magic from the Makefile rule: \0260\002\032\0316
    out += bytes([0o260, 0o002, 0o032, 0o316])
    open(sys.argv[2], "wb").write(out)
    print("squeeze: raw=%d -> %d bytes (+4 magic)" % (len(raw), len(out)))


if __name__ == "__main__":
    main()
