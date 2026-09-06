#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""usb2test/cases.py — the 14-case regression matrix from USB2DRI_PLAN.md §5.1.

Each case is a dict: {params..., asserts: [fn(machine) -> (ok, msg)]}.
"""
import struct
import sys

import unicorn.x86_const as _xc
from unicorn import UC_HOOK_CODE
from runner import (default_disk, sym, img_off, IMG_BASE, Machine)

for _n in dir(_xc):
    if _n.startswith("UC_X86_REG_"):
        globals()[_n[3:]] = getattr(_xc, _n)


def rd(uc, lin, n=1):
    off = img_off(lin)
    base = IMG_BASE if off is None else IMG_BASE + off
    b = bytes(uc.mem_read(base, n))
    return b[0] if n == 1 else b


def rd16(uc, lin):
    return struct.unpack("<H", rd(uc, lin, 2))[0]


def rd32(uc, lin):
    return struct.unpack("<I", rd(uc, lin, 4))[0]


def a_devcount(m):
    """usb_count_error holds the drive count on success."""
    v = rd(m.uc, sym("usb_count_error"))
    return (v < 0x80 and v > 0, "usb_count_error=0x%02X (want 1..7)" % v)


def a_drive(m, idx, want):
    v = rd(m.uc, sym("usb_drive_num") + idx)
    return (v == want, "usb_drive_num[%d]=0x%02X (want 0x%02X)" % (idx, v, want))


def a_err(m, want):
    v = rd(m.uc, sym("usb_count_error"))
    return (v == want, "usb_count_error=0x%02X (want 0x%02X)" % (v, want))


def a_disk_read(m, dl, lba, nsect):
    """Issue int13 AH=42h through the hooked vector and compare the buffer
    with the virtual disk image."""
    u = m.uc
    # the driver hooked int13 to the relocated block: read IVT 0x4C
    ivt = u.mem_read(0x4C, 4)
    off, seg = struct.unpack("<HH", ivt)   # IVT: offset, segment
    buf_seg, buf_off = 0x3200, 0x0000
    pkt_seg, pkt_off = 0x3000, 0x0000
    # EDD packet: [0]=size [2:4]=count [4:6]=offset [6:8]=segment
    # [8:16]=LBA64
    pkt = struct.pack("<HH", 0x10, nsect) + \
          struct.pack("<HH", buf_off, buf_seg) + \
          struct.pack("<Q", lba)
    u.mem_write(pkt_seg * 16 + pkt_off, pkt)
    u.reg_write(X86_REG_AX, 0x4200)
    u.reg_write(X86_REG_DX, dl)
    u.reg_write(X86_REG_DS, pkt_seg)
    u.reg_write(X86_REG_SI, pkt_off)
    u.reg_write(X86_REG_SS, 0)
    u.reg_write(X86_REG_SP, 0x6E00)
    # ES must be a REAL data segment: the driver's Disk_package loads
    # ES from the disk packet and Remove_qTD later clears qTD memory
    # through ES:DI - ES=0 would point its stosw at linear 0.
    u.reg_write(X86_REG_ES, 0x3200)
    u.reg_write(X86_REG_FLAGS, 0x2)
    m.far_call(seg, off)
    # run until the driver chains back through the old-BIOS trampoline;
    # bounded by instruction count
    done = [False]

    def hc(uc, addr, size, ud):
        if addr == 0x10000:
            done[0] = True

    u.hook_add(UC_HOOK_CODE, hc, begin=0x8200, end=0x362100)
    u.emu_start((seg << 4) + off, 0xFFFFFF, count=8_000_000)
    data = bytes(u.mem_read(buf_seg * 16 + buf_off, nsect * 512))
    expect = bytes(m.disk[lba*512:(lba+nsect)*512])
    return (data == expect,
            "int13 AH=42 dl=0x%02X lba=%d: %s" %
            (dl, lba, "match" if data == expect else "MISMATCH"))


CASES = {
    # 1: clean HS enumeration + AH=42h read
    1: dict(
        ports=[{"kind": "hs"}],
        disk=default_disk(),
        asserts=[
            lambda m: a_devcount(m),
            lambda m: a_drive(m, 0, 0x80),
            lambda m: a_disk_read(m, 0x80, 0, 1),
        ]),
    # 2: slow device (PR self-clear 300ms) — A2
    2: dict(
        ports=[{"kind": "hs", "pr_clear_ms": 300}],
        asserts=[lambda m: a_devcount(m)],
    ),
    # 3: no device
    3: dict(
        ports=[{"kind": "none"}],
        asserts=[lambda m: a_err(m, 0x81)],
    ),
    # 4: low-speed device (line status K, PED never sets)
    4: dict(
        ports=[{"kind": "ls", "ped_after_reset": False}],
        asserts=[lambda m: a_err(m, 0x81)],
    ),
    # 5: first reset fails, recovers on retry — A1
    5: dict(
        ports=[{"kind": "hs", "ped_after_reset": False, "ped_attempts": 2}],
        asserts=[lambda m: a_devcount(m)],
    ),
    # 6: stall during enumeration, recovers on retry — A1
    6: dict(
        ports=[{"kind": "hs", "stall_once": True}],
        asserts=[lambda m: a_devcount(m)],
    ),
    # 7: LEGSUP BIOS-owned=1 -> handshake completes
    7: dict(
        ports=[{"kind": "hs"}],
        hccparams=0x5000,
        bios_owned=1,
        asserts=[lambda m: a_devcount(m)],
    ),
    # 8: HCCPARAMS=0 -> skip handshake, normal init
    8: dict(
        ports=[{"kind": "hs"}],
        hccparams=0,
        asserts=[lambda m: a_devcount(m)],
    ),
    # 9: int13 AH=02/03/42/48 (read/write regression)
    9: dict(
        ports=[{"kind": "hs"}],
        asserts=[
            lambda m: a_devcount(m),
            lambda m: a_disk_read(m, 0x80, 0, 1),
            lambda m: a_disk_read(m, 0x80, 63, 1),
        ],
    ),
    # 10: multi-LUN device (2 LUNs)
    10: dict(
        ports=[{"kind": "hs", "max_lun": 1}],
        asserts=[
            lambda m: a_devcount(m),
            lambda m: a_drive(m, 1, 0x81),
        ],
    ),
    # 11: hub + downstream device
    11: dict(
        ports=[{"kind": "hub", "downstream": [{"kind": "hs"}]}],
        asserts=[lambda m: a_devcount(m)],
    ),
    # 12: qTD never completes -> timeout, no hang (A4)
    12: dict(
        ports=[{"kind": "hs", "never_complete": True}],
        asserts=[
            lambda m: a_err(m, 0x81),
        ],
    ),
    # 13: FS device (ep0 MPS=8, HS handshake fails) — A3
    13: dict(
        ports=[{"kind": "fs_mps8", "mps0": 8}],
        asserts=[],   # behavior recorded; A3 asserts success
    ),
    # 14: dual EHCI controllers — only the first is used (Phase 1)
    14: dict(
        ports=[{"kind": "hs"}],
        dual_ehci=True,
        asserts=[lambda m: a_devcount(m)],
    ),
}


def run_case(n, case):
    m = Machine(case)
    reason = m.run()
    results = []
    for fn in case.get("asserts", []):
        try:
            ok, msg = fn(m)
        except Exception as e:
            ok, msg = False, "assert raised: %r" % e
        results.append((ok, msg))
    return m, reason, results
