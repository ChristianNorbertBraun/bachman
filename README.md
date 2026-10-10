# bachman

Narrow tools that let a chat agent prepare a podcast episode for publishing, without ever holding the platform credentials.

Named after Erlich Bachman from the series Silicon Valley, because it is here for the show. It is the sibling of [Son of Anton](https://github.com/ChristianNorbertBraun/son-of-anton) and updates itself the same way.

The episode is uploaded by hand to Spotify for Creators and YouTube. The agent then finds the draft, reads the transcript and the planning document, writes title and description and, after your yes, enters them on YouTube and on Spotify together with the publish time.

## How it works

```
chat (e.g. Telegram) --> chat agent (its own unix user, any model)
                           |  MCP over HTTP, 127.0.0.1:8766, bearer token
                           v
                         bachman.service (unix user bachman)  <- holds the Spotify cookies and the Google sign-in
                           |  read-only requests
                           v
                         Spotify for Creators, Google Docs
```

- The unix user `bachman` owns the credentials. The agent's user cannot read them and only reaches Bachman through its tools.
- Bachman is plain code with no language model in it. It listens on loopback only, checks a bearer token and the `Host` header, and rejects requests that carry an `Origin`.
- `bachman/spotify.py` only reads: it refuses every operation that is not a query and contains no REST write. `bachman/google.py` only sends GET requests to Google APIs, and `bachman/trello.py` only GET requests to Trello. There are three writes, each in its own module. `bachman/spwriter.py` schedules a Spotify draft with title and description; it cannot upload, delete or publish right away. `bachman/docwriter.py` adds a new episode block to the planning document: it only inserts text and styles the inserted paragraphs, and it refuses any other kind of request. `bachman/ytwriter.py` sets title, description and publish time of a private YouTube video and sends every other field back unchanged; it cannot upload, delete or publish right away.

## Tools

| Tool | What it does |
|---|---|
| `podcast_list_episodes` | Unpublished Spotify drafts first (id, length, upload date, transcript available or not), the scheduled episodes, the next episode number, the latest published titles, and the private videos on YouTube (id, length, scheduled or not) |
| `podcast_get_transcript` | Spotify's automatic transcript of one episode, in parts of 40,000 characters |
| `podcast_get_agenda` | The planning document in Google Docs: the notes of the episode in preparation, the topics of the marked episodes, the numbered outline, or one section with its text and links |
| `podcast_create_agenda` | **Writes.** Adds the notes block for a new episode to the planning document: the template kept in the document, between a `START <topic>` and an `END` line, above the newest episode |
| `podcast_get_board` | The show's Trello board: every list with its cards (labels, due date, checklist progress), or the list of boards |
| `podcast_get_card` | One Trello card in full: description, checklists, attachments, latest comments |
| `podcast_schedule_youtube` | **Writes.** Sets title, description and publish time of a private YouTube video. Preview first, the write needs the confirmation code from the preview |
| `podcast_schedule_spotify` | **Writes.** Sets title, description (HTML) and publish time of a Spotify draft, optionally the paid-promotion setting. Preview first, the write needs the confirmation code from the preview |
| `bachman_update_check` | Installed version, newest release, result of the last update attempt. Read-only |
| `bachman_update_apply` | Installs the newest release you published. Only on the user's request |

The next episode number is taken from published titles that start with a number and a bar, like `12 | Some title`.

Transcript parts stay below 50,000 characters on purpose: the Hermes agent moves larger MCP results into a file, and reading that file back proved unreliable.

## Spotify for Creators has no official API

Bachman uses the internal API of the web app, authenticated with the session cookies `sp_dc` and `sp_key`. This is unofficial: Spotify can change or block it at any time, and it is probably not covered by the terms of use. Use a dedicated podcast login, never a private account, because the cookies grant full access to the account.

`creators-graph.spotify.com` only accepts persisted queries, addressed by a hash. The hashes are compiled into the public web bundle. `bachman/ops.py` reads them from there and caches them in `~/.local/state/bachman/ops.json`. When Spotify deploys a new web app and a hash is rejected, Bachman rebuilds the cache once and retries.

A rejected login is not retried until the cookie files change, so a dead session cannot turn into a stream of login attempts.

## Setup

Requirements: Linux with systemd, Python 3.11+, `python3-requests` from the distribution and `bubblewrap` (for the update tests). No pip packages.

1. Create the user once, as an admin: `sudo bash setup/root-setup.sh`. It creates `bachman` without sudo, password or SSH, with home 700 and linger, plus a temporary sudoers rule so the admin can act as that user. Pass another name as the first argument if you prefer one.
2. Install the first version, as `bachman`. Download the source archive of a release, unpack it to `~/releases/<version>` and point `~/current` at it:

```
mkdir -p ~/releases && cd ~/releases
curl -sL https://github.com/<owner>/bachman/archive/refs/tags/v0.1.2.tar.gz | tar -xz
mv bachman-0.1.2 0.1.2 && ln -sfn ~/releases/0.1.2 ~/current
```
3. Enter the cookies yourself: `sudo -u bachman bash ~bachman/current/setup/set-spotify-cookies.sh`. Nothing is printed.
4. Create the bearer token for the agent, as `bachman`:
   `umask 077; mkdir -p ~/.config/bachman; python3 -c "import secrets;print(secrets.token_urlsafe(48))" > ~/.config/bachman/token-merlin`
5. Copy `examples/config.toml` to `~/.config/bachman/config.toml` and fill in your repository and GitHub login. Without it the update tools are off.
6. Install the service: copy `examples/bachman.service` to `~bachman/.config/systemd/user/`, then `systemctl --user daemon-reload && systemctl --user enable --now bachman` (as `bachman`, with `XDG_RUNTIME_DIR=/run/user/<uid>`).
7. Wire the agent. For Hermes, in `config.yaml`:

```yaml
mcp_servers:
  bachman:
    url: "http://127.0.0.1:8766/mcp"
    headers:
      Authorization: "Bearer ${BACHMAN_BRIDGE_TOKEN}"
    tools:
      prompts: false
      resources: false
    sampling:
      enabled: false
    timeout: 90
    connect_timeout: 15
```

   Put the same token as `BACHMAN_BRIDGE_TOKEN` into the agent's environment and restart its gateway. There is no `tools.include` list on purpose, so a tool that arrives with an update becomes visible without editing the config.

## The planning document (Google Docs)

Many shows keep one long document with the agenda, notes and links of every episode. `podcast_get_agenda` reads it with the Google Docs API and hands the agent only the part it needs. Link targets are written out next to their text.

- **Markers.** Put the notes of an episode in preparation between a line `START <topic>` and a line `END`. With one marked episode the tool returns exactly that part by default. With several it lists the topics of the 4 most recent ones, in document order, and the agent asks for one by topic. `section = "topics"` lists the topics of all marked episodes, and every marked episode, older ones included, can be read by its topic. A start marker without an end marker is ignored. Other marker words can be set in `config.toml`.
- **Recent episodes.** The document carries no dates, so its order decides what is recent: by default the newest marked episode is at the top. Two optional settings under `[agenda]` change that:

  ```toml
  [agenda]
  max_recent_episodes = 4          # how many marked episodes the default answer lists (1 to 100)
  sort_direction = "newest_first"  # "oldest_first" when new episodes are added at the bottom
  ```
- **Sections.** A heading starts a section, and a tab or a page break starts a page that is named after its first line. `section = "outline"` returns the numbered titles, and a title, part of a title or a number like `#12` returns one section with the deeper sections below it.

1. In the Google Cloud Console, signed in to the podcast's Google account: create a project, enable the **Google Docs API** and the **YouTube Data API v3**, set up the consent screen (audience External) and **publish it to production**, otherwise the sign-in expires after 7 days. Create an OAuth client of type **Desktop app**.
2. Store the client: `sudo -u bachman bash ~bachman/current/setup/set-google-client.sh`.
3. Sign in once: `sudo -u bachman env PYTHONPATH=/home/bachman/current python3 -m bachman google-login`. Open the printed address in a browser that is signed in to the podcast account and agree. The browser then fails to load a page on `127.0.0.1`; copy that address from the address bar and paste it into the terminal. Bachman runs on another machine than your browser, so nothing listens there, and the address carries the one-time code.
4. Put the document into `config.toml` (`[agenda] document = "..."`) and restart the service.

### A new episode from a template

Keep a template in the document between a line `TEMPLATE` and a line `TEMPLATE END` (other words can be set in `config.toml`). `podcast_create_agenda` copies its lines, puts `START <topic>` before and `END` after them and inserts the block above the newest episode (below it with `sort_direction = "oldest_first"`; the block is always put in front of an existing line, so there must be one after the last episode). Headings and lists are kept: a checklist stays a checklist (with empty boxes), a numbered list stays numbered, a bullet list stays a bullet list, each with its nesting. Other formatting is not kept. If the episode below started on a new page, it gets its page break back. The tool refuses a topic that is already marked, reads the document again after the change and reports success only when the new episode is there. The Google account needs edit rights on the document.

The sign-in asks for two things: read and write access to the account's Google Docs, and YouTube access for the planned publishing tools. Google offers no narrower YouTube scope for changing a video's title, description and publish time, which is one reason the token stays with Bachman. Only the refresh token is stored (`~/.config/bachman/google/token.json`, mode 600). Revoke it any time in the Google account under third-party access.

## The Trello board

If the show keeps its topic pool and the state of each episode on a Trello board, `podcast_get_board` and `podcast_get_card` let the agent read it. Create a Power-Up in the Trello account to get an API key, authorize a token for it and store both with `sudo -u bachman bash ~bachman/current/setup/set-trello-key.sh`. A token is valid for every board of the account, so use an account that only holds the show's boards. Put the main board into `config.toml` (`[trello] board = "..."`). Key and token travel in the `Authorization` header, never in the address. Both tools only read.

## Scheduling on YouTube

`podcast_schedule_youtube` works on a video that was uploaded as **private** in YouTube Studio. It takes the video id, a title, a plain-text description and a publish time.

- **Two steps.** The first call changes nothing and returns a preview with a confirmation code. The chat agent shows the preview to the user and calls again with that code after the user's yes. The code is derived from the video, the title, the description and the time, so what is written is exactly what was shown.
- **Rules checked before anything is sent.** Title at most 100 characters and one line, description at most 5000 bytes, no `<` or `>` (YouTube rejects them), nothing from your own `forbidden` list, the time at least 15 minutes and at most a year ahead, the video private and processed. A time without an offset is meant in `[publish] timezone`.
- **Nothing else changes.** Tags, category, language and the other settings of the video are sent back as they are, because the API deletes what an update leaves out. The video stays private until YouTube publishes it at the set time.
- **Read-back.** YouTube's reads lag a few seconds behind its writes. The tool checks the answer to the update and then reads the video again, several times if needed, and says so when the new values never showed up.

A scheduled video can be rescheduled with the same tool. Taking a schedule off again is not offered; do that in YouTube Studio.

The tool uses the official YouTube Data API with the sign-in described above. An update costs 50 of the 10,000 quota units a project gets per day.

## Scheduling on Spotify

`podcast_schedule_spotify` works on an episode that was uploaded in Spotify for Creators and left as a **draft**. It takes the episode id, a title, the description as HTML and a publish time, and optionally whether the episode contains paid promotion.

- **Same two steps as on YouTube:** a preview with a confirmation code first, the write only with that code. The preview shows the description as readers will see it.
- **Rules checked before anything is sent.** The description may only use `p`, `strong`, `a`, `ul`, `ol` and `li`; a link carries exactly one attribute, `href`, with an http or https address; no other attributes anywhere. Title at most 200 characters, at most 4000 characters of visible text, nothing from your `forbidden` list, the time 15 minutes to a year ahead. The episode must be an unpublished draft with processed media, or already scheduled.
- **What is sent** is what the web app's editor sends when you press Schedule: the texts, the episode's existing type and flags, and the publish time. Spotify adds `rel` and `target` to links on its own.
- **Read-back.** The tool reads the episode again and reports success only when title, description and publish time are stored.

This part uses the unofficial internal API, see the section about it above. It was tried on a short audio draft: saving, scheduling 30 days ahead, the paid-promotion setting and taking the schedule off again all behaved as in the web app. `podcast_list_episodes` shows a scheduled episode under its own heading. Taking a schedule off is not offered by the tool; do that in Spotify for Creators.

Config and credentials live in `$XDG_CONFIG_HOME/bachman` (default `~/.config/bachman`), state in `$XDG_STATE_HOME/bachman` (default `~/.local/state/bachman`). Set the two variables in the service unit to move them.

## Updating

Bachman updates itself from its own releases. You publish a release on GitHub (UI or CLI) with a tag `vX.Y.Z` whose `bachman/version.py` says the same; then `python3 -m bachman update` (or "update Bachman" in the chat, via `bachman_update_apply`) installs it:

1. Only a release published **by the login in `[update] publisher`**, no draft, no pre-release and newer than the running version is accepted (no downgrades without `--force`). Nothing the chat agent does can publish one. To be sure, restrict tag creation of `v*` to yourself in a GitHub ruleset.
2. The source archive is unpacked next to the old version (`~/releases/<version>`, plain files only, no links or `..`). Its own tests run in a bubblewrap sandbox that cannot see the home of the service user, so the stored credentials are out of reach until the tests passed. Then it must be able to read your real config (`bachman config-check`).
3. `~/current` points to the new version in one step, the service restarts and must report the new version and stay up for 15 seconds.
4. If that fails the symlink goes back and the old version is started again. The result is kept for `bachman_update_check`. Three versions are kept.

Merging a pull request never changes the running installation; only a release you publish and an update you ask for do.

This closes a loop with Son of Anton: ask it for a missing tool, review and merge its draft pull request, publish a release, and tell the chat agent to update Bachman. Your review is the gate. The code that runs next to the credentials is whatever you merged and released.

## Tests

```
python3 -m unittest discover -s tests
```

The tests use a fake HTTP module and fake releases. They cover the hash extraction, the query format, that cookies only go to the login host, that mutations are refused without a request, the refresh after a stale hash, the login back-off, the tool output, the HTTP guards, the Google sign-in and document parsing, the YouTube and Spotify rules and the two-step confirmation, and the update (wrong publisher, unsafe archives, failing tests, rollback). They run on every pull request, on `main` and on release tags.

## Status

- Done: read access to Spotify (drafts, transcript) and Trello (board, cards), reading the planning document in Google Docs and adding a new episode to it, scheduling a private video on YouTube and a draft on Spotify, self-update.
- Open: scheduling on Spotify was only tried with an audio draft, not with a video episode.

## License

MIT, see [LICENSE](LICENSE).
