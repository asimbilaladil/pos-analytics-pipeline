"""Laynes Intelligence read-only MCP server (Grok bot access).

Separate localhost-bound service. Reuses chat_sql's validated, read-only data
layer; owns only its own credential / audit / job store (SQLite).
"""
