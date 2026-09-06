"""HTTP API for Laynes Intelligence.

This package is a THIN transport layer. It owns no analytics: every business
answer comes from chat_sql.answer_question, unchanged, so A1-A12, the AST
validator, the laynes_ro role and the A12 reconciliation gate all keep working
exactly as they do under the Streamlit app.

Nothing here ever hands the browser a database credential, an Anthropic key, or
raw SQL. The client sends a question and receives rendered text.
"""
