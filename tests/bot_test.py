#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

from bot_lib.config import BotConfig  # noqa: E402
from bot_lib.runtime import BotContext  # noqa: E402


class StubResponse:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"code":"ACCEPTED","accepted":true}'


class BotSubmissionTests(unittest.TestCase):
    def test_capture_submission_is_deduplicated_and_redacted_in_events(self) -> None:
        flag = "FLAG{aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}"
        with tempfile.TemporaryDirectory() as temp_dir:
            event_file = Path(temp_dir) / "events.jsonl"
            config = BotConfig(
                deployment_id="deploy-1",
                gameserver_url="http://gameserver:8000",
                submission_token="team-secret-token",
            )
            context = BotContext(
                config=config,
                num_teams=2,
                my_team=1,
                event_file=str(event_file),
            )

            with patch("urllib.request.urlopen", return_value=StubResponse()) as urlopen:
                first = context.submit_flag(flag, target_team=2, action_id="exploit.sqli")
                second = context.submit_flag(flag, target_team=2, action_id="exploit.sqli")

            self.assertTrue(first["accepted"])
            self.assertEqual(second["code"], "LOCAL_DUPLICATE")
            self.assertEqual(urlopen.call_count, 1)

            raw_events = event_file.read_text(encoding="utf-8")
            self.assertNotIn(flag, raw_events)
            self.assertNotIn(config.submission_token, raw_events)
            events = [json.loads(line) for line in raw_events.splitlines()]
            self.assertEqual(events[-1]["code"], "LOCAL_DUPLICATE")
            self.assertEqual(len(events[-1]["flag_fingerprint"]), 12)

    def test_missing_submission_credentials_is_reported_without_network_call(self) -> None:
        context = BotContext(config=BotConfig(), num_teams=2, my_team=1)
        with patch("urllib.request.urlopen") as urlopen:
            result = context.submit_flag(
                "FLAG{bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}",
                target_team=2,
                action_id="exploit.cmdi",
            )
        self.assertEqual(result["code"], "NOT_CONFIGURED")
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
