#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class VisualizerProxyConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = (ROOT / "visualizer" / "nginx.conf.template").read_text(
            encoding="utf-8"
        )
        cls.dockerfile = (ROOT / "visualizer" / "Dockerfile").read_text(encoding="utf-8")
        cls.arena_config = (
            ROOT / "visualizer" / "src" / "data" / "arenaConfig.ts"
        ).read_text(encoding="utf-8")
        cls.vite_config = (ROOT / "visualizer" / "vite.config.js").read_text(
            encoding="utf-8"
        )

    def test_nginx_template_proxies_browser_visible_routes(self) -> None:
        self.assertIn("location /api/", self.template)
        self.assertIn("proxy_pass http://gameserver:8000/api/;", self.template)
        self.assertIn("location /bot-api/", self.template)
        self.assertIn("proxy_pass http://bot-controller:${ARENA_BOT_API_PORT}/;", self.template)
        self.assertIn("location = /firewall-ws", self.template)
        self.assertIn(
            "proxy_pass http://host.docker.internal:${ARENA_FIREWALL_WS_PORT};",
            self.template,
        )

    def test_firewall_route_uses_websocket_upgrade_headers(self) -> None:
        self.assertIn("proxy_set_header Upgrade $http_upgrade;", self.template)
        self.assertIn("proxy_set_header Connection $connection_upgrade;", self.template)
        self.assertIn("proxy_read_timeout 1h;", self.template)

    def test_dockerfile_installs_nginx_template_for_envsubst(self) -> None:
        self.assertIn(
            "COPY visualizer/nginx.conf.template /etc/nginx/templates/default.conf.template",
            self.dockerfile,
        )

    def test_frontend_defaults_to_same_origin_browser_routes(self) -> None:
        self.assertIn("VITE_BOT_API_URL || '/bot-api'", self.arena_config)
        self.assertIn("VITE_FIREWALL_WS_URL", self.arena_config)
        self.assertIn("window.location.host", self.arena_config)
        self.assertIn("/firewall-ws", self.arena_config)

    def test_vite_dev_server_proxies_same_origin_routes(self) -> None:
        self.assertIn("'/bot-api'", self.vite_config)
        self.assertIn("target: 'http://localhost:7878'", self.vite_config)
        self.assertIn("urlPath.replace(/^\\/bot-api/, '')", self.vite_config)
        self.assertIn("'/firewall-ws'", self.vite_config)
        self.assertIn("target: 'ws://localhost:6789'", self.vite_config)
        self.assertIn("ws: true", self.vite_config)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
