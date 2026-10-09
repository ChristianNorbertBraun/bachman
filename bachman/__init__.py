"""Bachman: narrow MCP tools for preparing podcast episodes, run by its own unprivileged unix user.

The chat agent (Merlin) reaches Spotify for Creators only through these tools. The session
cookies stay with this process and are never returned to the caller.
"""
