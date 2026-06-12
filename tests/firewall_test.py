#!/usr/bin/env python3

import importlib.util
import os
import queue
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


if __name__ == "__main__":
    unittest.main()
