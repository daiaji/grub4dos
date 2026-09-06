#!/usr/bin/env python3
"""ext_syms.py — resolve grub4dos image symbols to absolute VMAs.

The PE symbol table (objdump -t) stores per-symbol *section-relative*
values; the section VMAs come from objdump -h.  nm reports .text symbols
correctly (its PE reader assumes section 1 base == 0) but misplaces .data
symbols, so we compute absolute addresses properly:

    abs = section_vma(sec) + symbol_value

The images we test are loaded at their link origin (VMA == physical), so
these absolute addresses are directly usable as fs:[ABS(...)] targets.

Usage: ext_syms.py pre_stage2.exec [symbol...]
Prints "name=0xADDR" lines for the requested symbols (default: all that
both tables agree on), plus JSON with the full map on stdout.
"""
import json
import subprocess
import sys
import re

EXE = r"D:\tools\x64-gcc\mingw64\bin\objdump.exe"


def objdump(*args):
    return subprocess.run([EXE, *args], capture_output=True, text=True).stdout


def main():
    img = sys.argv[1]
    # section idx/name -> VMA
    sec_vma = {}
    for line in objdump("-h", img).splitlines():
        m = re.match(r"\s*(\d+)\s+(\S+)\s+([0-9a-f]+)\s+([0-9a-f]+)\s+([0-9a-f]+)\s+", line)
        if m:
            sec_vma[m.group(2)] = int(m.group(4), 16)
            sec_vma.setdefault(int(m.group(1)), int(m.group(4), 16))
    # symbol -> (sec, value).  NOTE: objdump -t numbers sections from 1,
    # objdump -h indexes them from 0, so the section table lookup uses sec-1.
    syms = {}
    for line in objdump("-t", img).splitlines():
        m = re.match(r"\[(\d+)\]\(sec\s*(-?\d+)\).*?0x([0-9a-f]+)\s+(\S+)$", line)
        if m:
            syms[m.group(4)] = (int(m.group(2)), int(m.group(3), 16))
    out = {}
    for name, (sec, val) in syms.items():
        if sec < 0:
            continue  # absolute section (-1): value is already absolute
        vma = sec_vma.get(sec - 1)
        if vma is None:
            continue
        out[name] = vma + val
    want = sys.argv[2:]
    for name in want:
        print("%s=0x%X" % (name, out.get(name, 0)))
    if not want:
        for name in sorted(out):
            print("%s=0x%X" % (name, out[name]))
    print("__JSON__" + json.dumps(out))


if __name__ == "__main__":
    main()
