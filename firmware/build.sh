#!/usr/bin/env bash
#
# End-to-end build of the TomTom ttsystem (kernel + minimal initramfs + tomdisp).
#
# Tested on a Raspberry Pi 4 (arm64/Raspberry Pi OS). The TomTom cross toolchain
# is an old i386-hosted gcc 3.3.4; on arm64 it is executed through qemu-i386.
#
# Usage:  ./build.sh            (see variables below)
#
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${WORK:-$HERE/work}"
OUT="$HERE/out"

# --- sources ---------------------------------------------------------------
KERNEL_URL="${KERNEL_URL:-https://archive.org/download/golinux-tt1114405.tar.gz/golinux-tt1114405.tar.gz}"
PATCH_URL="${PATCH_URL:-https://raw.githubusercontent.com/george-hopkins/opentom/master/patchs/kernel_tt1114405_opentom.patch}"
TOOLCHAIN_URL="${TOOLCHAIN_URL:-http://download.tomtom.com/open/gpl/toolchain_redhat_gcc-3.3.4_glibc-2.3.2-20060131a.tar.gz}"
BUSYBOX_URL="${BUSYBOX_URL:-https://busybox.net/downloads/busybox-1.22.1.tar.bz2}"

# --- toolchain (i386 gcc 3.3.4 running under qemu-i386) --------------------
# Provide the extracted toolchain here (dir containing bin/arm-linux-gcc):
CROSS="${CROSS:-$WORK/gcc-3.3.4_glibc-2.3.2}"
# i386 runtime prefix for qemu (the loader + libs), see README / docs/notes.md
if [ -n "${QEMU_LD_PREFIX:-}" ]; then export QEMU_LD_PREFIX; fi

mkdir -p "$WORK" "$OUT"

echo "==> fetching sources"
[ -d "$WORK/linux-s3c24xx" ] || { cd "$WORK"; curl -sL -o kernel.tgz "$KERNEL_URL"; tar xzf kernel.tgz; }
[ -f "$WORK/opentom.patch" ] || curl -sL -o "$WORK/opentom.patch" "$PATCH_URL"
[ -d "$WORK/busybox-1.22.1" ] || { cd "$WORK"; curl -sL -o busybox.tar.bz2 "$BUSYBOX_URL"; tar xf busybox.tar.bz2; }

export PATH="$CROSS/bin:$PATH"
export ARCH=arm CROSS_COMPILE=arm-linux-

echo "==> building tomdisp (static ARM)"
"${CROSS}/bin/arm-linux-gcc" -static -O2 -o "$WORK/tomdisp" "$HERE/../client/tomdisp.c"
"${CROSS}/bin/arm-linux-strip" "$WORK/tomdisp"

echo "==> patching + configuring kernel"
cd "$WORK/linux-s3c24xx"
[ -f .opentom_patched ] || { patch -p1 < "$WORK/opentom.patch"; touch .opentom_patched; }
cp "$HERE/kernel.config" .config
yes "" | make ARCH=arm CROSS_COMPILE=arm-linux- oldconfig >/dev/null

echo "==> building kernel modules (gadget)"
make -j"$(nproc)" ARCH=arm CROSS_COMPILE=arm-linux- modules >/dev/null

echo "==> assembling initramfs"
ROOT="$WORK/initramfs"
rm -rf "$ROOT"; mkdir -p "$ROOT"/{bin,dev,proc,sys,tmp,mnt/sdcard} \
    "$ROOT/lib/modules/2.6.13/kernel/drivers/usb/gadget" "$ROOT/dev/pts"
install -m755 "$HERE/init" "$ROOT/init"
install -m755 "$WORK/tomdisp" "$ROOT/bin/tomdisp"
install -m644 "$WORK/linux-s3c24xx/drivers/usb/gadget/s3c24xx_udc.ko" \
              "$WORK/linux-s3c24xx/drivers/usb/gadget/g_ether.ko" \
              "$ROOT/lib/modules/2.6.13/kernel/drivers/usb/gadget/"
# busybox (static)
cd "$WORK/busybox-1.22.1"
cp "$HERE/busybox.config" .config
yes "" | make ARCH=arm CROSS_COMPILE=arm-linux- oldconfig >/dev/null
make -j"$(nproc)" ARCH=arm CROSS_COMPILE=arm-linux- >/dev/null
install -m755 busybox "$ROOT/bin/busybox"
for a in sh mount umount insmod ifconfig telnetd sleep mdev lsmod cat ls echo kill \
         killall mkdir mknod ln rm ps top ping dd sync ash head tail grep wget chmod \
         uname id date free df; do ( cd "$ROOT/bin" && ln -sf busybox "$a" ); done
# device nodes
( cd "$ROOT/dev"
  mknod console c 5 1; mknod null c 1 3; mknod zero c 1 5
  mknod tty c 5 0; mknod ptmx c 5 2
  chmod 666 null zero; chmod 600 console )

echo "==> embedding initramfs + building zImage"
cd "$WORK/linux-s3c24xx"
sed -i "s#^CONFIG_INITRAMFS_SOURCE=.*#CONFIG_INITRAMFS_SOURCE=\"$ROOT\"#" .config
yes "" | make ARCH=arm CROSS_COMPILE=arm-linux- oldconfig >/dev/null
make -j"$(nproc)" ARCH=arm CROSS_COMPILE=arm-linux- zImage >/dev/null

echo "==> packing ttsystem"
( cd "$ROOT" && find . | cpio -H newc -o 2>/dev/null | gzip -9 ) > "$WORK/initramfs.cpio.gz"
gcc -O2 -w -include string.h -o "$WORK/mktt" "$HERE/mktt.c" \
    "$HERE/crypto/md5.c" "$HERE/crypto/blowfish.c" -I"$HERE/crypto"
"$WORK/mktt" "$WORK/initramfs.cpio.gz" "$WORK/linux-s3c24xx/arch/arm/boot/zImage" > "$OUT/ttsystem"

echo
echo "==> done: $OUT/ttsystem  ($(stat -c%s "$OUT/ttsystem") bytes)"
echo "    copy it to the root of the TomTom internal storage (keep ttsystem.orig!)"
