import json
import tempfile
import unittest
from pathlib import Path

import app


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "telegram.sqlite3"
        app.init_db(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_webhook_update_is_deduplicated_and_acknowledged_once(self):
        update = {"update_id": 105, "message": {"message_id": 1, "text": "hello"}}
        first_seq, first_inserted = app.store_update(update, self.path)
        second_seq, second_inserted = app.store_update(update, self.path)
        self.assertTrue(first_inserted)
        self.assertFalse(second_inserted)
        self.assertEqual(first_seq, second_seq)

        batch = app.get_batch(self.path)
        self.assertEqual(len(batch["items"]), 1)
        self.assertEqual(batch["items"][0]["kind"], "text")
        self.assertEqual(batch["items"][0]["text"], "hello")
        self.assertEqual(batch["batch_cursor"], first_seq)

        self.assertEqual(app.acknowledge(first_seq, self.path), first_seq)
        self.assertEqual(app.get_batch(self.path)["items"], [])
        # A repeated ack is safe and cannot move the cursor backwards.
        self.assertEqual(app.acknowledge(first_seq, self.path), first_seq)

    def test_voice_is_returned_as_capability_url_without_file_id(self):
        update = {"update_id": 106, "message": {"message_id": 2, "voice": {
            "file_id": "not-returned-to-client", "duration": 7, "mime_type": "audio/ogg"
        }}}
        app.store_update(update, self.path)
        batch = app.get_batch(self.path)
        voice = batch["items"][0]
        self.assertEqual(voice["kind"], "voice")
        self.assertTrue(voice["voice_url"].startswith("/voice/"))
        self.assertNotIn("file_id", json.dumps(voice))

    def test_unsupported_updates_are_still_in_ack_batch(self):
        app.store_update({"update_id": 107, "callback_query": {"id": "x"}}, self.path)
        batch = app.get_batch(self.path)
        self.assertEqual(batch["items"][0]["kind"], "other")
        self.assertEqual(app.acknowledge(batch["batch_cursor"], self.path), batch["batch_cursor"])
        self.assertEqual(app.get_batch(self.path)["items"], [])


if __name__ == "__main__":
    unittest.main()
