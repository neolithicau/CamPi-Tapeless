#!/usr/bin/env bash
# Read-only diagnostics. Safe to run without sudo.
set -u
printf '\n=== System ===\n'
uname -a
if [[ -r /proc/device-tree/model ]]; then tr -d '\0' </proc/device-tree/model; echo; fi
printf '\n=== FireWire build ===\n'
for bin in dvgrab dvcont ffmpeg ffprobe mpv v4l2-ctl arecord nmcli; do
    printf '%-12s ' "$bin"; command -v "$bin" || true
done
lsmod | grep -E 'firewire(_ohci|_core)?' || true
ls -l /dev/fw* 2>/dev/null || true
ls -l /sys/bus/firewire/devices 2>/dev/null || true
printf '\n=== PCIe ===\n'
lspci -nn 2>/dev/null | grep -Ei 'firewire|1394|xio22|ohci' || true
printf '\n=== SPI / video / storage ===\n'
ls -l /dev/spidev* /dev/video* 2>/dev/null || true
lsblk -o NAME,TYPE,TRAN,FSTYPE,MOUNTPOINTS,SIZE 2>/dev/null || true
printf '\n=== Service ===\n'
systemctl is-enabled campi.service 2>/dev/null || true
systemctl is-active campi.service 2>/dev/null || true
printf '\nDone. (No system changes made.)\n'
