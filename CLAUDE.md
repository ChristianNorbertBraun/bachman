# Working on Bachman

- Python 3.11+, standard library plus `requests`. Do not add dependencies.
- Run the tests with `python3 -m unittest discover -s tests`. Every change needs a test.
- Tests must not need the network, not even loopback: sandboxes that run them (Son of Anton's, for one) block connections to 127.0.0.1. Test the HTTP handler directly with a fake connection, as `HttpTest` does.
- The updater runs these tests inside a bubblewrap sandbox before it installs a release (home `/home/sandbox`, no access to the real home, system read-only). A test that depends on the real home or on files outside the checkout blocks every update.
- A change that should reach an installation needs a new version: raise `__version__` in `bachman/version.py` (patch for fixes, minor for a new tool) in the same pull request. The owner publishes the release tag by hand.
- A new tool goes into `TOOLS` and the handler map in `bachman/bridge.py`, with a description whose first sentence states the purpose. List it in the README table.
- `bachman/spotify.py` and `bachman/google.py` stay read-only: they must never send a mutation or another write. `bachman/docwriter.py` is the only module that writes to Google Docs, and only by inserting. Writes to a platform belong in their own module, behind a tool that states what it changes.
- Never log or return cookies, tokens or request headers. Errors shown to the chat agent must not contain paths or internals.
- Code, comments, commit messages and pull requests are in English.
