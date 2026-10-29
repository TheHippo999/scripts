#!/usr/bin/env python3
"""
net_monitor.py — Internet uptime + speed monitor

WHAT IT DOES
  1. Uptime check: every CHECK_INTERVAL_SECONDS, tries to open a TCP
     connection to a couple of rock-solid public hosts (Cloudflare /
     Google DNS on port 53/443). No raw ICMP, no admin/root needed,
     works the same on Windows/Mac/Linux.
     Only STATE CHANGES (up -> down, down -> up) get written to the
     log, so the file stays readable instead of filling up with a
     line every 30 seconds.

  2. Speed test: every SPEEDTEST_INTERVAL_SECONDS (default hourly),
     or whenever you run the script with --speedtest, it runs a real
     speed test (via the `speedtest-cli` library) and logs
     download/upload/ping.

LOG FILES
  Plain text (human-readable, open in TextEdit):
    uptime_log.txt     "2026-08-31 14:32:10 | DOWN"
                        "2026-08-31 14:36:42 | UP (outage lasted 4m32s)"
    speedtest_log.txt   "2026-08-31 14:00:05 | Down: 85.3 Mbps | Up: 12.1 Mbps | Ping: 14 ms"

  CSV (same data, structured — open directly in Excel):
    uptime_log.csv      timestamp, event, outage_duration_seconds
    speedtest_log.csv   timestamp, download_mbps, upload_mbps, ping_ms, server, error, vpn, vpn_location
                        (ping_ms is left blank, with a note in "error", when the
                        speedtest library returns a bogus reading like 1800000 ms)
                        (vpn is 1 if a VPN connection was detected at test time, else 0 —
                        see is_vpn_connected())
                        (vpn_location is the geolocated city/region/country of your public
                        IP at test time, e.g. "Melbourne, VIC, AU" — only looked up when
                        vpn=1, left blank otherwise. See get_vpn_location().)

SETUP
  pip install speedtest-cli

USAGE
  python net_monitor.py                # run continuously (monitor + hourly speed tests)
  python net_monitor.py --speedtest    # run ONE speed test right now and exit
  python net_monitor.py --summary      # print a quick readable summary of the logs so far

  Leave it running in a terminal, or set it up as a background/scheduled
  task (see notes at the bottom of this file) so it survives reboots.
"""

import argparse
import csv
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------- CONFIG ---
VERSION = "1.4.0"                     # bump this each time you generate/copy a new build

# Used to geolocate your public IP when a VPN is detected, so you can see
# which exit server/location a given speed test ran through. No API key
# needed for light, occasional use (we only call this when vpn=1, i.e. at
# most once per speed test).
VPN_LOCATION_LOOKUP_URL = "https://ipinfo.io/json"
VPN_LOCATION_TIMEOUT_SECONDS = 5

CHECK_INTERVAL_SECONDS = 30          # how often to test connectivity
SPEEDTEST_INTERVAL_SECONDS = 60 * 60  # how often to auto-run a speed test (1 hour)

# Ookla's speedtest library occasionally returns junk ping values (seen in
# the wild: 1800000 ms) instead of failing cleanly. Anything above this
# threshold is treated as bad data, not a real measurement.
MAX_VALID_PING_MS = 2000

# (host, port) pairs used for the uptime check — TCP connect, no admin needed
PROBE_TARGETS = [
    ("1.1.1.1", 53),   # Cloudflare DNS
    ("8.8.8.8", 53),   # Google DNS
]
PROBE_TIMEOUT_SECONDS = 5

LOG_DIR = Path(__file__).resolve().parent
UPTIME_LOG = LOG_DIR / "uptime_log.txt"
SPEEDTEST_LOG = LOG_DIR / "speedtest_log.txt"
UPTIME_CSV = LOG_DIR / "uptime_log.csv"
SPEEDTEST_CSV = LOG_DIR / "speedtest_log.csv"
# -----------------------------------------------------------------------------


def ensure_csv(path: Path, header: list) -> None:
    """Create the CSV with a header row if it doesn't exist yet."""
    if not path.exists():
        with path.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(header)


def append_csv_row(path: Path, row: list) -> None:
    with path.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def is_connected() -> bool:
    """True if we can open a TCP connection to at least one probe target."""
    for host, port in PROBE_TARGETS:
        try:
            with socket.create_connection((host, port), timeout=PROBE_TIMEOUT_SECONDS):
                return True
        except OSError:
            continue
    return False


def is_vpn_connected() -> bool:
    """
    Best-effort check for whether traffic is currently routing through a VPN
    (e.g. ExpressVPN). We don't rely on any VPN vendor's CLI — ExpressVPN's
    menu bar app doesn't ship one — instead we ask macOS which interface the
    default route is using. VPN clients (ExpressVPN included) install a
    virtual "utun" interface and point the default route at it once
    connected; your normal Wi-Fi/Ethernet route uses "en0"/"en1" etc.

    This is macOS-specific (uses `route get default`). On other platforms
    it always returns False — let us know if you end up running this
    somewhere other than your Mac and we can add a branch for it.
    """
    if sys.platform != "darwin":
        return False

    try:
        result = subprocess.run(
            ["route", "get", "default"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("interface:"):
            interface = line.split(":", 1)[1].strip()
            return interface.startswith("utun") or interface.startswith("ppp") or interface.startswith("ipsec")

    return False


def get_vpn_location() -> str:
    """
    Best-effort geolocation of your current public IP (i.e. your VPN exit
    node, since this is only called when is_vpn_connected() is True).
    Returns something like "Melbourne, VIC, AU", or "" if the lookup fails
    for any reason (no internet, service down, rate-limited, etc.) — a
    failed lookup should never take down the whole speed test.
    """
    try:
        req = urllib.request.Request(
            VPN_LOCATION_LOOKUP_URL,
            headers={"User-Agent": "net_monitor.py"},
        )
        with urllib.request.urlopen(req, timeout=VPN_LOCATION_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return ""

    city = data.get("city", "")
    region = data.get("region", "")
    country = data.get("country", "")
    parts = [p for p in (city, region, country) if p]
    return ", ".join(parts)


def format_duration(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return "".join(parts)


def run_speedtest() -> None:
    """Run one speed test and append the result to speedtest_log.txt."""
    try:
        import speedtest
    except ImportError:
        print("speedtest-cli isn't installed. Run: pip install speedtest-cli")
        append_line(SPEEDTEST_LOG, f"{now_str()} | ERROR: speedtest-cli not installed")
        return

    print(f"[{now_str()}] Running speed test...")
    ensure_csv(
        SPEEDTEST_CSV,
        ["timestamp", "download_mbps", "upload_mbps", "ping_ms", "server", "error", "vpn", "vpn_location"],
    )

    vpn_connected = is_vpn_connected()
    vpn_flag = "1" if vpn_connected else "0"
    vpn_location = get_vpn_location() if vpn_connected else ""
    vpn_suffix = f" | VPN: {vpn_flag}" + (f" ({vpn_location})" if vpn_location else "")

    try:
        st = speedtest.Speedtest()
        st.get_best_server()
        download_mbps = st.download() / 1_000_000
        upload_mbps = st.upload() / 1_000_000
        raw_ping_ms = st.results.ping
        server_name = st.results.server.get("sponsor", "") if st.results.server else ""

        ping_valid = raw_ping_ms is not None and 0 <= raw_ping_ms <= MAX_VALID_PING_MS
        ping_display = f"{raw_ping_ms:.0f} ms" if ping_valid else "invalid"
        ping_csv = f"{raw_ping_ms:.0f}" if ping_valid else ""
        note = "" if ping_valid else f"ping discarded (raw value: {raw_ping_ms})"

        line = (
            f"{now_str()} | Down: {download_mbps:.1f} Mbps | "
            f"Up: {upload_mbps:.1f} Mbps | Ping: {ping_display}"
            f"{vpn_suffix}"
        )
        append_csv_row(
            SPEEDTEST_CSV,
            [now_str(), f"{download_mbps:.2f}", f"{upload_mbps:.2f}", ping_csv, server_name, note, vpn_flag, vpn_location],
        )
    except Exception as e:
        line = f"{now_str()} | ERROR: speed test failed ({e}){vpn_suffix}"
        append_csv_row(SPEEDTEST_CSV, [now_str(), "", "", "", "", str(e), vpn_flag, vpn_location])

    append_line(SPEEDTEST_LOG, line)
    print(line)


def monitor_loop() -> None:
    print(f"Monitoring started. Logs:\n  {UPTIME_LOG}\n  {SPEEDTEST_LOG}")
    print("Press Ctrl+C to stop.\n")

    ensure_csv(UPTIME_CSV, ["timestamp", "event", "outage_duration_seconds"])

    connected = is_connected()
    state_changed_at = datetime.now()
    append_line(UPTIME_LOG, f"{now_str()} | {'UP' if connected else 'DOWN'} (monitor started)")
    append_csv_row(UPTIME_CSV, [now_str(), "UP" if connected else "DOWN", ""])

    last_speedtest = datetime.now()
    # Run one speed test right away so you have a baseline
    run_speedtest()

    try:
        while True:
            time.sleep(CHECK_INTERVAL_SECONDS)
            now_connected = is_connected()

            if now_connected != connected:
                duration = datetime.now() - state_changed_at
                if now_connected:
                    append_line(
                        UPTIME_LOG,
                        f"{now_str()} | UP (outage lasted {format_duration(duration)})",
                    )
                    append_csv_row(UPTIME_CSV, [now_str(), "UP", f"{int(duration.total_seconds())}"])
                else:
                    append_line(UPTIME_LOG, f"{now_str()} | DOWN")
                    append_csv_row(UPTIME_CSV, [now_str(), "DOWN", ""])
                connected = now_connected
                state_changed_at = datetime.now()

            if datetime.now() - last_speedtest >= timedelta(seconds=SPEEDTEST_INTERVAL_SECONDS):
                if connected:
                    run_speedtest()
                last_speedtest = datetime.now()

    except KeyboardInterrupt:
        print("\nMonitoring stopped.")


def print_summary() -> None:
    print("=== Uptime log ===")
    if UPTIME_LOG.exists():
        lines = UPTIME_LOG.read_text(encoding="utf-8").splitlines()
        down_events = [l for l in lines if "DOWN" in l]
        print(f"Total DOWN events recorded: {len(down_events)}")
        for l in lines[-10:]:
            print(" ", l)
    else:
        print("No uptime log yet.")

    print("\n=== Speed test log (last 10) ===")
    if SPEEDTEST_LOG.exists():
        lines = SPEEDTEST_LOG.read_text(encoding="utf-8").splitlines()
        for l in lines[-10:]:
            print(" ", l)
    else:
        print("No speed test log yet.")


def main():
    print(f"net_monitor.py v{VERSION}")

    parser = argparse.ArgumentParser(description="Internet uptime + speed monitor")
    parser.add_argument("--speedtest", action="store_true", help="Run one speed test now and exit")
    parser.add_argument("--summary", action="store_true", help="Print a quick summary of the logs")
    args = parser.parse_args()

    if args.speedtest:
        run_speedtest()
    elif args.summary:
        print_summary()
    else:
        monitor_loop()


if __name__ == "__main__":
    main()

# -----------------------------------------------------------------------------
# RUNNING IT LONG-TERM
#
# macOS / Linux (cron doesn't suit a continuous loop well — use a simple
# background run, or a systemd user service on Linux):
#   nohup python3 net_monitor.py > monitor_output.log 2>&1 &
#
# Linux systemd (recommended for reliability across reboots):
#   Create ~/.config/systemd/user/net-monitor.service with:
#     [Unit]
#     Description=Internet uptime and speed monitor
#     [Service]
#     ExecStart=/usr/bin/python3 /path/to/net_monitor.py
#     Restart=always
#     [Install]
#     WantedBy=default.target
#   Then: systemctl --user enable --now net-monitor.service
#
# Windows: use Task Scheduler to run "pythonw net_monitor.py" at logon,
# or just leave a terminal window open.
# -----------------------------------------------------------------------------