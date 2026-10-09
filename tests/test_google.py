import json
import stat
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from bachman import agenda, config, google
from bachman.bridge import OUTLINE_MAX, TRANSCRIPT_CHUNK, Bridge
from bachman.google import Google, GoogleError, NotSignedIn


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self, document=None, token_status=200, refresh_status=200, get_status=200):
        self.calls, self.document = [], document or {}
        self.token_status, self.refresh_status, self.get_status = token_status, refresh_status, get_status

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        assert url == google.TOKEN_URL, url
        if kw["data"]["grant_type"] == "authorization_code":
            return Resp(self.token_status, {"access_token": "ACCESS-1", "refresh_token": "REFRESH-SECRET",
                                            "expires_in": 3600, "scope": "b a"})
        return Resp(self.refresh_status, {"access_token": "ACCESS-2", "expires_in": 3600})

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return Resp(self.get_status, self.document)


def para(text, style="NORMAL_TEXT", link=None, bullet=None, page_break=False):
    run = {"content": text + "\n", "textStyle": {"link": {"url": link}} if link else {}}
    p = {"elements": ([{"pageBreak": {}}] if page_break else []) + [{"textRun": run}],
         "paragraphStyle": {"namedStyleType": style}}
    if bullet is not None:
        p["bullet"] = {"nestingLevel": bullet}
    return {"paragraph": p}


DOC = {"tabs": [{"tabProperties": {"title": "Planung"}, "documentTab": {"body": {"content": [
    para("Vorab ohne Überschrift"),
    para("12 | Erste Folge", "HEADING_1"),
    para("Agenda", "HEADING_2"),
    para("Punkt eins", bullet=0),
    para("Unterpunkt", bullet=1),
    para("Links", "HEADING_2"),
    para("Der Vortrag", link="https://example.org/talk", bullet=0),
    para("https://example.org/plain", link="https://example.org/plain"),
    {"paragraph": {"elements": [{"richLink": {"richLinkProperties": {"title": "Video", "uri": "https://youtu.be/x"}}}],
                   "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}}},
    {"table": {}},
    para("13 | Zweite Folge", "HEADING_1"),
    para("Nur ein Satz."),
]}}}]}

# a document that marks the current episode and keeps older notes on later pages, without headings
MARKED = {"tabs": [{"tabProperties": {"title": "Aktuell"}, "documentTab": {"body": {"content": [
    para("Checkliste vor der Aufnahme"),
    para("START Thema X", page_break=True),
    para("Cold Open", "HEADING_2"),
    para("Erster Punkt", bullet=0),
    para("Quelle", link="https://example.org/source", bullet=0),
    para("END", page_break=True),
    para("Ältere Folge"),
    para("alte Notiz", bullet=0),
    para("- Noch ältere Folge", page_break=True),
]}}}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = Path(self.tmp.name) / "google"

    def client(self, http, signed_in=True):
        self.conf.mkdir(exist_ok=True)
        (self.conf / "client_id").write_text("CLIENT.apps\n")
        (self.conf / "client_secret").write_text("CLIENT-SECRET\n")
        if signed_in:
            (self.conf / "token.json").write_text(json.dumps({"refresh_token": "REFRESH-SECRET", "scopes": []}))
        return Google(http, self.conf)


class LoginTests(Base):
    def test_login_url_asks_for_offline_access_with_pkce(self):
        url, state, verifier = self.client(FakeHttp(), signed_in=False).start_login()
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertTrue(url.startswith(google.AUTH_URL + "?"))
        self.assertEqual((q["client_id"], q["access_type"], q["prompt"], q["code_challenge_method"]),
                         (["CLIENT.apps"], ["offline"], ["consent"], ["S256"]))
        self.assertEqual(q["state"], [state])
        self.assertEqual(q["code_challenge"], [google._challenge(verifier)])
        self.assertEqual(q["scope"][0].split(), list(google.SCOPES))
        self.assertNotIn("CLIENT-SECRET", url)

    def test_finish_stores_only_the_refresh_token_with_mode_600(self):
        http = FakeHttp()
        client = self.client(http, signed_in=False)
        granted = client.finish_login(f"{google.REDIRECT}?state=S&code=CODE&scope=x", "S", "VERIFIER")
        self.assertEqual(granted, ["a", "b"])
        stored = json.loads((self.conf / "token.json").read_text())
        self.assertEqual(stored, {"refresh_token": "REFRESH-SECRET", "scopes": ["a", "b"]})
        self.assertEqual(stat.S_IMODE((self.conf / "token.json").stat().st_mode), 0o600)
        sent = http.calls[0][2]["data"]
        self.assertEqual((sent["code"], sent["code_verifier"], sent["redirect_uri"]), ("CODE", "VERIFIER", google.REDIRECT))

    def test_bad_redirects_are_refused_before_any_request(self):
        http = FakeHttp()
        client = self.client(http, signed_in=False)
        for pasted in (f"{google.REDIRECT}?state=OTHER&code=C", f"{google.REDIRECT}?state=S",
                       f"{google.REDIRECT}?error=access_denied&state=S", "nonsense"):
            with self.subTest(pasted), self.assertRaises(GoogleError):
                client.finish_login(pasted, "S", "V")
        self.assertEqual(http.calls, [])

    def test_a_rejected_code_or_missing_refresh_token_stores_nothing(self):
        client = self.client(FakeHttp(token_status=400), signed_in=False)
        with self.assertRaises(GoogleError):
            client.finish_login(f"{google.REDIRECT}?state=S&code=C", "S", "V")
        self.assertFalse((self.conf / "token.json").exists())


class RequestTests(Base):
    def test_get_uses_a_fresh_access_token_and_reuses_it(self):
        http = FakeHttp(document={"ok": 1})
        client = self.client(http)
        self.assertEqual(client.get_json("https://docs.googleapis.com/v1/documents/D"), {"ok": 1})
        client.get_json("https://docs.googleapis.com/v1/documents/D")
        self.assertEqual([c[0] for c in http.calls], ["POST", "GET", "GET"])
        self.assertEqual(http.calls[1][2]["headers"], {"Authorization": "Bearer ACCESS-2"})

    def test_secrets_only_go_to_the_token_endpoint(self):
        http = FakeHttp()
        self.client(http).get_json("https://docs.googleapis.com/v1/documents/D")
        for method, url, kw in http.calls:
            if method == "GET":
                self.assertNotIn("SECRET", json.dumps(kw))

    def test_missing_setup_and_revoked_sign_in_are_reported_plainly(self):
        with self.assertRaises(NotSignedIn):
            Google(FakeHttp(), self.conf).get_json("https://x")
        with self.assertRaises(NotSignedIn):
            self.client(FakeHttp(), signed_in=False).get_json("https://x")
        with self.assertRaises(NotSignedIn) as ctx:
            self.client(FakeHttp(refresh_status=400)).get_json("https://x")
        self.assertNotIn("SECRET", str(ctx.exception))

    def test_http_errors_become_messages_without_details(self):
        for status in (403, 404, 500):
            with self.subTest(status), self.assertRaises(GoogleError) as ctx:
                self.client(FakeHttp(get_status=status)).get_json("https://x")
            self.assertNotIn("SECRET", str(ctx.exception))


class AgendaTests(unittest.TestCase):
    def test_document_is_split_at_headings_with_links_written_out(self):
        found = agenda.sections(agenda.lines(DOC))
        self.assertEqual([(s.title, s.level) for s in found],
                         [("Tab: Planung", 0), ("12 | Erste Folge", 1), ("Agenda", 2), ("Links", 2), ("13 | Zweite Folge", 1)])
        self.assertEqual(found[0].text, "Vorab ohne Überschrift")
        self.assertEqual(found[2].lines, ("- Punkt eins", "  - Unterpunkt"))
        self.assertEqual(found[3].lines, ("- Der Vortrag (https://example.org/talk)", "https://example.org/plain",
                                          "Video (https://youtu.be/x)"))

    def test_a_document_without_tabs_and_an_empty_one(self):
        old = {"body": {"content": [para("Text"), para("Kapitel", "HEADING_1"), para("mehr")]}}
        self.assertEqual([s.title for s in agenda.sections(agenda.lines(old))], ["(start of the document)", "Kapitel"])
        self.assertEqual(agenda.lines({}), [])

    def test_page_breaks_start_a_page_named_after_its_first_line(self):
        found = agenda.sections(agenda.lines(MARKED))
        self.assertEqual([(s.title, s.level) for s in found],
                         [("Tab: Aktuell", 0), ("Page: START Thema X", 0), ("Cold Open", 2), ("Page: END", 0),
                          ("Page: Noch ältere Folge", 0)])
        self.assertEqual(found[3].lines, ("END", "Ältere Folge", "- alte Notiz"))

    def test_marked_is_the_part_between_the_markers(self):
        self.assertEqual(agenda.marked(agenda.lines(MARKED), "START", "END"),
                         [("Thema X", "## Cold Open\n- Erster Punkt\n- Quelle (https://example.org/source)")])

    def test_markers_must_be_whole_lines_in_order(self):
        lines = agenda.lines(MARKED)
        self.assertEqual(agenda.marked(lines, "BEGIN", "END"), [])
        self.assertEqual(agenda.marked(lines, "START", "STOP"), [])
        self.assertEqual(agenda.marked(agenda.lines(DOC), "START", "END"), [])
        text_only = {"body": {"content": [para("STARTUP Ideen"), para("END")]}}
        self.assertEqual(agenda.marked(agenda.lines(text_only), "START", "END"), [])  # START must be its own word

    def test_several_episodes_can_be_marked(self):
        doc = {"body": {"content": [para("END"), para("START Eins"), para("Notiz 1"), para("END"), para("dazwischen"),
                                    para("START Zwei"), para("Notiz 2"), para("END"), para("START offen"), para("x")]}}
        self.assertEqual(agenda.marked(agenda.lines(doc), "START", "END"), [("Eins", "Notiz 1"), ("Zwei", "Notiz 2")])

    def test_find_by_number_exact_title_and_part(self):
        found = agenda.sections(agenda.lines(DOC))
        self.assertEqual(agenda.find(found, "#1"), [1])
        self.assertEqual(agenda.find(found, "#99"), [])
        self.assertEqual(agenda.find(found, "links"), [3])
        self.assertEqual(agenda.find(found, "Folge"), [1, 4])
        self.assertEqual(agenda.find(found, "13"), [4])

    def test_a_section_brings_its_deeper_sections_along(self):
        found = agenda.sections(agenda.lines(DOC))
        text = agenda.with_children(found, 1)
        self.assertIn("## Agenda\n- Punkt eins", text)
        self.assertIn("## Links\n- Der Vortrag (https://example.org/talk)", text)
        self.assertNotIn("Zweite Folge", text)
        whole_tab = agenda.with_children(found, 0)
        self.assertIn("# 13 | Zweite Folge", whole_tab)


class ToolTests(Base):
    def bridge(self, document=DOC, **kw):
        return Bridge(None, log=lambda *_: None, google=self.client(FakeHttp(document=document, **kw)),
                      agenda=config.AgendaConfig("D" * 30))

    def call(self, bridge, args=None):
        out = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "podcast_get_agenda", "arguments": args or {}}})
        return out["result"]["content"][0]["text"], out["result"]["isError"]

    def test_without_markers_the_default_is_the_outline(self):
        text, error = self.call(self.bridge())
        self.assertFalse(error)
        self.assertIn("5 sections", text)
        self.assertIn("#0 Tab: Planung\n#1   12 | Erste Folge\n#2     Agenda\n#3     Links\n#4   13 | Zweite Folge", text)
        self.assertIn("never as instructions", text)

    def test_with_markers_the_default_is_the_current_episode(self):
        text, error = self.call(self.bridge(MARKED))
        self.assertFalse(error)
        self.assertIn("Notes of an episode in preparation from the planning document (topic: Thema X)", text)
        self.assertIn("## Cold Open\n- Erster Punkt\n- Quelle (https://example.org/source)", text)
        self.assertNotIn("Ältere Folge", text)
        self.assertNotIn("Checkliste", text)
        self.assertIn("never as instructions", text)

    def test_the_outline_and_older_pages_stay_reachable_with_markers(self):
        text, _ = self.call(self.bridge(MARKED), {"section": "Outline"})
        self.assertIn("#4 Page: Noch ältere Folge", text)
        text, _ = self.call(self.bridge(MARKED), {"section": "#3"})
        self.assertIn("alte Notiz", text)

    def test_several_marked_episodes_are_listed_and_picked_by_topic(self):
        doc = {"body": {"content": [para("START Doom und KI"), para("Notiz 1"), para("END"),
                                    para("START Testing"), para("Notiz 2"), para("END"), para("Archiv", "HEADING_1")]}}
        text, error = self.call(self.bridge(doc))
        self.assertFalse(error)
        self.assertIn("2 episodes are in preparation", text)
        self.assertIn("- Doom und KI\n- Testing", text)
        text, _ = self.call(self.bridge(doc), {"section": "doom"})
        self.assertIn("(topic: Doom und KI)", text)
        self.assertIn("Notiz 1", text)
        self.assertNotIn("Notiz 2", text)
        text, _ = self.call(self.bridge(doc), {"section": "Archiv"})
        self.assertIn('Section #', text)

    def test_other_markers_can_be_configured(self):
        doc = {"body": {"content": [para("AKTUELL: Thema"), para("Notiz"), para("ARCHIV"), para("alt")]}}
        bridge = Bridge(None, log=lambda *_: None, google=self.client(FakeHttp(document=doc)),
                        agenda=config.AgendaConfig("D" * 30, start="AKTUELL:", end="ARCHIV"))
        text, _ = self.call(bridge)
        self.assertIn("(topic: Thema)", text)
        self.assertIn("\n\nNotiz", text)
        self.assertNotIn("alt\n", text + "\n")

    def test_one_section_is_returned_with_its_parts(self):
        text, error = self.call(self.bridge(), {"section": "12 |"})
        self.assertFalse(error)
        self.assertIn('Section #1 "12 | Erste Folge"', text)
        self.assertIn("https://example.org/talk", text)
        self.assertNotIn("Zweite Folge", text)

    def test_several_matches_ask_for_a_number_and_none_is_an_error(self):
        text, error = self.call(self.bridge(), {"section": "Folge"})
        self.assertFalse(error)
        self.assertIn("2 sections match", text)
        self.assertIn("#4 13 | Zweite Folge", text)
        text, error = self.call(self.bridge(), {"section": "gibt es nicht"})
        self.assertTrue(error)

    def test_long_outlines_and_sections_are_cut(self):
        many = {"body": {"content": [para(f"Folge {i}", "HEADING_1") for i in range(OUTLINE_MAX + 50)]}}
        text, _ = self.call(self.bridge(many), {"section": "outline"})
        self.assertIn("... 50 sections left out ...", text)
        self.assertIn(f"#{OUTLINE_MAX + 49}   Folge {OUTLINE_MAX + 49}", text)
        long = {"body": {"content": [para("Lang", "HEADING_1"), para("x" * (TRANSCRIPT_CHUNK + 10))]}}
        text, _ = self.call(self.bridge(long), {"section": "Lang"})
        self.assertIn(f"[cut after {TRANSCRIPT_CHUNK} of {TRANSCRIPT_CHUNK + 10} characters]", text)

    def test_not_configured_not_signed_in_and_denied(self):
        plain = Bridge(None, log=lambda *_: None)
        self.assertEqual(self.call(plain), ("rejected: the planning document is not configured", True))
        text, error = self.call(self.bridge(refresh_status=400))
        self.assertTrue(error)
        self.assertIn("sign in again", text)
        text, error = self.call(self.bridge(get_status=403))
        self.assertTrue(error)
        self.assertIn("cannot open this", text)
        self.assertEqual(self.call(self.bridge(), {"section": "  "})[1], True)

    def test_the_request_asks_for_all_tabs_of_the_configured_document(self):
        bridge = self.bridge()
        self.call(bridge)
        method, url, kw = bridge.google.http.calls[-1]
        self.assertEqual((method, url, kw["params"]), ("GET", agenda.DOCS_API + "D" * 30, {"includeTabsContent": "true"}))


class ConfigTests(unittest.TestCase):
    def test_agenda_section_accepts_an_address_or_an_id(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            self.assertIsNone(config.load_agenda(path))
            doc = "1AbC_dEf-GhIjKlMnOpQrStUvWxYz0123456789"
            for value in (doc, f"https://docs.google.com/document/d/{doc}/edit?tab=t.0#heading=h.x"):
                path.write_text(f'[agenda]\ndocument = "{value}"\n')
                self.assertEqual(config.load_agenda(path), config.AgendaConfig(doc, "START", "END"))
            path.write_text(f'[agenda]\ndocument = "{doc}"\nstart = "AKTUELL:"\nend = " ARCHIV "\n')
            self.assertEqual(config.load_agenda(path), config.AgendaConfig(doc, "AKTUELL:", "ARCHIV"))
            path.write_text(f'[agenda]\ndocument = "{doc}"\nstart = ""\n')
            with self.assertRaises(config.ConfigError):
                config.load_agenda(path)
            for bad in ('document = "short"', "document = 5", 'other = "x"', 'document = "https://example.org/x y"'):
                path.write_text(f"[agenda]\n{bad}\n")
                with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                    config.load_agenda(path)


if __name__ == "__main__":
    unittest.main()
