# EOS 5D Shutter Count

Reads the shutter count of a **Canon EOS 5D (classic, 2005)** over USB on Windows.
No Canon software, Zadig, third-party drivers or certificates are needed.

![status](https://img.shields.io/badge/platform-Windows%2010%2F11%20x64-blue)
![license](https://img.shields.io/badge/license-GPL--3.0-green)

## Download and use

1. Download `EOS5D_ShutterCount.exe` from the [Releases](../../releases) page.
   It is a single file with everything inside — nothing to install.
2. On the camera, set **Menu > Communication > Print/PTP**, then turn it on and connect it
   to the PC with a USB cable.
3. Start the program (Windows asks for administrator rights — this is required).
4. Wait until the *Camera* panel says **Connected – normal mode**.
5. Click **Check shutter count**. After a few seconds the count appears.

The window shows:

- **Camera** — not connected / connected in normal mode / connected in service mode, and the current Windows driver.
- **Shutter count** — the result, or *Failed* if something went wrong.
- **Log** — everything the process does, step by step.

A log file, `shutter_count_log.txt`, is written next to the exe (each line is time-stamped,
new runs are appended). Attach it when reporting a problem.

## What it does

1. Temporarily switches the camera's USB device to Microsoft's built-in **WinUSB** driver
   (`%WINDIR%\INF\winusb.inf`, already part of Windows).
2. Sends the PTP command `0x901F`, which re-enumerates the camera as its service-mode USB
   device (`04A9:3086`), and gives that device WinUSB too.
3. Opens a Canon DCP session, runs `MonOpen`, reads 24 bytes of camera memory at
   `0x938..0x94F`, runs `MonClose` and returns the camera to PTP mode (`SetUSBToPTPMode`).
4. Removes the modified device nodes so Windows binds its normal MTP driver again.
5. Decodes the count: `page` = big-endian 16-bit value at `0x943`, `live` = `ring[page % 8]`
   where `ring` is the 8 bytes at `0x945`; **count = page × 256 + live**.
   If `live` is `0xFF` the result is accurate to within 255.

The camera is left in its normal state and with its normal Windows driver afterwards.

## Requirements

- Windows 10 or 11, 64-bit
- Administrator rights
- Canon EOS 5D (original model, USB ID `04A9:3102`). Other models are not supported.

## Running from source

The repository contains:

| File | Purpose |
| --- | --- |
| `eos5d_shutter_count_auto2.py` | The reader itself (command-line script) |
| `shutter_count_app.py` | The window; runs the script's `main()` unchanged and shows its output |
| `build_exe.bat` | Builds the single-file exe with PyInstaller |
| `requirements.txt` | Python packages needed |

Needs 64-bit Python 3.10+ on 64-bit Windows:

```bat
py -m pip install -r requirements.txt
```

Command line (from an elevated PowerShell/CMD):

```bat
py eos5d_shutter_count_auto2.py        rem add -v for raw packet output
```

Window version:

```bat
py shutter_count_app.py
```

Build the exe (result in `dist\EOS5D_ShutterCount.exe`):

```bat
build_exe.bat
```

## Troubleshooting

- **"Canon EOS 5D 04A9:3102 was not found"** — the camera is off, not connected, or the
  cable/port is faulty. Check that Windows sees it in *This PC* first.
- **Camera stays in service mode** — run the check again; it continues from service mode.
  If needed, switch the camera off and on.
- **Camera not visible in Windows after a failed run** — unplug and re-plug the cable;
  Windows reinstalls its normal driver.
- Always look at the log (window or `shutter_count_log.txt`) for the exact step that failed.

## License

This project is licensed under the **GNU General Public License v3.0** — see [LICENSE](LICENSE).
You may use, study, share and modify it; versions you distribute must stay under GPL-3.0
and include their source code.

## Disclaimer

This tool talks to the camera's service interface and changes Windows USB drivers
temporarily. It is provided as is, without any warranty; use it at your own risk.
Not affiliated with or endorsed by Canon Inc. "Canon" and "EOS" are trademarks of Canon Inc.
