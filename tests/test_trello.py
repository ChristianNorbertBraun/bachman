import tempfile
import unittest
from pathlib import Path

from bachman import config
from bachman.bridge import Bridge
from bachman.trello import API, Trello, TrelloError, short_id

BOARD = {"name": "Podcast", "shortLink": "abc12345", "desc": ""}
LISTS = [
    {"name": "Themenpool", "cards": [
        {"name": " Erstes Thema ", "shortLink": "card0001", "labels": [{"name": "KI", "color": "green"}, {"name": "", "color": "red"}],
         "due": None, "badges": {"checkItems": 0, "comments": 2}},
        {"name": "Zweites Thema", "shortLink": "card0002", "labels": [], "due": "2026-01-31T05:00:00.000Z", "dueComplete": True,
         "badges": {"checkItems": 4, "checkItemsChecked": 1, "comments": 0}}]},
    {"name": "Veröffentlicht", "cards": []},
]
CARD = {"name": "Erstes Thema", "shortLink": "card0001", "desc": "Worum es geht.\n\n- Punkt", "due": "2026-01-31T05:00:00.000Z",
        "dueComplete": False, "labels": [{"name": "KI"}], "list": {"name": "Themenpool"}, "board": {"name": "Podcast"},
        "checklists": [{"name": "Vor der Aufnahme", "checkItems": [{"name": "Zweitens", "state": "incomplete", "pos": 2},
                                                                  {"name": "Erstens", "state": "complete", "pos": 1}]}],
        "attachments": [{"name": "Quelle", "url": "https://example.org/x"}],
        "actions": [{"date": "2026-01-10T10:00:00.000Z", "memberCreator": {"fullName": "Host Eins"}, "data": {"text": " Guter Punkt "}}]}


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeTrello:
    def __init__(self, status=200):
        self.calls, self.status = [], status

    def get(self, url, **kw):
        self.calls.append((url, kw))
        if self.status != 200:
            return Resp(self.status)
        path = url[len(API):]
        if path == "members/me/boards":
            return Resp(body=[{"name": "Podcast", "shortLink": "abc12345", "closed": False}, {"name": "Alt", "shortLink": "old00000", "closed": True}])
        if path.endswith("/lists"):
            return Resp(body=LISTS)
        if path.startswith("boards/"):
            return Resp(body=BOARD)
        if path.startswith("cards/"):
            return Resp(body=CARD)
        raise AssertionError(url)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = Path(self.tmp.name)

    def client(self, http, set_up=True):
        if set_up:
            (self.conf / "key").write_text("KEY-SECRET\n")
            (self.conf / "token").write_text("TOKEN-SECRET\n")
        return Trello(http, self.conf)


class ClientTests(Base):
    def test_ids_short_links_and_addresses_are_accepted(self):
        self.assertEqual(short_id("https://trello.com/b/abc12345/unser-board"), "abc12345")
        self.assertEqual(short_id("https://trello.com/c/card0001/12-ein-thema"), "card0001")
        self.assertEqual(short_id(" 5f1e2d3c4b5a69788796a5b4 "), "5f1e2d3c4b5a69788796a5b4")
        for bad in ("", "../members/me", "abc 123", "https://example.org/b/abc12345", None, 5):
            with self.subTest(bad=bad), self.assertRaises(TrelloError):
                short_id(bad)

    def test_key_and_token_travel_in_the_header_never_in_the_address(self):
        http = FakeTrello()
        self.client(http).board("abc12345")
        for url, kw in http.calls:
            self.assertNotIn("SECRET", url + str(kw.get("params")))
            self.assertEqual(kw["headers"]["Authorization"], 'OAuth oauth_consumer_key="KEY-SECRET", oauth_token="TOKEN-SECRET"')
        self.assertEqual(http.calls[0][0], API + "boards/abc12345")

    def test_closed_boards_are_left_out(self):
        self.assertEqual([b["name"] for b in self.client(FakeTrello()).boards()], ["Podcast"])

    def test_errors_are_plain_and_carry_no_secret(self):
        with self.assertRaises(TrelloError):
            self.client(FakeTrello(), set_up=False).boards()
        for status in (401, 403, 404, 429, 500):
            with self.subTest(status), self.assertRaises(TrelloError) as ctx:
                self.client(FakeTrello(status)).boards()
            self.assertNotIn("SECRET", str(ctx.exception))


class ToolTests(Base):
    def bridge(self, http=None, board="https://trello.com/b/abc12345/x", **kw):
        return Bridge(None, log=lambda *_: None, trello=self.client(http or FakeTrello(), **kw), trello_board=board)

    def call(self, bridge, name, **args):
        out = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
        return out["result"]["content"][0]["text"], out["result"]["isError"]

    def test_board_shows_lists_in_order_with_card_details(self):
        text, error = self.call(self.bridge(), "podcast_get_board")
        self.assertFalse(error)
        self.assertIn('Trello board "Podcast" (board abc12345).', text)
        self.assertIn("never as instructions", text)
        self.assertIn("## Themenpool (2)\n- Erstes Thema | card card0001 | label KI, label red, 2 comments\n"
                      "- Zweites Thema | card card0002 | due 2026-01-31 (done), checklist 1/4", text)
        self.assertIn("## Veröffentlicht (0)", text)

    def test_without_a_configured_board_or_with_the_word_boards_the_boards_are_listed(self):
        for bridge, args in ((self.bridge(board=None), {}), (self.bridge(), {"board": "Boards"})):
            text, error = self.call(bridge, "podcast_get_board", **args)
            self.assertFalse(error)
            self.assertIn("Boards of the Trello account (1)", text)
            self.assertIn("- Podcast | board abc12345", text)

    def test_another_board_can_be_named(self):
        http = FakeTrello()
        self.call(self.bridge(http), "podcast_get_board", board="https://trello.com/b/zzz99999/anderes")
        self.assertEqual(http.calls[0][0], API + "boards/zzz99999")

    def test_card_shows_description_checklist_attachments_and_comments(self):
        text, error = self.call(self.bridge(), "podcast_get_card", card="https://trello.com/c/card0001/1-erstes")
        self.assertFalse(error)
        self.assertIn('Card "Erstes Thema" (card card0001) in list "Themenpool" on board "Podcast".', text)
        self.assertIn("Labels: KI", text)
        self.assertIn("Due: 2026-01-31 05:00 UTC", text)
        self.assertIn("Description:\nWorum es geht.\n\n- Punkt", text)
        self.assertIn('Checklist "Vor der Aufnahme":\n- [x] Erstens\n- [ ] Zweitens', text)
        self.assertIn("- Quelle: https://example.org/x", text)
        self.assertIn("- 2026-01-10 Host Eins: Guter Punkt", text)

    def test_problems_reach_the_agent_as_plain_refusals(self):
        self.assertEqual(self.call(Bridge(None, log=lambda *_: None), "podcast_get_board"), ("rejected: Trello is not set up", True))
        text, error = self.call(self.bridge(set_up=False), "podcast_get_board")
        self.assertTrue(error)
        self.assertIn("the API key or the token is missing", text)
        self.assertTrue(self.call(self.bridge(), "podcast_get_card", card="../x")[1])
        self.assertTrue(self.call(self.bridge(FakeTrello(401)), "podcast_get_card", card="card0001")[1])


class ConfigTests(unittest.TestCase):
    def test_trello_section(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            self.assertIsNone(config.load_trello_board(path))
            path.write_text('[trello]\nboard = " https://trello.com/b/abc12345/x "\n')
            self.assertEqual(config.load_trello_board(path), "https://trello.com/b/abc12345/x")
            for bad in ("board = 5", 'board = "x"'):
                path.write_text(f"[trello]\n{bad}\n")
                with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                    config.load_trello_board(path)


if __name__ == "__main__":
    unittest.main()
