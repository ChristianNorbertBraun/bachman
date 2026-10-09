import datetime as dt
import json
import unittest

from test_bachman import Base, FakeHttp, Resp, episode

from bachman import config, rules, spwriter
from bachman.bridge import Bridge
from bachman.spotify import REST, SpotifyError
from bachman.spwriter import Change

NOW = dt.datetime(2026, 1, 10, 12, 0, tzinfo=dt.timezone.utc)
WHEN = dt.datetime(2026, 1, 11, 5, 0, tzinfo=dt.timezone.utc)
HTML = '<p>Erster Absatz.</p><p><strong>Links</strong></p><ul><li>Quelle: <a href="https://example.org/x">https://example.org/x</a></li></ul>'


def overview(**kw):
    base = {"userId": 42, "title": "", "description": "", "isDeleted": False, "isPublished": False, "isDraft": True,
            "publishOnUnixTimestamp": None, "podcastEpisodeType": "full", "podcastEpisodeIsExplicit": False,
            "isVideoEighteenPlus": False, "totalDuration": 3046608,
            "episodeAudios": [{"audioTransformationStatus": "finished"}]}
    return {**base, **kw}


class FakeSpotify(FakeHttp):
    """Adds the REST side: the episode details, the update and the paid-promotion setting."""

    def __init__(self, details=None, update_status=200, promo_status=200, lag=0, **kw):
        super().__init__(**kw)
        self.details, self.update_status, self.promo_status, self.lag = details or overview(), update_status, promo_status, lag
        self.rest = []

    def get(self, url, **kw):
        if url.startswith(REST) and url.endswith("/overview"):
            self.rest.append(("GET", url, kw))
            if self.lag > 0 and any(c[0] == "POST" for c in self.rest):
                self.lag -= 1
                return Resp(body=overview())
            return Resp(body=self.details)
        return super().get(url, **kw)

    def post(self, url, **kw):
        if url.startswith(REST):
            self.rest.append(("POST", url, kw))
            if self.update_status == 200:
                sent = json.loads(kw["data"])
                stamp = int(dt.datetime.fromisoformat(sent["publishOn"].replace("Z", "+00:00")).timestamp())
                # Spotify adds attributes to links and keeps isPublished false until the time has come
                stored = sent["description"].replace('">', '" rel="ugc noopener noreferrer" target="_blank">')
                self.details = {**self.details, "title": sent["title"], "description": stored, "publishOnUnixTimestamp": stamp}
            return Resp(status=self.update_status, body={})
        return super().post(url, **kw)

    def put(self, url, **kw):
        self.rest.append(("PUT", url, kw))
        return Resp(status=self.promo_status, body={})

    def writes(self):
        return [c for c in self.rest if c[0] in ("POST", "PUT")]


class TextTests(unittest.TestCase):
    def test_html_is_put_on_one_line_and_the_visible_text_is_returned(self):
        title, html, text = spwriter.check_text(" 137 | Titel ", "<p>Eins</p>\n\n<ul>\n  <li>Zwei</li>\n</ul>")
        self.assertEqual((title, html, text), ("137 | Titel", "<p>Eins</p><ul><li>Zwei</li></ul>", "Eins\nZwei"))

    def test_only_the_six_tags_and_plain_links_are_allowed(self):
        spwriter.check_text("t", HTML)
        bad = {"heading": "<h2>x</h2>", "line break": "<p>a<br>b</p>", "script": "<p>x</p><script>alert(1)</script>",
               "class": '<p class="x">y</p>', "link with target": '<p><a href="https://e.org" target="_blank">x</a></p>',
               "javascript link": '<p><a href="javascript:alert(1)">x</a></p>', "link without href": "<p><a>x</a></p>",
               "unclosed": "<p>x", "stray end": "<p>x</p></ul>", "plain text": "nur Text", "empty": "<p></p>"}
        for name, html in bad.items():
            with self.subTest(name), self.assertRaises(rules.Refused):
                spwriter.check_text("t", html)

    def test_title_and_length_limits(self):
        for title in ("", "x" * 201, "a\nb", "<b>fett</b>"):
            with self.subTest(title=title[:10]), self.assertRaises(rules.Refused):
                spwriter.check_text(title, "<p>x</p>")
        spwriter.check_text("t", "<p>" + "x" * 4000 + "</p>")
        with self.assertRaises(rules.Refused):
            spwriter.check_text("t", "<p>" + "x" * 4001 + "</p>")

    def test_forbidden_characters_are_checked_in_the_visible_text(self):
        with self.assertRaises(rules.Refused) as ctx:
            spwriter.check_text("Doom – KI", "<p>Text — Strich</p>", ("–", "—"))
        self.assertIn("the title contains '–'", str(ctx.exception))
        self.assertIn("the description contains '—'", str(ctx.exception))
        spwriter.check_text("t", '<p><a href="https://example.org/a–b">https://example.org/a-b</a></p>', ("–",))

    def test_not_text_is_refused(self):
        with self.assertRaises(rules.Refused):
            spwriter.check_text(5, "<p>x</p>")


class EpisodeTests(unittest.TestCase):
    def test_a_draft_and_a_scheduled_episode_are_fine(self):
        spwriter.check_episode(overview())
        spwriter.check_episode(overview(publishOnUnixTimestamp=4102444800))  # far in the future: rescheduling

    def test_everything_else_is_refused(self):
        bad = {"published": overview(isPublished=True), "published in the past": overview(publishOnUnixTimestamp=1000),
               "deleted": overview(isDeleted=True), "no media": overview(episodeAudios=[]),
               "still processing": overview(episodeAudios=[{"audioTransformationStatus": "processing"}])}
        for name, details in bad.items():
            with self.subTest(name), self.assertRaises(rules.Refused):
                spwriter.check_episode(details)

    def test_the_body_is_what_the_editor_sends_when_scheduling(self):
        change = Change("7", "137 | Neu", HTML, WHEN, None)
        self.assertEqual(spwriter.body(overview(podcastEpisodeNumber=137), change), {
            "userId": 42, "title": "137 | Neu", "description": HTML, "episodeType": "full",
            "podcastEpisodeIsExplicit": False, "isVideoEighteenPlus": False, "isPublished": True,
            "publishOn": "2026-01-11T05:00:00.000Z", "wizardDraftedToPublishOn": "2026-01-11T05:00:00.000Z",
            "episodeNumber": 137})

    def test_the_code_covers_the_paid_promotion_setting_too(self):
        codes = {Change("7", "t", "<p>d</p>", WHEN, s).code for s in (None, True, False)}
        self.assertEqual(len(codes), 3)
        self.assertNotEqual(Change("7", "t", "<p>d</p>", WHEN, None).code, Change("8", "t", "<p>d</p>", WHEN, None).code)


class ApplyTests(Base):
    def change(self, sponsored=None):
        return Change("7", "137 | Neu", HTML, WHEN, sponsored)

    def test_one_post_to_the_update_of_that_episode_then_a_read(self):
        http = FakeSpotify()
        client = self.client(http)
        self.assertTrue(spwriter.apply(client, overview(), self.change(), sleep=lambda s: None))
        (method, url, kw), = http.writes()
        self.assertEqual((method, url, kw["params"]), ("POST", f"{REST}/v3/episodes/7/update", {"isMumsCompatible": "true"}))
        self.assertEqual(kw["headers"]["Authorization"], "Bearer BEARER-SECRET")
        self.assertEqual(kw["headers"]["Anchor-Client-Type"], "web")
        self.assertEqual(json.loads(kw["data"])["isPublished"], True)

    def test_paid_promotion_is_set_first_and_a_failure_stops_before_the_schedule(self):
        http = FakeSpotify()
        self.assertTrue(spwriter.apply(self.client(http), overview(), self.change(True), sleep=lambda s: None))
        self.assertEqual([c[0] for c in http.writes()], ["PUT", "POST"])
        self.assertEqual(json.loads(http.writes()[0][2]["data"]), {"containsSponsoredContent": True, "publishOn": 1768107600000})
        http = FakeSpotify(promo_status=500)
        with self.assertRaises(SpotifyError) as ctx:
            spwriter.apply(self.client(http), overview(), self.change(False), sleep=lambda s: None)
        self.assertIn("nothing was scheduled", str(ctx.exception))
        self.assertEqual([c[0] for c in http.writes()], ["PUT"])

    def test_a_rejected_update_and_a_lagging_read(self):
        with self.assertRaises(SpotifyError) as ctx:
            spwriter.apply(self.client(FakeSpotify(update_status=400)), overview(), self.change(), sleep=lambda s: None)
        self.assertNotIn("SECRET", str(ctx.exception))
        waits = []
        self.assertTrue(spwriter.apply(self.client(FakeSpotify(lag=2)), overview(), self.change(), sleep=waits.append))
        self.assertEqual(waits, [2, 2])
        self.assertFalse(spwriter.apply(self.client(FakeSpotify(lag=99)), overview(), self.change(), sleep=lambda s: None, attempts=2))


class ToolTests(Base):
    ARGS = {"episode_id": "70000007", "title": "137 | Neu", "description": HTML, "publish_at": "2026-01-11T06:00"}

    def bridge(self, http):
        return Bridge(self.client(http), log=lambda *_: None, publish=config.PublishConfig("Europe/Berlin", ("–", "—")),
                      now=lambda: NOW, sleep=lambda s: None)

    def call(self, bridge, **args):
        out = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "podcast_schedule_spotify", "arguments": {**self.ARGS, **args}}})
        return out["result"]["content"][0]["text"], out["result"]["isError"]

    def code(self, text):
        return text.rsplit('confirm = "', 1)[1].split('"')[0]

    def test_preview_shows_the_text_as_readers_see_it_and_writes_nothing(self):
        http = FakeSpotify()
        text, error = self.call(self.bridge(http), sponsored=True)
        self.assertFalse(error)
        self.assertTrue(text.startswith("PREVIEW, nothing was changed."))
        self.assertIn('currently titled "(none)", 50.8 min, not scheduled', text)
        self.assertIn("Publish time: Sunday 2026-01-11 06:00 (Europe/Berlin), that is 2026-01-11T05:00:00.000Z UTC", text)
        self.assertIn("Contains paid promotion: yes", text)
        self.assertIn("Erster Absatz.\nLinks\nQuelle: https://example.org/x", text)
        self.assertEqual(http.writes(), [])

    def test_confirmed_call_schedules_and_reports(self):
        http = FakeSpotify()
        bridge = self.bridge(http)
        text, error = self.call(bridge, confirm=self.code(self.call(bridge)[0]))
        self.assertFalse(error)
        self.assertTrue(text.startswith("Scheduled on Spotify. Spotify shows the new values."))
        self.assertIn("Contains paid promotion: unchanged", text)
        self.assertEqual([c[0] for c in http.writes()], ["POST"])

    def test_a_code_for_other_values_and_refusals_write_nothing(self):
        http = FakeSpotify()
        bridge = self.bridge(http)
        code = self.code(self.call(bridge)[0])
        text, error = self.call(bridge, confirm=code, sponsored=True)
        self.assertFalse(error)
        self.assertIn("does not belong to these values", text)
        cases = {"odd id": {"episode_id": "7/../8"}, "dash": {"title": "Doom – KI"}, "bad html": {"description": "<h1>x</h1>"},
                 "past": {"publish_at": "2026-01-01T06:00"}, "sponsored text": {"sponsored": "ja"}}
        for name, args in cases.items():
            with self.subTest(name):
                text, error = self.call(bridge, confirm="deadbeef", **args)
                self.assertTrue(error, text)
        published = self.bridge(FakeSpotify(details=overview(isPublished=True)))
        self.assertTrue(self.call(published)[1])
        self.assertEqual(http.writes(), [])

    def test_the_episode_list_separates_scheduled_from_published(self):
        http = FakeSpotify()
        http.items = [episode(3, "", None), episode(4, "137 | Geplant", 4102444800), episode(2, "136 | Alt", 1790380860)]
        out = self.bridge(http).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "podcast_list_episodes", "arguments": {}}})
        text = out["result"]["content"][0]["text"]
        self.assertIn("Unpublished drafts on Spotify: 1", text)
        self.assertIn("Scheduled on Spotify: 1\n- id 4 | 137 | Geplant | scheduled for 2100-01-01 01:00 (Europe/Berlin)", text)
        self.assertIn("Next episode number: 138", text)
        self.assertIn("Latest published episodes:\n- 136 | Alt", text)


if __name__ == "__main__":
    unittest.main()
