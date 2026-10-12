import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from test_bachman import Base, Resp
from test_spwriter import FakeSpotify
from test_youtube import Base as GoogleBase, FakeYouTube

from bachman import config, writer
from bachman.bridge import Bridge
from bachman.rules import Refused
from bachman.writer import Draft, Drafts, Expert, ExpertConfig, ExpertError

NOW = dt.datetime(2026, 1, 10, 12, 0, tzinfo=dt.timezone.utc)
HTML = '<p>Ein Absatz.</p><p><strong>Links</strong></p><ul><li>Quelle: <a href="https://example.org/x">https://example.org/x</a></li></ul>'


def answer(**over):
    parts = {"TITLE": "137 | Ein guter Titel", "ALTERNATIVES": "137 | Zweiter\n- 137 | Dritter\n137 | Vierter",
             "DESCRIPTION": "Ein Absatz.\n\n## Links\n- Quelle: https://example.org/x", "SPOTIFY_HTML": HTML,
             "YOUTUBE_TEXT": "Ein Absatz.\n\nLinks:\n- Quelle: https://example.org/x",
             "LINKS": "https://example.org/x, from the notes, \"Example\"", "NOTES": "none", "SPONSORED": "yes", **over}
    return "\n".join(f"=== {name} ===\n{text}" for name, text in parts.items() if text is not None)


class FakeExpert:
    def __init__(self, *answers, error=None):
        self.cfg, self.answers, self.error, self.prompts = ExpertConfig("http://127.0.0.1:8768/mcp", instructions="Be brief."), list(answers), error, []

    def ask(self, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise ExpertError(self.error)
        return self.answers.pop(0)


class ParseTests(unittest.TestCase):
    def test_sections_are_cut_at_their_markers(self):
        found = writer.parse("Sure, here it is:\n" + answer())
        self.assertEqual(found["TITLE"], "137 | Ein guter Titel")
        self.assertEqual(found["SPOTIFY_HTML"], HTML)
        self.assertEqual(found["SPONSORED"], "yes")

    def test_a_code_fence_around_a_section_is_removed(self):
        found = writer.parse(answer(DESCRIPTION="```markdown\nEin Absatz.\n```", SPOTIFY_HTML=f"```html\n{HTML}\n```"))
        self.assertEqual((found["DESCRIPTION"], found["SPOTIFY_HTML"]), ("Ein Absatz.", HTML))

    def test_a_missing_or_empty_section_is_named(self):
        with self.assertRaises(ExpertError) as e:
            writer.parse(answer(YOUTUBE_TEXT=None, NOTES=" "))
        self.assertEqual(str(e.exception), "the expert's answer has no section YOUTUBE_TEXT, NOTES")
        with self.assertRaises(ExpertError):
            writer.parse("I cannot do that.")

    def test_to_draft_applies_the_rules_of_both_platforms(self):
        draft = writer.to_draft(writer.parse(answer()), "3", ("–",), clock=lambda: 5)
        self.assertRegex(draft.id, r"^d-[0-9a-f]{8}$")
        self.assertEqual((draft.title, draft.alternatives, draft.sponsored, draft.created),
                         ("137 | Ein guter Titel", ("137 | Zweiter", "137 | Dritter", "137 | Vierter"), True, 5))
        bad = answer(TITLE="137 | Ein – Titel", YOUTUBE_TEXT="Text <b>", SPOTIFY_HTML="<h1>x</h1>")
        with self.assertRaises(Refused) as e:
            writer.to_draft(writer.parse(bad), "3", ("–",))
        message = str(e.exception)
        self.assertEqual(message.count("the title contains"), 1)  # named once although both checks see it
        self.assertIn("< or >", message)
        self.assertIn("h1", message)

    def test_a_heading_line_in_front_of_the_description_is_dropped(self):
        draft = writer.to_draft(writer.parse(answer(DESCRIPTION="## Description\nEin Absatz.")), "3", ())
        self.assertEqual(draft.description, "Ein Absatz.")
        self.assertFalse(writer.to_draft(writer.parse(answer(SPONSORED="no")), "3", ()).sponsored)


class PromptTests(unittest.TestCase):
    def test_holds_number_format_notes_and_transcript(self):
        prompt = writer.build_prompt(138, "Gesprochener Text", [("Thema A", "Notiz A"), ("Thema B", "Notiz B")], "Be brief.")
        self.assertIn("The episode number is 138.", prompt)
        self.assertIn("Be brief.", prompt)
        for name in writer.SECTIONS:
            self.assertIn(f"=== {name} ===", prompt)
        self.assertIn("--- notes with the topic: Thema A ---\nNotiz A", prompt)
        self.assertIn("use only the notes that belong to this transcript", prompt)
        self.assertTrue(prompt.endswith("Gesprochener Text"))
        self.assertNotIn("PREVIOUS VERSION", prompt)

    def test_long_material_is_cut(self):
        prompt = writer.build_prompt(None, "x" * (writer.MAX_TRANSCRIPT + 5), [("A", "n" * writer.MAX_NOTES), ("B", "zzz")])
        self.assertIn(f"[cut after {writer.MAX_TRANSCRIPT} of {writer.MAX_TRANSCRIPT + 5} characters]", prompt)
        self.assertNotIn("zzz", prompt)
        self.assertNotIn("episode number", prompt)

    def test_a_change_request_carries_the_previous_version(self):
        old = Draft("d-00000000", "3", "137 | Alt", ("137 | B",), "Alter Text", HTML, "yt", "l", "n", False)
        prompt = writer.build_prompt(137, "T", [], previous=old, feedback=" kürzer bitte ")
        self.assertIn("[YOUR PREVIOUS VERSION]\n=== TITLE ===\n137 | Alt", prompt)
        self.assertIn("[CHANGE THE HOST ASKS FOR]\nkürzer bitte\n", prompt)


class ExpertHttp:
    def __init__(self, resp=None, boom=None):
        self.resp, self.boom, self.calls = resp, boom, []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        if self.boom:
            raise self.boom
        return self.resp


class ExpertTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.token = Path(self._tmp.name) / "token"
        self.token.write_text("EXPERT-SECRET\n")
        self.cfg = ExpertConfig("http://127.0.0.1:8768/mcp", skill="podcast/shownotes", timeout_seconds=100)

    def ok(self, text="the answer", error=False):
        return Resp(body={"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}], "isError": error}})

    def test_calls_the_tool_with_prompt_skill_and_token(self):
        http = ExpertHttp(self.ok())
        self.assertEqual(Expert(http, self.cfg, self.token).ask("Write it"), "the answer")
        url, kw = http.calls[0]
        self.assertEqual(url, "http://127.0.0.1:8768/mcp")
        self.assertEqual(kw["json"]["params"], {"name": "gregory_ask", "arguments": {"prompt": "Write it", "skill": "podcast/shownotes"}})
        self.assertEqual((kw["headers"]["Authorization"], kw["timeout"]), ("Bearer EXPERT-SECRET", 120))

    def test_failures_have_safe_messages(self):
        cases = ((ExpertHttp(self.ok("rejected: the limit of 30 requests per day is used up", error=True)), "the expert refused: rejected: the limit"),
                 (ExpertHttp(Resp(status=401, body={})), "the expert answered with HTTP 401"),
                 (ExpertHttp(Resp(body={"error": {"code": -32602}})), "cannot be read"),
                 (ExpertHttp(boom=ConnectionError("http://127.0.0.1:8768 EXPERT-SECRET")), "the expert cannot be reached (ConnectionError)"))
        for http, message in cases:
            with self.subTest(message), self.assertRaises(ExpertError) as e:
                Expert(http, self.cfg, self.token).ask("x")
            self.assertIn(message, str(e.exception))
            self.assertNotIn("SECRET", str(e.exception))
        self.token.unlink()
        with self.assertRaises(ExpertError) as e:
            Expert(ExpertHttp(self.ok()), self.cfg, self.token).ask("x")
        self.assertEqual(str(e.exception), "the token for the expert is missing")


class DraftsTests(unittest.TestCase):
    def test_keeps_the_newest_and_refuses_unknown_ids(self):
        with tempfile.TemporaryDirectory() as d:
            store = Drafts(Path(d) / "state" / "drafts.json")
            for i in range(writer.KEEP + 3):
                store.save(Draft(f"d-{i:08x}", "3", f"t{i}", ("a",), "d", HTML, "y", "l", "n", True, i))
            self.assertEqual(len(json.loads(store.path.read_text())), writer.KEEP)
            self.assertEqual(oct(store.path.stat().st_mode & 0o777), "0o600")
            newest = store.get(f"d-{writer.KEEP + 2:08x}")
            self.assertEqual((newest.title, newest.alternatives, newest.sponsored), (f"t{writer.KEEP + 2}", ("a",), True))
            for bad in ("d-00000000", "../etc", 7, None):
                with self.subTest(bad=bad), self.assertRaises(Refused):
                    store.get(bad)


class DraftToolTests(Base):
    def bridge(self, expert, http=None, **kw):
        self.http = http or FakeSpotify()
        self.drafts = Drafts(Path(self.tmp.name) / "state" / "drafts.json")
        self.logged = []
        return Bridge(self.client(self.http), log=self.logged.append, publish=config.PublishConfig("Europe/Berlin", ("–", "—")),
                      now=lambda: NOW, sleep=lambda s: None, expert=expert, drafts=self.drafts, **kw)

    def call(self, bridge, name="podcast_draft_texts", **args):
        out = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
        return out["result"]["content"][0]["text"], out["result"]["isError"]

    def draft_id(self, text):
        return text.split("Draft ", 1)[1].split(" ", 1)[0]

    def test_the_service_collects_the_material_itself_and_stores_the_result(self):
        expert = FakeExpert(answer())
        text, error = self.call(self.bridge(expert))
        self.assertFalse(error, text)
        prompt = expert.prompts[0]
        self.assertIn("The episode number is 137.", prompt)  # the newest published one is 136
        self.assertIn("Be brief.", prompt)
        self.assertTrue(prompt.endswith("Hallo Welt. " * 10))
        draft = self.drafts.get(self.draft_id(text))
        self.assertEqual((draft.episode_id, draft.title, draft.html, draft.sponsored), ("3", "137 | Ein guter Titel", HTML, True))
        self.assertIn("for the Spotify episode 3 (50.0 min)", text)
        self.assertIn("Title: 137 | Ein guter Titel\nOther titles:\n- 137 | Zweiter\n- 137 | Dritter", text)
        self.assertIn("Contains paid promotion: yes", text)
        self.assertIn(f'with draft = "{draft.id}"', text)
        self.assertNotIn("<p>", text)  # the chat agent never needs the HTML
        self.assertEqual(self.http.writes(), [])

    def test_an_answer_that_fails_the_rules_gets_one_more_try(self):
        expert = FakeExpert(answer(TITLE="137 | Mit – Strich"), answer())
        text, error = self.call(self.bridge(expert))
        self.assertFalse(error, text)
        self.assertEqual(len(expert.prompts), 2)
        self.assertIn("[YOUR FIRST ANSWER WAS NOT ACCEPTED]\nthe title contains '–'", expert.prompts[1])
        self.drafts.path.unlink()
        expert = FakeExpert(answer(TITLE="137 | Mit – Strich"), "no sections at all")
        text, error = self.call(self.bridge(expert))
        self.assertTrue(error)
        self.assertEqual(text, "rejected: the expert's answer has no section " + ", ".join(writer.SECTIONS))
        self.assertFalse(self.drafts.path.exists())  # nothing is stored when the second answer fails too

    def test_a_change_goes_back_to_the_expert_with_the_old_version(self):
        expert = FakeExpert(answer(), answer(TITLE="137 | Kürzer"))
        bridge = self.bridge(expert)
        first = self.draft_id(self.call(bridge)[0])
        text, error = self.call(bridge, draft=first, feedback="Titel kürzer")
        self.assertFalse(error, text)
        self.assertIn("[YOUR PREVIOUS VERSION]\n=== TITLE ===\n137 | Ein guter Titel", expert.prompts[1])
        self.assertIn("[CHANGE THE HOST ASKS FOR]\nTitel kürzer", expert.prompts[1])
        second = self.drafts.get(self.draft_id(text))
        self.assertEqual((second.title, second.episode_id), ("137 | Kürzer", "3"))
        self.assertNotEqual(second.id, first)

    def test_refusals(self):
        bridge = self.bridge(FakeExpert(answer()))
        cases = (({"episode_id": "999"}, "no unpublished draft with this id"), ({"feedback": "x"}, "feedback needs the id"),
                 ({"draft": "d-00000000", "feedback": "x"}, "there is no draft with this id"), ({"draft": "nope"}, "draft must be an id"))
        for args, message in cases:
            with self.subTest(message):
                text, error = self.call(bridge, **args)
                self.assertTrue(error)
                self.assertIn(message, text)
        first = self.draft_id(self.call(bridge)[0])
        text, error = self.call(bridge, draft=first)
        self.assertIn("say in feedback what should change", text)
        text, error = self.call(self.bridge(None))
        self.assertEqual((text, error), ("rejected: no expert is set up", True))
        text, error = self.call(self.bridge(FakeExpert(error="the expert refused: rejected: busy")))
        self.assertEqual((text, error), ("rejected: the expert refused: rejected: busy", True))

    def test_without_a_transcript_or_with_two_candidates_nothing_is_asked(self):
        expert = FakeExpert(answer())
        text, error = self.call(self.bridge(expert, FakeSpotify(transcript="")))
        self.assertEqual((text, error), ("rejected: there is no unpublished draft with a transcript yet", True))
        http = FakeSpotify()
        http.items.insert(0, dict(http.items[0], episodeId=4, uri="spotify:episode:u4"))
        text, error = self.call(self.bridge(expert, http))
        self.assertIn("2 drafts have a transcript, pass episode_id for the one you mean: id 4", text)
        self.assertEqual(expert.prompts, [])

    def test_spotify_is_scheduled_from_the_stored_draft(self):
        http = FakeSpotify()
        bridge = self.bridge(FakeExpert(answer()), http)
        draft = self.draft_id(self.call(bridge)[0])
        args = {"episode_id": "70000007", "draft": draft, "publish_at": "2026-01-11T06:00"}
        text, error = self.call(bridge, "podcast_schedule_spotify", **args)
        self.assertTrue(error)
        self.assertEqual(text, "rejected: this draft was written for episode 3, not for 70000007")
        self.drafts.save(Draft("d-0000abcd", "70000007", "137 | Ein guter Titel", (), "d", HTML, "yt", "l", "n", True))
        args["draft"] = "d-0000abcd"
        text, error = self.call(bridge, "podcast_schedule_spotify", **args)
        self.assertFalse(error, text)
        self.assertTrue(text.startswith("PREVIEW, nothing was changed."))
        self.assertIn("New title: 137 | Ein guter Titel", text)
        self.assertIn("Contains paid promotion: yes", text)  # taken from the draft
        self.assertIn("Ein Absatz.\nLinks\nQuelle: https://example.org/x", text)
        code = text.rsplit('confirm = "', 1)[1].split('"')[0]
        other, _ = self.call(bridge, "podcast_schedule_spotify", **args, title="137 | Anderer Titel")
        self.assertIn("New title: 137 | Anderer Titel", other)
        refused, _ = self.call(bridge, "podcast_schedule_spotify", **args, description=HTML)
        self.assertEqual(refused, "rejected: pass either draft or description, not both")
        self.assertEqual(http.writes(), [])
        text, error = self.call(bridge, "podcast_schedule_spotify", **args, confirm=code)
        self.assertTrue(text.startswith("Scheduled on Spotify."), text)
        sent = json.loads(next(c for c in http.writes() if c[0] == "POST")[2]["data"])
        self.assertEqual((sent["title"], sent["description"]), ("137 | Ein guter Titel", HTML))


class YouTubeFromDraftTests(GoogleBase):
    def test_youtube_is_scheduled_from_the_stored_draft(self):
        drafts = Drafts(self.conf / "drafts.json")
        drafts.save(Draft("d-0000abcd", "3", "137 | Ein guter Titel", (), "d", HTML, "Reiner Text\n- Quelle: https://example.org/x", "l", "n", False))
        http = FakeYouTube()
        bridge = Bridge(None, log=lambda *_: None, google=self.client(http), publish=config.PublishConfig("Europe/Berlin", ("–", "—")),
                        now=lambda: NOW, sleep=lambda s: None, drafts=drafts)

        def call(**args):
            out = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "podcast_schedule_youtube", "arguments": {"video_id": "vid0000001", "publish_at": "2026-01-11T06:00", **args}}})
            return out["result"]["content"][0]["text"], out["result"]["isError"]

        text, error = call(draft="d-0000abcd")
        self.assertFalse(error, text)
        self.assertIn("New title: 137 | Ein guter Titel", text)
        self.assertIn("Reiner Text\n- Quelle: https://example.org/x", text)
        text, error = call(draft="d-0000abcd", confirm=text.rsplit('confirm = "', 1)[1].split('"')[0])
        self.assertTrue(text.startswith("Scheduled on YouTube."), text)
        self.assertEqual(http.puts()[0][2]["json"]["snippet"]["title"], "137 | Ein guter Titel")
        text, error = call()
        self.assertTrue(error)  # neither a draft nor texts
        text, error = call(draft="d-ffffffff")
        self.assertIn("there is no draft with this id", text)


class ConfigTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text(text)
            return config.load_expert(path)

    def test_expert_section(self):
        self.assertIsNone(self.load(""))
        self.assertEqual(self.load('[expert]\nurl = "http://127.0.0.1:8768/mcp"\nskill = "podcast/shownotes"\n'),
                         ExpertConfig("http://127.0.0.1:8768/mcp", "podcast/shownotes"))
        for bad in ('expert = 3', '[expert]\nskill = "x"', '[expert]\nurl = "https://example.org/mcp"',
                    '[expert]\nurl = "http://127.0.0.1:8768/mcp"\ntimeout_seconds = 1',
                    '[expert]\nurl = "http://127.0.0.1:8768/mcp"\nskill = 3'):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                self.load(bad)


if __name__ == "__main__":
    unittest.main()
