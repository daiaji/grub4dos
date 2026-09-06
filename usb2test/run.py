#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""usb2test/run.py — run the 14-case regression matrix.

Usage:
    python run.py            # run all cases, print results
    python run.py 1 2 3      # run selected cases
    python run.py --json     # machine-readable results
"""
import argparse
import json
import sys

import cases
from runner import sym, img_off, IMG_BASE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", nargs="*", type=int)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    sel = args.cases or sorted(cases.CASES)
    out = {}
    for n in sel:
        case = cases.CASES[n]
        m, reason, results = cases.run_case(n, case)
        ok = all(r[0] for r in results)
        entry = {
            "case": n,
            "stop": reason,
            "ticks": m.ticks,
            "insns": m.insns,
            "doorbells": len(m.doorbell_ticks),
            "qtds": m.ehci.qtds_done,
            "asserts": [{"ok": o, "msg": msg} for o, msg in results],
        }
        out[n] = entry
        status = "PASS" if ok else "FAIL"
        print("[%s] case %2d  stop=%-14s ticks=%-6d insns=%-9d doorbells=%-3d qtds=%d"
              % (status, n, reason, m.ticks, m.insns,
                 len(m.doorbell_ticks), m.ehci.qtds_done))
        for o, msg in results:
            print("        %s %s" % ("ok  " if o else "FAIL", msg))
    if args.json:
        print(json.dumps(out))
    bad = [n for n, e in out.items() if not all(a["ok"] for a in e["asserts"])]
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
