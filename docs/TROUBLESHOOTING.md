# CamPi troubleshooting

Run `./scripts/check-system.sh` for a read-only snapshot of kernel/modules, FireWire, SPI, storage, software dependencies and service state.

## The UI does not start

```bash
systemctl status campi --no-pager
journalctl -u campi -b -n 100 --no-pager
ls -l /dev/spidev0.0 /dev/spidev0.1
```

Look for `ModuleNotFoundError` (venv/setup), SPI missing (interface disabled or insufficient permission), or GPIO access denied (group/backend). First test the Python script manually **with the service stopped**.

## No FireWire controller

```bash
lspci -nn | grep -iE 'firewire|1394|xio22'
sudo dmesg | grep -iE 'pcie|firewire|ohci' | tail -n 80
modinfo firewire_ohci
lsmod | grep firewire
ls -l /dev/fw*
```

If `lspci` shows no card, check the PCIe ribbon at **both ends**, power, HAT seating and `dtparam=pciex1`. Do not start with Python changes. If `lspci` sees the controller but `firewire_ohci` fails with error `-12`, inspect DMA range/overlay compatibility as explained in [INSTALL.md](../INSTALL.md#pcie-firewire--32-bit-dma-caveat).

If only the local controller appears, power on/connect the camera and check cable orientation and camera's active FireWire output mode. A controller-only condition does not prove video is streaming.

## `DV STREAM DETECTING` or REC yields no file

Make sure the camera provides a real live DV/HDV FireWire stream, not merely an enumerated node. CamPi detects DV/HDV before recording and verifies that a capture file grows. An HDV source requires `.m2t`, not raw DV.

```bash
tail -n 80 ~/.config/tapeless/capture.log
sudo dmesg | grep -iE 'firewire|ohci|reset'
```

For a known HDV camera, **with CamPi stopped**:

```bash
cd ~/captures
timeout -s INT 8s dvgrab -noavc -format hdv -size 0 hdv-debug-
```

If it works from a shell but not through systemd, check `/dev/fw*` permissions and the service user's groups/environment.


## Analogue input unavailable

```bash
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-formats-ext
arecord -l
```

Not every V4L2 node is the actual capture source. Some dongles expose multiple video nodes or separate audio interfaces. Confirm the device's advertised modes before choosing a manual fallback override in `/etc/campi/campi.env`.

## USB storage isn't appearing

```bash
lsblk -f
findmnt -T /mnt/usb-captures
ls -ld /mnt/usb-captures
```

Raspberry Pi OS Lite may not automount removable USB disks. Check the filesystem type and actual mount location. CamPi can discover other mounted USB media, but **it cannot record to an unmounted device**.

## Windows Samba share refresh is delayed by minutes

On the original Pi, Samba **4.22.11** with `smb3 directory leases = Auto` caused a stale Windows Explorer directory view despite ext4 storage and enabled inotify/change notifications. Explicitly setting the following under Samba's `[global]` section resolved it on that installation:

```ini
smb3 directory leases = no
```

Then `sudo testparm`, restart `smbd`, disconnect Windows' existing SMB sessions and reconnect. Test a locally created file with `touch ~/captures/refresh-test.txt`. Do not assume all file visibility issues require changing directory leases; inspect mounts, permissions and whether F5 shows the file first.

## Touch misses or phantom taps

Use **Interface > Touch calibration**, confirm XPT2046 `TP_IRQ` wiring and the correct SPI chip-select device. The Pi 5 is sensitive to ribbon-cable wiring, electrical noise and IRQ lines held low. The code deliberately waits for a clean touch release at startup.

## HDMI output absent / mpv cannot start

```bash
ls -l /dev/dri/*
mpv --version | head -n 2
journalctl -u campi -b -n 100 --no-pager
```

Check display detection, DRM access (`video`/`render` group), cable and mode negotiation. The touchscreen's `HDMI` indicator flashes red when a requested playback destination is unavailable. Note that LCD input and HDMI playback are separate subsystems.

## Wi-Fi changes and Power menu fail

CamPi uses `nmcli` when NetworkManager is present, and `sudo -n systemctl` for reboot/poweroff. Both can be subject to permissions. Never grant the service unrestricted passwordless root access. See [INSTALL.md](../INSTALL.md#optional-power-menu-permission).
