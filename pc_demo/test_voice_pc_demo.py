import unittest

from voice_pc_demo import DemoError, normalize_text, parse_intent, parse_iso8601_duration, select_latest_episode


class NormalizationTests(unittest.TestCase):
    def test_removes_czech_diacritics(self):
        self.assertEqual(normalize_text("Pusť Cinkátko!"), "pust cinkatko")


class IntentTests(unittest.TestCase):
    def test_czech_command(self):
        intent = parse_intent("Pusť poslední díl Cinkátka na YouTube.")
        self.assertEqual(intent.selector, "latest_episode")

    def test_english_command(self):
        intent = parse_intent("Let's play the latest video from Cinkatko on YouTube.")
        self.assertEqual(intent.channel_key, "cinkatko")

    def test_open_youtube_is_a_separate_allowlisted_action(self):
        intent = parse_intent("Open YouTube")
        self.assertEqual(intent.selector, "youtube_home")
        self.assertIsNone(intent.channel_key)

    def test_rejects_unrelated_pc_control(self):
        with self.assertRaises(DemoError):
            parse_intent("Delete all files from my computer")

    def test_requires_an_allowlisted_target(self):
        with self.assertRaises(DemoError):
            parse_intent("Open the calculator")


class DurationTests(unittest.TestCase):
    def test_youtube_duration(self):
        self.assertEqual(parse_iso8601_duration("PT1H2M3S"), 3723)


class SelectionTests(unittest.TestCase):
    def test_skips_short_and_selects_newest_episode(self):
        videos = [
            {
                "id": "short123456",
                "snippet": {
                    "title": "Newest short",
                    "publishedAt": "2026-08-04T10:00:00Z",
                    "liveBroadcastContent": "none",
                },
                "contentDetails": {"duration": "PT59S"},
                "status": {"privacyStatus": "public", "embeddable": True},
            },
            {
                "id": "episode1234",
                "snippet": {
                    "title": "Newest full episode",
                    "publishedAt": "2026-08-03T10:00:00Z",
                    "liveBroadcastContent": "none",
                },
                "contentDetails": {"duration": "PT45M"},
                "status": {"privacyStatus": "public", "embeddable": True},
            },
            {
                "id": "episode0000",
                "snippet": {
                    "title": "Older full episode",
                    "publishedAt": "2026-07-20T10:00:00Z",
                    "liveBroadcastContent": "none",
                },
                "contentDetails": {"duration": "PT50M"},
                "status": {"privacyStatus": "public", "embeddable": True},
            },
        ]
        selected = select_latest_episode(videos, min_duration_seconds=600)
        self.assertEqual(selected.video_id, "episode1234")


if __name__ == "__main__":
    unittest.main()
