#!/bin/sh
# usb2test/build.sh — reproduce the stage2 build (stage2/Makefile.am rules)
# without GNU make, for the Windows/mingw toolchain used by usb2test.
#
# Flag fidelity: all compile/link flags are read from the configure-generated
# stage2/Makefile, so this script stays in sync with the autotools build.
#
# Outputs (in stage2/): pre_stage2 (raw binary + 4-byte magic), grldr,
# stage2_size.h, plus usb2test/pre_stage2.map-symbols via nm.
#
# Also implements the USB2DRI_PLAN.md §A7 guardrail: measure Ending-USB2DRI
# from the linked image and fail the build if it exceeds 3072 bytes.
set -e

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
TOP=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$TOP/stage2"

# ---- toolchain: prefer a mingw gcc on PATH --------------------------------
# NOTE: the x86_64-hosted toolchain is required (its gas understands .code64,
# which grub4dos' asm.S uses for the long-mode paths); it produces 32-bit
# output via -m32 -mno-sse, exactly what configure.ac does for x86_64 hosts.
: ${I686_BIN:="/d/tools/x64-gcc/mingw64/bin"}
if [ -d "$I686_BIN" ]; then
  PATH="$I686_BIN:$PATH"
  export PATH
fi

# ---- read flags from the generated Makefile (single source of truth) ------
MK=Makefile
getvar() { sed -n "s/^$1 = //p" "$MK" | head -1; }
CC=$(getvar CC)
CPPFLAGS=$(getvar CPPFLAGS)
CFLAGS=$(getvar CFLAGS)
STAGE2_CFLAGS=$(getvar STAGE2_CFLAGS)
FSYS_CFLAGS=$(getvar FSYS_CFLAGS)
SERIAL_FLAGS=$(getvar SERIAL_FLAGS)
HERCULES_FLAGS=$(getvar HERCULES_FLAGS)
GRAPHICS_FLAGS=$(getvar GRAPHICS_FLAGS)
GFX_FLAGS=$(getvar GFX_FLAGS)
BIN_LDFLAGS=$(getvar BIN_LDFLAGS)
BIN_LDFLAGS=${BIN_LDFLAGS//\$(top_srcdir)/$TOP}
LDFLAGS=$(getvar LDFLAGS)
[ -n "$BIN_LDFLAGS" ] || { echo "ERROR: BIN_LDFLAGS empty - run ./configure first"; exit 1; }
STAGE2_COMPILE="$STAGE2_CFLAGS -fno-builtin -nostdinc $SERIAL_FLAGS $HERCULES_FLAGS $GRAPHICS_FLAGS $GFX_FLAGS"
PRE_STAGE2_LINK="-m32 -nostartfiles -e _start -nostdlib -Wl,-N -Wl,-Ttext -Wl,8200 $BIN_LDFLAGS"
START_LINK="-m32 -nostartfiles -e _start -nostdlib -Wl,-N -Wl,-Ttext -Wl,8000 $BIN_LDFLAGS"
PRE_CFLAGS="$STAGE2_COMPILE $FSYS_CFLAGS"
CFLAGS_ALL="$CFLAGS -fno-leading-underscore"
# automake DEFAULT_INCLUDES equivalent (-nostdinc removes the default paths)
INC="-I. -I.."
DEFS="-DHAVE_CONFIG_H"

echo "== compiler: $CC"
"$CC" --version | head -1

# ---- pre_stage2.exec ------------------------------------------------------
SOURCES="asm.S bios.c boot.c builtins.c char_io.c cmdline.c common.c console.c \
dec_lz4.c dec_lzma.c dec_vhd.c disk_io.c fsys_ext2fs.c fsys_fat.c fsys_ntfs.c \
fsys_iso9660.c fsys_pxe.c fsys_initrd.c fsys_ipxe.c fsys_fb.c gunzip.c \
hercules.c md5.c serial.c stage2.c terminfo.c tparm.c graphics.c"

OBJS=""
for src in $SOURCES; do
  obj="${src%.*}.o"
  OBJS="$OBJS $obj"
  # incremental: skip unchanged sources (asm.S also depends on the
  # shared headers/config.h; grldrstart.S on stage2_size.h is handled
  # separately by always relinking + regenerating below)
  if [ -f "$obj" ] && [ "$src" -nt "$obj" ] || { [ "$src" = "asm.S" ] && \
     { [ shared.h -nt "$obj" ] || [ config.h -nt "$obj" ] || [ a20.inc -nt "$obj" ]; }; }; then
    :
  elif [ -f "$obj" ] && [ -z "$(find . -maxdepth 1 -newer "$obj" -name '*.h' -o -maxdepth 1 -newer "$obj" -name '*.inc' 2>/dev/null)" ]; then
    continue
  fi
  echo "  CC   $src"
  case "$src" in
    *.S) "$CC" $DEFS $INC $CPPFLAGS $CFLAGS_ALL $PRE_CFLAGS -c "$src" -o "$obj" ;;
    *)   "$CC" $DEFS $INC $CPPFLAGS $CFLAGS_ALL $PRE_CFLAGS -c "$src" -o "$obj" ;;
  esac
done

echo "  LD   pre_stage2.exec"
"$CC" -o pre_stage2.exec $OBJS $LDFLAGS $PRE_STAGE2_LINK

# ---- pre_stage2 raw + the Makefile.am head/tail zero-squeeze --------------
echo "  OBJ  pre_stage2"
"$OBJCOPY" -O binary pre_stage2.exec pre_stage2.raw 2>/dev/null || objcopy -O binary pre_stage2.exec pre_stage2.raw
python "$SCRIPT_DIR/squeeze.py" pre_stage2.raw pre_stage2
rm -f pre_stage2.raw pre_stage2_fullsize pre_stage2_head pre_stage2_tail

# ---- stage2_size.h + grldr -------------------------------------------------
SIZE=$(wc -c < pre_stage2)
echo "#define STAGE2_SIZE $SIZE" > stage2_size.h
echo "  CC   grldrstart.S"
# binutils 2.39+ treats multi-digit labels like `102:` as numeric labels and
# emits a DISP16 *ABS* relocation for them, which fails in PE output (ld
# assertion in coff-i386.c).  Rename the three sites in a build-time copy.
python - "$SCRIPT_DIR" <<'PYEOF'
import sys, os
srcdir = sys.argv[1]
src = open(os.path.join(srcdir, "..", "stage2", "grldrstart.S"),
           encoding="utf-8", errors="replace").read()
lines = src.split("\n")
defs = {2457: "LBL102_1", 4867: "LBL102_2", 6438: "LBL102_3"}
for ln, name in defs.items():
    lines[ln - 1] = lines[ln - 1].replace("102:", name + ":", 1)
lines[2466] = lines[2466].replace("jmp 102b", "jmp LBL102_1")
lines[4862] = lines[4862].replace("je 102", "je LBL102_2")
lines[6443] = lines[6443].replace("jne 102b", "jne LBL102_3")
open("grldrstart.work.S", "w", newline="\n").write("\n".join(lines))
PYEOF
"$CC" $DEFS $INC $CPPFLAGS $CFLAGS_ALL $STAGE2_COMPILE -c grldrstart.work.S -o grldrstart.o
rm -f grldrstart.work.S
echo "  LD   grldrstart.exec"
"$CC" -o grldrstart.exec grldrstart.o $LDFLAGS $START_LINK
objcopy -O binary grldrstart.exec grldrstart
cat grldrstart pre_stage2 > grldr
printf '%b%b' '\000\000\000\000\000\000\000\000\000\000\000\000' >> grldr

# ---- USB2DRI_PLAN.md §A7 guardrail ----------------------------------------
echo "== A7: measuring Ending-USB2DRI from the linked image"
nm pre_stage2.exec > usb2dri.nm
python "$SCRIPT_DIR/a7check.py" usb2dri.nm pre_stage2

echo "== done: pre_stage2 ($SIZE bytes), grldr ($(wc -c < grldr) bytes)"
