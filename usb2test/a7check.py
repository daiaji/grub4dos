#!/usr/bin/env python3
"""a7check.py — USB2DRI_PLAN.md §A7 resident-budget guardrail.

Reads `nm` output of pre_stage2.exec, measures:
  * Ending - USB2DRI          (the rep movsb resident copy length; must be
                               <= 3072 so it fits the 3KB carved from 0x413)
  * the whole 0x13C0 block placeholder end for reference
and dumps the EXT_C symbols the usb2test harness needs to place variables.
"""
import sys

MAX_RESIDENT = 3072
WANT = ["USB2DRI", "Ending", "usb_count_error", "usb_drive_num",
        "usb_md_address", "max_dri", "usb_delay", "One_transfer", "init_usb"]


def main():
    syms = {}
    for line in open(sys.argv[1]):
        parts = line.split()
        if len(parts) >= 3:
            syms.setdefault(parts[2], int(parts[0], 16))
    for name in WANT:
        if name not in syms:
            # nm on PE may underscore symbols; try with a leading underscore
            alt = "_" + name
            if alt in syms:
                syms[name] = syms[alt]
    missing = [n for n in WANT if n not in syms]
    if missing:
        print("A7: WARNING symbols not found: %s" % ", ".join(missing))

    if "USB2DRI" in syms and "Ending" in syms:
        resident = syms["Ending"] - syms["USB2DRI"]
        ok = resident <= MAX_RESIDENT
        print("A7: USB2DRI=0x%X Ending=0x%X  Ending-USB2DRI=%d bytes  "
              "(limit %d)  %s" % (syms["USB2DRI"], syms["Ending"], resident,
                                  MAX_RESIDENT,
                                  "OK" if ok else "EXCEEDED - BUILD FAILS"))
        if not ok:
            sys.exit(1)
        if "init_usb" in syms:
            print("A7: init_usb=0x%X (block entry)" % syms["init_usb"])
    else:
        print("A7: USB2DRI/Ending symbols missing - cannot measure")
        sys.exit(1)

    with open(sys.argv[2], "rb") as f:
        img = f.read()
    base = syms["USB2DRI"] - 0x8200
    sig = img[base + 0x20: base + 0x28]
    print("A7: block signature at +0x20: %r (expect b'USB2DRI ')" % sig)
    for n in WANT[2:]:
        if n in syms:
            v = syms[n]
            off = v - 0x8200
            inimg = 0 <= off < len(img)
            print("A7: %-16s = 0x%05X (img off 0x%05X %s)"
                  % (n, v, off, "ok" if inimg else "OUT OF IMAGE"))


if __name__ == "__main__":
    main()
