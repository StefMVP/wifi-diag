# wifi-diag

Sequential Wi-Fi and home network diagnostics CLI and agent skill for Linux and macOS.

Measures RF link quality, nearby channel congestion, idle gateway and internet latency, 5-hop traceroute (with Double NAT detection), uncached DNS latency, and loaded throughput/bufferbloat — without disconnecting wired Ethernet.

Credit: Inspired by the diagnostic workflow by [@brunovolpato](https://x.com/brunovolpato/status/2124706069440057360).

## Requirements

- Python 3.10+ (zero third-party Python packages)
- `ping` and `curl`
- Linux: `ip`, `iw`, and `nmcli` (for RF scan)
- macOS: `route`, `networksetup`, and `system_profiler`

## Install

```sh
git clone https://github.com/StefMVP/wifi-diag.git
cd wifi-diag
chmod +x wifi_diag.py
```

To use the agent skill, copy or symlink `skills/wifi-diag` into your agent's skills directory.

## Usage

Run a baseline diagnostic (tests all active default-route interfaces sequentially without dropping Ethernet):

```sh
./wifi_diag.py run --out baseline.json
```

Skip the load test (RF, idle latency, traceroute, and DNS only):

```sh
./wifi_diag.py run --skip-load --out baseline.json
```

Test specific interfaces or adjust duration:

```sh
./wifi_diag.py run --iface eth0 --iface wlan0 --ping-count 30 --load-seconds 6 --out baseline.json
```

After applying router fixes, run again and print a Before vs. After Markdown table:

```sh
./wifi_diag.py run --out after.json
./wifi_diag.py diff baseline.json after.json
```

## License

MIT — see [LICENSE](LICENSE).
