import gzip
import http.client
import json
import tempfile
import unittest
from pathlib import Path

from bachman import ops
from bachman.bridge import TRANSCRIPT_CHUNK, Bridge, serve
from bachman.spotify import GRAPHQL, LoginExpired, Spotify, SpotifyError

H1, H2, H3 = "a" * 64, "b" * 64, "c" * 64


def op_js(name, kind, digest):
    return ('x={__meta__:{hash:"%s"},kind:"Document",definitions:[{kind:"OperationDefinition",'
            'operation:"%s",name:{kind:"Name",value:"%s"},variableDefinitions:[]}]};' % (digest, kind, name))


BUNDLE = (op_js("WebGetIndexedEpisodeList", "query", H1) + op_js("getEpisodeTranscript", "query", H2)
          + op_js("getEpisodeTranscriptAvailability", "query", H2) + op_js("updateEpisodeChapters", "mutation", H3))


class Resp:
    def __init__(self, status=200, body=None, text="", content=None):
        self.status_code, self._body, self.text = status, body, text
        self.content = content if content is not None else text.encode()

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def episode(eid, title, published, minutes=50.0):
    return {"episodeId": eid, "uri": f"spotify:episode:u{eid}", "title": title,
            "publishedOn": {"seconds": published} if published else None, "createdOn": {"seconds": 1790960609},
            "contentType": "EPISODE_CONTENT_TYPE_VIDEO", "asset": {"lengthMs": int(minutes * 60000)}}


class FakeHttp:
    """Stands in for the `requests` module and records every call."""

    def __init__(self, transcript="Hallo Welt. " * 10, login_ok=True, graph=None):
        self.calls, self.transcript, self.login_ok, self.graph = [], transcript, login_ok, graph
        self.items = [episode(3, "", None), episode(2, "136 | A title with a number", 1790380860),
                      episode(1, "135 | The one before", 1789257060)]

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        if "oauth2/v2/auth" in url:
            if not self.login_ok:
                return Resp(text='{"error": "login_required"}')
            return Resp(text="const authorizationResponse = {type: 'authorization_response', response: "
                             "{code: 'CODE', state: '%s'}};" % kw["params"]["state"])
        if url == ops.APP_URL:
            return Resp(text='<script src="https://cdn.example.net/builds/bundle-00ff.js"></script>')
        if url.endswith("bundle-00ff.js"):
            return Resp(content=gzip.compress(BUNDLE.encode()))
        raise AssertionError(url)

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if "api/token" in url:
            return Resp(body={"access_token": "BEARER-SECRET", "expires_in": 3600})
        assert url == GRAPHQL, url
        if self.graph:
            return self.graph(kw["json"])
        op = kw["json"]["operationName"]
        if op == "WebGetIndexedEpisodeList":
            return Resp(body={"data": {"showByShowUri": {"episodesV2": {"items": self.items}}},
                              "errors": [{"message": "An error occurred while processing the request."}]})
        node = ({"transcriptTextLength": len(self.transcript)} if op.endswith("Availability")
                else {"transcriptText": self.transcript})
        return Resp(body={"data": {"episodeByUri": {"transcript": {"transcript": node}}}})

    def graph_calls(self):
        return [c for c in self.calls if c[1] == GRAPHQL]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.conf = root / "spotify"
        self.conf.mkdir()
        for name, value in (("sp_dc", "DC-SECRET"), ("sp_key", "KEY-SECRET"), ("show_id", "SHOW")):
            (self.conf / name).write_text(value + "\n")
        self.ops_path = root / "state/ops.json"

    def client(self, http, with_ops=True):
        if with_ops:
            self.ops_path.parent.mkdir(parents=True, exist_ok=True)
            self.ops_path.write_text(json.dumps(ops.extract(BUNDLE)))
        return Spotify(http, self.conf, self.ops_path)


class OpsTest(Base):
    def test_extract_reads_name_hash_and_type(self):
        found = ops.extract(BUNDLE)
        self.assertEqual(found["WebGetIndexedEpisodeList"], {"hash": H1, "type": "query"})
        self.assertEqual(found["updateEpisodeChapters"]["type"], "mutation")

    def test_refresh_decodes_gzipped_bundle_and_writes_cache(self):
        found = ops.refresh(FakeHttp(), self.ops_path)
        self.assertEqual(len(found), 4)
        self.assertEqual(ops.load(self.ops_path), found)


class SpotifyTest(Base):
    def test_query_sends_persisted_hash_and_bearer(self):
        http = FakeHttp()
        self.client(http).episodes()
        _, _, kw = http.graph_calls()[0]
        self.assertEqual(kw["json"]["extensions"]["persistedQuery"], {"version": 1, "sha256Hash": H1})
        self.assertEqual(kw["json"]["variables"]["showUri"], "spotify:show:SHOW")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer BEARER-SECRET")

    def test_cookies_only_go_to_the_login_host(self):
        http = FakeHttp()
        self.client(http).episodes()
        for _, url, kw in http.calls:
            if "cookies" in kw:
                self.assertTrue(url.startswith("https://accounts.spotify.com/"), url)
            self.assertNotIn("DC-SECRET", json.dumps(kw.get("json", {})) + json.dumps(kw.get("headers", {})))

    def test_mutation_is_refused_without_a_request(self):
        http = FakeHttp()
        with self.assertRaises(SpotifyError):
            self.client(http).query("updateEpisodeChapters", {})
        self.assertEqual(http.graph_calls(), [])

    def test_bearer_is_reused(self):
        http = FakeHttp()
        client = self.client(http)
        client.episodes()
        client.episodes()
        self.assertEqual(sum(1 for c in http.calls if "api/token" in c[1]), 1)

    def test_field_errors_with_data_are_tolerated(self):
        episodes = self.client(FakeHttp()).episodes()
        self.assertEqual([e["id"] for e in episodes], ["3", "2", "1"])
        self.assertIsNone(episodes[0]["published"])
        self.assertTrue(episodes[0]["video"])

    def test_stale_hash_triggers_one_refresh_and_retry(self):
        state = {"n": 0}

        def graph(payload):
            state["n"] += 1
            if state["n"] == 1:
                return Resp(body={"errors": [{"message": "PersistedQueryNotFound"}]})
            return Resp(body={"data": {"showByShowUri": {"episodesV2": {"items": []}}}})

        http = FakeHttp(graph=graph)
        self.assertEqual(self.client(http).episodes(), [])
        self.assertEqual(state["n"], 2)
        self.assertTrue(any(c[1] == ops.APP_URL for c in http.calls))

    def test_missing_ops_cache_is_built_on_first_use(self):
        http = FakeHttp()
        self.assertEqual(len(self.client(http, with_ops=False).episodes()), 3)
        self.assertTrue(self.ops_path.exists())

    def test_expired_login_is_not_retried_until_cookies_change(self):
        http = FakeHttp(login_ok=False)
        client = self.client(http)
        for _ in range(3):
            with self.assertRaises(LoginExpired):
                client.episodes()
        self.assertEqual(sum(1 for c in http.calls if "oauth2/v2/auth" in c[1]), 1)
        http.login_ok = True
        import os
        later = (self.conf / "sp_dc").stat().st_mtime + 10
        os.utime(self.conf / "sp_dc", (later, later))
        self.assertEqual(len(client.episodes()), 3)

    def test_http_error_message_has_no_secrets(self):
        http = FakeHttp(graph=lambda payload: Resp(status=500, body={}))
        with self.assertRaises(SpotifyError) as ctx:
            self.client(http).episodes()
        self.assertNotIn("SECRET", str(ctx.exception))


class BridgeTest(Base):
    def bridge(self, http=None):
        self.http = http or FakeHttp()
        self.logged = []
        return Bridge(self.client(self.http), log=self.logged.append)

    def call(self, bridge, name, args=None):
        out = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": name, "arguments": args or {}}})
        return out["result"]["content"][0]["text"], out["result"]["isError"]

    def test_initialize_and_tools_list(self):
        bridge = self.bridge()
        init = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        tools = bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["podcast_list_episodes", "podcast_get_transcript", "podcast_get_agenda",
                                                      "bachman_update_check", "bachman_update_apply"])
        self.assertIsNone(bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_list_puts_drafts_first_and_derives_next_number(self):
        text, error = self.call(self.bridge(), "podcast_list_episodes")
        self.assertFalse(error)
        lines = text.splitlines()
        self.assertEqual(lines[0], "Unpublished drafts on Spotify: 1")
        self.assertIn("id 3 | title: (none yet) | video | 50.0 min | uploaded 2026-10-02 | transcript: yes, 120 characters", lines[1])
        self.assertIn("Next episode number: 137", text)
        self.assertIn("- 136 | A title with a number | published 2026-09-26", text)

    def test_list_without_transcript(self):
        text, _ = self.call(self.bridge(FakeHttp(transcript="")), "podcast_list_episodes")
        self.assertIn("transcript: not available yet", text)

    def test_transcript_is_paged(self):
        long_text = "".join(chr(97 + i % 26) for i in range(TRANSCRIPT_CHUNK + 500))
        bridge = self.bridge(FakeHttp(transcript=long_text))
        first, error = self.call(bridge, "podcast_get_transcript", {"episode_id": "3"})
        self.assertFalse(error)
        self.assertIn(f"characters 0 to {TRANSCRIPT_CHUNK} of {len(long_text)}", first)
        self.assertIn(f"again with offset {TRANSCRIPT_CHUNK}", first)
        second, _ = self.call(bridge, "podcast_get_transcript", {"episode_id": "3", "offset": TRANSCRIPT_CHUNK})
        self.assertIn(long_text[TRANSCRIPT_CHUNK:], second)
        self.assertIn("The transcript is complete.", second)

    def test_transcript_input_errors(self):
        bridge = self.bridge()
        for args in ({"episode_id": "abc"}, {"episode_id": "999"}, {"episode_id": "3", "offset": -1},
                     {"episode_id": "3", "offset": 10_000}, {"episode_id": "3", "offset": True}):
            text, error = self.call(bridge, "podcast_get_transcript", args)
            self.assertTrue(error, args)
            self.assertTrue(text.startswith("rejected: "), text)

    def test_unexpected_failure_does_not_leak_details(self):
        def boom(payload):
            raise RuntimeError("secret path /home/bachman/.config/bachman/spotify/sp_dc")

        text, error = self.call(self.bridge(FakeHttp(graph=boom)), "podcast_list_episodes")
        self.assertTrue(error)
        self.assertEqual(text, "failed: RuntimeError")
        self.assertNotIn("sp_dc", " ".join(self.logged))

    def test_unknown_tool_and_method(self):
        bridge = self.bridge()
        out = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "podcast_delete"}})
        self.assertEqual(out["error"]["code"], -32602)
        self.assertEqual(bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "nope"})["error"]["code"], -32601)


class HttpTest(Base):
    def setUp(self):
        super().setUp()
        bridge = Bridge(self.client(FakeHttp()), log=lambda *_: None)
        self.server = serve(bridge, {"merlin": "T" * 40}, 0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, method="POST", path="/mcp", token="T" * 40, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        hdrs = {"Content-Type": "application/json"}
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        hdrs.update(headers or {})
        payload = json.dumps(body if body is not None else {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, resp.read()

    def test_ping_with_token(self):
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"], {})

    def test_wrong_or_missing_token(self):
        self.assertEqual(self.request(token="x" * 40)[0], 401)
        self.assertEqual(self.request(token=None)[0], 401)

    def test_foreign_host_and_browser_origin_are_blocked(self):
        self.assertEqual(self.request(headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request(headers={"Origin": "https://evil.example"})[0], 403)

    def test_other_paths_and_methods(self):
        self.assertEqual(self.request(path="/other")[0], 404)
        self.assertEqual(self.request(method="GET")[0], 405)

    def test_tool_call_over_http(self):
        status, body = self.request(body={"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                          "params": {"name": "podcast_list_episodes", "arguments": {}}})
        self.assertEqual(status, 200)
        self.assertIn("Next episode number: 137", json.loads(body)["result"]["content"][0]["text"])

    def test_notification_only_gets_202(self):
        self.assertEqual(self.request(body={"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)


if __name__ == "__main__":
    unittest.main()
