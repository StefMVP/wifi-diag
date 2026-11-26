import unittest

import wifi_diag


class WifiDiagTests(unittest.TestCase):
    def test_parse_ping_summary_linux(self) -> None:
        sample = (
            "30 packets transmitted, 30 received, 0% packet loss, time 5824ms\n"
            "rtt min/avg/max/mdev = 4.087/4.902/6.530/0.484 ms\n"
        )
        res = wifi_diag.parse_ping_summary(sample)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res["min_ms"], 4.087)
        self.assertEqual(res["avg_ms"], 4.902)
        self.assertEqual(res["max_ms"], 6.53)
        self.assertEqual(res["jitter_ms"], 0.484)
        self.assertEqual(res["loss_pct"], 0.0)

    def test_parse_ping_summary_macos(self) -> None:
        sample = (
            "10 packets transmitted, 10 packets received, 0.0% packet loss\n"
            "round-trip min/avg/max/stddev = 2.100/3.200/5.400/0.900 ms\n"
        )
        res = wifi_diag.parse_ping_summary(sample)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res["avg_ms"], 3.2)
        self.assertEqual(res["jitter_ms"], 0.9)

    def test_parse_iw_station_retry_pct(self) -> None:
        sample = (
            "\ttx packets:\t1000\n"
            "\ttx retries:\t72\n"
            "\ttx failed:\t1\n"
            "\tWMM/WME:\tyes\n"
        )
        res = wifi_diag.parse_iw_station(sample)
        self.assertEqual(res["tx_retry_pct"], 7.2)
        self.assertEqual(res["wmm"], "yes")

    def test_parse_nmcli_and_recommend_channels(self) -> None:
        sample = (
            "*:AA\\:BB\\:CC\\:DD\\:EE\\:01:HomeNet:48:5240 MHz:1170 Mbit/s:85:WPA2\n"
            " :AA\\:BB\\:CC\\:DD\\:EE\\:02:Neighbor1:6:2437 MHz:260 Mbit/s:60:WPA2\n"
            " :AA\\:BB\\:CC\\:DD\\:EE\\:03:Neighbor2:1:2412 MHz:130 Mbit/s:40:WPA2\n"
            " :AA\\:BB\\:CC\\:DD\\:EE\\:04:ISPGw:157:5785 MHz:1170 Mbit/s:82:WPA2\n"
            " :AA\\:BB\\:CC\\:DD\\:EE\\:05:ISPGw2:157:5785 MHz:1170 Mbit/s:82:WPA2\n"
        )
        aps = wifi_diag.parse_nmcli_scan(sample)
        self.assertEqual(len(aps), 5)
        self.assertTrue(aps[0]["in_use"])
        summary = wifi_diag.summarize_channels(aps)
        self.assertEqual(summary["recommended_24ghz_channel"], 11)
        self.assertEqual(summary["recommended_5ghz_band"], "36-48")

    def test_detect_double_nat(self) -> None:
        double_hops = [
            {"hop": 1, "ip": "10.0.0.1"},
            {"hop": 2, "ip": "192.168.1.1"},
            {"hop": 3, "ip": "1.1.1.1"},
        ]
        cgnat_hops = [
            {"hop": 1, "ip": "192.168.0.1"},
            {"hop": 2, "ip": "100.64.0.1"},
        ]
        single_hops = [
            {"hop": 1, "ip": "10.0.0.1"},
            {"hop": 2, "ip": "1.1.1.1"},
        ]
        self.assertTrue(wifi_diag.detect_double_nat(double_hops))
        self.assertTrue(wifi_diag.detect_double_nat(cgnat_hops))
        self.assertFalse(wifi_diag.detect_double_nat(single_hops))

    def test_format_diff_markdown(self) -> None:
        before = {
            "tested_interfaces": ["wlan0"],
            "idle_ping": {"wlan0": {"gateway": {"avg_ms": 5.0}}},
            "load": {
                "wlan0": {
                    "downlink_mbps": 50.0,
                    "uplink_mbps": 40.0,
                    "uplink_loaded_rtt": {"bloat_ms": 50.0},
                }
            },
            "dns_uncached": {"router": {"avg_ms": 40.0}},
        }
        after = {
            "tested_interfaces": ["wlan0"],
            "idle_ping": {"wlan0": {"gateway": {"avg_ms": 3.0}}},
            "load": {
                "wlan0": {
                    "downlink_mbps": 300.0,
                    "uplink_mbps": 180.0,
                    "uplink_loaded_rtt": {"bloat_ms": 15.0},
                }
            },
            "dns_uncached": {"router": {"avg_ms": 30.0}},
        }
        md = wifi_diag.format_diff_markdown(before, after)
        self.assertIn("6.0x", md)
        self.assertIn("-70%", md)
        self.assertIn("Router DNS", md)


if __name__ == "__main__":
    unittest.main()
