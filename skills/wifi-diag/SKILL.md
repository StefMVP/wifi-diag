---
name: wifi-diag
description: Diagnose Wi-Fi and home network bottlenecks sequentially on Linux or macOS without disconnecting Ethernet, audit router settings screenshots, and produce a Before vs. After benchmark table.
---

# Wi-Fi and Home Network Diagnostics

Credit: Adapted from the sequential diagnostic prompt by [@brunovolpato](https://x.com/brunovolpato/status/2124706069440057360).

## Rules
- Run all 4 stages strictly sequentially. Never run idle ping during throughput load.
- Keep wired Ethernet connected. On Linux, bind Wi-Fi probes to the Wi-Fi interface (`--iface`) so active Ethernet work never drops.
- Scripts report measurements; you identify root causes and rank fixes by impact. Skip placebo OS tweaks.

## Workflow

1. **Capture baseline**:
   ```sh
   python3 wifi_diag.py run --out baseline.json
   ```
   On macOS, you can also run `networkQuality -v` after idle checks for native RPM and L4S output.

2. **Analyze the JSON and rank real bottlenecks**:
   - **Wi-Fi PHY vs. measured throughput**: Compare `wifi_rf.link` bitrate/signal against `load.<wifi>.downlink_mbps` and `uplink_mbps`. Strong signal (`-55 dBm` or better) with `< 100 Mbps` throughput and high `tx_retry_pct` points to airtime contention or a bad router setting (for example, CTS/RTS threshold set too low).
   - **RF channel congestion**: Inspect `wifi_rf.channels`. Recommend only `1`, `6`, or `11` on 2.4 GHz (20 MHz) and the cleanest 80 MHz block on 5 GHz (`36-48` or `149-161`). Check whether an ISP gateway broadcasts on top of a mesh backhaul channel.
   - **Double NAT**: If `double_nat_detected` is `true` (two RFC1918 hops in `traceroute_5hop`), recommend Bridge Mode / IP Passthrough on the ISP gateway and disabling its Wi-Fi radios.
   - **DNS latency**: Compare `dns_uncached` across system stub, router, `1.1.1.1`, and `8.8.8.8`.
   - **Bufferbloat**: Compare `idle_rtt_ms` against `downlink_loaded_rtt` and `uplink_loaded_rtt` (`bloat_ms` and `rpm`).

3. **Request router model or Wireless Settings screenshot**:
   - Ask for the router model or a screenshot of its Wireless Settings and Advanced Wireless Settings pages.
   - Check high-impact router traps immediately:
     - **CTS/RTS Threshold**: Must be `2347` (IEEE 802.11 default). Values like `64` force RTS/CTS handshakes on every data packet and cut throughput by 50–80%.
     - **Channel width & PHY**: Unlock 80 MHz (or 160 MHz where supported), keep WMM and Wi-Fi 6 / AX / OFDMA enabled.
     - **Unused SSIDs**: Disable unused guest networks; keep IoT guest networks isolated from LAN.
     - **ISP gateway**: Put the upstream ISP gateway in Bridge Mode / IP Passthrough and turn off its Wi-Fi radios.

4. **Re-run and compare**:
   After the user applies changes, run the exact same command to a new file and output the comparison table:
   ```sh
   python3 wifi_diag.py run --out after.json
   python3 wifi_diag.py diff baseline.json after.json
   ```
