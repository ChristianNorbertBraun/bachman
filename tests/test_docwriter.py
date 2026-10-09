import json
import tempfile
import unittest
from pathlib import Path

from bachman import agenda, config, docwriter, google
from bachman.bridge import Bridge
from bachman.docwriter import WriteRefused
from bachman.google import Google


def para(text, index, style="NORMAL_TEXT", bullet=None, page_break=False):
    p = {"elements": ([{"pageBreak": {}}] if page_break else []) + [{"textRun": {"content": text + "\n", "textStyle": {}}}],
         "paragraphStyle": {"namedStyleType": style}}
    if bullet is not None:
        p["bullet"] = {"nestingLevel": bullet}
    return {"startIndex": index, "paragraph": p}


def document(extra=()):
    return {"tabs": [{"tabProperties": {"title": "Plan", "tabId": "t.0"}, "documentTab": {"body": {"content": [
        para("Checkliste", 1),
        para("TEMPLATE", 12),
        para("Cold Open", 21, "HEADING_2"),
        para("Einstieg", 31, bullet=0),
        para("Detail", 40, bullet=1),
        para("Links:", 47),
        para("TEMPLATE END", 54),
        para("START Altes Thema", 67, page_break=True),
        para("alte Notiz", 85),
        para("END", 96),
        *extra,
    ]}}}]}


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body


class FakeDocs:
    """Returns the document, and after a batchUpdate a document that contains the new episode."""

    def __init__(self, doc=None, write_status=200, appears=True):
        self.calls, self.doc, self.write_status, self.appears, self.written = [], doc or document(), write_status, appears, False

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if url == google.TOKEN_URL:
            return Resp(200, {"access_token": "ACCESS", "expires_in": 3600})
        self.written = self.write_status == 200
        return Resp(self.write_status)

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        if self.written and self.appears:
            return Resp(200, document(extra=[para("START Neues Thema", 200), para("Cold Open", 220, "HEADING_2"), para("END", 240)]))
        return Resp(200, self.doc)

    def writes(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] != google.TOKEN_URL]


LINES = agenda.lines(document())
MARKS = ("START", "END", "TEMPLATE", "TEMPLATE END")


class PlanTests(unittest.TestCase):
    def test_lines_know_their_position_tab_and_nesting(self):
        by_text = {l.text: l for l in LINES}
        self.assertEqual((by_text["Cold Open"].index, by_text["Cold Open"].tab_id, by_text["Cold Open"].level), (21, "t.0", 2))
        self.assertEqual((by_text["  - Detail"].bullet, by_text["- Einstieg"].bullet, by_text["Links:"].bullet), (1, 0, -1))

    def test_block_is_inserted_above_the_newest_episode(self):
        planned = docwriter.plan(LINES, "  Neues   Thema ", *MARKS)
        self.assertEqual((planned.topic, planned.index, planned.tab_id), ("Neues Thema", 67, "t.0"))
        insert = planned.requests[0]["insertText"]
        self.assertEqual(insert["location"], {"index": 67, "tabId": "t.0"})
        self.assertEqual(insert["text"], "START Neues Thema\nCold Open\nEinstieg\n\tDetail\nLinks:\nEND\n")

    def test_new_paragraphs_are_reset_then_styled_from_the_bottom_up(self):
        planned = docwriter.plan(LINES, "Neues Thema", *MARKS)
        end = 67 + len(planned.requests[0]["insertText"]["text"])
        self.assertEqual(planned.requests[1]["updateParagraphStyle"]["range"], {"startIndex": 67, "endIndex": end, "tabId": "t.0"})
        self.assertEqual(planned.requests[1]["updateParagraphStyle"]["paragraphStyle"], {"namedStyleType": "NORMAL_TEXT"})
        self.assertEqual(planned.requests[2]["deleteParagraphBullets"]["range"]["startIndex"], 67)
        styled = [(next(iter(r)), next(iter(r.values()))["range"]["startIndex"]) for r in planned.requests[3:]]
        # "START Neues Thema\n" is 18 characters: Cold Open at 85, Einstieg at 95, Detail at 104
        self.assertEqual(styled, [("createParagraphBullets", 104), ("createParagraphBullets", 95), ("updateParagraphStyle", 85)])
        self.assertEqual(planned.requests[-1]["updateParagraphStyle"]["paragraphStyle"], {"namedStyleType": "HEADING_2"})

    def test_every_request_only_adds_or_styles(self):
        planned = docwriter.plan(LINES, "Neues Thema", *MARKS)
        for request in planned.requests:
            self.assertLessEqual(set(request), docwriter.ALLOWED)
            self.assertFalse(any("delete" in k.lower() and k != "deleteParagraphBullets" or "replace" in k.lower() for k in request))

    def test_without_an_episode_the_block_goes_right_after_the_template(self):
        only_template = [l for l in LINES if l.index < 85]
        only_template = [l for l in only_template if not l.text.startswith("START")] + [agenda.Line("Archiv", 0, False, "", 300, "t.0")]
        self.assertEqual(docwriter.plan(only_template, "Erste", *MARKS).index, 300)

    def test_refusals(self):
        cases = {
            "same topic again": (LINES, "altes thema"), "empty": (LINES, "  "), "too long": (LINES, "x" * 81),
            "not text": (LINES, 5), "the end marker": (LINES, "END"),
            "no template": ([l for l in LINES if l.text != "TEMPLATE"], "Neu"),
            "empty template": ([l for l in LINES if l.index not in (21, 31, 40, 47)], "Neu"),
        }
        for name, (lines, topic) in cases.items():
            with self.subTest(name), self.assertRaises(WriteRefused):
                docwriter.plan(lines, topic, *MARKS)

    def test_a_line_break_in_the_topic_cannot_smuggle_in_a_marker(self):
        planned = docwriter.plan(LINES, "Thema\nEND\nSTART Zwei", *MARKS)
        self.assertEqual(planned.requests[0]["insertText"]["text"].split("\n")[0], "START Thema END START Zwei")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = Path(self.tmp.name)

    def client(self, http, scopes=(docwriter.WRITE_SCOPE,)):
        (self.conf / "client_id").write_text("C.apps")
        (self.conf / "client_secret").write_text("S")
        (self.conf / "token.json").write_text(json.dumps({"refresh_token": "R", "scopes": list(scopes)}))
        return Google(http, self.conf)


class ApplyTests(Base):
    def test_requests_go_to_batch_update_of_the_document(self):
        http = FakeDocs()
        docwriter.apply(self.client(http), "DOC", docwriter.plan(LINES, "Neu", *MARKS))
        (_, url, kw), = http.writes()
        self.assertEqual(url, "https://docs.googleapis.com/v1/documents/DOC:batchUpdate")
        self.assertEqual(kw["headers"], {"Authorization": "Bearer ACCESS"})
        self.assertEqual(kw["json"]["requests"][0]["insertText"]["location"]["index"], 67)

    def test_a_read_only_sign_in_and_foreign_requests_send_nothing(self):
        planned = docwriter.plan(LINES, "Neu", *MARKS)
        http = FakeDocs()
        with self.assertRaises(WriteRefused):
            docwriter.apply(self.client(http, scopes=("https://www.googleapis.com/auth/documents.readonly",)), "DOC", planned)
        evil = docwriter.Plan("x", (), 1, "", ({"deleteContentRange": {}},))
        with self.assertRaises(WriteRefused):
            docwriter.apply(self.client(http), "DOC", evil)
        self.assertEqual(http.writes(), [])

    def test_google_errors_are_reported_without_details(self):
        for status in (403, 500):
            with self.subTest(status), self.assertRaises(google.GoogleError):
                docwriter.apply(self.client(FakeDocs(write_status=status)), "DOC", docwriter.plan(LINES, "Neu", *MARKS))


class ToolTests(Base):
    def bridge(self, http, **kw):
        return Bridge(None, log=lambda *_: None, google=self.client(http, **kw), agenda=config.AgendaConfig("D" * 30))

    def call(self, bridge, topic="Neues Thema"):
        out = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "podcast_create_agenda", "arguments": {"topic": topic}}})
        return out["result"]["content"][0]["text"], out["result"]["isError"]

    def test_success_is_only_reported_after_reading_the_document_again(self):
        http = FakeDocs()
        text, error = self.call(self.bridge(http))
        self.assertFalse(error)
        self.assertIn('Added the episode "Neues Thema"', text)
        self.assertIn("4 lines copied from the template", text)
        self.assertEqual([c[0] for c in http.calls if c[1] != google.TOKEN_URL], ["GET", "POST", "GET"])

    def test_a_write_that_does_not_show_up_is_an_error(self):
        text, error = self.call(self.bridge(FakeDocs(appears=False)))
        self.assertTrue(error)
        self.assertIn("check it by hand", text)

    def test_refusals_reach_the_agent_and_write_nothing(self):
        http = FakeDocs()
        text, error = self.call(self.bridge(http), topic="Altes Thema")
        self.assertTrue(error)
        self.assertIn("already has an episode", text)
        text, error = self.call(self.bridge(http, scopes=()), topic="Neu")
        self.assertTrue(error)
        self.assertIn("sign in again", text)
        self.assertEqual(http.writes(), [])
        plain = Bridge(None, log=lambda *_: None)
        self.assertEqual(self.call(plain), ("rejected: the planning document is not configured", True))


if __name__ == "__main__":
    unittest.main()
