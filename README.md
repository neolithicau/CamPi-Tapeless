# CamPi

**A tapeless, touch-controlled DV/HDV and analog video recording interface for Raspberry Pi 5.**

CamPi turns a Raspberry Pi 5 into a self-contained **direct-to-edit video capture unit** for DV/HDV camcorders, DV/HDV tape decks and analog sources (including analog camcorders). Instead of requiring a chain of devices and conversion steps, this **directly records a native FireWire DV/HDV stream (or a USB dongle-based analog capture feed) to an SD card or attached USB storage device**, then makes the resulting raw files available for editing or archiving.

Designed for enthusiasts working with legacy camcorders and video equipment, CamPi is a DIY alternative for tapeless camcorder recording, H/DV transfer and digital video archiving. At the time of writing, all of the off-the-shelf options for tapeless camcorders do not interface with FireWire, and while there are several projects on the way in that space, I wanted one a bit quicker than that. Firewire was the original goal, and then I just kept expanding with additional features as outlined below.

My original build uses a **Pi 5 1gb, Raspberry Pi OS Lite 64-bit, PCIe TO MiniPCIe Adapter, a Startech PCIe-to-FireWire card (MPEX1394B3), and a 320 × 240 SPI touchscreen (Aliexpress / SHCHV 2.4inch display)**, and a little 5v buzzer on GPIO 16 (optional) for audible confirmation of recording start/stop. My capture directories are also shared via SAMBA on any connected Wifi, but that's a seperate set of optional configs which is out of the scope of this script.

> **Details of testing:**
> DV and HDV FireWire capture and tapedeck functions, analog captures, touchscreen and HDMI recording playback have been tested at length on my setup. I tested various Amazon el-cheapo analog video adapters, and various types of MiniDV and HDV cameras on the StarTech firewire adapter (MiniDV PAL, MiniDV NTSC and HDV units - all working great). You should see easy success with this script if you replicate my off-the-shelf environment as described above.

**NOTE:** Follow [INSTALL.md](INSTALL.md) and check the [limitations](#limitations-and-compatibility). This script is provided with absolutely zero support or warranty. I do not guarantee it will work in any instance outside my own operating environment and the prescribed hardware list, but I have produced it with some redundancy and fallbacks to allow it to operate in any similar environment. 

**AI use and slop disclosure:** This script was produced entirely using ChatGPT and I make absolutely zero guarantees around anything other than my attempts at quality control - I have polished the proverbial as much as it can be polished. As a package, it works, and it works great (for me). This isn't just some thing I've slopped out in 5 seconds - I have used, tested, refined and optimised the project at great length to squash as many bugs as possible.


## Features and what it does

| Function | CamPi behaviour |
| --- | --- |
| **Live DV capture** | Records native SD DV over IEEE 1394/FireWire using `dvgrab`, with PAL/NTSC stream identification. Outputs `.dv`. Home screen displays incoming data information including video and audio details. |
| **Live HDV capture** | Detects MPEG-2 transport-stream HDV over FireWire and records native `.m2t` files using `dvgrab`. Recognises formats such as 1080i and 720p where reported by the connected camera. |
| **DV/HDV Tape-deck transport functionality** | Provides FireWire AV/C transport controls for compatible H/DV devices, using `dvcont` where available. Camera-dependent, and your camera should be in tape playback mode before using this. Intended for use transferring tapes to digital. |
| **Analog capture** | Discovers V4L2 USB video inputs and matching ALSA audio input, then records **FFV1 lossless video** with **PCM audio** into `.mkv` using FFmpeg. Available modes depend on the dongle. This is a bonus feature and not a primary focus of the script. |
| **Storage** | Supports internal capture storage or writable mounted USB media. Auto-select mode prefers USB and falls back to internal storage when USB is not available. |
| **Touchscreen and buzzer** | Operates on a 320 × 240 ILI9341 SPI LCD with XPT2046 resistive touchscreen, including touch calibration, menu navigation, notifications and a configurable buzzer (optional). |
| **Recordings and HDMI playback** | View, delete or playback recordings (with FF/RW controls, etc) over a detected HDMI display using mpv (recommend the port closest to USB-C power in). A basic holding screen with storage and source information is shown when this mode is active. |
| **Networking** | Displays LAN/Wi-Fi connectivity and addresses, with basic NetworkManager Wi-Fi controls. Recordings can be exposed over an independently configured Samba share (not handled by this script). |
| **Settings and statuses** | Device overview, storage status, error history, network status, recording details and persistent preferences.  |
| **Themes** | Basic, Genuine Sorny, Genuine Panafonics and Genuine JayVC (all inspired from those original menu systems of 2000s camcorders; as well as being a bonus Simpsons joke). |

## Hardware

**Original verified configuration:**

- Raspberry Pi 5 1gb with **Raspberry Pi OS Lite 64-bit** (Debian Trixie based).
- 16K-page custom Raspberry Pi kernel, verified as `6.18.51-v8-16k-firewire+` on the original installation, with `CONFIG_FIREWIRE=m` and `CONFIG_FIREWIRE_OHCI=m`.
- PCIe breakout/HAT, plus **Startech PCIe-to-FireWire card (MPEX1394B3)** PCIe FireWire controller.
- IEEE 1394 DV/HDV camcorder or tape deck connected to the FireWire card.
- MPI2411-style **ILI9341 320 × 240 SPI LCD** and **XPT2046 touch controller** (REQUIRED).
- Optional passive 3-pin buzzer (Signal pin connected to GPIO16; VCC and GND as preferred).
- Optional USB storage and/or a V4L2/ALSA USB analogue video capture device. Original analogue capture testing used an MS210x-class dongle.
- Optional HDMI display for recording playback (Note: This was not designed as a live monitor - only to playback recordings).

**Note regarding touchscreen requirement:** No other type or size of screen has been scoped for this project. There is no fallback currently in place if you are not using one. This may be revisited in a future revision as it's probably the only missing interface feature.

### Original project LCD configuration

CamPi uses **BCM GPIO numbers** in settings, not physical header numbers.

| Signal | Default | Raspberry Pi physical pin |
| --- | --- | --- |
| LCD and touch SPI bus | SPI0 | SPI0 on GPIO10/9/11 for MOSI/MISO/SCLK |
| LCD chip select | CE0 / device 0 | 24 |
| Touch chip select | CE1 / device 1 | 26 |
| LCD D/C | BCM22 | 15 |
| LCD reset | BCM27 | 13 |
| Touch IRQ | BCM17 | 11 |
| Optional buzzer input | BCM16 | GPIO16 |

**Warning:** ILI9341/XPT2046 boards vary in pinout and supply voltage. Verify your *particular* screen schematic. Do not apply 5 V to Pi GPIO inputs. Configurable pins do not make unsupported displays compatible: the code currently renders to **ILI9341** specifically.

## Quick overview of setup steps

1. Install Raspberry Pi OS Lite 64-bit and enable SPI and PCIe.
2. Build or install a kernel with FireWire support and verify your actual IEEE 1394 card.
3. Install CamPi's Python and Linux dependencies.
4. Connect the compatible SPI display/touch hardware.
5. Run `campi.py` manually for calibration and hardware validation.
6. Optionally install the systemd service for automatic startup and Samba for access from a PC.

**All commands, a tested-hardware baseline, kernel build, permissions and recovery instructions:** **[INSTALL.md](INSTALL.md)**.

## Configuration

CamPi keeps settings/calibration under `~/.config/tapeless/` by default. Recordings default to `~/captures/`. A preferred USB mount is `/mnt/usb-captures/`, but CamPi also discovers other mounted writable USB volumes through `lsblk`.

Optional environment overrides are in [examples/campi.env.example](examples/campi.env.example). You can ignore this setup and just use the script itself for permanent variable settings if you prefer. 

Examples of campi.env:

```ini
CAMPI_LCD_DC=22
CAMPI_LCD_RST=27
CAMPI_TOUCH_IRQ_GPIO=17
CAMPI_INTERNAL_CAPTURE_DIR=/home/myuser/captures
CAMPI_USB_CAPTURE_DIR=/mnt/usb-captures
```

The sample service loads overrides from `/etc/campi/campi.env` if present.

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
tests/test_portability.py          Hardware-independent static tests
examples/pciex1-32bit-dma.dts     Optional experimental DMA overlay source
.gitignore                        Excludes local configuration, recordings and builds
```

## Limitations, compatibility and any other notes

- Again - **only the outlined set of hardware is known-working**. All other usage environments are untested.
- A **custom kernel may be necessary** for proper Firewire support. Rebuilding your kernel after any Raspberry Pi OS kernel updates may be required. Recommend just sticking to whatever version you build it with and never update (this thing is primarily used offline anyway).
- The **StarTech Firewire card mentioned is tested and working on system power only** - no additional power required.
- **Touch calibration** is required on first run. There is currently no fallback in place for cases where a touchscreen is not detected.
- The tape-deck functions are designed for a **compatible DV AV/C transport**, and functionality is not guaranteed for every single iteration of DV/HDV deck or camera. You should put your camera in tape playback mode before using this feature. I tested the DV tape-deck using Sony DV and HDV equipment and it worked well.
- **USB analogue capture** requires a V4L2 device and typically an ALSA input. Pixel formats, PAL/NTSC support and audio pairing depend on the quality of the actual dongle.
- **HDMI playback of recordings is not guaranteed on every single audio/display configuration**. Use the port closest to the USB-C power input. It's not intended for use as a live monitor view of an incoming feed.
- **Samba, USB automount, sudo permission for Reboot/Shut down, and NetworkManager setup**: this script does not install or configure these functions automatically. If you want these features, you will need to configure them seperately.
- **If the script terminates suddenly, make sure your power supply is strong enough for all your accessories.** If the Pi is suffering from low voltage or current drag, it will just stop running or crash sporadically. My battery setup could not power an analog USB dongle, external hard drive and the Pi at the same time. Recommend using internal storage only for analog capture. 

## Attribution/Credits

CamPi v1.0 was produced by Sam Mortimer, with all code "written" by ChatGPT. 

This project is not at all affiliated with Sony, Panasonic, JVC, Raspberry Pi Ltd or other listed hardware vendors. Theme names are parody only.

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


