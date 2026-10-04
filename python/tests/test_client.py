"""HTTP client tick body shape (M6 multi-intent queues)."""

import json
import unittest
from unittest.mock import MagicMock, patch

from agentrealm_agent.client import Client, _tick_body


class TickBodyTest(unittest.TestCase):
    def test_omit_intents_keeps_queue(self):
        self.assertEqual(_tick_body(None, None), {})

    def test_clear_queue(self):
        self.assertEqual(_tick_body([], None), {"intents": []})

    def test_single_intent(self):
        step = {"verb": "Step", "direction": "right"}
        self.assertEqual(_tick_body(step, None), {"intents": [step]})

    def test_ordered_multi_intent_queue(self):
        queue = [
            {"verb": "Step", "direction": "right"},
            {"verb": "Wait"},
            {"verb": "Wait"},
            {"verb": "Step", "direction": "up"},
        ]
        self.assertEqual(_tick_body(queue, None), {"intents": queue})

    def test_snapshot_version(self):
        self.assertEqual(
            _tick_body(None, 42),
            {"snapshot_version": 42},
        )
        self.assertEqual(
            _tick_body([{"verb": "Wait"}], 7),
            {"snapshot_version": 7, "intents": [{"verb": "Wait"}]},
        )


class ClientTickTest(unittest.TestCase):
    def test_posts_normalized_body(self):
        client = Client("https://example.test", "key")
        response_body = json.dumps({"tick": 1}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as urlopen:
            client.tick(
                9,
                [{"verb": "Step", "direction": "left"}, {"verb": "Wait"}],
                snapshot_version=3,
            )

        req = urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://example.test/characters/9/tick")
        self.assertEqual(req.method, "POST")
        self.assertEqual(
            json.loads(req.data),
            {
                "snapshot_version": 3,
                "intents": [
                    {"verb": "Step", "direction": "left"},
                    {"verb": "Wait"},
                ],
            },
        )

    def test_single_intent_dict_on_wire(self):
        client = Client("https://example.test", "key")
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"{}"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as urlopen:
            client.tick(1, {"verb": "Use", "target_kind": "npc", "target_id": 2})

        body = json.loads(urlopen.call_args[0][0].data)
        self.assertEqual(body, {"intents": [{"verb": "Use", "target_kind": "npc", "target_id": 2}]})


if __name__ == "__main__":
    unittest.main()
