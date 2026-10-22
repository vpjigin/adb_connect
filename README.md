# adb_connect

Find Android devices on your local network and connect to them over Wi-Fi with ADB, from one interactive menu.

Works on macOS, Windows and Linux. It needs only Python 3.7+ and has no third-party packages.

## Features

- **Finds adb automatically.** It checks your `PATH`, `ANDROID_HOME` / `ANDROID_SDK_ROOT`, the Android Studio SDK and common install folders. If adb isn't found, it shows the download link and asks for the path. The path is remembered after that.
- **Discovers devices two ways:**
  - **mDNS:** finds phones with Android 11+ *Wireless debugging* turned on, even though they use random ports.
  - **Subnet scan:** checks your `/24` network for open ADB ports (5555 by default).
- **Readable device names.** The first time it connects to a phone, it reads the manufacturer and model (e.g. `Samsung SM-S921B`) and shows that name in later scans.
- **Pairing, USB-to-Wi-Fi and manual connect**, all from the same menu.

## Usage

```bash
python3 adb_connect.py
```

| Option | Description |
| --- | --- |
| `--adb PATH` | Path to adb, or to the `platform-tools` folder |
| `--subnet CIDR` | Subnet to scan, e.g. `192.168.1.0/24` (default: your local /24) |
| `--ports N [N ...]` | Ports to scan (default `5555`) |
| `--timeout SEC` | Timeout for each host in the scan (default `0.4`) |
| `--no-scan` | Use mDNS only and skip the subnet scan (faster) |

### Menu

```
  Devices found on the network:
    1. 192.168.1.4:45089      Samsung SM-S921B  (wireless debugging)  [connected]

  [number] connect   m) manual IP   p) pair (Android 11+)   u) USB -> Wi-Fi
  a) attached devices   d) disconnect all   r) rescan   q) quit
```

## Connecting a phone

### Android 11+ (Wireless debugging)

1. On the phone, open **Settings → Developer options → Wireless debugging** and turn it on.
2. The first time only: tap **Pair device with pairing code**. In the script, choose `p` and enter the IP:port and the 6-digit code shown on the phone.
3. Choose `r` to rescan, then choose the device number to connect.

The connect port changes whenever Wireless debugging is turned off and on again. Rescan to pick up the new port.

### Older Android (USB first)

1. Connect the phone by USB with **USB debugging** on, and accept the prompt on the phone.
2. In the script, choose `u`. It reads the phone's Wi-Fi IP, runs `adb tcpip 5555` and connects.
3. Unplug the cable.

## Getting adb

If adb isn't installed, download **Android SDK Platform-Tools**:

- Official page: https://developer.android.com/tools/releases/platform-tools
- macOS: https://dl.google.com/android/repository/platform-tools-latest-darwin.zip
- Windows: https://dl.google.com/android/repository/platform-tools-latest-windows.zip
- Linux: https://dl.google.com/android/repository/platform-tools-latest-linux.zip

On macOS you can also run `brew install android-platform-tools`.

mDNS discovery needs platform-tools 31 or newer.

## Saved settings

The adb path and known device names are stored in `~/.adb_connect.json`. Delete that file to reset them.
