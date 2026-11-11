# Technical notes

Notes gathered while building this, in case someone hits the same walls.

## Device identification

```
ttgo.bif:  DeviceName=TomTom XXL IQ Routes Edition
           DeviceVersionHW=ONE XXL IQ Routes
           BootLoaderVersion=55279  ApplicationVersion=9510
           CurrentMap=Western_and_Central_Europe_2GB  (840.2562, 2009)
dmesg:     CPU S3C2412 (id 0x32412000), core 264 MHz, 64 MB RAM
           Detected type 42, name TomTom XXL IQ Routes Edition   (GOTYPE_AUSTIN)
display:   /proc/fb = "0 s3c24xx-lcd"; 480x272, 16bpp RGB565, line=960
storage:   mmcblk0 = 1953792 KiB (the internal 2 GB flash), no SD slot
backlight: /sys/class/backlight/s3c/brightness (max 100)
touch:     tomtomgo-tsinput -> /dev/input/event*, /dev/ts, /dev/tsraw
usb:       s3c2410-ohci (USB 1.1 host) + s3c24xx_usbmode;
           the stock kernel has NO gadget -> PC mode = bootloader mass storage
```

## Cross toolchain on arm64

The TomTom toolchain (`gcc-3.3.4_glibc-2.3.2`) is i386-hosted. On an arm64 Pi:

```bash
sudo apt-get install -y qemu-user-static binfmt-support
# i386 glibc is not installable next to the RPi libc (version mismatch),
# so extract it manually to / :
apt-get download libc6:i386 libgcc-s1:i386
sudo dpkg-deb -x libc6_*_i386.deb /
sudo dpkg-deb -x libgcc-s1_*_i386.deb /
sudo ldconfig
# now arm-linux-gcc runs transparently via the i386 binfmt handler
```

## Kernel build gotchas

* `gen_initramfs_list.sh` needs **gawk** (not mawk). Without it device-node
  entries are malformed ("Unrecognized nod format").
* With framebuffer console you must **disable `CONFIG_VGA_CONSOLE`** on ARM
  (otherwise `vgacon.c` fails on `PCIMEM_BASE`).
* The bootloader passes no initrd and there is no `root=`, so the initramfs must
  be **embedded** (`CONFIG_INITRAMFS_SOURCE=<dir>`), not supplied as an external
  blob.
* `/init` runs as PID1; if it exits the kernel panics. It must never return.
* There is no `/bin/sh` inside the cpio before `/init` runs its
  `busybox --install`; therefore `/init`'s shebang must be
  `#!/bin/busybox sh`, not `#!/bin/sh`. Symptom otherwise:
  `Kernel panic - not syncing: No init found`.
* `busybox --install -s /bin` did not create applet symlinks in our config; we
  pre-create them in the image and call applets directly (`busybox wget ...`).

## ttsystem layout

```
offset 0   : "TTBL"
+4         : u32 kernel_size      (zImage)
+8         : u32 kernel_addr = 0x31700000
+12        : zImage
           : 16-byte signature (MD5 then Blowfish with the reversed key)
           : u32 root_size        (cpio.gz)
           : u32 root_addr = 0x31000000
           : cpio.gz
           : 16-byte signature
           : 12-byte start sequence  (= 0x00000000, 0x31700000, 0x30000000)
```

The bootloader loads each blob **to its address** and jumps to `0x31700000`.
The stock image is kernel-first; `mktt.c` matches it.

## Recovery when a bad ttsystem won't boot

The TomTom shows the bootloader logo and hangs (no Linux, so no USB gadget). To
get the internal disk back: keep the device **connected to a PC and power-cycle**
(off ~20 s, on) — the bootloader re-exposes the FAT as mass storage. Then restore
`ttsystem.orig`. Keep a copy of `ttsystem.orig` on the device itself.

## USB Ethernet + routing pitfall

`g_ether` presents CDC-ECM; the Pi binds `cdc_ether` and creates `usb0`. If you
assign `192.168.1.1/24` and your LAN is also `192.168.1.0/24`, the connected
route hijacks your LAN (you can lose SSH/Internet). Use a **/32 + host route**:

```
nmcli con add type ethernet ifname usb0 con-name ttdisplay \
  ipv4.method manual ipv4.addresses 192.168.1.1/32 \
  ipv4.routes "192.168.1.10/32" ipv4.never-default yes
```

NetworkManager re-manages `usb0` on every re-enumeration, so a manual `ip addr`
is wiped — use a persistent nmcli connection.

## Backlight

`/sys/class/backlight/s3c/brightness`, 0–100. No RTC on the device, so schedule
it from the Pi (which has the correct time).
