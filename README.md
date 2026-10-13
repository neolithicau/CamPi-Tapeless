# CamPi

**A tapeless, touch-controlled DV/HDV and analogue-video recorder for Raspberry Pi 5.**

CamPi turns a Raspberry Pi 5 into a self-contained **direct-to-edit video capture unit** for DV/HDV camcorders, DV/HDV tape decks and analog sources (including analog camcorders).

Instead of requiring a chain of devices and conversion steps, this **records a native FireWire DV/HDV stream (or a USB analogue capture feed) to an SD card or attached USB storage device**, then makes the resulting files available for editing or archiving.

My original build uses a **Pi 5 1gb, Raspberry Pi OS Lite 64-bit, PCIe TO MiniPCIe Adapter, a Startech PCIe-to-FireWire card (MPEX1394B3), and a 320 × 240 SPI touchscreen (Aliexpress / SHCHV 2.4inch display)**, and a little 5v buzzer on GPIO 16 (optional). My capture directories are also shared via SAMBA on any connected Wifi, but that's a seperate set of config which is out of the scope of this script.

> **Status: Working and ready.** DV and HDV FireWire capture and tapedeck functions, analog captures, touchscreen and HDMI recording playback have been tested at length on my Pi5 1gb setup. I have tested this with various Amazon el-cheapo analog video adapters, and tested various types of MiniDV camera on the StarTech firewire adapter (MiniDV PAL, MiniDV NTSC and HDV units all tested). Again, if your specific device or setup doesn't work, please don't be surprised.

**NOTE:** This script is **not validated across multiple Pi 5s or display/card combinations**. Follow [INSTALL.md](INSTALL.md) and check the [limitations](#limitations-and-compatibility). This script is provided with absolutely zero support or warranty. I do not guarantee it will work in any instance outside my own operating environment, but I have produced it with some redundancy and fallbacks to allow it to operate in any similar environment. This script was produced entirely using ChatGPT and I make absolutely zero guarantees around anything other than my attempts at quality control - I have polished the proverbial as much as it can be polished. As a package, it works, and it works great (for me).


## Features and what it does

| Function | CamPi behaviour |
| --- | --- |
| **Live DV capture** | Records native SD DV over IEEE 1394/FireWire using `dvgrab`, with PAL/NTSC stream identification. Outputs `.dv`. |
| **Live HDV capture** | Detects MPEG-2 transport-stream HDV over FireWire and records native `.m2t` files using `dvgrab`. Recognises formats such as 1080i and 720p where reported by the camera. |
| **DV/HDV Tape-deck transport** | Provides FireWire AV/C transport controls for compatible DV devices, using `dvcont` where available. Camera-dependent, and your camera should be in tape playback mode before using this. |
| **Analogue capture** | Discovers V4L2 USB video input and matching ALSA audio input, then records **FFV1 lossless video** with **PCM audio** into `.mkv` using FFmpeg. Available modes depend on the dongle. This is a bonus feature and not a focus of the script. |
| **Storage** | Supports internal capture storage or writable mounted USB media. Auto mode prefers USB and falls back to internal storage when USB is not available. |
| **Touchscreen interface** | Operates on a 320 × 240 ILI9341 SPI LCD with XPT2046 resistive touchscreen, including touch calibration, menu navigation, notifications and a configurable buzzer. |
| **HDMI playback** | Plays recordings over a detected HDMI display using mpv (recommend the port closest to USB-C power in). Includes playback, seek control, storage information and an HDMI-disconnected warning. |
| **Networking** | Displays LAN/Wi-Fi connectivity and addresses, with basic NetworkManager Wi-Fi controls. Recordings can be exposed over an independently configured Samba share. |
| **Settings and statuses** | Device overview, storage status, error history, network status, recording details and persistent preferences. |
| **Themes** | Basic, Genuine Sorny, Genuine Panafonics and Genuine JayVC (all inspired from those original menu systems of 2000s camcorders). |

## Hardware

**Original verified configuration:**

- Raspberry Pi 5 1gb with **Raspberry Pi OS Lite 64-bit** (Debian Trixie based).
- 16K-page custom Raspberry Pi kernel, verified as `6.18.51-v8-16k-firewire+` on the original installation, with `CONFIG_FIREWIRE=m` and `CONFIG_FIREWIRE_OHCI=m`.
- PCIe breakout/HAT, plus **Startech PCIe-to-FireWire card (MPEX1394B3)** PCIe FireWire controller.
- IEEE 1394 DV/HDV camcorder or tape deck connected to the FireWire card.
- MPI2411-style **ILI9341 320 × 240 SPI LCD** and **XPT2046 touch controller**.
- Optional passive 3-pin buzzer (configured for GPIO16 and a low-level trigger).
- Optional USB storage and/or a V4L2/ALSA USB analogue video capture device. Original analogue capture testing used an MS210x-class dongle.
- Optional HDMI display for playback (Note: This was not designed as a live monitor - only to playback recordings).

**Note regarding touchscreen:** No other type or size of screen has been scoped for this project. There is no fallback currently in place if you are not using one.

### Original LCD pin configuration

CamPi uses **BCM GPIO numbers** in settings, not physical header numbers.

| Signal | Default | Raspberry Pi physical pin |
| --- | --- | --- |
| LCD and touch SPI bus | SPI0 | SPI0 on GPIO10/9/11 for MOSI/MISO/SCLK |
| LCD chip select | CE0 / device 0 | 24 |
| Touch chip select | CE1 / device 1 | 26 |
| LCD D/C | BCM22 | 15 |
| LCD reset | BCM27 | 13 |
| Touch IRQ | BCM17 | 11 |
| Optional buzzer input | BCM16 | 36 |

**Warning:** ILI9341/XPT2046 boards vary in pinout and supply voltage. Verify your *particular* screen schematic. Do not apply 5 V to Pi GPIO inputs. Configurable pins do not make unsupported displays compatible: the code currently renders to **ILI9341** specifically.

## Quick start

1. Install Raspberry Pi OS Lite 64-bit and enable SPI and PCIe.
2. Build or install a kernel with FireWire support and verify your actual IEEE 1394 card.
3. Install CamPi's Python and Linux dependencies.
4. Connect the compatible SPI display/touch hardware.
5. Run `campi.py` manually for calibration and hardware validation.
6. Optionally install the systemd service for automatic startup and Samba for access from a PC.

**All commands, a tested-hardware baseline, kernel build, permissions and recovery instructions:** **[INSTALL.md](INSTALL.md)**.

## Configuration

CamPi keeps settings/calibration under `~/.config/tapeless/` by default for backward compatibility with older builds of the project. Recordings default to `~/captures/`. A preferred USB mount is `/mnt/usb-captures/`, but CamPi also discovers other mounted writable USB volumes through `lsblk`.

Optional environment overrides are in [examples/campi.env.example](examples/campi.env.example). Examples:

```ini
CAMPI_LCD_DC=22
CAMPI_LCD_RST=27
CAMPI_TOUCH_IRQ_GPIO=17
CAMPI_INTERNAL_CAPTURE_DIR=/home/myuser/captures
CAMPI_USB_CAPTURE_DIR=/mnt/usb-captures
```

The sample service loads overrides from `/etc/campi/campi.env` if present. **Never commit a live settings file, Wi-Fi passwords, personal recordings or private logs to the repository.**

## Project layout

```text
campi.py                          Main touch UI and capture implementation
README.md                         Project overview
INSTALL.md                        Full Pi 5 setup and FireWire build guide
requirements.txt                  Python packages
examples/campi.env.example        Optional per-device settings
scripts/install-service.sh        Generate and install a user-specific systemd unit
scripts/check-system.sh           Read-only installation diagnostics
docs/TROUBLESHOOTING.md           Common problems and commands
docs/PORTABILITY.md               Source portability audit and test matrix
tests/test_portability.py          Hardware-independent static tests
examples/pciex1-32bit-dma.dts     Optional experimental DMA overlay source
.gitignore                        Excludes local configuration, recordings and builds
```

## Limitations and compatibility

- Only the outlined hardware is known-working. All other environments are untested.
- A **custom kernel may be necessary** for proper Firewire support. Rebuilding your kernel after Raspberry Pi OS kernel updates may be required. Keep a bootable stock kernel for recovery.
- Some FireWire cards require more power, appropriate bridge wiring and an appropriate DMA overlay. PCIe adapter compatibility varies. However, the StarTech is tested and working on system power only.
- Touch calibration is required on the first run. The LCD is not a generic HDMI/DSI framebuffer UI.
- The tape-deck controller is designed for **compatible DV AV/C transport**, not guaranteed for every HDV deck/camera. You should put your camera in tape playback mode before using this feature.
- USB analogue capture requires a V4L2 device and typically an ALSA input. Pixel formats, PAL/NTSC support and audio pairing depend on the actual dongle.
- HDMI playback depends on DRM/KMS/mpv and hardware/display permissions; it is not guaranteed on every audio/display configuration.
- The application currently assumes a **single active capture workflow** and is not a multi-user web service.
- Samba, USB automount, sudo permission for Reboot/Shut down and NetworkManager setup are **system administration tasks**; the Python script does not install or configure them automatically.

## Attribution and publishing

CamPi v1.0 was produced by Sam Mortimer, with all code "written" by ChatGPT. 

This project is not at all affiliated with Sony, Panasonic, JVC, Raspberry Pi Ltd or other listed hardware vendors. Theme names are parody only (and a light reference to The Simpsons).

As AI is the primary code writer, I fully encourage spinoffs and updates at your leisure. I do not claim to own the code, but I did produce the project and would appreciate a throwaway credit if that floats your boat.
 

### SonyCam Original Font

CamPi includes **SonyCam Original**, a recreation of the classic Sony Digital Handycam on-screen display typography.

- **Creator:** Xane Myers (Xane123)
- **Source:** https://fontstruct.com/fontstructions/show/1646448/sonycam-original
- **Licence:** Creative Commons Attribution-ShareAlike 3.0
- **Licence URL:** https://creativecommons.org/licenses/by-sa/3.0/

The font is distributed unmodified under its original licence. CamPi is an independent project and is not affiliated with or endorsed by Sony.

The font's licensing terms are separate from those of the CamPi application.

## References

- [Official Raspberry Pi kernel build documentation](https://www.raspberrypi.com/documentation/computers/linux_kernel.html)
- [Official Raspberry Pi PCIe documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html)
- [Raspberry Pi Linux source](https://github.com/raspberrypi/linux)
- [Debian dvgrab package](https://packages.debian.org/trixie/dvgrab)
- [Luma.LCD SPI documentation](https://luma-lcd.readthedocs.io/en/latest/hardware.html)
- [Discussion of Pi 5 FireWire DMA overlays](https://github.com/raspberrypi/linux/issues/7317)


