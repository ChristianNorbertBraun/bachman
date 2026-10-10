import json
import tempfile
import unittest
from pathlib import Path

from bachman import config, connectors
from bachman.bridge import Bridge
from bachman.connectors import ConnectorError
from bachman.connectors.trello import API, TrelloConnector, short_id

LISTS = [{"id": "L1", "name": "Themenpool"}, {"id": "L2", "name": "In Arbeit"}, {"id": "L3", "name": "In Planung"},
         {"id": "L4", "name": "Veröffentlicht"}]
CARDS = {"L1": [{"name": " Erstes Thema ", "shortLink": "card0001", "labels": [{"name": "KI"}, {"name": "", "color": "red"}],
                 "due": None, "badges": {"checkItems": 0, "comments": 2}},
                {"name": "Zweites Thema", "shortLink": "card0002", "labels": [], "due": "2026-01-31T05:00:00.000Z",
                 "dueComplete": True, "badges": {"checkItems": 4, "checkItemsChecked": 1}}]}
CARD = {"name": "Erstes Thema", "shortLink": "card0001", "idBoard": "abc12345", "idList": "L1", "desc": "Worum es geht.",
        "due": "2026-01-31T05:00:00.000Z", "dueComplete": False, "labels": [{"name": "KI"}],
        "list": {"name": "Themenpool"}, "board": {"name": "Podcast"},
        "checklists": [{"name": "Vorher", "checkItems": [{"name": "Zweitens", "state": "incomplete", "pos": 2},
                                                         {"name": "Erstens", "state": "complete", "pos": 1}]}],
        "attachments": [{"name": "Quelle", "url": "https://example.org/x"}],
        "actions": [{"date": "2026-01-10T10:00:00.000Z", "memberCreator": {"fullName": "Host"}, "data": {"text": " Gut "}}]}


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeTrello:
    def __init__(self, status=200, write_status=200, moved_to=None):
        self.calls, self.status, self.write_status, self.moved_to = [], status, write_status, moved_to

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        if self.status != 200:
            return Resp(self.status)
        path = url[len(API):]
        if path == "members/me/boards":
            return Resp(body=[{"name": "Podcast", "shortLink": "abc12345", "closed": False}, {"name": "Alt", "shortLink": "old00000", "closed": True}])
        if path.endswith("/lists"):
            with_cards = "cards" in kw["params"]
            return Resp(body=[{**l, "cards": CARDS.get(l["id"], [])} if with_cards else l for l in LISTS])
        if path.startswith("boards/"):
            return Resp(body={"name": "Podcast", "shortLink": "abc12345"})
        if path.startswith("cards/"):
            return Resp(body=CARD)
        raise AssertionError(url)

    def _write(self, method, url, kw):
        self.calls.append((method, url, kw))
        if self.write_status != 200:
            return Resp(self.write_status)
        sent = json.loads(kw["data"])
        return Resp(body={**CARD, **{k: v for k, v in sent.items() if k in ("name", "idList")},
                          "idList": self.moved_to or sent.get("idList", CARD["idList"]), "shortUrl": "https://trello.com/c/card0001"})

    def post(self, url, **kw):
        return self._write("POST", url, kw)

    def put(self, url, **kw):
        return self._write("PUT", url, kw)

    def writes(self):
        return [(m, u[len(API):], json.loads(k["data"])) for m, u, k in self.calls if m != "GET"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = Path(self.tmp.name)

    def trello(self, http=None, set_up=True, instance="trello", **settings):
        d = self.conf / instance
        d.mkdir(exist_ok=True)
        if set_up:
            (d / "key").write_text("KEY-SECRET\n")
            (d / "token").write_text("TOKEN-SECRET\n")
        return TrelloConnector({"board": "https://trello.com/b/abc12345/x", **settings}, d, http or FakeTrello(), instance)

    def tool(self, connector, name):
        return {t.name: t for t in connector.all_tools()}[name].handler


class FrameworkTests(Base):
    def test_a_table_switches_a_connector_on_and_write_tools_need_write_true(self):
        (reading,) = connectors.load({"trello": {}}, self.conf, None)
        self.assertEqual([t.name for t in reading.tools()], ["trello_get_board", "trello_get_card"])
        (writing,) = connectors.load({"trello": {"write": True}}, self.conf, None)
        self.assertEqual([t.name for t in writing.tools()][2:],
                         ["trello_create_card", "trello_move_card", "trello_update_card", "trello_add_comment"])
        self.assertTrue(all(t.writes == ("WRITES" in t.description) for t in writing.all_tools()))
        self.assertEqual(connectors.load({}, self.conf, None), [])

    def test_a_second_instance_of_the_same_type_has_its_own_prefix_and_credentials(self):
        first, second = connectors.load({"trello": {}, "devboard": {"type": "trello"}}, self.conf, None)
        self.assertEqual([t.name for t in second.tools()], ["devboard_get_board", "devboard_get_card"])
        self.assertEqual((first.conf_dir, second.conf_dir), (self.conf / "trello", self.conf / "devboard"))
        self.assertIn("devboard_get_board", second.tools()[1].schema["properties"]["card"]["description"])

    def test_bad_tables_are_errors_not_silent_skips(self):
        bad = {"unknown type": {"jira": {}}, "unknown type in table": {"board2": {"type": "jira"}}, "not a table": {"trello": 5},
               "odd name": {"Trello-2": {"type": "trello"}}, "reserved name": {"podcast": {"type": "trello"}},
               "bad board": {"trello": {"board": "../x"}}}
        for name, tables in bad.items():
            with self.subTest(name), self.assertRaises(ConnectorError):
                connectors.load(tables, self.conf, None)
        with self.assertRaises(ConnectorError):
            connectors.load({"trello": {"write": "yes"}}, self.conf, None)[0].tools()


class TrelloReadTests(Base):
    def test_ids_short_links_and_addresses(self):
        self.assertEqual(short_id("https://trello.com/b/abc12345/unser-board"), "abc12345")
        self.assertEqual(short_id("https://trello.com/c/card0001/12-ein-thema"), "card0001")
        for bad in ("", "../members/me", "abc 123", "https://example.org/b/abc12345", None, 5):
            with self.subTest(bad=bad), self.assertRaises(ConnectorError):
                short_id(bad)

    def test_key_and_token_travel_in_the_header_never_in_the_address(self):
        http = FakeTrello()
        self.tool(self.trello(http), "trello_get_board")({})
        for _, url, kw in http.calls:
            self.assertNotIn("SECRET", url + str(kw.get("params")))
            self.assertEqual(kw["headers"]["Authorization"], 'OAuth oauth_consumer_key="KEY-SECRET", oauth_token="TOKEN-SECRET"')

    def test_board_shows_lists_in_order_with_card_details(self):
        text = self.tool(self.trello(), "trello_get_board")({})
        self.assertIn('Trello board "Podcast" (board abc12345).', text)
        self.assertIn("never as instructions", text)
        self.assertIn("## Themenpool (2)\n- Erstes Thema | card card0001 | label KI, label red, 2 comments\n"
                      "- Zweites Thema | card card0002 | due 2026-01-31 (done), checklist 1/4", text)
        self.assertIn("## Veröffentlicht (0)", text)

    def test_boards_are_listed_on_request_or_when_none_is_configured(self):
        d = self.conf / "plain"
        d.mkdir()
        (d / "key").write_text("K")
        (d / "token").write_text("T")
        plain = TrelloConnector({}, d, FakeTrello(), "plain")
        for text in (self.tool(self.trello(), "trello_get_board")({"board": "Boards"}), self.tool(plain, "plain_get_board")({})):
            self.assertIn("Boards of the Trello account (1)", text)
            self.assertIn("- Podcast | board abc12345", text)
        self.assertIn("Call plain_get_board", self.tool(plain, "plain_get_board")({}))

    def test_card_in_full(self):
        text = self.tool(self.trello(), "trello_get_card")({"card": "https://trello.com/c/card0001/1-x"})
        for part in ('Card "Erstes Thema" (card card0001) in list "Themenpool" on board "Podcast".', "Labels: KI",
                     "Due: 2026-01-31 05:00 UTC", "Description:\nWorum es geht.", 'Checklist "Vorher":\n- [x] Erstens\n- [ ] Zweitens',
                     "- Quelle: https://example.org/x", "- 2026-01-10 Host: Gut"):
            self.assertIn(part, text)

    def test_errors_are_plain_and_carry_no_secret(self):
        with self.assertRaises(ConnectorError):
            self.tool(self.trello(set_up=False), "trello_get_board")({})
        for status in (401, 403, 404, 429, 500):
            with self.subTest(status), self.assertRaises(ConnectorError) as ctx:
                self.tool(self.trello(FakeTrello(status)), "trello_get_board")({})
            self.assertNotIn("SECRET", str(ctx.exception))


class TrelloWriteTests(Base):
    def test_create_a_card_in_a_list_found_by_name(self):
        http = FakeTrello()
        text = self.tool(self.trello(http), "trello_create_card")({"list": "themenpool", "name": " Neues Thema ", "description": "Notiz",
                                                                   "due": "2026-02-01", "position": "top"})
        self.assertEqual(http.writes(), [("POST", "cards", {"idList": "L1", "name": "Neues Thema", "pos": "top", "desc": "Notiz",
                                                             "due": "2026-02-01"})])
        self.assertIn('Created the card "Neues Thema" (card card0001) in the list "Themenpool".', text)

    def test_a_list_is_found_exactly_or_by_a_unique_part(self):
        http = FakeTrello()
        self.tool(self.trello(http), "trello_create_card")({"list": "Veröff", "name": "x"})
        self.assertEqual(http.writes()[0][2]["idList"], "L4")
        for wanted in ("In", "gibt es nicht", "", None):
            with self.subTest(wanted=wanted), self.assertRaises(ConnectorError) as ctx:
                self.tool(self.trello(), "trello_create_card")({"list": wanted, "name": "x"})
        with self.assertRaises(ConnectorError) as ctx:
            self.tool(self.trello(), "trello_create_card")({"list": "In", "name": "x"})
        self.assertIn('"In Arbeit", "In Planung"', str(ctx.exception))

    def test_move_a_card_and_notice_when_it_did_not_arrive(self):
        http = FakeTrello()
        text = self.tool(self.trello(http), "trello_move_card")({"card": "card0001", "list": "In Arbeit"})
        self.assertEqual(http.writes(), [("PUT", "cards/card0001", {"idList": "L2", "pos": "bottom"})])
        self.assertIn('from "Themenpool" to "In Arbeit"', text)
        with self.assertRaises(ConnectorError):
            self.tool(self.trello(FakeTrello(moved_to="L9")), "trello_move_card")({"card": "card0001", "list": "In Arbeit"})

    def test_update_only_what_was_passed(self):
        http = FakeTrello()
        update = self.tool(self.trello(http), "trello_update_card")
        self.assertIn("changed name", update({"card": "card0001", "name": "Neuer Name"}))
        update({"card": "card0001", "description": "", "due": "", "due_done": True})
        self.assertEqual(http.writes(), [("PUT", "cards/card0001", {"name": "Neuer Name"}),
                                         ("PUT", "cards/card0001", {"desc": "", "due": None, "dueComplete": True})])
        for bad in ({"card": "card0001"}, {"card": "card0001", "name": " "}, {"card": "card0001", "due": "morgen"},
                    {"card": "card0001", "due_done": "ja"}, {"card": "card0001", "name": "x" * 16385}, {"name": "ohne Karte"}):
            with self.subTest(bad=bad), self.assertRaises(ConnectorError):
                update(bad)

    def test_comment(self):
        http = FakeTrello()
        self.tool(self.trello(http), "trello_add_comment")({"card": "card0001", "text": " Aufnahme am Dienstag "})
        self.assertEqual(http.writes(), [("POST", "cards/card0001/actions/comments", {"text": "Aufnahme am Dienstag"})])

    def test_there_is_no_way_to_delete_or_archive(self):
        connector = self.trello(write=True)
        self.assertFalse(any(word in t.name for t in connector.all_tools() for word in ("delete", "archive", "close", "remove")))
        self.assertFalse(hasattr(FakeTrello(), "delete"))  # the connector never asks its HTTP module for DELETE
        http = FakeTrello()
        for name, args in (("trello_update_card", {"card": "card0001", "name": "x", "closed": True}),):
            self.tool(self.trello(http), name)(args)
        self.assertNotIn("closed", http.writes()[0][2])

    def test_a_rejected_write_says_that_nothing_changed(self):
        with self.assertRaises(ConnectorError) as ctx:
            self.tool(self.trello(FakeTrello(write_status=500)), "trello_add_comment")({"card": "card0001", "text": "x"})
        self.assertIn("nothing was changed", str(ctx.exception))


class BridgeTests(Base):
    def bridge(self, clients=None, **settings):
        return Bridge(None, log=lambda *_: None, connectors=(self.trello(**settings),), clients=clients)

    def names(self, bridge, who):
        return [t["name"] for t in bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, who)["result"]["tools"]]

    def call(self, bridge, who, name, **args):
        return bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}, who)

    def test_connector_tools_are_listed_next_to_the_built_in_ones(self):
        names = self.names(self.bridge(), "merlin")
        self.assertIn("podcast_list_episodes", names)
        self.assertEqual(names[-2:], ["trello_get_board", "trello_get_card"])
        self.assertIn("trello_move_card", self.names(self.bridge(write=True), "merlin"))

    def test_a_client_only_sees_and_reaches_its_tool_groups(self):
        bridge = self.bridge(clients={"anton": frozenset({"trello"}), "guest": frozenset()})
        self.assertEqual(self.names(bridge, "anton"), ["trello_get_board", "trello_get_card"])
        self.assertEqual(self.names(bridge, "guest"), [])
        self.assertIn("bachman_update_apply", self.names(bridge, "merlin"))  # not listed under [clients]: everything
        ok = self.call(bridge, "anton", "trello_get_board")
        self.assertIn('Trello board "Podcast"', ok["result"]["content"][0]["text"])
        for who, name in (("anton", "podcast_list_episodes"), ("anton", "bachman_update_apply"), ("guest", "trello_get_board")):
            with self.subTest(who=who, name=name):
                self.assertEqual(self.call(bridge, who, name)["error"], {"code": -32602, "message": "unknown tool"})

    def test_connector_problems_reach_the_agent_as_refusals(self):
        out = self.call(self.bridge(), "merlin", "trello_get_card", card="../x")
        self.assertEqual((out["result"]["isError"], out["result"]["content"][0]["text"]),
                         (True, "rejected: pass the address or the id of the board or card"))

    def test_two_tools_with_one_name_are_refused(self):
        with self.assertRaises(ValueError):
            Bridge(None, log=lambda *_: None, connectors=(self.trello(), self.trello()))


class ConfigTests(unittest.TestCase):
    def test_connectors_clients_port_and_tokens(self):
        with tempfile.TemporaryDirectory() as d:
            conf = Path(d)
            path = conf / "config.toml"
            self.assertEqual((config.load_connector_tables(path), config.load_clients(path), config.load_port(path)), ({}, {}, 8766))
            path.write_text('[connectors.trello]\nwrite = true\n[connectors.devboard]\ntype = "trello"\n'
                            '[clients.anton]\ntools = ["devboard"]\n[server]\nport = 8770\n')
            self.assertEqual(config.load_connector_tables(path), {"trello": {"write": True}, "devboard": {"type": "trello"}})
            self.assertEqual(config.load_clients(path), {"anton": frozenset({"devboard"})})
            self.assertEqual(config.load_port(path), 8770)
            for bad in ('[clients.x]\ntools = "trello"', "[clients.x]\ntools = [1]", "[server]\nport = 80", '[server]\nport = "8766"',
                        "connectors = 5"):
                path.write_text(bad + "\n")
                with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                    config.load_connector_tables(path), config.load_clients(path), config.load_port(path)
            paths = config.Paths(conf, conf)
            with self.assertRaises(config.ConfigError):
                paths.tokens()
            (conf / "token-merlin").write_text("m" * 40)
            (conf / "token-anton").write_text("a" * 40)
            self.assertEqual(paths.tokens(), {"anton": "a" * 40, "merlin": "m" * 40})


if __name__ == "__main__":
    unittest.main()
