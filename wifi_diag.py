#!/usr/bin/env python3
"""Sequential Wi-Fi and home network diagnostics CLI (Linux and macOS).

Runs RF inspection, idle latency/traceroute, uncached DNS, and loaded
throughput/bufferbloat checks strictly sequentially so idle latency is
never polluted by load. On Linux, tests both active Ethernet and Wi-Fi
interfaces side by side without disconnecting Ethernet.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import platform
import random
import re
import shutil
import socket
import string
import struct
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CLOUDFLARE_DL_URL = "https://speed.cloudflare.com/__down?bytes=50000000"
CLOUDFLARE_UL_URL = "https://speed.cloudflare.com/__up"
CLOUDFLARE_HEADERS = [
    "-H",
    "Referer: https://speed.cloudflare.com/",
    "-A",
    "Mozilla/5.0",
]
DNS_TEST_ZONES = [
    "cloudflare.com",
    "github.com",
    "wikipedia.org",
    "archlinux.org",
    "openai.com",
]


def run_cmd(cmd: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)


def parse_ping_summary(output: str) -> dict[str, Any] | None:
    """Parse Linux (iputils) or macOS ping summary output."""
    loss_match = re.search(r"([\d.]+)%\s+packet loss", output)
    rtt_match = re.search(
        r"(?:rtt|round-trip)\s+min/avg/max/(?:mdev|stddev)\s*=\s*"
        r"([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)",
        output,
    )
    if not rtt_match:
        return None
    return {
        "min_ms": round(float(rtt_match.group(1)), 3),
        "avg_ms": round(float(rtt_match.group(2)), 3),
        "max_ms": round(float(rtt_match.group(3)), 3),
        "jitter_ms": round(float(rtt_match.group(4)), 3),
        "loss_pct": float(loss_match.group(1)) if loss_match else 0.0,
    }


def parse_iw_link(output: str) -> dict[str, Any]:
    info: dict[str, Any] = {}
    if "Not connected." in output or not output.strip():
        return info
    for key, pattern in [
        ("bssid", r"Connected to\s+([0-9a-fA-F:]+)"),
        ("ssid", r"SSID:\s*(.+)"),
        ("freq_mhz", r"freq:\s*([\d.]+)"),
        ("signal_dbm", r"signal:\s*(-?\d+)\s*dBm"),
        ("rx_bitrate", r"rx bitrate:\s*(.+)"),
        ("tx_bitrate", r"tx bitrate:\s*(.+)"),
    ]:
        match = re.search(pattern, output)
        if match:
            val: Any = match.group(1).strip()
            if key == "freq_mhz":
                val = float(val)
            elif key == "signal_dbm":
                val = int(val)
            info[key] = val
    return info


def parse_iw_station(output: str) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    for key, pattern in [
        ("tx_packets", r"tx packets:\s*(\d+)"),
        ("tx_retries", r"tx retries:\s*(\d+)"),
        ("tx_failed", r"tx failed:\s*(\d+)"),
        ("beacon_loss", r"beacon loss:\s*(\d+)"),
        ("rx_drop_misc", r"rx drop misc:\s*(\d+)"),
        ("wmm", r"WMM/WME:\s*(\w+)"),
        ("mfp", r"MFP:\s*(\w+)"),
        ("connected_seconds", r"connected time:\s*(\d+)"),
    ]:
        match = re.search(pattern, output)
        if match:
            raw = match.group(1).strip()
            stats[key] = int(raw) if raw.isdigit() else raw
    tx_pkts = stats.get("tx_packets", 0)
    tx_ret = stats.get("tx_retries", 0)
    if isinstance(tx_pkts, int) and tx_pkts > 0 and isinstance(tx_ret, int):
        stats["tx_retry_pct"] = round((tx_ret / tx_pkts) * 100.0, 2)
    return stats


def parse_nmcli_scan(output: str) -> list[dict[str, Any]]:
    """Parse terse `nmcli -t -f IN-USE,BSSID,SSID,CHAN,FREQ,RATE,SIGNAL,SECURITY dev wifi list`."""
    aps: list[dict[str, Any]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        # Escaped colons in BSSID are `\:`.
        parts = re.split(r"(?<!\\):", line)
        if len(parts) < 8:
            continue
        in_use = parts[0].strip() == "*"
        bssid = parts[1].replace("\\:", ":").strip()
        ssid = parts[2].replace("\\:", ":").strip() or "--"
        try:
            chan = int(parts[3].strip())
        except ValueError:
            continue
        freq_match = re.search(r"(\d+)", parts[4])
        freq_mhz = int(freq_match.group(1)) if freq_match else 0
        rate = parts[5].strip()
        try:
            signal = int(parts[6].strip())
        except ValueError:
            signal = 0
        security = parts[7].strip()
        band = "2.4GHz" if freq_mhz < 3000 else ("5GHz" if freq_mhz < 5900 else "6GHz")
        aps.append(
            {
                "in_use": in_use,
                "bssid": bssid,
                "ssid": ssid,
                "channel": chan,
                "freq_mhz": freq_mhz,
                "band": band,
                "rate": rate,
                "signal_pct": signal,
                "security": security,
            }
        )
    return aps


def summarize_channels(aps: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, dict[int, int]] = {"2.4GHz": {}, "5GHz": {}, "6GHz": {}}
    for ap in aps:
        band = ap.get("band", "5GHz")
        chan = int(ap["channel"])
        counts.setdefault(band, {})
        counts[band][chan] = counts[band].get(chan, 0) + 1

    # Cleanest non-overlapping 2.4 GHz channel (1, 6, or 11).
    c24 = counts["2.4GHz"]
    best_24 = min([1, 6, 11], key=lambda ch: (c24.get(ch, 0), ch))

    # Cleanest non-DFS 5 GHz channel group (36-48 vs 149-161).
    c5 = counts["5GHz"]
    low_5 = sum(c5.get(ch, 0) for ch in (36, 40, 44, 48))
    high_5 = sum(c5.get(ch, 0) for ch in (149, 153, 157, 161))
    best_5 = "36-48" if low_5 <= high_5 else "149-161"

    return {
        "counts_by_band": {
            band: {str(k): v for k, v in sorted(ch_map.items())}
            for band, ch_map in counts.items()
            if ch_map
        },
        "recommended_24ghz_channel": best_24,
        "recommended_5ghz_band": best_5,
    }


PRIVATE_OR_CGNAT_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
)


def is_private_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in net for net in PRIVATE_OR_CGNAT_NETS)
    except ValueError:
        return False


def detect_double_nat(hops: list[dict[str, Any]]) -> bool:
    """Return True when both hop 1 and hop 2 are private/RFC1918 addresses."""
    hop_ips = {h["hop"]: h.get("ip") for h in hops if h.get("ip")}
    h1, h2 = hop_ips.get(1), hop_ips.get(2)
    return bool(h1 and h2 and is_private_ip(h1) and is_private_ip(h2))


def discover_linux_interfaces() -> dict[str, Any]:
    rc, route_out, _ = run_cmd(["ip", "route"])
    default_routes: list[dict[str, Any]] = []
    gateway: str | None = None
    for line in route_out.splitlines():
        if line.startswith("default "):
            gw_m = re.search(r"via\s+(\S+)", line)
            dev_m = re.search(r"dev\s+(\S+)", line)
            src_m = re.search(r"src\s+(\S+)", line)
            met_m = re.search(r"metric\s+(\d+)", line)
            if dev_m:
                entry = {
                    "iface": dev_m.group(1),
                    "gateway": gw_m.group(1) if gw_m else None,
                    "src": src_m.group(1) if src_m else None,
                    "metric": int(met_m.group(1)) if met_m else 0,
                }
                default_routes.append(entry)
                if gateway is None and entry["gateway"]:
                    gateway = entry["gateway"]

    default_routes.sort(key=lambda r: r["metric"])

    wifi_ifaces: list[str] = []
    rc_iw, iw_out, _ = run_cmd(["iw", "dev"])
    if rc_iw == 0:
        for match in re.finditer(r"Interface\s+(\S+)", iw_out):
            wifi_ifaces.append(match.group(1))

    return {
        "gateway": gateway,
        "default_routes": default_routes,
        "wifi_interfaces": wifi_ifaces,
    }


def discover_macos_interfaces() -> dict[str, Any]:
    rc, out, _ = run_cmd(["route", "-n", "get", "default"])
    gw_m = re.search(r"gateway:\s*(\S+)", out)
    dev_m = re.search(r"interface:\s*(\S+)", out)
    iface = dev_m.group(1) if dev_m else "en0"
    gw = gw_m.group(1) if gw_m else None

    wifi_ifaces: list[str] = []
    rc_ns, ns_out, _ = run_cmd(["networksetup", "-listallhardwareports"])
    if rc_ns == 0:
        blocks = ns_out.split("\n\n")
        for b in blocks:
            if "Wi-Fi" in b or "AirPort" in b:
                m = re.search(r"Device:\s*(\S+)", b)
                if m:
                    wifi_ifaces.append(m.group(1))

    return {
        "gateway": gw,
        "default_routes": [{"iface": iface, "gateway": gw, "metric": 0}],
        "wifi_interfaces": wifi_ifaces,
    }


def run_wifi_rf_check(wifi_iface: str | None, is_linux: bool) -> dict[str, Any]:
    if not wifi_iface:
        return {"available": False}
    result: dict[str, Any] = {"available": True, "interface": wifi_iface}
    if is_linux:
        _, link_out, _ = run_cmd(["iw", "dev", wifi_iface, "link"])
        _, sta_out, _ = run_cmd(["iw", "dev", wifi_iface, "station", "dump"])
        _, dev_out, _ = run_cmd(["iw", "dev", wifi_iface, "info"])
        width_m = re.search(r"channel\s+(\d+)\s+\((\d+)\s+MHz\),\s*width:\s*(\d+\s*MHz)", dev_out)
        link = parse_iw_link(link_out)
        if width_m:
            link["channel"] = int(width_m.group(1))
            link["channel_width"] = width_m.group(3)
        result["link"] = link
        result["station"] = parse_iw_station(sta_out)

        if shutil.which("nmcli"):
            _, scan_out, _ = run_cmd(
                [
                    "nmcli",
                    "-t",
                    "-f",
                    "IN-USE,BSSID,SSID,CHAN,FREQ,RATE,SIGNAL,SECURITY",
                    "dev",
                    "wifi",
                    "list",
                    "--rescan",
                    "yes",
                ],
                timeout=20.0,
            )
            aps = parse_nmcli_scan(scan_out)
            result["scan_ap_count"] = len(aps)
            result["channels"] = summarize_channels(aps)
            result["nearby_aps"] = aps
    else:
        _, sp_out, _ = run_cmd(["system_profiler", "SPAirPortDataType"], timeout=20.0)
        result["raw_summary"] = sp_out[:2000]
    return result


def ping_target(target: str, count: int, iface: str | None, is_linux: bool) -> dict[str, Any] | None:
    cmd = ["ping", "-c", str(count), "-i", "0.2"]
    if is_linux:
        cmd.append("-q")
        if iface:
            cmd.extend(["-I", iface])
    elif iface:
        cmd.extend(["-b", iface])
    cmd.append(target)
    _, out, _ = run_cmd(cmd, timeout=max(15.0, count * 0.4 + 5.0))
    return parse_ping_summary(out)


def run_short_traceroute(target: str, iface: str | None, is_linux: bool, max_hops: int = 5) -> list[dict[str, Any]]:
    hops: list[dict[str, Any]] = []
    for ttl in range(1, max_hops + 1):
        cmd = ["ping", "-c", "1"]
        if is_linux:
            cmd.extend(["-t", str(ttl), "-W", "2"])
            if iface:
                cmd.extend(["-I", iface])
        else:
            cmd.extend(["-m", str(ttl), "-W", "2000"])
        cmd.append(target)
        _, out, err = run_cmd(cmd, timeout=4.0)
        combined = out + "\n" + err
        ip_m = re.search(r"[Ff]rom\s+(?:[^\s(]+\s+\()?(\d+\.\d+\.\d+\.\d+)\)?", combined)
        hops.append({"hop": ttl, "ip": ip_m.group(1) if ip_m else None})
    return hops


def query_dns_uncached(server: str, zones: list[str], timeout: float = 3.0) -> dict[str, Any] | None:
    times: list[float] = []
    for zone in zones:
        label = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        domain = f"{label}.{zone}"
        tid = random.randint(0, 65535)
        header = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
        qname = b"".join(bytes([len(p)]) + p.encode() for p in domain.split(".")) + b"\x00"
        pkt = header + qname + struct.pack(">HH", 1, 1)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        t0 = time.perf_counter()
        try:
            sock.sendto(pkt, (server, 53))
            sock.recvfrom(512)
            times.append((time.perf_counter() - t0) * 1000.0)
        except OSError:
            pass
        finally:
            sock.close()
    if not times:
        return None
    return {
        "avg_ms": round(sum(times) / len(times), 1),
        "min_ms": round(min(times), 1),
        "max_ms": round(max(times), 1),
        "samples": len(times),
    }


def run_dns_check(gateway: str | None) -> dict[str, Any]:
    servers: list[tuple[str, str]] = [("system_stub", "127.0.0.53")]
    if gateway:
        servers.append(("router", gateway))
    servers.extend([("cloudflare", "1.1.1.1"), ("google", "8.8.8.8")])

    results: dict[str, Any] = {}
    for name, ip in servers:
        res = query_dns_uncached(ip, DNS_TEST_ZONES)
        if res is not None:
            results[name] = {"server": ip, **res}
    return results


def run_load_test(iface: str, load_seconds: int, is_linux: bool) -> dict[str, Any]:
    idle = ping_target("1.1.1.1", count=10, iface=iface, is_linux=is_linux)
    if not idle:
        return {"error": f"Unable to ping 1.1.1.1 via {iface}"}

    ping_base = ["ping", "-i", "0.2"]
    if is_linux:
        ping_base.extend(["-I", iface, "-w", str(load_seconds), "-q", "1.1.1.1"])
    else:
        ping_base.extend(["-t", str(load_seconds), "1.1.1.1"])

    curl_iface = ["--interface", iface] if is_linux else []

    # Downlink (4 parallel streams)
    ping_proc = subprocess.Popen(ping_base, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    t0 = time.perf_counter()
    dl_procs = [
        subprocess.Popen(
            [
                "curl",
                *curl_iface,
                "-sS",
                "-o",
                "/dev/null",
                "-m",
                str(load_seconds),
                "-w",
                "%{size_download}",
                *CLOUDFLARE_HEADERS,
                CLOUDFLARE_DL_URL,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    dl_bytes = sum(int(float(p.communicate()[0].strip() or 0)) for p in dl_procs)
    dl_elapsed = max(time.perf_counter() - t0, 0.01)
    dl_ping = parse_ping_summary(ping_proc.communicate()[0])
    dl_mbps = round((dl_bytes * 8) / dl_elapsed / 1e6, 1)

    time.sleep(1.5)

    # Uplink (4 parallel streams)
    payload_file = tempfile.NamedTemporaryFile(delete=False)
    try:
        payload_file.write(b"\x00" * (25 * 1024 * 1024))
        payload_file.close()
        ping_proc = subprocess.Popen(ping_base, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        t0 = time.perf_counter()
        ul_procs = [
            subprocess.Popen(
                [
                    "curl",
                    *curl_iface,
                    "-sS",
                    "-o",
                    "/dev/null",
                    "-m",
                    str(load_seconds),
                    "-X",
                    "POST",
                    "--data-binary",
                    f"@{payload_file.name}",
                    "-w",
                    "%{size_upload}",
                    CLOUDFLARE_UL_URL,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        ul_bytes = sum(int(float(p.communicate()[0].strip() or 0)) for p in ul_procs)
        ul_elapsed = max(time.perf_counter() - t0, 0.01)
        ul_ping = parse_ping_summary(ping_proc.communicate()[0])
        ul_mbps = round((ul_bytes * 8) / ul_elapsed / 1e6, 1)
    finally:
        if os.path.exists(payload_file.name):
            os.unlink(payload_file.name)

    def bloat_metrics(loaded: dict[str, Any] | None) -> dict[str, Any] | None:
        if not loaded:
            return None
        bloat = round(loaded["avg_ms"] - idle["avg_ms"], 1)
        rpm = round(60000.0 / loaded["avg_ms"]) if loaded["avg_ms"] > 0 else 0
        return {**loaded, "bloat_ms": bloat, "rpm": rpm}

    return {
        "idle_rtt_ms": idle,
        "downlink_mbps": dl_mbps,
        "downlink_loaded_rtt": bloat_metrics(dl_ping),
        "uplink_mbps": ul_mbps,
        "uplink_loaded_rtt": bloat_metrics(ul_ping),
    }


def collect_ecn_status(is_linux: bool) -> dict[str, str]:
    info: dict[str, str] = {}
    if is_linux:
        _, out, _ = run_cmd(["sysctl", "net.ipv4.tcp_ecn", "net.core.default_qdisc"])
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()
    return info


def cmd_run(args: argparse.Namespace) -> int:
    is_linux = platform.system() == "Linux"
    net = discover_linux_interfaces() if is_linux else discover_macos_interfaces()
    gateway = net.get("gateway")

    if args.iface:
        target_ifaces = args.iface
    else:
        target_ifaces = [r["iface"] for r in net["default_routes"]]
        if not target_ifaces:
            print("No active default route interfaces found.", file=sys.stderr)
            return 1

    wifi_iface = net["wifi_interfaces"][0] if net["wifi_interfaces"] else None

    print("[1/4] Inspecting Wi-Fi link and RF congestion...", file=sys.stderr)
    wifi_rf = run_wifi_rf_check(wifi_iface, is_linux)

    print("[2/4] Running idle latency and 5-hop traceroute (sequential)...", file=sys.stderr)
    idle_ping: dict[str, Any] = {}
    for iface in target_ifaces:
        entry: dict[str, Any] = {}
        if gateway:
            entry["gateway"] = ping_target(gateway, args.ping_count, iface, is_linux)
        entry["cloudflare_1_1_1_1"] = ping_target("1.1.1.1", args.ping_count, iface, is_linux)
        idle_ping[iface] = entry

    traceroute_hops = run_short_traceroute("1.1.1.1", target_ifaces[0], is_linux, max_hops=5)
    double_nat = detect_double_nat(traceroute_hops)

    print("[3/4] Measuring uncached DNS latency...", file=sys.stderr)
    dns = run_dns_check(gateway)

    load_results: dict[str, Any] = {}
    if not args.skip_load:
        print("[4/4] Measuring throughput and bufferbloat under load (sequential)...", file=sys.stderr)
        for iface in target_ifaces:
            load_results[iface] = run_load_test(iface, args.load_seconds, is_linux)
            time.sleep(1.0)
    else:
        print("[4/4] Skipping load test (--skip-load).", file=sys.stderr)

    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "gateway": gateway,
        "tested_interfaces": target_ifaces,
        "wifi_rf": wifi_rf,
        "idle_ping": idle_ping,
        "traceroute_5hop": traceroute_hops,
        "double_nat_detected": double_nat,
        "dns_uncached": dns,
        "ecn": collect_ecn_status(is_linux),
        "load": load_results,
    }

    encoded = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(encoded + "\n", encoding="utf-8")
        print(f"Saved report to {args.out}", file=sys.stderr)
    else:
        print(encoded)
    return 0


def format_delta(before: float | None, after: float | None, higher_is_better: bool) -> str:
    if before is None or after is None or before == 0:
        return "—"
    pct = ((after - before) / abs(before)) * 100.0
    sign = "+" if pct > 0 else ""
    if higher_is_better and after >= before * 1.5:
        ratio = after / before
        return f"**{sign}{pct:.0f}% ({ratio:.1f}x)**"
    return f"{sign}{pct:.0f}%"


def format_diff_markdown(before: dict[str, Any], after: dict[str, Any]) -> str:
    lines = [
        "| Metric | Before | After | Change |",
        "|---|---:|---:|---:|",
    ]
    ifaces = list(dict.fromkeys(before.get("tested_interfaces", []) + after.get("tested_interfaces", [])))
    for iface in ifaces:
        b_load = before.get("load", {}).get(iface, {})
        a_load = after.get("load", {}).get(iface, {})
        b_dl, a_dl = b_load.get("downlink_mbps"), a_load.get("downlink_mbps")
        b_ul, a_ul = b_load.get("uplink_mbps"), a_load.get("uplink_mbps")
        if b_dl is not None or a_dl is not None:
            lines.append(
                f"| `{iface}` Downlink | {b_dl} Mbps | {a_dl} Mbps | {format_delta(b_dl, a_dl, True)} |"
            )
        if b_ul is not None or a_ul is not None:
            lines.append(
                f"| `{iface}` Uplink | {b_ul} Mbps | {a_ul} Mbps | {format_delta(b_ul, a_ul, True)} |"
            )
        b_ul_bloat = (b_load.get("uplink_loaded_rtt") or {}).get("bloat_ms")
        a_ul_bloat = (a_load.get("uplink_loaded_rtt") or {}).get("bloat_ms")
        if b_ul_bloat is not None or a_ul_bloat is not None:
            lines.append(
                f"| `{iface}` UL Bufferbloat | +{b_ul_bloat} ms | +{a_ul_bloat} ms | {format_delta(b_ul_bloat, a_ul_bloat, False)} |"
            )
        b_gw = (before.get("idle_ping", {}).get(iface, {}).get("gateway") or {}).get("avg_ms")
        a_gw = (after.get("idle_ping", {}).get(iface, {}).get("gateway") or {}).get("avg_ms")
        if b_gw is not None or a_gw is not None:
            lines.append(
                f"| `{iface}` Idle GW Ping | {b_gw} ms | {a_gw} ms | {format_delta(b_gw, a_gw, False)} |"
            )

    b_dns = (before.get("dns_uncached", {}).get("router") or {}).get("avg_ms")
    a_dns = (after.get("dns_uncached", {}).get("router") or {}).get("avg_ms")
    if b_dns is not None or a_dns is not None:
        lines.append(f"| Router DNS (uncached) | {b_dns} ms | {a_dns} ms | {format_delta(b_dns, a_dns, False)} |")
    return "\n".join(lines)


def cmd_diff(args: argparse.Namespace) -> int:
    before = json.loads(Path(args.before).read_text(encoding="utf-8"))
    after = json.loads(Path(args.after).read_text(encoding="utf-8"))
    print(format_diff_markdown(before, after))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wifi-diag",
        description="Sequential Wi-Fi and home network diagnostics CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run sequential diagnostics and emit JSON.")
    run_p.add_argument("--out", help="Write JSON output to file instead of stdout.")
    run_p.add_argument("--iface", action="append", help="Interface to test (repeatable).")
    run_p.add_argument("--ping-count", type=int, default=30, help="Idle ping packet count (default: 30).")
    run_p.add_argument("--load-seconds", type=int, default=6, help="Load test duration per direction (default: 6).")
    run_p.add_argument("--skip-load", action="store_true", help="Skip throughput and bufferbloat tests.")
    run_p.set_defaults(func=cmd_run)

    diff_p = sub.add_parser("diff", help="Print Before vs. After Markdown comparison table.")
    diff_p.add_argument("before", help="Baseline JSON report path.")
    diff_p.add_argument("after", help="After-changes JSON report path.")
    diff_p.set_defaults(func=cmd_diff)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
