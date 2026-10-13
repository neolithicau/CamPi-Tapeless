# Installing CamPi on a Raspberry Pi 5

This guide is for **Raspberry Pi OS Lite 64-bit**, an **ILI9341 320×240 SPI LCD with XPT2046 touch**, and a **real PCIe FireWire host controller**. Follow the Raspberry Pi and card manufacturers' hardware instructions first. The script does not support arbitrary SPI, HDMI or DSI touchscreens without adapting the display/touch drivers.

## Tested hardware reference

CamPi has been tested successfully on a Raspberry Pi 5 running Raspberry Pi OS Lite 64-bit, with a StarTech PCIe IEEE 1394 card using a Texas Instruments XIO2213A/B/XIO2221 OHCI controller. Both native DV and HDV recording work with this combination.

The tested kernel reports `6.18.51-v8-16k-firewire+`; its FireWire driver is `firewire-ohci.ko.xz`. The relevant `/boot/firmware/config.txt` entries are:

```ini
auto_initramfs=1
dtparam=pciex1
kernel=kernel-firewire.img
dtoverlay=pciex1-32bit-dma
```

The 32-bit DMA overlay is specific to the tested configuration and **may not be necessary or appropriate on every PCIe setup**. Its installed binary has not been compared with the example overlay source shipped in this repository. The exact source commit and full build configuration for the tested kernel are not available, so these instructions are intended to produce a compatible kernel rather than an identical binary.

**Initramfs:** This tested installation boots without `/boot/firmware/initramfs-firewire`, even though `auto_initramfs=1` is set. An initramfs is only needed if the target operating system and root-storage configuration require one; see the conditional instructions below.

**Important:** Building and selecting a custom kernel can make the Pi unbootable. Have physical access or a way to mount the boot media from another machine. Back up the SD card, `/boot/firmware/config.txt`, and the existing kernel before making changes. Do not do the kernel step remotely unless you have a recovery path.

## 1. Prepare Raspberry Pi OS and hardware

Flash Raspberry Pi OS Lite 64-bit to the SD card, create your own user account and enable SSH if needed. Install updates, then reboot:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo reboot
```

Verify that you are on 64-bit userspace and check free space before attempting the kernel build:

```bash
uname -m   # should be aarch64
findmnt /boot/firmware
df -h "$HOME" /boot/firmware
```

Allow at least **20 GB free** for a native kernel source/build workspace, more if you keep previous build trees.

Enable SPI:

```bash
sudo raspi-config
# Interface Options > SPI > Enable
```

For a PCIe breakout that is not automatically enabled as HAT+, add this in the appropriate `[all]` section of `/boot/firmware/config.txt`:

```ini
dtparam=pciex1
```

**Start with the default Pi 5 PCIe Gen 2 speed**, not forced Gen 3. Shut down before connecting/reseating PCIe ribbon cables. Incorrect ribbon orientation or incomplete seating can prevent controller enumeration. Provide appropriate power to the PCIe adapter/card and use a correctly rated Pi 5 PSU. Do not assume a Pi automatically supplies sufficient FireWire bus power to devices connected via six-pin FireWire sockets.

For the default SPI wiring, see [the LCD pin table in README.md](README.md#default-lcd-pin-configuration). After enabling SPI, verify:

```bash
ls -l /dev/spidev0.0 /dev/spidev0.1
```

The GPIO functions in this project default to **LCD DC GPIO22**, **LCD reset GPIO27**, **touch IRQ GPIO17** and an optional **buzzer GPIO16**. Alternate pinouts can be configured later without editing `campi.py`.

## 2. Install kernel-build requirements

First, check whether your *current kernel already provides* OHCI FireWire:

```bash
uname -r
modinfo firewire_ohci 2>/dev/null | head
```

If `modinfo` succeeds **and `firewire_ohci` binds to your PCIe card and creates a usable FireWire host**, you might not need to rebuild the kernel. `modinfo` alone only proves the module is installed for the queried kernel, not that the PCIe adapter works. Otherwise install the native-build dependencies:

```bash
sudo apt install -y \
  git build-essential bc bison flex libssl-dev libelf-dev \
  libncurses-dev device-tree-compiler pciutils
```

The Pi 5 `bcm2712_defconfig` uses a **64-bit kernel and 16 KB memory pages**. The `rpi-6.18.y` branch may advance beyond the version listed in the tested hardware reference. Record the Git commit, `.config` and `make -s kernelrelease` so you can identify your own build. The `-campi-fw` suffix below is a custom label, not a required kernel version.

```bash
cd ~
git clone --depth=1 --branch rpi-6.18.y https://github.com/raspberrypi/linux.git campi-linux
cd ~/campi-linux
git rev-parse HEAD
export ARCH=arm64
make bcm2712_defconfig
```

Enable FireWire as modules, and give the custom kernel a unique local version:

```bash
scripts/config --file .config \
  --module FIREWIRE \
  --module FIREWIRE_OHCI \
  --set-str LOCALVERSION '-campi-fw' \
  --disable LOCALVERSION_AUTO
make olddefconfig

grep -E '^CONFIG_(FIREWIRE|FIREWIRE_OHCI|LOCALVERSION|LOCALVERSION_AUTO)=' .config
```

Expected core configuration:

```text
CONFIG_LOCALVERSION="-campi-fw"
CONFIG_LOCALVERSION_AUTO=n
CONFIG_FIREWIRE=m
CONFIG_FIREWIRE_OHCI=m
```

If your kernel tree has additional FireWire sub-options, use `make menuconfig` under **Device Drivers > IEEE 1394 (FireWire) support** to review them. Only the modern `firewire-core`/`firewire-ohci` stack is required; do not follow obsolete `raw1394` kernel instructions.

Build. This can take a long time and needs adequate storage/cooling:

```bash
make -j2 Image.gz modules dtbs
make -s kernelrelease
```

`-j2` is conservative for a Pi with limited memory. Increase cautiously on systems with more RAM and adequate cooling.

### Install and select the kernel

**Note:** If these backup instructions are over the top or you don't care about the contents of your install drive if you make a mistake, just glaze through this section. My device was produced from a fresh install and was not mission critical for any other purpose than this project.n

Before copying **any** custom Device Tree files (DTBs), back up the **entire** boot partition, not just `config.txt` and the kernel image. The commands below overwrite existing DTBs and overlays. Removing the custom `kernel=` line alone will not restore those files.

```bash
# In addition to a full SD-card backup:
BACKUP="$HOME/campi-boot-before-kernel-$(date +%Y%m%d-%H%M%S).tar.gz"
sudo tar -C /boot/firmware -czf "$BACKUP" .
sudo chown "$(id -un):$(id -gn)" "$BACKUP"
ls -lh "$BACKUP"
```

Store a copy of that archive off the Pi before rebooting. The archive contains the current kernel, boot configuration, Device Trees, overlays and any installed initramfs. Confirm that there is enough free space on the root and boot partitions.

From `~/campi-linux`, install the modules and write the kernel image to a **separate file**:

```bash
sudo make -j2 modules_install
sudo cp arch/arm64/boot/Image.gz /boot/firmware/kernel-firewire.img
sudo cp arch/arm64/boot/dts/broadcom/*.dtb /boot/firmware/
sudo cp arch/arm64/boot/dts/overlays/*.dtb* /boot/firmware/overlays/
sudo cp arch/arm64/boot/dts/overlays/README /boot/firmware/overlays/
sudo depmod "$(make -s kernelrelease)"
```

**Optional initramfs check:** `auto_initramfs=1` instructs Raspberry Pi firmware to look for a matching initramfs; it does **not** make one mandatory. A directly mounted ext4 SD-card root filesystem may boot without an initramfs if the necessary drivers are built into the kernel. Check whether your current OS and storage layout need early-boot modules or scripts before deciding.

Inspect the existing system before deciding whether an initramfs is needed:

```bash
findmnt -no SOURCE,FSTYPE /
grep -n -E '^(auto_initramfs=|initramfs )' /boot/firmware/config.txt
ls /boot/firmware/initramfs* 2>/dev/null || true
```

If the OS relies on early userspace to assemble or unlock root storage (for example, encrypted root, LVM, special storage or non-built-in root drivers), **you must preserve that boot requirement**. For installations that actually require an initramfs, install `initramfs-tools` if necessary, generate it **after** installing the matching custom-kernel modules, and copy it under the firmware's expected name:

```bash
sudo apt install -y initramfs-tools
KREL="$(make -s kernelrelease)"
if [ -f "/boot/initrd.img-$KREL" ]; then
  sudo update-initramfs -u -k "$KREL"
else
  sudo update-initramfs -c -k "$KREL"
fi
sudo cp "/boot/initrd.img-$KREL" /boot/firmware/initramfs-firewire
ls -lh /boot/firmware/initramfs-firewire
```

Only run this block if your operating system actually requires an initramfs. If your boot configuration uses an explicit `initramfs` directive instead of `auto_initramfs`, update it for the new image. See the [Raspberry Pi boot configuration documentation](https://www.raspberrypi.com/documentation/computers/config_txt.html#auto_initramfs).

Now add this in the active configuration section of `/boot/firmware/config.txt` (not inside a disabled or inappropriate model-specific section):

```ini
kernel=kernel-firewire.img
```

After dealing with the optional PCIe DMA overlay below, reboot:

```bash
sudo reboot
uname -r
sudo modprobe firewire_ohci    # loads firewire_core as a dependency
lsmod | grep firewire
lspci -nnk | grep -iE -A5 'firewire|1394|xio22'
ls -l /dev/fw*
sudo dmesg | grep -iE 'firewire|ohci|pcie' | tail -n 50
```

You should see the FireWire OHCI driver bound to your card and a **local host device**, commonly `/dev/fw0`. When the camcorder is powered on and enumerates, expect a separate remote FireWire device, commonly `/dev/fw1`. **CamPi uses the kernel's `is_local` flags where available, not only the `/dev/fw0` name.**

**Recovery:** If the custom kernel does not boot, mount its boot partition on another computer. Restore the saved boot partition files, including DTBs/overlays, or remove the custom `kernel=` and new `dtoverlay=` entries **and restore the previous DTBs/overlays**. The previous kernel must still have its matching `/lib/modules/<version>` directory. Changing `kernel=` alone is not a complete rollback.

### PCIe FireWire / 32-bit DMA caveat

The tested StarTech/TI controller uses `dtoverlay=pciex1-32bit-dma`. Other OHCI cards and firmware/kernel versions may not require a DMA overlay. **Confirm PCIe enumeration and try loading the FireWire driver before introducing one.** If the driver fails with `-12` (`ENOMEM`) or DMA-related errors, investigate one of these approaches at a time:

- Raspberry Pi's stock `dtoverlay=pcie-32bit-dma` overlay, if present in your firmware installation.
- The custom external-PCIe overlay named `pciex1-32bit-dma` provided as an example in this repository. A same-named overlay is enabled on the tested system, but the example `.dts` has **not** been verified as identical to the installed `.dtbo`. Check Device Tree target symbols and firmware compatibility.
- `pcie_aspm=off` on the **existing single line** in `/boot/firmware/cmdline.txt` only as a compatibility diagnostic when link stability is an issue. Do not casually change unrelated boot parameters.

The correct overlay depends on the PCIe controller, firmware and kernel. Changes to the DMA mappings can affect other hardware. For context, see the [Raspberry Pi kernel issue](https://github.com/raspberrypi/linux/issues/7317) and [PCIe device compatibility discussion](https://github.com/geerlingguy/raspberry-pi-pcie-devices/issues/811).

To check the active configuration on a particular Pi, run `grep -E '^(dtparam=pciex1|dtoverlay=.*(pcie|dma)|kernel=|auto_initramfs=)' /boot/firmware/config.txt`, `uname -r` and `sudo dmesg | grep -i firewire`.

The optional overlay source is at [examples/pciex1-32bit-dma.dts](examples/pciex1-32bit-dma.dts). It is an example, not a guaranteed fix for every controller. Review the source and build it with:

```bash
cd ~/CamPi   # or wherever you unpacked the project
dtc -@ -I dts -O dtb -o pciex1-32bit-dma.dtbo \
  examples/pciex1-32bit-dma.dts
sudo cp pciex1-32bit-dma.dtbo /boot/firmware/overlays/
```

Add **only if you selected that approach**:

```ini
dtoverlay=pciex1-32bit-dma
```

Do not enable both DMA overlays at once. If the card is not visible in `lspci`, fix PCIe link/power/ribbon issues **before** changing FireWire modules or writing application code.

## 3. Install application dependencies

```bash
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip python3-dev \
  python3-gpiozero python3-lgpio \
  dvgrab libavc1394-tools ffmpeg v4l-utils alsa-utils mpv \
  usbutils util-linux
```

`libavc1394-tools` provides `dvcont`, used for compatible tape decks. `v4l-utils` provides `v4l2-ctl`, and `alsa-utils` provides `arecord`. `mpv` and FFmpeg provide playback and stream inspection. **NetworkManager is optional** for Wi-Fi management; do not add/replace your live network management service blindly over SSH. On a confirmed NetworkManager-based setup, install `network-manager` separately if it is not already present.

Download the repository using **Code → Download ZIP** on GitHub, or clone it with Git. Extract or clone it so that `~/CamPi/campi.py` and `~/CamPi/requirements.txt` exist. If you choose another directory, substitute that path in the commands below.

A separate Python virtual environment is recommended. Use `--system-site-packages` so apt-provided Pi GPIO libraries remain available inside the environment:

```bash
cd ~
cd ~/CamPi
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -c 'import spidev, PIL, luma.lcd, gpiozero; print("Python dependencies OK")'
```

Confirm commands:

```bash
for command in dvgrab dvcont ffmpeg ffprobe v4l2-ctl arecord mpv; do
  command -v "$command" || echo "MISSING: $command"
done
```

## 4. Device and GPIO permissions

Give the account running CamPi access to SPI, GPIO and multimedia devices. Group names differ between distributions; first check which ones exist, then add only those groups:

```bash
getent group spi gpio video render audio
GROUPS_TO_ADD=$(for g in spi gpio video render audio; do getent group "$g" >/dev/null && printf '%s,' "$g"; done)
GROUPS_TO_ADD=${GROUPS_TO_ADD%,}
if [ -n "$GROUPS_TO_ADD" ]; then
  sudo usermod -aG "$GROUPS_TO_ADD" "$USER"
fi
```

Log out and back in (or reboot) before testing. Confirm:

```bash
id
ls -l /dev/spidev* /dev/fw* /dev/dri/* 2>/dev/null
```

FireWire device permissions differ by image/udev configuration. If `dvgrab` succeeds with root but is denied as your regular user, inspect `ls -l /dev/fw*`. One **optional** policy for a dedicated capture appliance is:

```bash
sudo tee /etc/udev/rules.d/60-campi-firewire.rules >/dev/null <<'RULE'
SUBSYSTEM=="firewire", GROUP="video", MODE="0660"
RULE
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=firewire
```

Members of `video` then get FireWire raw-device access. This is a security choice, not a requirement to apply if your device nodes already have appropriate permissions.

An optional buzzer can use **BCM GPIO16** as an active-low control signal. Choose a buzzer module with an input compatible with the Pi's **3.3 V GPIO logic**; its power requirements depend on the module. CamPi works without a buzzer.

## 5. Test capture independently of the UI

Before launching the touchscreen, verify your FireWire device is visible and powered, then test `dvgrab` manually. **Do not run another CamPi/dvgrab capture at the same time.**

```bash
mkdir -p ~/captures
cd ~/captures
# For a known live DV camera:
timeout -s INT 8s dvgrab -noavc -format raw -size 0 dv-test-
# For a known live HDV camera:
timeout -s INT 8s dvgrab -noavc -format hdv -size 0 hdv-test-
ls -lh
```

The camera must provide a live or playback signal in the selected FireWire mode. These two explicit test commands are format-specific; don't run them against the wrong type of stream. CamPi itself probes DV/HDV before starting capture.

If you're using analogue capture, first inspect available devices and capture modes:

```bash
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-formats-ext
arecord -l
```

The correct `/dev/videoX` and ALSA device index may differ. CamPi attempts to discover compatible USB devices dynamically.

## 6. Test the touchscreen / first run

Ensure the chosen display is wired correctly and the Pi's SPI interface is enabled. Run:

```bash
cd ~/CamPi
.venv/bin/python campi.py
```

On first run, follow the XPT2046 touchscreen calibration targets. By default, calibration is saved to `~/.config/tapeless/touch-calibration.json` (the directory name is intentionally `tapeless`). If you rotate or replace the screen, recalibrate using the Interface menu.

**Optional SonyCam status font:** CamPi runs without the font and falls back to a system font. To enable SonyCam Original for recorder status text, obtain a legitimately licensed copy of `sonycam-original.otf` and place it at `~/fonts/sonycam-original.otf`, or set `CAMPI_STATUS_FONT` to its absolute path in `/etc/campi/campi.env`. The application does **not** automatically search for a font in the repository's `fonts/` subdirectory. Respect the font's redistribution and attribution terms.

If using a different pinout, copy [examples/campi.env.example](examples/campi.env.example) to `/etc/campi/campi.env`, uncomment the relevant settings and either export them in your testing shell or run CamPi through the service after installation. Example for manual testing:

```bash
CAMPI_LCD_DC=22 CAMPI_LCD_RST=27 .venv/bin/python campi.py
```

**The Python program drives the LCD directly over SPI**, without requiring a desktop session or the LCD to be configured as Linux's primary framebuffer.

## 7. USB capture storage (optional)

CamPi prefers a **writable mounted USB volume** when storage policy is Auto. The default dedicated path is `/mnt/usb-captures`, but the application may also detect other mounted USB volumes. On Raspberry Pi OS Lite, mounting isn't automatic merely because a USB drive is plugged in.

For a dedicated **ext4** capture SSD, first identify its filesystem UUID safely:

```bash
lsblk -f
sudo mkdir -p /mnt/usb-captures
```

After checking the correct drive and UUID, add a line to `/etc/fstab`:

```fstab
UUID=REPLACE_WITH_YOUR_USB_FILESYSTEM_UUID /mnt/usb-captures ext4 nofail,noatime,x-systemd.automount,x-systemd.device-timeout=5s 0 2
```

Then activate the generated automount unit and **trigger it**:

```bash
sudo systemctl daemon-reload
sudo systemctl start "$(systemd-escape --path --suffix=automount /mnt/usb-captures)"
ls /mnt/usb-captures >/dev/null
findmnt -T /mnt/usb-captures -o TARGET,SOURCE,FSTYPE,OPTIONS
```

You may see both an `autofs` mount and the underlying `ext4` volume when using `x-systemd.automount`. **Verify that the mounted filesystem is the intended USB disk before changing permissions.** On a newly formatted ext4 volume, change only the mount root directory owner:

```bash
sudo chown "$USER:$(id -gn)" /mnt/usb-captures
```

Do not run `chown -R` indiscriminately against an existing video archive. For NTFS/exFAT, use that filesystem's mount options instead of copying an ext4 configuration.

If your USB disk is already mounted elsewhere, you can simply let CamPi find it, or point `CAMPI_USB_CAPTURE_DIR` to its mount path. The application automatically falls back to internal storage when the selected USB device is unavailable.

## 8. Launch at boot with systemd (optional)

Test manual startup first, then install the service **as your normal account**:

```bash
cd ~/CamPi
./scripts/install-service.sh
```

The installer generates `/etc/systemd/system/campi.service` with your Linux account and the project's absolute path. It also loads optional settings from `/etc/campi/campi.env` if that file exists.

```bash
sudo mkdir -p /etc/campi
sudo cp examples/campi.env.example /etc/campi/campi.env
sudo nano /etc/campi/campi.env  # uncomment settings only as needed
sudo systemctl restart campi.service
```

Useful commands:

```bash
systemctl status campi --no-pager
journalctl -u campi -b -n 80 --no-pager
sudo systemctl stop campi
sudo systemctl start campi
sudo systemctl restart campi
sudo systemctl disable --now campi
```

When replacing `campi.py`, stop or restart the service. You need `systemctl daemon-reload` only if its **unit file changes**, not for a normal Python update. Don't run CamPi manually at the same time as the service because both would access the same SPI bus and FireWire recording flow.

### Optional power menu permission

The on-screen Reboot/Shut down menu invokes `sudo -n systemctl reboot` / `sudo -n systemctl poweroff`. A non-root service may need a tightly scoped sudoers rule. Inspect your system's `systemctl` path first (`command -v systemctl`) and create a rule with `sudo visudo -f /etc/sudoers.d/campi-power` using your **actual** username:

```sudoers
YOUR_USER ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff
```

Do **not** grant passwordless sudo for all commands. NetworkManager controls may similarly require OS authorization and can be left unavailable if you don't need them.

## 9. Windows file access using Samba (optional)

Samba is optional. To browse recordings from Windows, create an **authenticated** share instead of allowing anonymous write access:

```bash
sudo apt install -y samba
sudo smbpasswd -a "$USER"
```

In `/etc/samba/smb.conf` (substitute your real Linux username/path):

```ini
[captures]
   path = /home/YOUR_USER/captures
   browseable = yes
   read only = no
   valid users = YOUR_USER
   force user = YOUR_USER
   create mask = 0664
   directory mask = 0775

[usb-captures]
   path = /mnt/usb-captures
   browseable = yes
   read only = no
   valid users = YOUR_USER
   force user = YOUR_USER
   create mask = 0664
   directory mask = 0775
```

Then:

```bash
sudo testparm
sudo systemctl restart smbd
```

Open `\\PI_HOSTNAME\captures` in Windows Explorer (replace `PI_HOSTNAME` with the Pi's network hostname). **If Samba 4.22 shows newly created recordings only after a long delay**, try `smb3 directory leases = no` under the `[global]` section of `/etc/samba/smb.conf`, restart `smbd`, and reconnect the Windows share. This worked on the tested system but is not necessarily required on other Samba versions.

## 10. Final checks

```bash
cd ~/CamPi
./scripts/check-system.sh
.venv/bin/python -m py_compile campi.py
systemctl status campi --no-pager
```

Successful hardware validation means:

1. SPI display shows the UI and touch calibration works.
2. FireWire controller is visible and a camera produces a remote `/dev/fw*` node.
3. Live DV or HDV is identified and REC produces a **growing** `.dv` or `.m2t` file.
4. Analogue capture (when connected) records MKV with video/audio as supported by that device.
5. A mounted USB storage volume is recognised, or the internal fallback works.
6. A connected HDMI display can play a selected recording.
7. The application restarts automatically after boot (if the service is enabled).

Continue with [Troubleshooting](docs/TROUBLESHOOTING.md) for specific errors and commands.

**FireWire capture troubleshooting:** If CamPi reports `FireWire stream produced no file`, inspect `~/.config/tapeless/capture.log`. A `dvgrab` error containing `errno: 16 (Device or resource busy)` indicates that a FireWire receiver/channel was unavailable. Ensure there is only one running instance of CamPi and no separate `dvgrab` process (`pgrep -af 'campi.py|dvgrab'`). CamPi attempts to stop its background stream detector before recording; consult [Troubleshooting](docs/TROUBLESHOOTING.md) if the problem persists.

## External technical references

- Raspberry Pi: https://www.raspberrypi.com/documentation/computers/linux_kernel.html
- Raspberry Pi PCIe: https://www.raspberrypi.com/documentation/computers/raspberry-pi.html
- Linux FireWire modules: https://cateee.net/lkddb/web-lkddb/FIREWIRE_OHCI.html
- Pi 5 DMA discussion: https://github.com/raspberrypi/linux/issues/7317
- DVgrab Debian Trixie: https://packages.debian.org/trixie/dvgrab
- libavc1394 tools / dvcont: https://packages.debian.org/trixie/libavc1394-tools
- Luma.LCD hardware: https://luma-lcd.readthedocs.io/en/latest/hardware.html
