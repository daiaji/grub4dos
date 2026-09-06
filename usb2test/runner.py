#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
usb2test/runner.py — Unicorn-based simulation harness for the grub4dos
built-in EHCI driver (USB2DRI block), following USB2DRI_PLAN.md §5.1.

Method (ported from plpbt-decompile/tools/bootlab.py):
  * the pre_stage2 image is loaded at its link origin (linear 0x8200), so
    every absolute reference inside the driver (%fs:ABS(EXT_C(...)) and the
    (x - USB2DRI) block-relative forms) hits the right bytes;
  * BIOS stubs: int 1Ah PCI BIOS32 (B101/B103/B108-B10D), int 13h (old-BIOS
    disk, AH=02/03/08/15/41/42/48), int 15h (A20 + memory), BDA (0x413,
    0x410, 0x475, 0x46C tick advanced by a virtual clock), port I/O
    (0xCF8 in the Delay loop, 0x60/0x64/0x92 for the A20 fallback);
  * virtual EHCI controller: register page at 0x80..0x100 (the driver
    reaches it through ES=8 in its real<->protected excursion, linear
    = ES<<4 + SI = 0x80 + SI; CAPLENGTH at 0x80, HCSPARAMS 0x84,
    HCCPARAMS 0x88, USBCMD 0x90, USBSTS 0x94, USBINTR 0x98,
    ASYNCLISTADDR 0xA8, CONFIGFLAG 0xD0, PORTSC 0xD4+4n);
  * QH/qTD engine: a USBCMD write with value&0x24 (the driver's 0x80021
    doorbell, asm.S Set_QH) runs the async list and raises USBINT|IAA;
    W1C semantics on USBSTS writes; overlay write-back of the retired qTD
    (the driver continues its toggle from overlay token bit 31).

Virtual device model: descriptors + BOT mass storage (CBW/CSW), hub
enumeration (class 09), configurable per-case behaviour: no device,
low-speed, MPS8, slow PR self-clear, reset retry, transfer stall,
qTD-never-completes, LEGSUP BIOS-owned, dual controllers, multi-LUN.

The test shell is replaced by the harness itself: set CS:IP to the block
entry (mimicking init_usb's lcall) with a return frame pointing at a hlt
sentry; after Initialization's lret the run stops there.  Then the harness
reads the driver's result variables and drives int 13h services directly
through the hooked IVT entry (far-call to the relocated block).
"""
import os
import re
import struct
import sys
import json

from unicorn import *
from unicorn.x86_const import *

_xc = sys.modules["unicorn.x86_const"]
for _n in dir(_xc):
    if _n.startswith("UC_X86_REG_"):
        globals()[_n[3:]] = getattr(_xc, _n)

HERE = os.path.dirname(os.path.abspath(__file__))
STAGE2 = os.path.join(HERE, "..", "stage2")
IMG = os.path.join(STAGE2, "pre_stage2")
IMG_BASE = 0x8200                    # link origin (VMA == physical)

# ------------------------------------------------------------------ symbols
# ext_syms.py resolves PE symbol values to absolute linear addresses.
_SYMS = None


def load_syms():
    global _SYMS
    if _SYMS is None:
        import subprocess
        r = subprocess.run([sys.executable,
                            os.path.join(HERE, "ext_syms.py"), "pre_stage2.exec"],
                           cwd=STAGE2, capture_output=True, text=True)
        blob = r.stdout[r.stdout.index("__JSON__") + 8:]
        _SYMS = json.loads(blob)
    return _SYMS


def sym(name):
    return load_syms().get(name, 0)


def img_off(lin):
    """linear address -> offset in the squeezed pre_stage2 image."""
    if lin < IMG_BASE:
        return None
    off = lin - IMG_BASE
    if off < 0x300000:            # before the 3MB hole
        return off
    return off - 0x300000         # after the hole (image is squeezed)


def img_read(uc, lin, n):
    off = img_off(lin)
    if off is None:
        return bytes(uc.mem_read(lin, n))
    return bytes(uc.mem_read(IMG_BASE + off, n))


# ------------------------------------------------------------------ devices
# Descriptor templates (Linux ehci-hcd style bit definitions apply).
DEV_DESC_HS = bytes([
    0x12, 0x01, 0x00, 0x02, 0x00, 0x00, 0x00, 0x40,   # 18B, HS, ep0 64
    0x34, 0x12, 0x78, 0x56, 0x00, 0x01,
    0x01, 0x02, 0x03, 0x01])

DEV_DESC_MPS8 = bytes([
    0x12, 0x01, 0x00, 0x02, 0x00, 0x00, 0x00, 0x08,   # ep0 MPS=8
    0x34, 0x12, 0x78, 0x56, 0x00, 0x01,
    0x01, 0x02, 0x03, 0x01])

CFG_DESC_MSC = bytes([
    0x09, 0x02, 0x20, 0x00, 0x01, 0x01, 0x00, 0x80, 0xFA,
    0x09, 0x04, 0x00, 0x00, 0x02, 0x08, 0x06, 0x50, 0x00,
    0x07, 0x05, 0x81, 0x02, 0x40, 0x00, 0x00,
    0x07, 0x05, 0x02, 0x02, 0x40, 0x00, 0x00])

HUB_DESC = bytes([
    0x09, 0x29, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00])  # 1 downstream port

SCSI_INQUIRY = (bytes([
    0x00, 0x80, 0x05, 0x02, 0x1F, 0x00, 0x00, 0x00,
]) + b"USB2DRI  "[:8] + b"VIRTUAL-DISK    "[:16] + b"1.0")


def scsi_read_capacity(nsect):
    return struct.pack(">II", nsect - 1, 512)


def scsi_csw(tag, status=0):
    return b"USBS" + struct.pack("<IIB", tag, status, 0)


class UsbDevice:
    """Virtual USB device attached to one root port (or a hub port)."""

    def __init__(self, disk, kind="hs", mps0=64, max_lun=0,
                 pr_clear_ms=0, ped_after_reset=True, ped_attempts=1,
                 stall_once=False, never_complete=False, hub=None):
        self.disk = disk                       # bytearray image
        self.nsect = len(disk) // 512
        self.kind = kind
        self.mps0 = mps0
        self.max_lun = max_lun                 # 0..15
        self.pr_clear_ms = pr_clear_ms         # 0 = instant PR self-clear
        self.ped_after_reset = ped_after_reset
        self.ped_attempts = ped_attempts       # resets before PED grants
        self.reset_count = 0
        self.stall_once = stall_once
        self.stalled = False
        self.never_complete = never_complete   # qTD never retires
        self.hub = hub                         # downstream device list
        self.addr = 0
        self.data_in = None                    # pending IN payload
        self.cbw = None
        self.write = None                      # (lba, bytes_left)
        self.csw_pending = False
        self.setup = b""
        self.transfers = 0                     # completed qTD count
        self.stalled_t = 0

    # ---- control transfers ------------------------------------------------
    def control(self, setup):
        bm, bmtype, breq = setup[0], setup[1], setup[2]
        wval = setup[2] | (setup[3] << 8)
        wlen = setup[6] | (setup[7] << 8)
        self.transfers += 1
        if self.stall_once and not self.stalled                 and bmtype == 0x06 and wval == 0x0100:
            self.stalled = True
            self.stalled_t = self.transfers
            return "stall"
        if bmtype == 0x06 and bm & 0x80:          # GET_DESCRIPTOR
            if wval == 0x0100:                    # device
                d = DEV_DESC_MPS8 if self.mps0 == 8 else DEV_DESC_HS
                self.data_in = d
                return "in"
            if wval == 0x0200:                    # config
                self.data_in = CFG_DESC_MSC
                return "in"
            if wval == 0x2900 and self.hub:       # hub descriptor
                self.data_in = HUB_DESC
                return "in"
            return "stall"
        if bmtype == 0x05 and bm == 0x00:         # SET_ADDRESS
            self.addr = wval & 0x7F
            return "ok"
        if bmtype == 0x09:                        # SET_CONFIGURATION
            return "ok"
        if bmtype == 0xFE and bm == 0xA1:         # GET_MAX_LUN
            self.data_in = bytes([self.max_lun])
            return "in"
        if self.hub is not None:                  # hub class requests
            return self.hub_control(bm, bmtype, breq, wval, wlen)
        return "stall"

    def hub_control(self, bm, bmtype, breq, wval, wlen):
        if bm == 0x23 and bmtype == 0x03:         # SET_PORT_FEATURE
            return "ok"
        if bm == 0x23 and bmtype == 0x01:         # CLEAR_PORT_FEATURE
            return "ok"
        if bm == 0xA3 and bmtype == 0x00:         # GET_PORT_STATUS
            # bit0 connected, bit1 connect-change, bit8 power, bit9 lowspeed,
            # bit12 reset (we clear it when the downstream device is ready)
            st = 0x0101 if self.hub else 0x0001
            if self.hub:
                st |= 0x0100
            self.data_in = struct.pack("<H", st) + b"\x00\x00"
            return "in"
        return "stall"

    # ---- BOT --------------------------------------------------------------
    def bot_cbw(self, payload):
        self.write = None
        self.cbw = None
        if len(payload) < 31 or payload[:4] != b"USBC":
            return
        tag = struct.unpack("<I", payload[4:8])[0]
        dlen = struct.unpack("<I", payload[8:12])[0]
        cb = payload[15:31]
        self.cbw = {"tag": tag, "dlen": dlen, "flags": payload[12]}
        op = cb[0]
        if op == 0x12:                            # INQUIRY
            self.data_in = SCSI_INQUIRY
        elif op == 0x25:                          # READ CAPACITY(10)
            self.data_in = scsi_read_capacity(self.nsect)
        elif op == 0x28:                          # READ(10)
            lba = struct.unpack(">I", cb[2:6])[0]
            cnt = struct.unpack(">H", cb[7:9])[0]
            self.data_in = self.disk_read(lba, cnt)
        elif op == 0x2A:                          # WRITE(10)
            lba = struct.unpack(">I", cb[2:6])[0]
            cnt = struct.unpack(">H", cb[7:9])[0]
            self.write = (lba, cnt * 512)
            self.data_in = None
        else:
            self.data_in = None
            self.csw_pending = True

    def disk_read(self, start, n):
        if start < 0 or start + n > self.nsect:
            return None
        return bytes(self.disk[start*512:(start+n)*512])

    def disk_write(self, data):
        if not self.write:
            return False
        lba, left = self.write
        if lba * 512 + len(data) > self.nsect * 512:
            return False
        self.disk[lba*512:lba*512+len(data)] = data
        self.write = None if len(data) >= left else (lba + len(data)//512,
                                                     left - len(data))
        self.csw_pending = True
        return True

    def data_out(self, payload):
        if payload[:4] == b"USBC" and self.write is None:
            self.bot_cbw(payload)
        elif self.write is not None:
            self.disk_write(payload)

    def respond(self, nbytes):
        """Payload for the next IN transfer of nbytes bytes."""
        if self.never_complete:
            return None                       # qTD hangs forever
        if self.data_in is not None:
            data, self.data_in = self.data_in, None
            self.csw_pending = True
            return data[:nbytes].ljust(min(nbytes, len(data)), b"\0")
        if self.csw_pending and nbytes <= 15:
            self.csw_pending = False
            if self.cbw:
                return scsi_csw(self.cbw["tag"])
            return scsi_csw(0)
        return b"\0" * min(nbytes, 64)


def default_disk(nsect=16384):
    """MBR (active part 1 @ LBA 63) + tiny boot sector, like bootlab."""
    disk = bytearray(nsect * 512)
    mbr = bytearray(512)
    mbr[446] = 0x80
    mbr[447:454] = bytes([0, 1, 1, 6, 15, 63, 63])
    mbr[454:458] = struct.pack("<I", 63)
    mbr[458:462] = struct.pack("<I", nsect - 1)
    mbr[510:512] = b"\x55\xAA"
    disk[0:512] = mbr
    pbs = bytearray(512)
    pbs[510:512] = b"\x55\xAA"
    disk[63*512:64*512] = pbs
    return disk


# ------------------------------------------------------------------ machine
EHCI_BASE = 0x80          # linear base of the EHCI register window
REG = {                   # register offset inside the EHCI page
    "caplength": 0x00, "hcsparams": 0x04, "hccparams": 0x08,
    "usbcmd": 0x10, "usbsts": 0x14, "usbintr": 0x18,
    "asynclistaddr": 0x28, "configflag": 0x50,
    "portsc0": 0x54, "portsc1": 0x58, "portsc2": 0x5C, "portsc3": 0x60,
}

PORT_CCS = 0x001
PORT_CSC = 0x002
PORT_PED = 0x004
PORT_PEC = 0x008
PORT_PR  = 0x100
PORT_PP  = 0x1000
PORT_LS  = 0xC00       # bits 11:10 line status
PORT_LS_J = 0x800
PORT_LS_K = 0x400
PORT_LS_SE0 = 0x000


class Ehci:
    """Virtual EHCI controller: registers, root ports, async engine."""

    def __init__(self, uc, ports, tick_ms):
        self.uc = uc
        self.nports = len(ports)
        self.ports = ports            # list of UsbDevice or None
        self.usbcmd = 0
        self.usb_irq = 0              # raised USBSTS bits (W1C model)
        self.asynclistaddr = 0
        self.tick_ms = tick_ms        # virtual ms per tick
        self.doorbells = 0
        self.qtds_done = 0
        self.last_doorbell_tick = 0
        self.pr_set_tick = [0] * 4
        self.pr_clear_tick = [0] * 4
        self.port_state = [0] * 4     # PR pending flag per port

    # ---- root port state ------------------------------------------------
    def port_read(self, idx, now_ms):
        p = self.ports[idx] if idx < len(self.ports) else None
        v = 0
        if p is not None:
            v |= PORT_CCS
        # PED grant predicate: an EHCI root port asserts PED only after
        # a successful HS chirp handshake.  A low-speed device never
        # handshakes, so PED must never set for kind == "ls" (the port
        # would be handed to a companion controller on real hardware).
        def ped_grant(p):
            return p is None or (p.kind != "ls" and p.kind != "fs"
                                 and p.reset_count >= p.ped_attempts)
        if self.port_state[idx] & 1:              # PR asserted
            if p is not None and p.pr_clear_ms > 0:
                if now_ms >= self.pr_clear_tick[idx]:
                    self.port_state[idx] &= ~1    # PR self-cleared
                    if ped_grant(p):
                        self.port_state[idx] |= 2     # PED pending
            else:
                self.port_state[idx] &= ~1
                if ped_grant(p):
                    self.port_state[idx] |= 2
        if self.port_state[idx] & 1:              # still resetting
            v |= PORT_PR                          # visible to the driver
        if self.port_state[idx] & 2:              # PED granted
            v |= PORT_PED
        if self.port_state[idx] & 8:              # LS device after reset
            v |= PORT_LS_K
        elif p is not None:
            # pre-reset line state: J (10b) for HS-capable devices,
            # K (01b) for low-speed - the driver reads these to
            # classify the device before issuing the reset
            if p.kind == "ls":
                v |= PORT_LS_K
            else:
                v |= PORT_LS_J  # J = 10b (bit11) - HS idle
        v |= PORT_PP
        return v

    def port_write(self, idx, value):
        """Capture PORTSC writes: PR assert/deassert, CSC ack, PP."""
        now = self.ticks_ms()
        p = self.ports[idx] if idx < len(self.ports) else None
        if value & PORT_PR:
            self.port_state[idx] |= 1
            self.pr_set_tick[idx] = now
            if p is not None:
                # The real clear timer starts when the driver deasserts
                # with the 0x1000 write below; this is only a safety
                # fallback so PR never sticks if the driver misbehaves.
                self.pr_clear_tick[idx] = max(self.pr_clear_tick[idx],
                                              now + 10000)
                p.reset_count += 1
                # PED is withheld for the first (ped_attempts - 1)
                # resets - models a device whose HS handshake needs
                # several reset cycles (case 5 / A1)
        if not (value & PORT_PR) and p is not None and (self.port_state[idx] & 1):
            # 0x1000 write = deassert request: start the device's
            # self-clear timer (A2 polls this bit).  Runs on ANY write
            # that clears PR while PR is asserted - the driver's
            # Port_Reset does exactly this (0x1100 then 0x1000).
            self.pr_clear_tick[idx] = now + max(1, p.pr_clear_ms)
        if value & PORT_CSC:
            self.port_state[idx] &= ~4

    def ticks_ms(self):
        return self.tick_ms * self._tick_count()

    _tick_count = None

    # ---- async engine ----------------------------------------------------
    def process_async(self):
        """Run the QH/qTD list after a doorbell; raise USBINT|IAA."""
        u = self.uc
        self.doorbells += 1
        al = self.asynclistaddr
        if not al or al & 1:
            self.usb_irq |= 0x21
            return
        q = al & ~0x1F
        for _ in range(8):
            qh = [int.from_bytes(u.mem_read(q + 4*i, 4), "little")
                  for i in range(12)]
            cur = qh[3]
            t = cur if cur and not (cur & 1) else 0
            last = 0
            steps = 0
            while t and not (t & 1) and steps < 8:
                last = t
                t = self.exec_qtd(t)
                steps += 1
            if last:
                td = [int.from_bytes(u.mem_read(last + 4*i, 4), "little")
                      for i in range(8)]
                u.mem_write(q + 0x0C, struct.pack("<I", 0))       # cur=0
                u.mem_write(q + 0x10, struct.pack("<I", 1))       # next term
                u.mem_write(q + 0x14, struct.pack("<I", 1))       # alt term
                u.mem_write(q + 0x18, struct.pack("<I", td[2]))   # token
            self.usb_irq |= 0x21
            q = qh[0] & ~0x1F
            if qh[0] & 1 or q == al or q == 0:
                break

    def exec_qtd(self, addr):
        """Execute one active qTD (token bit7); returns next pointer."""
        u = self.uc
        td = [int.from_bytes(u.mem_read(addr + 4*i, 4), "little")
              for i in range(8)]
        token = td[2]
        if not (token & 0x80):
            return td[0]
        pid = (token >> 8) & 3
        nbytes = (token >> 16) & 0x7FFF
        buf0 = td[3]
        dev = self.active_device()
        if dev is None:
            # no device: complete with error so the driver's status path
            # is exercised (halted qTD)
            token = (token & ~0xFF) ^ 0x40
            u.mem_write(addr + 8, struct.pack("<I", token))
            return td[0]
        if dev.never_complete:
            return td[0]                    # leave active: driver times out
        if pid == 2:                        # SETUP
            dev.setup = bytes(u.mem_read(buf0, 8))
            r = dev.control(dev.setup)
            if r == "stall":
                token = (token & ~0xFF) ^ 0x40   # halted
            else:
                token = (token & ~0xFF) ^ 0x80000000
        elif pid == 1:                      # IN
            data = dev.respond(nbytes)
            if data is None:
                return td[0]
            u.mem_write(buf0, data[:nbytes])
            token = (token & ~0xFF) ^ 0x80000000
        else:                               # OUT
            payload = bytes(u.mem_read(buf0, min(nbytes, 31)))
            dev.data_out(payload)
            token = (token & ~0xFF) ^ 0x80000000
        u.mem_write(addr + 8, struct.pack("<I", token))
        self.qtds_done += 1
        return td[0]

    def active_device(self):
        """The device currently being addressed: root port 0 unless the
        address assignment tells us otherwise."""
        for p in self.ports:
            if p is not None:
                return p
        return None


class Machine:
    """Unicorn x86-16 environment running the driver block."""

    def __init__(self, case):
        self.case = case
        self.img = open(IMG, "rb").read()
        self.syms = load_syms()
        self.disk = bytearray(case.get("disk") or default_disk())
        self.tick_insns = case.get("tick_insns", 2000)  # "CPU speed"
        self.tick_ms = 55.0                     # BIOS tick period: 1 tick = 55ms
        self.ticks = 0
        self.insns = 0
        self.cf8_units = 0
        self.port_irq = 0
        self.done = False
        self.stop_reason = "max_insns"
        self.notes = []
        self.doorbell_ticks = []
        self.trail = []

        # ---- build the virtual devices per root port ----------------------
        self.ehci = Ehci(self.uc if hasattr(self, "uc") else None,
                         self._make_ports(), self.tick_ms)

    def _make_ports(self):
        ports = []
        disk = self.disk
        for spec in self.case.get("ports", [None]):
            if spec is None:
                ports.append(None)
                continue
            kind = spec.get("kind", "hs")
            if kind == "none":
                ports.append(None)
                continue
            hub = None
            if kind == "hub":
                hub = []
                for s in spec.get("downstream", []):
                    hub.append(UsbDevice(disk, kind=s.get("kind", "hs"),
                                         mps0=s.get("mps0", 64),
                                         max_lun=s.get("max_lun", 0),
                                         pr_clear_ms=s.get("pr_clear_ms", 0),
                                         ped_after_reset=s.get("ped_after_reset", True),
                                         ped_attempts=s.get("ped_attempts", 1),
                                         stall_once=s.get("stall_once", False),
                                         never_complete=s.get("never_complete", False)))
            d = UsbDevice(disk, kind=kind,
                          mps0=spec.get("mps0", 64),
                          max_lun=spec.get("max_lun", 0),
                          pr_clear_ms=spec.get("pr_clear_ms", 0),
                          ped_after_reset=spec.get("ped_after_reset", True),
                          ped_attempts=spec.get("ped_attempts", 1),
                          stall_once=spec.get("stall_once", False),
                          never_complete=spec.get("never_complete", False),
                          hub=hub)
            ports.append(d)
        while len(ports) < self.case.get("n_ports", 1):
            ports.append(None)
        return ports[:4]

    # ---- unicorn setup ----------------------------------------------------
    def setup(self):
        uc = Uc(UC_ARCH_X86, UC_MODE_16)
        self.uc = uc
        self.ehci.uc = uc
        self.ehci.mach = self
        Ehci._tick_count = lambda e: e.mach.ticks
        uc.mem_map(0, 0x400000)                  # 4MB: conv + 3MB code
        img = bytearray(self.img)
        # Init_cs immediate (segment 0x8000) -> 0x7000: the
        # harness has no grub4dos C environment at 0x80000;
        # Disk_Info scratch maps into free hole RAM.  Position-
        # independent: locate the `movw $0x8000, %cs:[disp16]`
        # encoding (c7 06 disp16 00 80) wherever the build puts it.
        import re as _re
        m2 = _re.search(rb"\xc7\x06..\x00\x80", img)
        assert m2, "Init_cs immediate not found"
        p = m2.start() + 4
        img[p:p + 2] = b"\x00\x70"
        uc.mem_write(IMG_BASE, bytes(img))
        # stub return sentry at 0x1000:0 (hlt)
        uc.mem_write(0x10000, b"\xF4")
        # BDA: 640KB conventional, device word, hd count, boot drive mark
        uc.mem_write(0x413, struct.pack("<H", 640))
        uc.mem_write(0x410, struct.pack("<H", 0))
        uc.mem_write(0x475, struct.pack("<B", 0))
        uc.mem_write(0x46C, struct.pack("<I", 0))
        # boot-drive marker read by Get_dri (fs:0x8280)
        off = img_off(0x8280)
        if off is not None:
            uc.mem_write(IMG_BASE + off, b"\x80")
        # old-BIOS int13 chain trampoline at 0xF100:0 = `int 0xFE; retf 2`
        uc.mem_write(0xF1000, b"\xCD\xFE\xCA\x02\x00")
        uc.mem_write(0x4C, struct.pack("<HH", 0x0000, 0xF100))
        # EHCI register page (materialized values live in RAM at 0x80..)
        regs = bytearray(0x100)
        regs[REG["caplength"]] = 0x10            # CAPLENGTH = 16
        struct.pack_into("<I", regs, REG["hcsparams"],
                         0x00010002)             # 1 port, PPC
        hcc = self.case.get("hccparams", 0)      # 0: no ECP (case 8)
        struct.pack_into("<I", regs, REG["hccparams"], hcc)
        uc.mem_write(EHCI_BASE, bytes(regs))
        # config space: ECP chain at 0x50 for LEGSUP cases
        self._cfg = bytearray(0x100)
        struct.pack_into("<I", self._cfg, 0x00, 0x70208086)
        struct.pack_into("<I", self._cfg, 0x08, 0x000C0320)
        struct.pack_into("<I", self._cfg, 0x10, EHCI_BASE)   # BAR0
        if hcc:
            self._cfg[0x50] = 0x01               # cap ID = 1 (LEGSUP)
            self._cfg[0x52] = 1 if self.case.get("bios_owned", 0) else 0
        # second controller for case 14
        self._cfg2 = None
        if self.case.get("dual_ehci", False):
            self._cfg2 = bytearray(0x100)
            struct.pack_into("<I", self._cfg2, 0x00, 0x1234ABCD)
            struct.pack_into("<I", self._cfg2, 0x08, 0x000C0320)
            struct.pack_into("<I", self._cfg2, 0x10, 0x0000F000)

    # ---- BIOS stubs --------------------------------------------------------
    def int13(self):
        uc, ah = self.uc, self.reg(X86_REG_AX) >> 8
        dl = self.reg(X86_REG_DX) & 0xFF
        if ah == 0x02:      # read sectors CHS
            al = self.reg(X86_REG_AX) & 0xFF
            ch = (self.reg(X86_REG_CX) >> 8) & 0xFF
            cl = self.reg(X86_REG_CX) & 0xFF
            dh = (self.reg(X86_REG_DX) >> 8) & 0xFF
            cyl = ((ch << 8) | (cl >> 6)) & 0x3FF
            sec = cl & 0x3F
            lba = (cyl * 16 + dh) * 63 + (sec - 1)
            es, bx = self.reg(X86_REG_ES), self.reg(X86_REG_BX)
            data = self.disk_read(lba, al)
            if data is not None:
                self.wr(es, bx, data)
                self.cf(0)
                self.set(X86_REG_AX, al)
            else:
                self.cf(1)
                self.set(X86_REG_AX, 0x0400)
            return
        if ah == 0x03:      # write sectors CHS (no-op old BIOS)
            self.cf(0)
            self.set(X86_REG_AX, 0)
            return
        if ah == 0x08:
            self.set(X86_REG_CX, (1023 << 8) | 63)
            self.set(X86_REG_DX, (15 << 8) | dl)
            self.set(X86_REG_AX, 0)
            self.cf(0)
            return
        if ah == 0x41:
            self.set(X86_REG_BX, 0xAA55)
            self.set(X86_REG_CX, 0x0001)
            self.cf(0)
            return
        if ah == 0x42:      # extended read
            ds, si = self.reg(X86_REG_DS), self.reg(X86_REG_SI)
            pkt = self.rd(ds, si, 16)
            cnt = pkt[2]
            f1 = struct.unpack("<H", pkt[4:6])[0]
            f2 = struct.unpack("<H", pkt[6:8])[0]
            lba = struct.unpack("<Q", pkt[8:16])[0] & 0xFFFFFFFF
            data = self.disk_read(lba, cnt)
            if data is not None:
                self.wr(f2, f1, data)
                self.set(X86_REG_AX, 0)
                self.cf(0)
            else:
                self.set(X86_REG_AX, 0x0400)
                self.cf(1)
            return
        if ah == 0x48:      # EDD params
            ds, si = self.reg(X86_REG_DS), self.reg(X86_REG_SI)
            base = self.lin(ds, si)
            req = self.uc.mem_read(base, 1)[0]
            size_out = 0x42 if req >= 0x42 else 0x1E
            self.uc.mem_write(base, bytes([size_out]))
            self.uc.mem_write(base + 1, bytes([0]))
            self.uc.mem_write(base + 2, struct.pack("<I", 1023))
            self.uc.mem_write(base + 6, struct.pack("<I", 16))
            self.uc.mem_write(base + 10, struct.pack("<I", 63))
            self.uc.mem_write(base + 14, struct.pack("<I", 512))
            self.uc.mem_write(base + 22, struct.pack("<Q", self.ehci.ports[0].nsect if self.ehci.ports[0] else 0))
            self.uc.mem_write(base + 0x28, b"USB ")
            self.set(X86_REG_AX, 0)
            self.cf(0)
            return
        self.cf(1)
        self.set(X86_REG_AX, 0x0100)

    def int15(self):
        ah = self.reg(X86_REG_AX) >> 8
        if ah == 0x24:      # A20 gate: ok
            self.set(X86_REG_AX, 0)
            self.cf(0)
            return
        if ah == 0x88:
            self.set(X86_REG_AX, 0x1000)
            self.cf(0)
            return
        self.cf(1)
        self.set(X86_REG_AH, 0x86)

    # ---- PCI BIOS ----------------------------------------------------------
    def pci_bios(self):
        ax = self.reg(X86_REG_AX)
        al = ax & 0xFF
        if al == 0x01:      # B101
            self.set(X86_REG_AX, 0x0000)
            self.uc.reg_write(X86_REG_EDX, 0x20494350)
            self.set(X86_REG_BX, 0x0201)
            self.set(X86_REG_CX, 0x0004)
            self.cf(0)
            return
        if al == 0x03:      # B103 find by class 0x0C0320
            want = self.reg(X86_REG_ECX) & 0xFFFFFF
            idx = self.reg(X86_REG_SI) & 0xFFFF
            devs = []
            if want == 0x0C0320:
                devs = [0x10]
                if self.case.get("dual_ehci", False):
                    devs.append(0x12)
            if idx < len(devs):
                self.set(X86_REG_BX, devs[idx] << 3)
                self.set(X86_REG_AX, 0x0000)
                self.cf(0)
                return
            self.set(X86_REG_AH, 0x86)
            self.cf(1)
            return
        if al in (0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D):
            dev = (self.reg(X86_REG_BX) & 0xFF) >> 3
            reg = self.reg(X86_REG_DI) & 0xFF
            cfg = self._cfg if dev == 0x10 else (self._cfg2 if dev == 0x12 else None)
            if cfg is None:
                self.set(X86_REG_AH, 0x86)
                self.cf(1)
                return
            if al == 0x08:
                self.set(X86_REG_CL, cfg[reg])
                self.set(X86_REG_AX, 0)
                self.cf(0)
            elif al == 0x09:
                v = struct.unpack_from("<H", cfg, reg & ~1)[0]
                self.set(X86_REG_CX, v)
                self.set(X86_REG_AX, 0)
                self.cf(0)
            elif al == 0x0A:
                v = struct.unpack_from("<I", cfg, reg & ~3)[0]
                self.uc.reg_write(X86_REG_ECX, v)
                self.set(X86_REG_AX, 0)
                self.cf(0)
            elif al == 0x0B:
                struct.pack_into("<B", cfg, reg, self.reg(X86_REG_CL))
                self.set(X86_REG_AX, 0)
                self.cf(0)
            elif al == 0x0C:
                struct.pack_into("<H", cfg, reg & ~1, self.reg(X86_REG_CX))
                self.set(X86_REG_AX, 0)
                self.cf(0)
            else:
                struct.pack_into("<I", cfg, reg & ~3,
                                 self.uc.reg_read(X86_REG_ECX))
                self.set(X86_REG_AX, 0)
                self.cf(0)
            return
        self.set(X86_REG_AH, 0x81)
        self.cf(1)

    # ---- hooks -------------------------------------------------------------
    def on_intr(self, uc, intno, ud=None):
        if intno == 0x1A:
            self.pci_bios()
            return
        if intno == 0x13:
            v = self.uc.mem_read(4 * 0x13, 4)
            off, seg = struct.unpack("<HH", v)
            if seg or off:
                self.far_call(seg, off)
                return
            self.int13()
            return
        if intno == 0xFE:   # old-BIOS chain trampoline
            # Unicorn advanced IP past `int 0xFE` without pushing an
            # interrupt frame, but the trampoline's `retf 2` expects one
            # (hardware pushes FLAGS,CS,IP).  Build the frame ourselves,
            # service int13 inline, patch the returned flags into the
            # frame, then pop it (manual iret).
            u = self.uc
            ss = self.reg(X86_REG_SS)
            sp = self.reg(X86_REG_SP)
            ip = self.reg(X86_REG_IP)
            cs = self.reg(X86_REG_CS)
            fl = self.reg(X86_REG_FLAGS) | 0x2
            self.wr(ss, sp - 6, struct.pack("<HHH", ip, cs, fl))
            self.set(X86_REG_SP, (sp - 6) & 0xFFFF)
            self.int13()
            self.do_iret()
            return
        if intno == 0x15:
            self.int15()
            return
        if intno == 0x10:   # video: accept everything, no output
            self.cf(0)
            return
        if intno == 0x16:   # keyboard: "no key"
            self.cf(1)
            self.set(X86_REG_AX, 0)
            return
        raise UnknownInt(intno,
                         (self.reg(X86_REG_CS), self.reg(X86_REG_IP),
                          self.reg(X86_REG_AX)))

    def far_call(self, vcs, vip):
        uc = self.uc
        sp = self.reg(X86_REG_SP)
        ss = self.reg(X86_REG_SS)
        ip = self.reg(X86_REG_IP)
        cs = self.reg(X86_REG_CS)
        fl = self.reg(X86_REG_FLAGS) | 0x2
        fl &= ~0x100
        frame = struct.pack("<HHH", ip, cs, fl)
        self.wr(ss, sp - 6, frame)
        self.set(X86_REG_SP, (sp - 6) & 0xFFFF)
        self.set(X86_REG_CS, vcs)
        self.set(X86_REG_IP, vip)

    # ---- helpers -----------------------------------------------------------
    def reg(self, n):   return self.uc.reg_read(n)
    def set(self, n, v): self.uc.reg_write(n, v & 0xFFFF)
    def lin(self, seg, off): return ((seg << 4) + off) & 0xFFFFF
    def cf(self, v):
        f = self.reg(X86_REG_FLAGS)
        f = (f | 1) if v else (f & ~1)
        self.set(X86_REG_FLAGS, f)
    def rd(self, seg, off, n): return bytes(self.uc.mem_read(self.lin(seg, off), n))
    def wr(self, seg, off, data): self.uc.mem_write(self.lin(seg, off), data)
    def disk_read(self, start, n):
        if start < 0 or start + n > len(self.disk) // 512:
            return None
        return bytes(self.disk[start*512:(start+n)*512])

    # ---- register-page hooks ----------------------------------------------
    def hook_mmio_read(self, uc, access, address, size, value, ud):
        # window base 0x00 (ES=0x08 has base 0 in unicorn); EHCI page
        # register offsets are relative to EHCI_BASE=0x80 in the model
        off = address - EHCI_BASE   # window base 0x80 = Register_pci
        if self.case.get("mmio_log"):
            u = self.uc
            self.notes.append("R 0x%02X (esi=0x%X es=0x%X)" % (
                off, u.reg_read(X86_REG_ESI), u.reg_read(X86_REG_ES)))
        if 0x54 <= off <= 0x64:                     # PORTSC window
            idx = (off - 0x54) // 4
            if idx >= len(self.ehci.ports):
                return False
            v = self.ehci.port_read(idx, self.ticks * self.tick_ms)
            if self.case.get("pr_trace") and idx == 0 and (v & 0x100):
                self.notes.append("PR still set now=%.0f clear=%.0f" % (
                    self.ticks * self.tick_ms, self.ehci.pr_clear_tick[0]))
            uc.mem_write(address, struct.pack("<I", v))
            return False
        if off == REG["usbsts"]:
            halted = 0x1000 if not (self.ehci.usbcmd & 1) else 0
            uc.mem_write(address,
                         struct.pack("<I", halted | self.ehci.usb_irq))
            return False
        if off == REG["usbcmd"]:
            v = struct.unpack("<I", uc.mem_read(address, 4))[0] & ~2
            uc.mem_write(address, struct.pack("<I", v))
            return False
        return False

    def hook_mmio_write(self, uc, access, address, size, value, ud):
        off = address - EHCI_BASE   # window base 0x80 = Register_pci
        if self.case.get("mmio_log"):
            u = self.uc
            self.notes.append("W 0x%02X=0x%X (esi=0x%X)" % (
                off, value, u.reg_read(X86_REG_ESI)))
        if off == REG["usbsts"]:
            self.ehci.usb_irq &= ~value & 0xFFFF    # W1C
            return True
        if off == REG["usbcmd"]:
            self.ehci.usbcmd = value & 0x1F
            if value & 0x24:
                self.doorbell_ticks.append(self.ticks)
                self.ehci.process_async()
            return True
        if off == REG["asynclistaddr"]:
            self.ehci.asynclistaddr = value & ~1
            return True
        if 0x54 <= off <= 0x64:
            idx = (off - 0x54) // 4
            if idx >= len(self.ehci.ports):
                return True
            self.ehci.port_write(idx, value)
            return True
        return False

    # ---- port I/O ----------------------------------------------------------
    def hook_port(self, uc, address, size, value, ud):
        """Handle IN/OUT instructions by decoding the opcode at CS:IP."""
        cs = self.reg(X86_REG_CS)
        ip = self.reg(X86_REG_EIP)
        l = ((cs << 4) + ip) & 0xFFFFF
        try:
            b0 = uc.mem_read(l, 1)[0]
        except Exception:
            return
        wide, ilen = False, 1
        if b0 == 0x66:
            try:
                b1 = uc.mem_read(l + 1, 1)[0]
            except Exception:
                return
            if b1 not in (0xED, 0xEF):
                return
            op, wide, ilen = b1, True, 2
        elif b0 in (0xEC, 0xED, 0xEE, 0xEF):
            op = b0
        else:
            return
        dx = self.reg(X86_REG_EDX) & 0xFFFF
        if op in (0xEC, 0xED):          # IN
            v = 0
            if dx == 0xCF8:
                # Delay() busy-waits here: Count_1ms CF8 reads = 1ms.
                # Drive the virtual tick from the CF8 count so Delay(ms)
                # consumes a real (virtual) ms.  Count_1ms is read from
                # the block data (set by Determine_delay_units).
                self.cf8_units += 1
                c1 = self.c1ms()
                if c1 and self.cf8_units >= c1:
                    self.cf8_units = 0
                    self.bump_tick()
            uc.reg_write(X86_REG_EAX, v)
        else:                            # OUT
            pass
        uc.reg_write(X86_REG_EIP, ip + ilen)

    def c1ms(self):
        """Runtime Count_1ms (CF8 reads per ms), read from the block
        data where Determine_delay_units stored it."""
        try:
            import struct as _s
            return _s.unpack("<H", bytes(self.uc.mem_read(0xC7A0 + 0x18, 2)))[0] or 128
        except Exception:
            return 128

    def bump_tick(self):
        self.ticks += 1
        t = int.from_bytes(self.uc.mem_read(0x46C, 4), "little")
        self.uc.mem_write(0x46C, struct.pack("<I", (t + 1) & 0xFFFFFFFF))

    # ---- code hook ----------------------------------------------------------
    def hook_code(self, uc, addr, size, ud):
        if self.done:
            return
        if addr == 0x6F0C:               # sentry hlt
            self.done = True
            self.stop_reason = "sentry"
            return
        self.insns += 1
        if self.case.get("trace"):
            self.trail.append(addr)
            if len(self.trail) > 200:
                self.trail.pop(0)
        if self.insns >= self.case.get("max_insns", 60_000_000):
            self.done = True
            self.stop_reason = "max_insns"
            return
        if self.tick_insns and (self.insns % self.tick_insns) == 0:
            self.bump_tick()
        try:
            b0 = uc.mem_read(addr, 2)
        except Exception:
            return
        # Unicorn (16-bit mode) raises #GP on control-register loads/stores
        # and cannot honour lgdt; the driver's real<->protected excursion
        # (Protected_Mode) only needs them to be side-effect free because
        # segment addressing is already "linear = seg<<4 + off" here.
        if b0[0] == 0x0F and b0[1] in (0x20, 0x21, 0x22, 0x23, 0x01):
            modrm = uc.mem_read(addr + 2, 1)[0]
            cs = uc.reg_read(X86_REG_CS)
            ip = addr - ((cs << 4) & 0xFFFFF)
            if b0[1] == 0x01 and (modrm & 0xF8) in (0x10, 0x18):  # lgdt/lidt
                uc.reg_write(X86_REG_EIP, (ip + 6) & 0xFFFF)
                return
            if b0[1] in (0x20, 0x21, 0x22, 0x23):                # CR r/w
                # 0F 20/22 + modrm = 3 bytes.  Skipped, not emulated:
                # pushl/popl around them keep the stack balanced and CR
                # values are never consumed by the driver.
                uc.reg_write(X86_REG_EIP, (ip + 3) & 0xFFFF)
                return
                # `DATA32 ret` (66 c3) in 16-bit mode = 32-bit NEAR return: pop a
        # dword into EIP (CS unchanged).  The callers of prot_to_real run
        # in .code32, so their `call` pushed a 4-byte return address that
        # this ret consumes.  Unicorn mis-executes it; do it manually.
        if b0[0] == 0x66 and b0[1] == 0xC3 and uc.reg_read(X86_REG_CS) != 0:
            # DATA32 ret in a 16-bit (real-mode) segment: 32-bit near
            # ret, popping the 4-byte return address pushed by the
            # .code32 caller of prot_to_real.  In PM (CS==0) unicorn
            # handles the operand-size-prefixed ret natively.
            u = self.uc
            ss = self.reg(X86_REG_SS)
            sp = self.reg(X86_REG_SP)
            eip = struct.unpack("<I", u.mem_read(ss * 16 + (sp & 0xFFFF), 4))[0]
            self.set(X86_REG_SP, (sp + 4) & 0xFFFF)
            self.set(X86_REG_EIP, eip)
            return
        # mov sreg, r/m16 (8E /r): unicorn's 16-bit segment cache uses
        # real-mode-style bases (selector<<4).  The driver's PM data
        # selector 0x10 (PM_DS32, base 0) must map to base 0 -> rewrite
        # it to selector 0.  ES=0x08 (the EHCI window in Protected_Mode)
        # already lands at base 0 in unicorn, which our MMIO hooks at
        # 0x00..0x100 cover.
        if b0[0] == 0x8E:
            modrm = uc.mem_read(addr + 1, 1)[0]
            reg = (modrm >> 3) & 7
            if reg in (0, 1, 2, 3, 5) and (modrm & 0xC7) == 0xC0:
                val = uc.reg_read(X86_REG_AX + (modrm & 7))
                if val in (0x10, 0x18, 0x08):
                    cs = uc.reg_read(X86_REG_CS)
                    ip = addr - ((cs << 4) & 0xFFFFF)
                    # All PM data selectors (0x08/0x10/0x18) have base 0
                    # in grub4dos' GDT - map them to selector 0 so the
                    # unicorn real-mode-style cache also gives base 0.
                    # The harness stack is SS=0/SP flat-linear, matching
                    # the driver's PM (base 0) and real-mode (SS=0)
                    # stack convention.
                    self.set(X86_REG_EAX + (modrm & 7), 0)
                    uc.reg_write(X86_REG_EIP, (ip + 2) & 0xFFFF)
                    self.load_sreg(reg, 0)
                    return
        # ljmp sel16,off16 (EA): unicorn refuses to load a NULL selector
        # (CS=0) from the instruction - `ljmp $0,$realcseg` (used by the
        # startup and every mode switch) silently falls through, leaving
        # CS at the stale selector and shifting all real-mode addressing.
        # Emulate the 16-bit far jump manually.
        if b0[0] == 0xEA:
            off = struct.unpack("<H", uc.mem_read(addr + 1, 2))[0]
            sel = struct.unpack("<H", uc.mem_read(addr + 3, 2))[0]
            # Unicorn's 16-bit segment cache uses real-mode-style bases
            # (selector<<4).  Map grub4dos' GDT selectors onto that:
            #   0x28 = PM_CS32 (base 0, 4GB)   -> CS 0
            #   0x18 = PM_CS16 (int13 base)    -> int13 segment
            #   0x08 = PM_DS16 (data window)   -> handled on ES (below)
            if sel == 0x28:
                self.set(X86_REG_CS, 0)
            elif sel == 0x18:
                self.set(X86_REG_CS, 0x820)
            else:
                self.set(X86_REG_CS, sel)
            self.set(X86_REG_IP, off)
            return
        # lret/retf (0xCB) pops ip+cs (2x16); iret (0xCF) pops ip+cs+flags
        # (3x16); retf imm16 (0xCA) pops ip+cs then skips imm16.  Unicorn
        # decodes all of them as 32-bit far returns, which skews the 16-bit
        # frames - execute the 16-bit semantics manually.
        if b0[0] == 0xCA:
            self.do_retf()
            self.set(X86_REG_SP, (self.reg(X86_REG_SP) + 2) & 0xFFFF)
            return
        if b0[0] == 0xCB:
            self.do_retf()
            return
        if b0[0] == 0xCF:
            self.do_iret()
            return
        # rep/lock prefixes: pause (f3 90) is a plain delay -> skip.  Real string
# ops (f3 a4/a5/a6/a7/aa/ab) must be EMULATED: the startup `repz movsl`
# performs the 3MB de-squeeze copy of the 32-bit code (see build squeeze);
# skipping it leaves the destination zero and the boot dies in the hole.
        if b0[0] == 0x66 and b0[1] in (0xF2, 0xF3):
            op = uc.mem_read(addr + 2, 1)[0]
            if op in (0xA5, 0xA7, 0xAB):
                self.emu_rep(op, dword=True)
                return
        if b0[0] in (0xF2, 0xF3):
            op = uc.mem_read(addr + 1, 1)[0]
            if op in (0xA4, 0xA5, 0xA6, 0xA7, 0xAA, 0xAB):
                self.emu_rep(op)
                return
            cs = uc.reg_read(X86_REG_CS)
            ip = addr - ((cs << 4) & 0xFFFFF)
            uc.reg_write(X86_REG_EIP, (ip + 2) & 0xFFFF)   # prefix + op
            return
        if b0[0] == 0xF0:
            cs = uc.reg_read(X86_REG_CS)
            ip = addr - ((cs << 4) & 0xFFFFF)
            uc.reg_write(X86_REG_EIP, (ip + 1) & 0xFFFF)
            return
        # port I/O decode (UC_HOOK_INSN unreliable in this build)
        if b0[0] in (0xEC, 0xED, 0xEE, 0xEF) or b0[0] == 0x66:
            self.hook_port(uc, addr, size, 0, None)

    def emu_rep(self, op, dword=False):
        """Emulate one 16-bit rep-prefixed string instruction (ESI/EDI are
        linear in the flat model, CX counts words for the 32-bit forms)."""
        u = self.uc
        # Manual string ops must honor the segment bases (real-mode
        # style: DS/ES << 4); unicorn's own segment translation is
        # bypassed here because we drive the registers ourselves.
        ds_base = (u.reg_read(X86_REG_DS) & 0xFFFF) << 4
        es_base = (u.reg_read(X86_REG_ES) & 0xFFFF) << 4
        esi = ds_base + (u.reg_read(X86_REG_ESI) & 0xFFFF)
        edi = es_base + (u.reg_read(X86_REG_EDI) & 0xFFFF)
        ecx = u.reg_read(X86_REG_ECX) & 0xFFFF
        if ecx > 0x10000:      # corrupted count: don't loop into the void
            ecx = 0
        # 16-bit operand size: A5/A7/AB are WORD forms (movsw/cmpsw/
        # stosw); the o32 startup copy uses `66 f3 a5` (movsl) which is
        # handled by the operand-size prefix check below.
        wide = op in (0xA5, 0xA7, 0xAB)
        step = 4 if (wide and dword) else (2 if wide else 1)
        dflag = 0 if (u.reg_read(X86_REG_FLAGS) & 0x400) else 1
        sdir = step if dflag else -step
        if op == 0xA4:                       # movsb
            for _ in range(ecx):
                u.mem_write(edi, bytes(u.mem_read(esi, 1)))
                esi += sdir
                edi += sdir
        elif op == 0xA5:                     # movsd/movsw (o32/16-bit)
            for _ in range(ecx):
                u.mem_write(edi, bytes(u.mem_read(esi, step)))
                esi += sdir
                edi += sdir
        elif op == 0xAA:                     # stosb
            al = u.reg_read(X86_REG_EAX) & 0xFF
            for _ in range(ecx):
                u.mem_write(edi, bytes([al]))
                edi += sdir
        elif op == 0xAB:                     # stosw / stosd (o32)
            if step == 4:
                v = struct.pack("<I", u.reg_read(X86_REG_EAX))
            else:
                v = struct.pack("<H", u.reg_read(X86_REG_AX))
            for _ in range(ecx):
                u.mem_write(edi, v)
                edi += sdir
        elif op in (0xA6, 0xA7):             # cmpsb/cmpsd: run until diff
            for _ in range(ecx):
                a = u.mem_read(esi, step)
                b = u.mem_read(edi, step)
                esi += sdir
                edi += sdir
                if a != b:
                    ecx = ecx - _ - 1
                    # set ZF from compare result
                    f = self.reg(X86_REG_FLAGS)
                    f = (f | 0x40) if a == b else (f & ~0x40)
                    self.set(X86_REG_FLAGS, f)
                    break
        u.reg_write(X86_REG_ESI, (esi - ds_base) & 0xFFFF)
        u.reg_write(X86_REG_EDI, (edi - es_base) & 0xFFFF)
        u.reg_write(X86_REG_ECX, ecx & 0xFFFF)
        # EIP register already holds the segment-relative offset; the
        # rep instruction is prefix(1)+opcode(1) (2 bytes; o32 form is
        # handled by the 66 branch which skips 3 via its own path).
        ip = u.reg_read(X86_REG_EIP)
        u.reg_write(X86_REG_EIP, (ip + 2) & 0xFFFF)

    def load_sreg(self, regidx, value):
        """Load DS/ES/FS/GS/SS (regidx per 8E /r encoding) with value."""
        m = {0: X86_REG_ES, 1: X86_REG_DS, 2: X86_REG_FS,
             3: X86_REG_GS, 5: X86_REG_SS}
        self.uc.reg_write(m[regidx], value)

    def do_retf(self):
        """16-bit far return: pop IP, CS (2x16).  After a push, SP points
        AT the frame, so the words live at [SP] and [SP+2] (pop is upward)."""
        u = self.uc
        ss = self.reg(X86_REG_SS)
        sp = self.reg(X86_REG_SP)
        ip = struct.unpack("<H", u.mem_read(ss * 16 + sp, 2))[0]
        cs = struct.unpack("<H", u.mem_read(ss * 16 + sp + 2, 2))[0]
        self.set(X86_REG_SP, (sp + 4) & 0xFFFF)
        self.set(X86_REG_CS, cs)
        self.set(X86_REG_IP, ip)

    def do_iret(self):
        """16-bit interrupt return: pop IP, CS, FLAGS (3x16) at [SP..SP+4]."""
        u = self.uc
        ss = self.reg(X86_REG_SS)
        sp = self.reg(X86_REG_SP)
        ip = struct.unpack("<H", u.mem_read(ss * 16 + sp, 2))[0]
        cs = struct.unpack("<H", u.mem_read(ss * 16 + sp + 2, 2))[0]
        fl = struct.unpack("<H", u.mem_read(ss * 16 + sp + 4, 2))[0]
        self.set(X86_REG_SP, (sp + 6) & 0xFFFF)
        self.set(X86_REG_CS, cs)
        self.set(X86_REG_IP, ip)
        f = self.reg(X86_REG_FLAGS)
        f = (f & ~0x100) | (fl & 0x100)          # restore IF from frame
        self.set(X86_REG_FLAGS, f)

    # ---- run ----------------------------------------------------------------
    def run(self, entry_off=None):
        self.setup()
        u = self.uc
        u.reg_write(X86_REG_CS, 0x820)
        u.reg_write(X86_REG_IP, 0)
        u.reg_write(X86_REG_DS, 0x820)
        u.reg_write(X86_REG_ES, 0x820)
        # SS=0 (base 0): the driver's PM (SS=0x10) and real-mode (SS=0)
        # stacks both sit at linear SS:SP with base 0, so the harness
        # stack must live in the same flat space.  SP=0x7FF0 grows down
        # into free RAM below the image (0x8200) and above STACKOFF.
        u.reg_write(X86_REG_SS, 0)
        # 0x6F00: below the driver's STACKOFF (0x7000..) so the two
        # stacks never interleave; above our int13 buffers (0x3400)
        u.reg_write(X86_REG_SP, 0x6F00)
        u.reg_write(X86_REG_FS, 0)
        u.reg_write(X86_REG_FLAGS, 0x0002)
        # far-call to the block entry (mimics init_usb's lcall):
        # push return frame -> sentry at 0:0x7FFC
        u.mem_write(0x6EFA, struct.pack("<HHH", 0x6F0C, 0, 0x2))
        u.reg_write(X86_REG_SP, 0x6EFA)
        # The block is a CS-relative world: CS base must equal the
        # USB2DRI block VMA (all %cs:(x - USB2DRI) displacements and
        # ds_Linear = DS<<4 assume it), IP = offset inside the block.
        usb2dri = self.syms.get("USB2DRI", IMG_BASE)
        block_seg = usb2dri >> 4
        entry = entry_off or (self.syms.get("Initialization", 0) - usb2dri)
        u.reg_write(X86_REG_CS, block_seg)
        u.reg_write(X86_REG_IP, entry & 0xFFFF)
        u.reg_write(X86_REG_DS, block_seg)
        u.reg_write(X86_REG_ES, block_seg)

        u.hook_add(UC_HOOK_INTR, self.on_intr)
        # hook ALL low memory: the 0xF100:0 old-BIOS trampoline lives
        # outside the image, and its `retf 2` must run through our 16-bit
        # emulation (a native 32-bit retf there loads a garbage CS).
        u.hook_add(UC_HOOK_CODE, self.hook_code, begin=0x0000, end=0x3FFFFF)
        # MMIO read/write hooks over the EHCI register page
        # MMIO hooks: the driver's Protected_Mode excursion selects
        # ES=0x08 as a data window onto the EHCI register page.  Unicorn's
        # default GDT gives selector 0x08 base 0, so route the window to
        # linear 0x00 and hook 0x00..0x100 as MMIO.  (Low real-mode
        # vectors are only read through int dispatch, never byte-copied
        # from this window, so the overlap is safe.)
        for a in range(0x80, 0x180):
            u.hook_add(UC_HOOK_MEM_READ, self.hook_mmio_read, begin=a, end=a)
            u.hook_add(UC_HOOK_MEM_WRITE, self.hook_mmio_write, begin=a, end=a)
        try:
            u.emu_start(usb2dri + (entry & 0xFFFF), 0xFFFFFF,
                        count=self.case.get("max_insns", 60_000_000))
        except UcError as e:
            ip = u.reg_read(X86_REG_EIP)
            cs = u.reg_read(X86_REG_CS)
            try:
                here = bytes(u.mem_read(((cs << 4) + ip) & 0xFFFFF, 8)).hex(" ")
            except Exception:
                here = "?"
            self.stop_reason = "uc_error: %s @ CS:IP=%04X:%04X bytes=%s" % (
                e, cs, ip, here)
        except UnknownInt as e:
            self.stop_reason = "unknown_int: %s" % e
        return self.stop_reason


class UnknownInt(Exception):
    def __init__(self, intno, where=None):
        self.intno = intno
        self.where = where
        super().__init__("int %02Xh %s" % (intno, where))
