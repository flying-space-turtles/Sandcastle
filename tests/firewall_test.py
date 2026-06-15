#!/usr/bin/env python3

import importlib.util
import os
import queue
import socket
import struct
import sys
import types
import unittest
from pathlib import Path


class _FakeWebsockets(types.ModuleType):
    async def serve(self, *args, **kwargs):
        raise AssertionError("serve should not run in unit tests")


sys.modules.setdefault("websockets", _FakeWebsockets("websockets"))
os.environ.update(
    {
        "CTF_NETWORK": "10.10.0.0/16",
        "WS_PORT": "6789",
        "PROXY_PORT": "15000",
        "EVENT_QUEUE_SIZE": "2",
        "CAPTURE_RCVBUF_BYTES": "65536",
        "RECENT_ICMP_LIMIT": "2",
    }
)

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sandcastle_firewall",
    ROOT / "firewall" / "firewall.py",
)
firewall = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(firewall)


class FirewallTest(unittest.TestCase):
    def setUp(self):
        firewall._event_queue = queue.Queue(maxsize=2)
        firewall._dropped_events = 0
        firewall._recent_icmp_events.clear()

    def test_event_queue_is_bounded(self):
        firewall._enqueue_event({"id": "one"})
        firewall._enqueue_event({"id": "two"})
        firewall._enqueue_event({"id": "three"})

        self.assertEqual(firewall._event_queue.qsize(), 2)
        self.assertEqual(firewall._dropped_events, 1)

    def test_recent_icmp_cache_is_bounded(self):
        self.assertTrue(firewall._should_emit_icmp_event("10.10.1.3", "10.10.2.3", 8, 0, 1))
        self.assertTrue(firewall._should_emit_icmp_event("10.10.1.3", "10.10.2.3", 8, 0, 2))
        self.assertTrue(firewall._should_emit_icmp_event("10.10.1.3", "10.10.2.3", 8, 0, 3))

        self.assertEqual(len(firewall._recent_icmp_events), 2)

    def test_tcp_event_preserves_original_and_masked_identity(self):
        firewall._emit_event(
            src_ip="10.10.1.3",
            dst_ip="10.10.2.3",
            dst_port=8080,
            masked_src_ip="10.10.0.1",
            first_payload=b"GET /sc004-token HTTP/1.1\r\n",
        )

        event = firewall._event_queue.get_nowait()
        self.assertEqual(event["srcIp"], "10.10.1.3")
        self.assertEqual(event["dstIp"], "10.10.2.3")
        self.assertEqual(event["maskedSrcIp"], "10.10.0.1")
        self.assertEqual(event["type"], "http")

    def test_proxy_input_rule_accepts_only_redirected_ctf_proxy_traffic(self):
        rule = firewall._proxy_input_rule_spec()

        self.assertIn("filter", rule)
        self.assertIn("INPUT", rule)
        self.assertIn("10.10.0.0/16", rule)
        self.assertIn("15000", rule)
        self.assertIn("conntrack", rule)
        self.assertIn("DNAT", rule)
        self.assertIn(firewall.INPUT_RULE_COMMENT, rule)

    def test_redirect_rule_exempts_proxy_gateway_source(self):
        rule = firewall._rule_spec()

        self.assertIn("10.10.0.0/16", rule)
        self.assertIn("!", rule)
        self.assertIn("10.10.0.1/32", rule)
        self.assertLess(rule.index("!"), rule.index("-d"))

    def test_install_and_remove_manage_redirect_and_input_rules(self):
        calls = []

        def fake_run(args, check=True):
            calls.append((args, check))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        original_run = firewall._run_iptables
        try:
            firewall._run_iptables = fake_run
            firewall.install_redirect_rule()
            firewall.remove_redirect_rule()
        finally:
            firewall._run_iptables = original_run

        install_calls = [args for args, _ in calls if "-I" in args or "-A" in args]
        self.assertIn(firewall._proxy_input_rule_spec(), install_calls)
        self.assertIn(firewall._rule_spec(), install_calls)
        inspected = [args for args, _ in calls if args[:3] == ["-t", "filter", "-S"]]
        self.assertTrue(inspected)


class FirewallParsingTest(unittest.TestCase):
    @staticmethod
    def _attr(attr_type: int, payload: bytes) -> bytes:
        length = 4 + len(payload)
        padding = b"\0" * (firewall._align_netlink(length) - length)
        return struct.pack("HH", length, attr_type) + payload + padding

    def test_tcp_classification_prefers_http_payload_then_known_port(self):
        self.assertEqual(firewall._classify_tcp(9999, "GET / HTTP/1.1"), "http")
        self.assertEqual(firewall._classify_tcp(22, "SSH-2.0-OpenSSH"), "ssh")
        self.assertEqual(firewall._classify_tcp(9999, "opaque"), "tcp")

    def test_identity_and_network_helpers_handle_known_and_invalid_addresses(self):
        self.assertEqual(firewall._ip_to_name("10.10.3.2"), "team3-ssh")
        self.assertEqual(firewall._ip_to_name("10.10.3.3"), "team3-vuln")
        self.assertEqual(firewall._ip_to_name("192.0.2.1"), "192.0.2.1")
        self.assertTrue(firewall._in_ctf_network("10.10.4.3"))
        self.assertFalse(firewall._in_ctf_network("not-an-ip"))

    def test_netlink_message_iterator_honors_alignment_and_truncation(self):
        first = struct.pack("IHHII", 19, 7, 0, 1, 0) + b"abc" + b"\0"
        second = struct.pack("IHHII", 18, 8, 0, 2, 0) + b"xy" + b"\0\0"
        malformed = struct.pack("IHHII", 99, 9, 0, 3, 0)

        self.assertEqual(
            list(firewall._iter_netlink_messages(first + second + malformed)),
            [(7, b"abc"), (8, b"xy")],
        )

    def test_conntrack_icmp_parser_extracts_nested_tuple(self):
        ip_attrs = self._attr(firewall.CTA_IP_V4_SRC, socket.inet_aton("10.10.1.3"))
        ip_attrs += self._attr(firewall.CTA_IP_V4_DST, socket.inet_aton("10.10.2.3"))
        proto_attrs = self._attr(firewall.CTA_PROTO_NUM, bytes([socket.IPPROTO_ICMP]))
        proto_attrs += self._attr(firewall.CTA_PROTO_ICMP_TYPE, bytes([8]))
        proto_attrs += self._attr(firewall.CTA_PROTO_ICMP_CODE, bytes([0]))
        proto_attrs += self._attr(firewall.CTA_PROTO_ICMP_ID, struct.pack("!H", 321))
        tuple_attrs = self._attr(firewall.CTA_TUPLE_IP, ip_attrs)
        tuple_attrs += self._attr(firewall.CTA_TUPLE_PROTO, proto_attrs)
        message = b"\0" * 4 + self._attr(firewall.CTA_TUPLE_ORIG, tuple_attrs)

        self.assertEqual(
            firewall._parse_conntrack_icmp_event(message),
            ("10.10.1.3", "10.10.2.3", 8, 0, 321),
        )
        self.assertIsNone(firewall._parse_conntrack_icmp_event(b"short"))


if __name__ == "__main__":
    unittest.main()
