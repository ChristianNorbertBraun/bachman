import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from bachman import config, google, youtube, ytwriter
from bachman.bridge import Bridge
from bachman.google import Google
from bachman.ytwriter import Change, Refused

NOW = dt.datetime(2026, 1, 10, 12, 0, tzinfo=dt.timezone.utc)
BERLIN = "Europe/Berlin"


def item(vid="vid0000001", title="TBD", description="TBD", privacy="private", publish_at=None, duration="PT50M47S",
         upload="processed"):
    status = {"privacyStatus": privacy, "uploadStatus": upload, "embeddable": True, "license": "youtube",
              "publicStatsViewable": True, "selfDeclaredMadeForKids": False, "madeForKids": False}
    if publish_at:
        status["publishAt"] = publish_at
    return {"id": vid, "contentDetails": {"duration": duration}, "status": status,
            "snippet": {"title": title, "description": description, "categoryId": "28", "tags": ["a", "b"],
                        "defaultLanguage": "de-DE", "defaultAudioLanguage": "de-DE", "channelId": "C",
                        "thumbnails": {"x": 1}, "publishedAt": "2026-01-01T00:00:00Z"}}


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else {}

    def json(self):
        return self._body


class FakeYouTube:
    """A channel with a few uploads. A PUT changes the stored video; reads can lag behind by `lag` reads."""

    def __init__(self, videos=None, put_status=200, lag=0, error_reason="invalidPublishAt", echo=None):
        self.videos = {v["id"]: v for v in (videos or [item(), item("vid0000002", "136 | Alt", privacy="public")])}
        self.calls, self.put_status, self.lag, self.error_reason, self.echo = [], put_status, lag, error_reason, echo
        self.stale = None

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return Resp(200, {"access_token": "ACCESS", "expires_in": 3600})

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        params = kw.get("params") or {}
        if url.endswith("/channels"):
            return Resp(200, {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
        if url.endswith("/playlistItems"):
            return Resp(200, {"items": [{"contentDetails": {"videoId": i}} for i in self.videos]})
        ids = params["id"].split(",")
        if self.stale is not None and self.lag > 0:
            self.lag -= 1
            return Resp(200, {"items": [self.stale]})
        return Resp(200, {"items": [self.videos[i] for i in ids if i in self.videos]})

    def put(self, url, **kw):
        self.calls.append(("PUT", url, kw))
        if self.put_status != 200:
            return Resp(self.put_status, {"error": {"errors": [{"reason": self.error_reason}]}})
        sent = kw["json"]
        old = self.videos[sent["id"]]
        self.stale = json.loads(json.dumps(old))
        new = json.loads(json.dumps(old))
        new["snippet"].update(sent["snippet"])
        new["status"].update(sent["status"])
        self.videos[sent["id"]] = new
        return Resp(200, self.echo if self.echo is not None else new)

    def puts(self):
        return [c for c in self.calls if c[0] == "PUT"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = Path(self.tmp.name)

    def client(self, http, scopes=(ytwriter.SCOPE,)):
        (self.conf / "client_id").write_text("C.apps")
        (self.conf / "client_secret").write_text("S")
        (self.conf / "token.json").write_text(json.dumps({"refresh_token": "R", "scopes": list(scopes)}))
        return Google(http, self.conf)


class ReadTests(Base):
    def test_durations(self):
        self.assertEqual([youtube.seconds(d) for d in ("PT50M47S", "PT3S", "PT1H2M3S", "P1DT1S", "", "nonsense")],
                         [3047, 3, 3723, 86401, 0, 0])

    def test_uploads_keep_playlist_order_and_include_private_videos(self):
        videos = youtube.uploads(self.client(FakeYouTube()))
        self.assertEqual([(v.id, v.privacy, v.minutes) for v in videos],
                         [("vid0000001", "private", 50.8), ("vid0000002", "public", 50.8)])

    def test_a_channel_without_uploads_and_an_unknown_video(self):
        http = FakeYouTube(videos=[item()])
        http.videos = {}
        self.assertEqual(youtube.uploads(self.client(http)), [])
        self.assertIsNone(youtube.get(self.client(FakeYouTube()), "nope000000"))


class RuleTests(unittest.TestCase):
    def test_times_without_offset_are_meant_in_the_configured_zone(self):
        when = ytwriter.parse_time("2026-01-11T06:00", BERLIN, NOW)
        self.assertEqual(Change("v", "t", "d", when).publish_utc, "2026-01-11T05:00:00Z")
        summer = ytwriter.parse_time("2026-07-05T06:00", BERLIN, NOW)
        self.assertEqual(Change("v", "t", "d", summer).publish_utc, "2026-07-05T04:00:00Z")
        self.assertEqual(Change("v", "t", "d", ytwriter.parse_time("2026-01-11T06:00:00Z", BERLIN, NOW)).publish_utc,
                         "2026-01-11T06:00:00Z")

    def test_times_in_the_past_too_soon_too_far_or_malformed_are_refused(self):
        for bad in ("2026-01-10T12:10", "2026-01-09T06:00", "2027-02-01T06:00", "Sonntag 6 Uhr", "", None, 5):
            with self.subTest(bad=bad), self.assertRaises(Refused):
                ytwriter.parse_time(bad, "UTC", NOW)
        with self.assertRaises(Refused):
            ytwriter.parse_time("2026-01-11T06:00", "Mars/Olympus", NOW)

    def test_text_limits(self):
        self.assertEqual(ytwriter.check_text("  137 | Titel ", " Text\n\nLinks:\n- https://example.org "),
                         ("137 | Titel", "Text\n\nLinks:\n- https://example.org"))
        bad = {"empty title": ("", "x"), "long title": ("x" * 101, "x"), "two-line title": ("a\nb", "x"),
               "angle bracket": ("a", "<b>x</b>"), "long description": ("a", "ä" * 2501), "not text": (5, "x")}
        for name, (title, description) in bad.items():
            with self.subTest(name), self.assertRaises(Refused):
                ytwriter.check_text(title, description)

    def test_the_description_limit_counts_bytes(self):
        ytwriter.check_text("a", "ä" * 2500)  # 5000 bytes

    def test_forbidden_characters_are_named(self):
        with self.assertRaises(Refused) as ctx:
            ytwriter.check_text("Doom – und KI", "Text — mit Strich", forbidden=("–", "—"))
        self.assertIn("the title contains '–'", str(ctx.exception))
        self.assertIn("the description contains '—'", str(ctx.exception))
        ytwriter.check_text("Doom - und KI", "x", forbidden=("–", "—"))

    def test_only_a_ready_private_video_can_be_scheduled(self):
        ytwriter.check_video(youtube.video_from(item()))
        ytwriter.check_video(youtube.video_from(item(publish_at="2026-02-01T05:00:00Z")))  # rescheduling is fine
        for bad in (None, youtube.video_from(item(privacy="public")), youtube.video_from(item(privacy="unlisted")),
                    youtube.video_from(item(upload="rejected"))):
            with self.subTest(bad=bad), self.assertRaises(Refused):
                ytwriter.check_video(bad)

    def test_the_code_belongs_to_exactly_these_values(self):
        when = ytwriter.parse_time("2026-01-11T06:00", BERLIN, NOW)
        base = Change("v", "t", "d", when)
        self.assertEqual(base.code, Change("v", "t", "d", when).code)
        for other in (Change("w", "t", "d", when), Change("v", "T", "d", when), Change("v", "t", "D", when),
                      Change("v", "t", "d", when + dt.timedelta(hours=1))):
            self.assertNotEqual(base.code, other.code)


class WriteTests(Base):
    def change(self):
        return Change("vid0000001", "137 | Neu", "Text", ytwriter.parse_time("2026-01-11T06:00", BERLIN, NOW))

    def test_the_update_keeps_every_other_field(self):
        video = youtube.video_from(item())
        sent = ytwriter.body(video, self.change())
        self.assertEqual(sent["snippet"], {"categoryId": "28", "tags": ["a", "b"], "defaultLanguage": "de-DE",
                                           "defaultAudioLanguage": "de-DE", "title": "137 | Neu", "description": "Text"})
        self.assertEqual(sent["status"], {"embeddable": True, "license": "youtube", "publicStatsViewable": True,
                                          "selfDeclaredMadeForKids": False, "privacyStatus": "private",
                                          "publishAt": "2026-01-11T05:00:00Z"})

    def test_apply_sends_one_put_and_confirms_with_a_read(self):
        http = FakeYouTube()
        g = self.client(http)
        self.assertTrue(ytwriter.apply(g, youtube.get(g, "vid0000001"), self.change(), sleep=lambda s: None))
        (_, url, kw), = http.puts()
        self.assertEqual((url, kw["params"], kw["headers"]), (youtube.API + "videos", {"part": "snippet,status"},
                                                              {"Authorization": "Bearer ACCESS"}))
        self.assertEqual(http.videos["vid0000001"]["status"]["privacyStatus"], "private")

    def test_lagging_reads_are_waited_for_and_reported_when_they_never_catch_up(self):
        waits = []
        http = FakeYouTube(lag=2)
        g = self.client(http)
        self.assertTrue(ytwriter.apply(g, youtube.video_from(item()), self.change(), sleep=waits.append))
        self.assertEqual(waits, [2, 2])
        http = FakeYouTube(lag=99)
        g = self.client(http)
        self.assertFalse(ytwriter.apply(g, youtube.video_from(item()), self.change(), sleep=lambda s: None, attempts=3))

    def test_a_rejected_update_names_the_reason_only(self):
        g = self.client(FakeYouTube(put_status=400))
        with self.assertRaises(google.GoogleError) as ctx:
            ytwriter.apply(g, youtube.video_from(item()), self.change(), sleep=lambda s: None)
        self.assertIn("HTTP 400, invalidPublishAt", str(ctx.exception))
        self.assertIn("nothing was changed", str(ctx.exception))

    def test_an_answer_with_other_values_is_an_error(self):
        g = self.client(FakeYouTube(echo=item(title="etwas anderes")))
        with self.assertRaises(google.GoogleError):
            ytwriter.apply(g, youtube.video_from(item()), self.change(), sleep=lambda s: None)

    def test_without_the_youtube_scope_nothing_is_sent(self):
        http = FakeYouTube()
        with self.assertRaises(Refused):
            ytwriter.apply(self.client(http, scopes=()), youtube.video_from(item()), self.change())
        self.assertEqual(http.puts(), [])


class ToolTests(Base):
    ARGS = {"video_id": "vid0000001", "title": "137 | Neu", "description": "Text", "publish_at": "2026-01-11T06:00"}

    def bridge(self, http, **kw):
        return Bridge(None, log=lambda *_: None, google=self.client(http),
                      publish=config.PublishConfig(BERLIN, ("–", "—")), now=lambda: NOW, sleep=lambda s: None, **kw)

    def call(self, bridge, **args):
        out = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "podcast_schedule_youtube", "arguments": {**self.ARGS, **args}}})
        return out["result"]["content"][0]["text"], out["result"]["isError"]

    def code(self, text):
        return text.rsplit('confirm = "', 1)[1].split('"')[0]

    def test_the_first_call_is_a_preview_and_changes_nothing(self):
        http = FakeYouTube()
        text, error = self.call(self.bridge(http))
        self.assertFalse(error)
        self.assertTrue(text.startswith("PREVIEW, nothing was changed."))
        self.assertIn('currently titled "TBD", 50.8 min, not scheduled', text)
        self.assertIn("Publish time: Sunday 2026-01-11 06:00 (Europe/Berlin), that is 2026-01-11T05:00:00Z UTC", text)
        self.assertIn("New title: 137 | Neu", text)
        self.assertEqual(http.puts(), [])

    def test_the_second_call_with_the_code_writes_and_reports(self):
        http = FakeYouTube()
        bridge = self.bridge(http)
        code = self.code(self.call(bridge)[0])
        text, error = self.call(bridge, confirm=code)
        self.assertFalse(error)
        self.assertTrue(text.startswith("Scheduled on YouTube. YouTube shows the new values."))
        self.assertEqual(len(http.puts()), 1)
        self.assertEqual(http.videos["vid0000001"]["snippet"]["title"], "137 | Neu")
        self.assertEqual(http.videos["vid0000001"]["status"]["publishAt"], "2026-01-11T05:00:00Z")

    def test_a_code_for_other_values_only_gives_a_new_preview(self):
        http = FakeYouTube()
        bridge = self.bridge(http)
        code = self.code(self.call(bridge)[0])
        text, error = self.call(bridge, confirm=code, title="137 | Ganz anders")
        self.assertFalse(error)
        self.assertIn("The confirmation code does not belong to these values.", text)
        self.assertNotEqual(self.code(text), code)
        self.assertEqual(http.puts(), [])

    def test_refusals_come_before_any_write(self):
        http = FakeYouTube()
        bridge = self.bridge(http)
        cases = {"public video": {"video_id": "vid0000002"}, "unknown video": {"video_id": "unbekannt1"},
                 "odd id": {"video_id": "../x"}, "dash in title": {"title": "Doom – KI"},
                 "past": {"publish_at": "2026-01-01T06:00"}, "angle": {"description": "<b>x</b>"}}
        for name, args in cases.items():
            with self.subTest(name):
                text, error = self.call(bridge, confirm="deadbeef", **args)
                self.assertTrue(error, text)
                self.assertTrue(text.startswith("rejected: "))
        self.assertEqual(http.puts(), [])

    def test_a_lagging_read_is_reported_as_such(self):
        http = FakeYouTube(lag=99)
        bridge = self.bridge(http)
        text, error = self.call(bridge, confirm=self.code(self.call(bridge)[0]))
        self.assertFalse(error)
        self.assertIn("check the video in YouTube Studio", text)

    def test_the_episode_list_names_the_private_videos(self):
        bridge = self.bridge(FakeYouTube(videos=[item(), item("vid0000003", "Dummy", duration="PT3S",
                                                              publish_at="2026-01-11T05:00:00Z"),
                                                 item("vid0000002", "136 | Alt", privacy="public")]))
        rows = bridge._youtube_lines()
        self.assertEqual(rows, ["Private videos on YouTube: 2",
                                "- video_id vid0000001 | title: TBD | 50.8 min | not scheduled",
                                "- video_id vid0000003 | title: Dummy | 0.1 min | scheduled for 2026-01-11 06:00 (Europe/Berlin)"])
        self.assertEqual(Bridge(None, log=lambda *_: None)._youtube_lines(), [])

    def test_a_youtube_problem_does_not_break_the_episode_list(self):
        g = Google(FakeYouTube(), self.conf)  # no sign-in stored
        rows = Bridge(None, log=lambda *_: None, google=g)._youtube_lines()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].startswith("YouTube: not available ("))


class ConfigTests(unittest.TestCase):
    def test_publish_section(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            self.assertEqual(config.load_publish(path), config.PublishConfig("UTC", ()))
            path.write_text('[publish]\ntimezone = "Europe/Berlin"\nforbidden = ["–", "—"]\n')
            self.assertEqual(config.load_publish(path), config.PublishConfig("Europe/Berlin", ("–", "—")))
            for bad in ('timezone = "Mars/Olympus"', "timezone = 5", 'forbidden = "–"', "forbidden = [1]", 'forbidden = [""]'):
                path.write_text(f"[publish]\n{bad}\n")
                with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                    config.load_publish(path)


if __name__ == "__main__":
    unittest.main()
