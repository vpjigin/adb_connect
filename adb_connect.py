#!/usr/bin/env python3
"""
adb_connect.py - Find ADB, discover Android devices on the local network, and connect.

Discovery uses two methods:
  1. mDNS (`adb mdns services`) - finds phones with Android 11+ "Wireless debugging" on
     (these use a random port, so a plain port scan won't find them).
  2. A threaded scan of your subnet for open ADB ports (default 5555) - finds phones
     that were switched to TCP mode with `adb tcpip 5555`.

Usage:
    python adb_connect.py
    python adb_connect.py --ports 5555 5556
    python adb_connect.py --subnet 192.168.1.0/24
    python adb_connect.py --adb "C:\\platform-tools\\adb.exe"
    python adb_connect.py --no-scan          # mDNS only (fast)

Requires Python 3.7+. No third-party packages.
"""
import argparse
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".adb_connect.json")
DOWNLOAD_PAGE = "https://developer.android.com/tools/releases/platform-tools"
DOWNLOAD_ZIPS = {
    "Windows": "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    "Darwin": "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip",
    "Linux": "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
}
SYSTEM = platform.system()
ADB_EXE = "adb.exe" if SYSTEM == "Windows" else "adb"


# --------------------------------------------------------------------------- config
def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# --------------------------------------------------------------------------- finding adb
def candidate_paths():
    home = os.path.expanduser("~")
    c = []
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        v = os.environ.get(var)
        if v:
            c.append(os.path.join(v, "platform-tools", ADB_EXE))

    if SYSTEM == "Windows":
        local = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        c += [
            os.path.join(local, "Android", "Sdk", "platform-tools", ADB_EXE),
            r"C:\platform-tools\adb.exe",
            r"C:\adb\adb.exe",
            r"C:\Android\platform-tools\adb.exe",
            os.path.join(pf, "platform-tools", ADB_EXE),
            os.path.join(pf86, "Android", "android-sdk", "platform-tools", ADB_EXE),
            os.path.join(home, "Downloads", "platform-tools", ADB_EXE),
            os.path.join(home, "scoop", "apps", "adb", "current", "platform-tools", ADB_EXE),
            r"C:\ProgramData\chocolatey\bin\adb.exe",
        ]
    elif SYSTEM == "Darwin":
        c += [
            os.path.join(home, "Library", "Android", "sdk", "platform-tools", "adb"),
            "/opt/homebrew/bin/adb",
            "/usr/local/bin/adb",
            os.path.join(home, "Downloads", "platform-tools", "adb"),
        ]
    else:
        c += [
            os.path.join(home, "Android", "Sdk", "platform-tools", "adb"),
            "/usr/bin/adb",
            "/usr/local/bin/adb",
            "/opt/android-sdk/platform-tools/adb",
            "/usr/lib/android-sdk/platform-tools/adb",
            os.path.join(home, "Downloads", "platform-tools", "adb"),
            "/snap/bin/adb",
        ]
    return c


def is_valid_adb(path):
    if not path or not os.path.isfile(path):
        return False
    try:
        r = subprocess.run([path, "version"], capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and "Android Debug Bridge" in (r.stdout + r.stderr)
    except Exception:
        return False


def normalize_user_path(raw):
    p = os.path.expanduser(raw.strip().strip('"').strip("'"))
    if os.path.isdir(p):
        for sub in ("", "platform-tools"):
            exe = os.path.join(p, sub, ADB_EXE)
            if os.path.isfile(exe):
                return exe
    return p


def print_download_help():
    print("\n  ADB was not found on this computer.")
    print("  Download the latest Android SDK Platform-Tools here:")
    print(f"    Official page : {DOWNLOAD_PAGE}")
    if SYSTEM in DOWNLOAD_ZIPS:
        print(f"    Direct ({SYSTEM}) : {DOWNLOAD_ZIPS[SYSTEM]}")
    else:
        for name, url in DOWNLOAD_ZIPS.items():
            print(f"    {name:8}: {url}")
    print("  Extract the zip, then give me the path to the 'platform-tools' folder or adb itself.\n")


def find_adb(cli_path=None):
    cfg = load_config()
    tried = []

    sources = [cli_path, cfg.get("adb_path"), os.environ.get("ADB"), shutil.which("adb")]
    sources += candidate_paths()

    for p in sources:
        if not p or p in tried:
            continue
        tried.append(p)
        p = normalize_user_path(p)
        if is_valid_adb(p):
            if cfg.get("adb_path") != p:
                cfg["adb_path"] = p
                save_config(cfg)
            return p

    print_download_help()
    while True:
        raw = input("Path to adb (or 'q' to quit): ").strip()
        if raw.lower() in ("q", "quit", "exit"):
            sys.exit(1)
        if not raw:
            continue
        p = normalize_user_path(raw)
        if is_valid_adb(p):
            cfg["adb_path"] = p
            save_config(cfg)
            print(f"  OK, using {p} (saved for next time)\n")
            return p
        print("  That doesn't look like a working adb binary. Try again.")


# --------------------------------------------------------------------------- adb helpers
def run_adb(adb, *args, timeout=20):
    try:
        r = subprocess.run([adb, *args], capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 1, "timed out"
    except Exception as e:
        return 1, str(e)


def list_attached(adb):
    """Return list of dicts from `adb devices -l`."""
    _, out = run_adb(adb, "devices", "-l")
    devices = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or line.startswith("*"):
            continue
        info = dict(kv.split(":", 1) for kv in parts[2:] if ":" in kv)
        devices.append({
            "serial": parts[0],
            "state": parts[1],
            "model": info.get("model", ""),
            "usb": not re.match(r"^[\d.]+:\d+$", parts[0]) and "._adb-tls" not in parts[0],
        })
    return devices


def print_attached(adb):
    devs = list_attached(adb)
    print("\n  Currently attached devices:")
    if not devs:
        print("    (none)")
    for d in devs:
        kind = "USB" if d["usb"] else "Wi-Fi"
        print(f"    {d['serial']:<32} {d['state']:<12} {kind:<6} {d['model']}")
    print()


# --------------------------------------------------------------------------- discovery
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packets are sent for UDP connect
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return None
    finally:
        s.close()


def discover_mdns(adb):
    _, out = run_adb(adb, "mdns", "services", timeout=10)
    results = []
    for line in out.splitlines():
        # Names can contain spaces, e.g. "adb-R5CX12ABC-xYz12A (2)"
        m = re.search(r"^\s*(.*?)\s+(_adb[-\w]*\._tcp)\.?\s+([\d.]+):(\d+)", line)
        if m:
            name = re.sub(r"\s*\(\d+\)$", "", m.group(1)).strip()
            sm = re.match(r"^adb-(.+)-[^-]+$", name)
            results.append({
                "name": name,
                "serial": sm.group(1) if sm else name,
                "service": m.group(2),
                "ip": m.group(3),
                "port": int(m.group(4)),
            })
    return results


def _port_open(ip, port, timeout):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def scan_subnet(network, ports, timeout):
    hosts = [str(h) for h in network.hosts()]
    targets = [(h, p) for h in hosts for p in ports]
    print(f"  Scanning {network} on port(s) {', '.join(map(str, ports))} "
          f"({len(targets)} checks)...")
    with ThreadPoolExecutor(max_workers=256) as ex:
        flags = list(ex.map(lambda t: _port_open(t[0], t[1], timeout), targets))
    return [t for t, ok in zip(targets, flags) if ok]


def discover(adb, args):
    entries, pairing = [], []
    seen = set()

    print("  Looking for devices via mDNS (Wireless debugging)...")
    for s in discover_mdns(adb):
        target = f"{s['ip']}:{s['port']}"
        if "pairing" in s["service"]:
            pairing.append({"target": target, "name": s["name"]})
            continue
        if target not in seen:
            seen.add(target)
            entries.append({"target": target, "ip": s["ip"], "serial": s["serial"],
                            "raw": s["name"], "kind": "wireless debugging"})

    if not args.no_scan:
        if args.subnet:
            network = ipaddress.ip_network(args.subnet, strict=False)
        else:
            ip = get_local_ip()
            if not ip or ip.startswith("127."):
                print("  Could not detect your local IP; use --subnet 192.168.x.0/24")
                network = None
            else:
                network = ipaddress.ip_network(f"{ip}/24", strict=False)
        if network:
            for ip, port in scan_subnet(network, args.ports, args.timeout):
                target = f"{ip}:{port}"
                if target not in seen:
                    seen.add(target)
                    entries.append({"target": target, "ip": ip, "serial": None,
                                    "raw": "unknown device", "kind": f"port {port}"})

    label_entries(adb, entries)
    return entries, pairing


def label_entries(adb, entries):
    """Give each entry a readable name: live model if connected, else the remembered name."""
    attached = list_attached(adb)
    known = load_config().get("devices", {})
    for e in entries:
        att = next((d for d in attached if d["serial"] == e["target"]
                    or (e["serial"] and d["serial"].startswith(f"adb-{e['serial']}-"))), None)
        name = known.get(e["serial"] or "") or known.get(e["ip"])
        if not name and att and att["model"]:
            name = att["model"].replace("_", "-")
        e["connected"] = bool(att and att["state"] == "device")
        e["label"] = f"{name or e['raw']}  ({e['kind']})" + ("  [connected]" if e["connected"] else "")


def remember_device(adb, target, serial=None):
    """Read manufacturer/model from the phone and cache it by serial and IP."""
    _, man = run_adb(adb, "-s", target, "shell", "getprop", "ro.product.manufacturer", timeout=8)
    _, model = run_adb(adb, "-s", target, "shell", "getprop", "ro.product.model", timeout=8)
    if not model or "error" in model.lower():
        return None
    name = f"{man.strip().title()} {model.strip()}".strip()
    cfg = load_config()
    devs = cfg.setdefault("devices", {})
    for key in filter(None, (serial, target.split(":")[0])):
        devs[key] = name
    save_config(cfg)
    return name


# --------------------------------------------------------------------------- actions
def connect(adb, target, serial=None):
    print(f"  Connecting to {target}...")
    _, out = run_adb(adb, "connect", target, timeout=15)
    print(f"  {out}")
    ok = "connected to" in out.lower()
    if ok:
        name = remember_device(adb, target, serial)
        if name:
            print(f"  Device: {name}")
    else:
        print("  Tips: make sure the phone is on the same Wi-Fi and unlocked. On Android 11+ "
              "Wireless debugging, pair first (option 'p'); the connect port changes each time.")
    return ok


def pair(adb, pairing):
    print("\n  On the phone: Developer options > Wireless debugging > Pair device with pairing code")
    default = pairing[0]["target"] if pairing else ""
    prompt = f"  Pairing IP:port [{default}]: " if default else "  Pairing IP:port: "
    target = input(prompt).strip() or default
    if not target:
        return
    code = input("  6-digit pairing code: ").strip()
    _, out = run_adb(adb, "pair", target, code, timeout=30)
    print(f"  {out}")
    if "success" in out.lower():
        print("  Paired. Rescan (r) to find the connect port, then select the device.")


def usb_to_wifi(adb):
    usb = [d for d in list_attached(adb) if d["usb"] and d["state"] == "device"]
    if not usb:
        print("  No authorized USB device found. Plug in the phone with USB debugging on.")
        return False
    for i, d in enumerate(usb, 1):
        print(f"    {i}. {d['serial']}  {d['model']}")
    choice = input("  Pick USB device [1]: ").strip() or "1"
    try:
        d = usb[int(choice) - 1]
    except (ValueError, IndexError):
        print("  Invalid choice.")
        return False

    serial = d["serial"]
    _, out = run_adb(adb, "-s", serial, "shell", "ip", "-f", "inet", "addr", "show", "wlan0")
    m = re.search(r"inet ([\d.]+)", out)
    if not m:
        _, out = run_adb(adb, "-s", serial, "shell", "ip", "route")
        m = re.search(r"src ([\d.]+)", out)
    if not m:
        print("  Couldn't read the phone's Wi-Fi IP. Is Wi-Fi on?")
        return False
    ip = m.group(1)
    print(f"  Phone IP: {ip}. Switching to TCP mode on 5555...")
    _, out = run_adb(adb, "-s", serial, "tcpip", "5555")
    print(f"  {out}")
    time.sleep(2)
    return connect(adb, f"{ip}:5555")


# --------------------------------------------------------------------------- main
def menu(adb, args):
    entries, pairing = discover(adb, args)
    while True:
        print("\n  Devices found on the network:")
        if not entries:
            print("    (none)")
        for i, e in enumerate(entries, 1):
            print(f"    {i}. {e['target']:<22} {e['label']}")
        if pairing:
            print("\n  Waiting to pair (use 'p'):")
            for p in pairing:
                print(f"       {p['target']:<22} {p['name']}")

        print("\n  [number] connect   m) manual IP   p) pair (Android 11+)   "
              "u) USB -> Wi-Fi\n  a) attached devices   d) disconnect all   r) rescan   q) quit")
        choice = input("\n  > ").strip().lower()

        if choice in ("q", "quit", "exit"):
            return
        if choice == "r":
            entries, pairing = discover(adb, args)
        elif choice == "m":
            t = input("  IP[:port] (default port 5555): ").strip()
            if t:
                if ":" not in t:
                    t += ":5555"
                if connect(adb, t):
                    print_attached(adb)
        elif choice == "p":
            pair(adb, pairing)
        elif choice == "u":
            if usb_to_wifi(adb):
                print_attached(adb)
        elif choice == "a":
            print_attached(adb)
        elif choice == "d":
            _, out = run_adb(adb, "disconnect")
            print(f"  {out}")
            label_entries(adb, entries)
        elif choice.isdigit() and 1 <= int(choice) <= len(entries):
            e = entries[int(choice) - 1]
            if connect(adb, e["target"], e["serial"]):
                label_entries(adb, entries)
                print_attached(adb)
        else:
            print("  Invalid choice.")


def main():
    ap = argparse.ArgumentParser(description="Discover and connect Android devices over Wi-Fi with ADB.")
    ap.add_argument("--adb", help="Path to adb executable or platform-tools folder")
    ap.add_argument("--subnet", help="Subnet to scan, e.g. 192.168.1.0/24 (default: auto /24)")
    ap.add_argument("--ports", type=int, nargs="+", default=[5555], help="Ports to scan (default 5555)")
    ap.add_argument("--timeout", type=float, default=0.4, help="Per-host scan timeout in seconds")
    ap.add_argument("--no-scan", action="store_true", help="Skip the subnet scan, use mDNS only")
    args = ap.parse_args()

    adb = find_adb(args.adb)
    _, ver = run_adb(adb, "version")
    print(f"\n  Using adb: {adb}\n  {ver.splitlines()[0] if ver else ''}")
    run_adb(adb, "start-server")

    try:
        menu(adb, args)
    except (KeyboardInterrupt, EOFError):
        print("\n  Bye.")


if __name__ == "__main__":
    main()
