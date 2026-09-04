"""
admin_chat.py — Laynes Intelligence, the login-gated chat assistant.

Served at the site root (https://intelligence.aygchicken.com/). A user logs in
(accounts live in the app_users table, see migrations/17), then asks questions
in plain English; chat_sql.py turns each one into a read-only SQL query, runs it,
and Claude summarises the result in the chat.

Run:
  streamlit run admin_chat.py --server.port 8504 --server.baseUrlPath / \
      --server.headless true

Env (in .env, loaded below):
  ANTHROPIC_API_KEY   required for the assistant
  CHAT_MODEL          optional, default claude-sonnet-5
  DB_HOST/PORT/NAME   Postgres (default localhost/5432/laynes)
  DB_USER/DB_PASS     read-write role, used ONLY for app_users + chat_query_log
  DB_RO_USER/DB_RO_PASS  read-only role, used for every generated query
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

import bcrypt
import psycopg2
import psycopg2.extras
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import chat_sql  # noqa: E402  (after load_dotenv so MODEL picks up env)

BCRYPT_ROUNDS = 12
SESSION_TTL_DAYS = 30
SESSION_COOKIE = "laynes_session"


# ─────────────────────────────── database ────────────────────────────────────
def _rw_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", 5432),
        dbname=os.getenv("DB_NAME", "laynes"),
        user=os.getenv("DB_USER", "laynes_user"),
        password=os.environ["DB_PASS"],
    )


def _query(sql, params=None, *, fetch="all", commit=False):
    conn = _rw_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            row = None
            if fetch == "all":
                row = cur.fetchall()
            elif fetch == "one":
                row = cur.fetchone()
        if commit:
            conn.commit()
        return row
    finally:
        conn.close()


# ─────────────────────── persistent session (survives refresh) ──────────────
# st.session_state does NOT survive a full page reload — a browser refresh
# (or navigating straight to the bare URL) opens a new WebSocket connection
# with blank state. This token, held in an actual browser cookie and backed
# by app_sessions, is what makes any of those keep you signed in instead of
# dropping you back to the login screen. (An earlier version carried the
# token in the URL's ?s= query string instead — that only worked if the
# link you followed happened to include it; a bare bookmark or a plain
# https://intelligence.aygchicken.com/ had nothing to restore from. A real
# cookie is what makes it work everywhere.)
def create_session(user_id) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
    _query("INSERT INTO app_sessions (token, user_id, expires_at) VALUES (%s,%s,%s)",
           (token, user_id, expires), fetch=None, commit=True)
    return token


def get_session_user(token: str):
    row = _query(
        "SELECT u.id, u.email, u.full_name, u.role, u.is_active "
        "FROM app_sessions s JOIN app_users u ON u.id = s.user_id "
        "WHERE s.token = %s AND s.expires_at > NOW()",
        (token,), fetch="one",
    )
    if not row or not row["is_active"]:
        return None
    return row


def delete_session(token: str):
    if not token:
        return
    try:
        _query("DELETE FROM app_sessions WHERE token = %s", (token,), fetch=None, commit=True)
    except psycopg2.Error:
        pass


def _set_session_cookie(token: str):
    """(Re)write the browser cookie holding the session token. Safe to call
    on every rerun while logged in — the browser just re-sets the same value
    and refreshes its expiry. Runs as a tiny invisible component, since
    Streamlit has no direct Python API to set response cookies."""
    max_age = SESSION_TTL_DAYS * 86400
    components.html(
        f'<script>document.cookie="{SESSION_COOKIE}={token}; path=/; '
        f'max-age={max_age}; SameSite=Lax; Secure";</script>',
        height=0,
    )


def _clear_session_cookie():
    components.html(
        f'<script>document.cookie="{SESSION_COOKIE}=; path=/; max-age=0";</script>',
        height=0,
    )


# Restore login from the session cookie BEFORE set_page_config, so the
# collapsed/expanded sidebar decision below already reflects a returning,
# signed-in user rather than momentarily treating them as logged out.
# st.context.cookies reads whatever the browser actually sent with this
# request — unlike a URL query param, it's there on any entry point
# (bookmark, typed URL, a plain page refresh), not just a link that
# happens to carry it.
if "user" not in st.session_state:
    _tok = st.context.cookies.get(SESSION_COOKIE)
    if _tok:
        _restored = get_session_user(_tok)
        if _restored:
            st.session_state.user = _restored
            st.session_state.session_token = _tok

st.set_page_config(
    page_title="Laynes Intelligence", page_icon="🐔", layout="centered",
    # Collapsed on the login screen (nothing to show); expanded once signed
    # in, so the chat history sidebar is visible immediately, not hidden
    # behind a collapse arrow the user has to discover.
    initial_sidebar_state="expanded" if "user" in st.session_state else "collapsed",
)

try:
    with open(os.path.join(os.path.dirname(__file__), "assets", "ayg-logo.png"), "rb") as _f:
        LOGO_URI = "data:image/png;base64," + base64.b64encode(_f.read()).decode()
except OSError:
    LOGO_URI = ""


# ─────────────────────────────── styling ────────────────────────────────────
THEME_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');

:root{
  --bg:#f5f6f8; --card:#ffffff; --card-2:#fbfcfd;
  --line:#e6e8ec; --line-strong:#d6dae0;
  --text:#141821; --muted:#6b7280;
  --accent:#d6202f; --accent-press:#b0182a; --accent-tint:#fdeced;
  --shadow-sm:0 1px 2px rgba(16,24,40,.05);
  --shadow-md:0 1px 3px rgba(16,24,40,.06), 0 12px 28px -14px rgba(16,24,40,.18);
}

#MainMenu, header[data-testid="stHeader"], footer, [data-testid="stToolbar"],
[data-testid="stSidebarNav"]{display:none!important;}

html, body, [class*="css"], .stApp *{
  font-family:'Plus Jakarta Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
}

.stApp{
  background:
    radial-gradient(900px 460px at 12% -8%, rgba(214,32,47,.07), transparent 60%),
    radial-gradient(760px 420px at 108% 0%, rgba(214,32,47,.05), transparent 55%),
    var(--bg);
  color:var(--text);
}
[data-testid="stAppViewContainer"]{background:transparent;}
.block-container{max-width:720px;padding-top:3rem;padding-bottom:4rem;}

/* ── inputs ───────────────────────────────────────────────────────────── */
.stTextInput label, .stSelectbox label{
  color:var(--muted)!important;font-weight:600;font-size:.78rem;
  letter-spacing:.03em;text-transform:uppercase;
}
/* the outer box carries the border so the password field's reveal button
   sits INSIDE the same box and both fields stay the same width */
.stTextInput div[data-baseweb="input"], .stSelectbox div[data-baseweb="select"] > div{
  background:#fff!important;border:1px solid var(--line-strong)!important;
  border-radius:10px!important;box-shadow:var(--shadow-sm);
  transition:border-color .15s ease, box-shadow .15s ease;
}
.stTextInput div[data-baseweb="input"]:focus-within{
  border-color:var(--accent)!important;box-shadow:0 0 0 3px var(--accent-tint)!important;
}
.stTextInput input{
  background:transparent!important;border:0!important;color:var(--text)!important;
  padding:.68rem .85rem!important;font-size:.97rem!important;box-shadow:none!important;
}
.stTextInput input:focus{outline:none!important;box-shadow:none!important;}

/* ── buttons ──────────────────────────────────────────────────────────── */
/* default = secondary (white w/ border) */
.stButton > button{
  width:100%!important;border-radius:10px;border:1px solid var(--line-strong);
  padding:.58rem .9rem;font-weight:600;font-size:.92rem;white-space:nowrap;
  background:#fff;color:var(--text);box-shadow:var(--shadow-sm);
  transition:transform .12s ease, border-color .12s ease, color .12s ease;
}
.stButton > button:hover{border-color:var(--accent);color:var(--accent);transform:translateY(-1px);}
.stButton > button:active{transform:translateY(0);}
/* primary = the login submit only */
.stFormSubmitButton > button, [data-testid="stFormSubmitButton"] button{
  width:100%!important;border-radius:10px;border:1px solid var(--accent-press);
  padding:.66rem 1rem;font-weight:700;font-size:.96rem;
  background:var(--accent);color:#fff;box-shadow:0 6px 16px -8px rgba(214,32,47,.5);
  transition:transform .12s ease, background .12s ease;
}
.stFormSubmitButton > button:hover, [data-testid="stFormSubmitButton"] button:hover{
  background:var(--accent-press);color:#fff;transform:translateY(-1px);
}
/* password reveal (eye) button — subtle icon inside the field */
[data-testid="stTextInput"] div[data-baseweb="input"] button{
  width:auto!important;min-width:0!important;background:transparent!important;
  border:0!important;box-shadow:none!important;color:var(--muted)!important;
  padding:0 .55rem!important;margin:0!important;
}
[data-testid="stTextInput"] div[data-baseweb="input"] button:hover{
  color:var(--text)!important;transform:none;background:transparent!important;
}
/* drop the "Press Enter to submit form" caption that overlaps the field */
[data-testid="InputInstructions"], div[data-testid="InputInstructions"]{display:none!important;}

/* ── login ────────────────────────────────────────────────────────────── */
.auth-brand{display:flex;flex-direction:column;align-items:center;gap:.85rem;margin:4vh 0 .6rem;}
.brand-card{
  background:#fff;border:1px solid var(--line);border-radius:16px;
  padding:.9rem 1.5rem;box-shadow:var(--shadow-md);
}
.brand-card img{display:block;height:44px;width:auto;}
.brand-card.sm{padding:.45rem .8rem;border-radius:11px;}
.brand-card.sm img{height:24px;}
.auth-brand h1{font-size:1.7rem;font-weight:800;margin:0;letter-spacing:-.01em;color:var(--text);}
.auth-sub{text-align:center;color:var(--muted);font-size:1rem;margin-bottom:1.6rem;}
[data-testid="stForm"]{
  background:var(--card);border:1px solid var(--line);border-radius:18px;
  padding:2.1rem 2rem 1.9rem;box-shadow:var(--shadow-md);
}
[data-testid="stForm"] .stTextInput input{padding:.82rem .95rem!important;font-size:1rem!important;}
[data-testid="stForm"] [data-testid="stFormSubmitButton"] button{padding:.8rem 1rem;font-size:1rem;margin-top:.3rem;}
.auth-foot{text-align:center;color:var(--muted);font-size:.82rem;margin-top:1.1rem;}

/* ── chat ─────────────────────────────────────────────────────────────── */
.hero{display:flex;align-items:center;gap:.7rem;margin-bottom:.15rem;}
.hero h1{font-size:1.28rem;font-weight:800;margin:0;letter-spacing:-.01em;}
.hero-sub{color:var(--muted);font-size:.92rem;margin:.15rem 0 1.4rem;}

[data-testid="stChatMessage"]{
  background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:.85rem 1rem;box-shadow:var(--shadow-sm);
}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]){
  background:var(--accent-tint);border-color:#f6d5d8;
}
[data-testid="stChatInput"]{
  background:#fff!important;border:1px solid var(--line-strong)!important;
  border-radius:12px!important;box-shadow:var(--shadow-sm);
}
[data-testid="stChatInput"] textarea{color:var(--text)!important;}
[data-testid="stChatInput"]:focus-within{
  border-color:var(--accent)!important;box-shadow:0 0 0 3px var(--accent-tint)!important;
}

/* top bar */
.topbar-title{display:flex;align-items:center;gap:.65rem;}
/* model pill — the selectbox restyled as a quiet menu, not a form field.
   No leading icon: the control is narrow and the padding is better spent
   on the value, so the longest id ("claude-haiku-4-5-20251001") still
   shows in full. padding-right keeps it clear of baseweb's chevron, which
   is parked absolutely at the right edge so it can never overlap text. */
.st-key-modelpill{display:flex;align-items:center;height:100%;}
.st-key-modelpill [data-testid="stSelectbox"]{width:100%;}
.st-key-modelpill div[data-baseweb="select"]{position:relative;}
.st-key-modelpill div[data-baseweb="select"] > div{
  background:transparent!important;border:1px solid transparent!important;
  border-radius:8px!important;min-height:32px!important;
  padding:2px 1.55rem 2px .5rem!important;
  font-size:12.5px!important;line-height:1.5!important;font-weight:500;
  color:var(--muted)!important;box-shadow:none!important;
  transition:background-color .16s ease,color .16s ease;
}
.st-key-modelpill div[data-baseweb="select"] > div:hover{
  background:#F0F2F5!important;color:var(--text)!important;
}
.st-key-modelpill div[data-baseweb="select"] > div > div{
  min-width:0!important;max-width:100%!important;padding:0!important;margin:0!important;
  display:flex!important;align-items:center!important;
  overflow:visible!important;line-height:1.5!important;
}
.st-key-modelpill div[data-baseweb="select"] > div > div:first-child > div{
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%;
}
.st-key-modelpill div[data-baseweb="select"] > div > div:last-child{
  position:absolute;right:.3rem;top:50%;transform:translateY(-50%);
  padding:0!important;width:auto!important;
}
.st-key-modelpill div[data-baseweb="select"] svg{width:14px;height:14px;color:var(--muted);}
.st-key-modelpill [data-testid="stIconMaterial"],
.st-key-modelpill span[translate="no"]{display:none!important;}
.topbar-title h1{font-size:1.18rem;font-weight:800;margin:0;letter-spacing:-.01em;}
.topbar-div{height:1px;background:var(--line);margin:.4rem 0 1.6rem;}

/* empty-state welcome */
.welcome{text-align:center;margin:2.2rem 0 1.6rem;}
.welcome h2{font-size:1.5rem;font-weight:800;margin:.9rem 0 .3rem;letter-spacing:-.01em;}
.welcome p{color:var(--muted);font-size:.98rem;margin:0;}

/* suggestion cards */
.st-key-sugwrap .stButton > button{
  width:100%!important;background:#fff;color:var(--text);
  border:1px solid var(--line-strong);border-radius:12px;box-shadow:var(--shadow-sm);
  font-weight:600;font-size:.9rem;line-height:1.4;
  display:flex;align-items:center;text-align:left;justify-content:flex-start;
  min-height:66px;height:100%;padding:.85rem 1rem;white-space:normal!important;
}
.st-key-sugwrap .stButton > button *{
  white-space:normal!important;overflow-wrap:anywhere;word-break:break-word;
  text-align:left;
}
.st-key-sugwrap .stButton > button:hover:not(:disabled){
  border-color:var(--accent);color:var(--text);background:var(--card-2);
  transform:translateY(-2px);box-shadow:var(--shadow-md);
}
.st-key-sugwrap [data-testid="stColumn"]{display:flex;}
.st-key-sugwrap [data-testid="stColumn"] > div,
.st-key-sugwrap [data-testid="stColumn"] .stButton{width:100%;}

/* inline notice (e.g. assistant not configured) */
.notice{
  background:#fff8ec;border:1px solid #f2dfb8;color:#8a6d3b;
  border-radius:12px;padding:.85rem 1rem;font-size:.92rem;margin:.4rem 0 1rem;
}

/* chat input */
[data-testid="stChatInput"] textarea::placeholder{color:var(--muted)!important;}
.inhint{color:var(--muted);font-size:.78rem;text-align:center;margin-top:.5rem;}

[data-testid="stExpander"]{border:1px solid var(--line);border-radius:10px;background:var(--card-2);}
[data-testid="stExpander"] summary{color:var(--muted);font-weight:600;}

/* ══════════════════════ sidebar — conversation rail ══════════════════════
   One job: navigating conversations. No branding, no CTA, no model control
   and no form widgets — those live in the top bar. Structure is
   header → history → flexible space → utilities.
   Icons are inline-SVG data URIs on ::before, so nothing depends on
   Streamlit's material ligature font (whose spans hold the literal icon
   name as text and leak it before the font loads). */

/* Streamlit's initial_sidebar_state only takes effect at the very first
   connection of a browser session — it does NOT re-apply on a later rerun
   within that same session (e.g. the one right after clicking "Sign in",
   which starts life on the login screen with the sidebar collapsed and no
   content, before "user" exists). Rather than chase that timing, force it
   open unconditionally with CSS. aria-expanded="false" is the collapsed
   state's marker; override its transform/width regardless of that state. */
[data-testid="stSidebar"]{
  background:#FBFBFC;border-right:1px solid #ECEEF1;box-shadow:none!important;
  width:248px!important;min-width:248px!important;
  transform:none!important;visibility:visible!important;
}
[data-testid="stSidebar"][aria-expanded="false"]{
  width:248px!important;min-width:248px!important;
  margin-left:0!important;transform:none!important;
}
[data-testid="collapsedControl"], [data-testid="stSidebarCollapsedControl"],
[data-testid="stSidebarCollapseButton"]{display:none!important;}

/* full-height flex column so .sb-spacer can floor the utilities. 3rem of
   top padding lines the "Conversations" header up with the top bar's title
   row rather than starting at an arbitrary height. */
[data-testid="stSidebar"] [data-testid="stSidebarContent"]{
  padding:3rem .94rem 1rem;display:flex;flex-direction:column;min-height:100vh;
}
[data-testid="stSidebar"] [data-testid="stSidebarContent"] > div:first-child,
[data-testid="stSidebar"] [data-testid="stSidebarContent"] [data-testid="stVerticalBlock"]{
  display:flex;flex-direction:column;flex:1 1 auto;min-height:0;gap:0;
}
[data-testid="stSidebar"] [data-testid="stVerticalBlockBorderWrapper"]{gap:0;}
[data-testid="stSidebar"] [data-testid="stElementContainer"]{width:100%;flex:0 0 auto;}
[data-testid="stSidebar"] [data-testid="stElementContainer"]:has(.sb-spacer){
  flex:1 1 auto!important;min-height:1rem;
}
.sb-spacer{height:100%;min-height:1rem;}
/* no material ligature icon may render as raw text anywhere in the rail */
[data-testid="stSidebar"] [data-testid="stIconMaterial"],
[data-testid="stSidebar"] button span[translate="no"]{display:none!important;}

/* ── header ──────────────────────────────────────────────────────────── */
.sb-head{
  font-size:13px;font-weight:600;color:#20232A;letter-spacing:-.005em;
  padding:0 .35rem;margin:0 0 .5rem;
}

/* ── shared row: the single shape every nav item in the rail uses ────── */
[data-testid="stSidebar"] .stButton > button,
[data-testid="stSidebar"] [data-testid="stDownloadButton"] > button{
  width:100%!important;background:transparent;color:#3d424e;
  border:1px solid transparent!important;box-shadow:none!important;
  border-radius:8px;font-weight:450;font-size:13px;line-height:1.4;
  min-height:36px;padding:6px 9px;
  text-align:left!important;justify-content:flex-start!important;
  transition:background-color .16s ease,color .16s ease;
}
[data-testid="stSidebar"] .stButton > button:hover,
[data-testid="stSidebar"] [data-testid="stDownloadButton"] > button:hover{
  background:#F0F2F5;color:#17191F;transform:none;
}
[data-testid="stSidebar"] .stButton > button:focus-visible,
[data-testid="stSidebar"] [data-testid="stDownloadButton"] > button:focus-visible{
  outline:2px solid var(--accent);outline-offset:-1px;
}
[data-testid="stSidebar"] .stButton > button p{
  margin:0!important;white-space:nowrap!important;
  overflow:hidden!important;text-overflow:ellipsis!important;
}

/* ── conversation list ───────────────────────────────────────────────── */
.st-key-convolist{
  flex:0 1 auto;min-height:0;overflow-y:auto;overflow-x:hidden;
  margin:0 -.2rem;padding:0 .2rem;
}
.st-key-convolist::-webkit-scrollbar{width:5px;}
.st-key-convolist::-webkit-scrollbar-thumb{background:#E1E3E8;border-radius:3px;}
.st-key-convolist::-webkit-scrollbar-track{background:transparent;}
.sb-daygroup{
  font-size:11px;font-weight:500;color:#8B919C;
  margin:.85rem 0 .15rem;padding:0 .55rem;
}
.sb-daygroup:first-child{margin-top:.1rem;}
.st-key-convolist [data-testid="stHorizontalBlock"]{
  gap:.1rem!important;align-items:center!important;margin-bottom:1px;
}
.st-key-convolist .stButton > button{position:relative;}
/* active row: neutral fill, darker text, 2px red rail — never a red button */
.st-key-convolist button[kind="primary"]{
  background:#ECEEF2!important;color:#17191F!important;font-weight:500!important;
  border-color:transparent!important;box-shadow:none!important;
}
.st-key-convolist button[kind="primary"]:hover{background:#E7E9EE!important;}
.st-key-convolist button[kind="primary"]::before{
  content:"";position:absolute;left:0;top:50%;transform:translateY(-50%);
  width:2px;height:16px;border-radius:0 2px 2px 0;background:var(--accent);
}
/* delete: revealed on row hover / keyboard focus only */
[class*="st-key-delwrap"] button{
  min-height:28px!important;height:28px!important;padding:0!important;
  background:transparent!important;border-color:transparent!important;
  color:#9aa1ad!important;font-size:12px!important;
  justify-content:center!important;text-align:center!important;
  opacity:0;transition:opacity .16s ease,color .16s ease,background-color .16s ease;
}
.st-key-convolist [data-testid="stHorizontalBlock"]:hover [class*="st-key-delwrap"] button,
[class*="st-key-delwrap"] button:focus-visible{opacity:1;}
[class*="st-key-delwrap"] button:hover{color:var(--accent)!important;background:#E7E9EE!important;}

/* ── empty state: left-aligned, directly under the header, no box ────── */
.sb-empty{padding:.35rem .55rem 0;max-width:190px;}
.sb-empty i{
  display:block;width:17px;height:17px;margin:0 0 .5rem;opacity:.45;
  background:center/17px no-repeat url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%236b7280' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2Z'/%3E%3C/svg%3E");
}
.sb-empty b{display:block;font-size:13px;font-weight:500;color:#3d424e;margin-bottom:.2rem;}
.sb-empty small{display:block;font-size:12px;color:#8B919C;line-height:1.5;}

/* ── bottom utilities: nav actions, not buttons ──────────────────────── */
.sb-divider{height:1px;background:#ECEEF1;margin:.5rem .2rem .45rem;}
.st-key-upwrap [data-testid="stFileUploaderDropzone"]{
  background:transparent!important;border:0!important;padding:0!important;
  min-height:0!important;gap:0!important;
}
.st-key-upwrap [data-testid="stFileUploaderDropzoneInstructions"]{display:none!important;}
.st-key-upwrap [data-testid="stFileUploaderDropzone"] button{
  width:100%!important;background:transparent!important;
  border:1px solid transparent!important;border-radius:8px!important;
  color:#3d424e!important;font-weight:450!important;font-size:12.5px!important;
  min-height:34px!important;padding:6px 9px!important;
  justify-content:flex-start!important;box-shadow:none!important;
  transition:background-color .16s ease,color .16s ease;
}
.st-key-upwrap [data-testid="stFileUploaderDropzone"] button:hover{
  background:#F0F2F5!important;color:#17191F!important;
}
/* the dropzone button's own label is hidden below; ::before/::after supply
   the icon and text so no ligature font is involved */
.st-key-upwrap [data-testid="stFileUploaderDropzone"] button::before{
  content:"Import conversation";order:2;font:inherit;color:inherit;
}
.st-key-upwrap [data-testid="stFileUploaderDropzone"] button::after{
  content:"";order:1;flex:0 0 auto;width:16px;height:16px;margin-right:8px;opacity:.75;
  background:center/16px no-repeat url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%236b7280' stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4'/%3E%3Cpath d='M17 8l-5-5-5 5'/%3E%3Cpath d='M12 3v12'/%3E%3C/svg%3E");
}
.st-key-upwrap [data-testid="stFileUploaderDropzone"] button > div,
.st-key-upwrap [data-testid="stFileUploaderDropzone"] button > span:not([translate="no"]){
  display:none!important;
}
/* helper text: present but clearly subordinate to the action above it */
.sb-uphint{
  color:#A2A8B3;font-size:11px;line-height:1.4;margin:-2px 0 2px;padding:0 9px 0 33px;
}
.st-key-upwrap [data-testid="stFileUploaderFile"]{min-width:0;font-size:12px;}
.st-key-upwrap [data-testid="stFileUploaderFileName"]{
  font-size:12px!important;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
.st-key-dl_convo button{
  font-size:12.5px!important;min-height:34px!important;padding:6px 9px!important;
  color:#3d424e!important;border-radius:8px!important;
}
.st-key-dl_convo button:disabled{opacity:.45!important;background:transparent!important;}
.st-key-dl_convo button::before{
  content:"";flex:0 0 auto;width:16px;height:16px;margin-right:8px;opacity:.75;
  background:center/16px no-repeat url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%236b7280' stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4'/%3E%3Cpath d='M7 10l5 5 5-5'/%3E%3Cpath d='M12 15V3'/%3E%3C/svg%3E");
}

[data-testid="stSidebar"] hr{margin:.9rem 0;border-color:#ECEEF1;}
hr{border-color:var(--line);}

/* ── responsive: below 900px the rail becomes an overlay drawer ──────── */
@media (max-width:900px){
  [data-testid="stSidebar"], [data-testid="stSidebar"][aria-expanded="false"]{
    position:fixed!important;z-index:99;height:100vh;
    box-shadow:0 0 0 100vmax rgba(16,24,40,.32), 0 18px 40px -12px rgba(16,24,40,.35);
    transition:transform .2s ease;
  }
  [data-testid="stSidebar"][aria-expanded="false"]{
    transform:translateX(-100%)!important;box-shadow:none;
  }
  [data-testid="stSidebarCollapsedControl"], [data-testid="stSidebarCollapseButton"]{
    display:flex!important;
  }
  .block-container{padding-left:1rem;padding-right:1rem;}
}
</style>
"""
st.markdown(THEME_CSS, unsafe_allow_html=True)


# ─────────────────────────────── auth ────────────────────────────────────────
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(BCRYPT_ROUNDS)).decode()


def verify_login(email: str, password: str):
    user = _query(
        "SELECT id, email, full_name, role, is_active, password_hash "
        "FROM app_users WHERE email = %s",
        (email.strip().lower(),),
        fetch="one",
    )
    if not user or not user["is_active"]:
        return None
    if not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        return None
    _query("UPDATE app_users SET last_login_at = NOW() WHERE id = %s",
           (user["id"],), fetch=None, commit=True)
    user.pop("password_hash", None)
    return user


def log_chat(user_id, question, steps, answer, error, duration_ms, conversation_id=None,
             model=None):
    last_sql = steps[-1]["sql"] if steps else None
    row_count = steps[-1]["row_count"] if steps else None
    try:
        _query(
            "INSERT INTO chat_query_log "
            "(user_id, question, generated_sql, row_count, error, duration_ms, model, steps, "
            " conversation_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (user_id, question, last_sql, row_count, error, duration_ms, model or chat_sql.MODEL,
             json.dumps({"answer": answer, "steps": steps}), conversation_id),
            fetch=None, commit=True,
        )
    except psycopg2.Error:
        pass  # logging must never break the chat


# ─────────────────────────── conversation persistence ───────────────────────
def list_conversations(user_id, limit=50):
    return _query(
        "SELECT id, title, model, updated_at FROM chat_conversations "
        "WHERE user_id = %s ORDER BY updated_at DESC LIMIT %s",
        (user_id, limit),
    )


def create_conversation(user_id, title, model=None):
    row = _query(
        "INSERT INTO chat_conversations (user_id, title, model) VALUES (%s, %s, %s) RETURNING id",
        (user_id, title[:120], model), fetch="one", commit=True,
    )
    return row["id"]


def get_conversation_model(conversation_id):
    row = _query("SELECT model FROM chat_conversations WHERE id = %s",
                 (conversation_id,), fetch="one")
    return row["model"] if row else None


def set_conversation_model(conversation_id, model):
    _query("UPDATE chat_conversations SET model = %s WHERE id = %s",
           (model, conversation_id), fetch=None, commit=True)


def touch_conversation(conversation_id, title=None):
    if title is not None:
        _query("UPDATE chat_conversations SET updated_at = NOW(), title = %s WHERE id = %s",
               (title[:120], conversation_id), fetch=None, commit=True)
    else:
        _query("UPDATE chat_conversations SET updated_at = NOW() WHERE id = %s",
               (conversation_id,), fetch=None, commit=True)


def save_message(conversation_id, role, content):
    _query("INSERT INTO chat_messages (conversation_id, role, content) VALUES (%s,%s,%s)",
           (conversation_id, role, content), fetch=None, commit=True)


def load_messages(conversation_id):
    rows = _query(
        "SELECT role, content FROM chat_messages WHERE conversation_id = %s ORDER BY created_at",
        (conversation_id,),
    )
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def delete_conversation(conversation_id, user_id):
    _query("DELETE FROM chat_conversations WHERE id = %s AND user_id = %s",
           (conversation_id, user_id), fetch=None, commit=True)


def load_conversation_full(conversation_id, user_id):
    """Fetch title + messages for export, scoped to the owning user."""
    convo = _query(
        "SELECT id, title, model, created_at FROM chat_conversations WHERE id = %s AND user_id = %s",
        (conversation_id, user_id), fetch="one",
    )
    if not convo:
        return None
    rows = _query(
        "SELECT role, content, created_at FROM chat_messages "
        "WHERE conversation_id = %s ORDER BY created_at",
        (conversation_id,),
    )
    return {
        "title": convo["title"],
        "model": convo["model"],
        "created_at": convo["created_at"].isoformat(),
        "messages": [{"role": r["role"], "content": r["content"],
                      "created_at": r["created_at"].isoformat()} for r in rows],
    }


def import_conversation(user_id, title, messages, model=None):
    """messages: [{"role": "user"|"assistant", "content": str}, ...]"""
    if model not in chat_sql.AVAILABLE_MODELS:
        model = None
    conversation_id = create_conversation(user_id, title, model=model)
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content:
            save_message(conversation_id, role, content)
    return conversation_id


# ─────────────────────────────── login screen ───────────────────────────────
def login_screen():
    _, mid, _ = st.columns([1, 3.6, 1])
    with mid:
        st.markdown(
            '<div class="auth-brand">'
            f'<div class="brand-card"><img src="{LOGO_URI}" alt="AYG Food Services"></div>'
            '<h1>Laynes Intelligence</h1></div>'
            '<div class="auth-sub">Sales &amp; operations, in plain English.</div>',
            unsafe_allow_html=True,
        )
        with st.form("login"):
            email = st.text_input("Email", placeholder="you@aygfoods.com")
            password = st.text_input("Password", type="password", placeholder="••••••••••")
            ok = st.form_submit_button("Sign in", use_container_width=True)
        st.markdown(
            '<div class="auth-foot">Authorised users only · access is logged</div>',
            unsafe_allow_html=True,
        )
    if ok:
        user = verify_login(email, password)
        if user:
            token = create_session(user["id"])
            st.session_state.user = user
            st.session_state.session_token = token
            st.session_state.messages = []
            st.session_state.conversation_id = None
            st.session_state.model_choice = chat_sql.MODEL
            st.rerun()
        else:
            with mid:
                st.error("Wrong email or password, or the account is inactive.")


# ─────────────────────────────── user management ────────────────────────────
def user_admin_panel():
    st.subheader("Users")
    users = _query(
        "SELECT id, email, full_name, role, is_active, last_login_at "
        "FROM app_users ORDER BY created_at"
    )
    st.dataframe(
        [{"email": u["email"], "name": u["full_name"], "role": u["role"],
          "active": u["is_active"],
          "last login": u["last_login_at"].strftime("%Y-%m-%d %H:%M") if u["last_login_at"] else "—"}
         for u in users],
        use_container_width=True, hide_index=True,
    )

    with st.expander("➕  Add a user"):
        with st.form("add_user"):
            ne = st.text_input("Email")
            nn = st.text_input("Full name")
            nr = st.selectbox("Role", ["viewer", "admin", "super_admin"])
            np = st.text_input("Temporary password", type="password")
            add = st.form_submit_button("Create user")
        if add:
            if not ne or not np:
                st.error("Email and password are required.")
            else:
                try:
                    _query(
                        "INSERT INTO app_users (email, password_hash, full_name, role, created_by) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (ne.strip().lower(), hash_password(np), nn.strip() or None, nr,
                         st.session_state.user["id"]),
                        fetch=None, commit=True,
                    )
                    st.success(f"Created {ne}. Share the password over a private channel.")
                    st.rerun()
                except psycopg2.errors.UniqueViolation:
                    st.error("That email already exists.")

    with st.expander("🔧  Deactivate / reactivate / reset password"):
        others = [u for u in users if u["id"] != st.session_state.user["id"]]
        if not others:
            st.caption("No other users yet.")
        for u in others:
            c1, c2, c3 = st.columns([3, 1, 2])
            c1.write(f"**{u['email']}** · {u['role']}")
            label = "Deactivate" if u["is_active"] else "Reactivate"
            if c2.button(label, key=f"tog{u['id']}"):
                _query("UPDATE app_users SET is_active = NOT is_active WHERE id = %s",
                       (u["id"],), fetch=None, commit=True)
                st.rerun()
            newp = c3.text_input("new password", key=f"pw{u['id']}", type="password",
                                 label_visibility="collapsed", placeholder="new password")
            if c3.button("Reset", key=f"rst{u['id']}") and newp:
                _query("UPDATE app_users SET password_hash = %s, password_changed_at = NOW() "
                       "WHERE id = %s", (hash_password(newp), u["id"]),
                       fetch=None, commit=True)
                st.success(f"Password reset for {u['email']}.")


# ─────────────────────────────── chat screen ────────────────────────────────
SUGGESTIONS = [
    "Last week's sales for Cypress",
    "Which location had the highest revenue yesterday?",
    "Network revenue by location for the last 7 days",
    "Airtex drive-through vs in-store split this month",
    "Top 5 selling products across all locations last week",
    "Which locations had the slowest kitchen times yesterday?",
]


def query_log_panel():
    """Super-admin only: what was asked and the SQL the assistant ran."""
    st.subheader("Query log")
    c1, c2 = st.columns([3, 1])
    needle = c1.text_input("Filter by question text", placeholder="e.g. cypress")
    limit = c2.number_input("Show last", min_value=20, max_value=1000, value=100, step=20)

    rows = _query(
        "SELECT l.asked_at, u.email, l.question, l.generated_sql, l.row_count, "
        "       l.error, l.duration_ms, l.model, l.steps "
        "FROM chat_query_log l JOIN app_users u ON u.id = l.user_id "
        "WHERE (%s = '' OR l.question ILIKE '%%' || %s || '%%') "
        "ORDER BY l.asked_at DESC LIMIT %s",
        (needle, needle, int(limit)),
    )
    if not rows:
        st.caption("No questions logged yet.")
        return

    st.dataframe(
        [{"when": r["asked_at"].strftime("%Y-%m-%d %H:%M"),
          "user": r["email"],
          "question": r["question"],
          "rows": r["row_count"],
          "ms": r["duration_ms"],
          "error": r["error"] or ""}
         for r in rows],
        use_container_width=True, hide_index=True,
    )

    st.markdown("###### Detail")
    for r in rows[:60]:
        head = f"{r['asked_at']:%m-%d %H:%M} · {r['email']} · {r['question'][:80]}"
        with st.expander(head):
            steps = (r["steps"] or {}).get("steps") if isinstance(r["steps"], dict) else None
            if steps:
                for i, s in enumerate(steps, 1):
                    st.caption(f"query {i} · "
                               + (f"{s['row_count']} rows" if s.get("error") is None
                                  else f"error: {s['error']}"))
                    st.code(s["sql"], language="sql")
            elif r["generated_sql"]:
                st.code(r["generated_sql"], language="sql")
            else:
                st.caption("No SQL was run for this question.")
            ans = (r["steps"] or {}).get("answer") if isinstance(r["steps"], dict) else None
            if ans:
                st.markdown("**Answer given:**")
                st.markdown(ans)


def _auto_title(first_prompt: str) -> str:
    title = " ".join(first_prompt.strip().split())
    return title[:57] + "…" if len(title) > 60 else (title or "New chat")


def _day_bucket(ts):
    """Bucket a conversation's updated_at into a chat-history date group.

    Mirrors how the list is already ordered (updated_at DESC), so buckets come
    out in order and never interleave. Naive timestamps are treated as UTC —
    updated_at is TIMESTAMPTZ, so psycopg2 hands these back tz-aware.
    """
    now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.utcnow()
    days = (now.date() - ts.date()).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days <= 7:
        return "Previous 7 days"
    if days <= 30:
        return "Previous 30 days"
    return ts.strftime("%B %Y")


def history_sidebar(user):
    with st.sidebar:
        # Conversation navigation only. Branding, "New chat", "Sign out" and
        # the model control all live in the top bar — the rail holds no
        # form widgets at all.
        st.markdown('<div class="sb-head">Conversations</div>', unsafe_allow_html=True)
        convos = list_conversations(user["id"])
        if not convos:
            st.markdown(
                '<div class="sb-empty"><i></i><b>No conversations yet</b>'
                '<small>Start a new chat and it will appear here.</small></div>',
                unsafe_allow_html=True,
            )
        else:
            active_id = st.session_state.get("conversation_id")
            with st.container(key="convolist"):
                seen_bucket = None
                for c in convos:
                    bucket = _day_bucket(c["updated_at"])
                    if bucket != seen_bucket:
                        st.markdown(f'<div class="sb-daygroup">{bucket}</div>',
                                    unsafe_allow_html=True)
                        seen_bucket = bucket
                    active = c["id"] == active_id
                    title_col, del_col = st.columns([6, 1])
                    with title_col:
                        if st.button(c["title"], key=f"convo{c['id']}", use_container_width=True,
                                    type="primary" if active else "secondary",
                                    help=c["title"]):
                            st.session_state.conversation_id = c["id"]
                            st.session_state.messages = load_messages(c["id"])
                            st.session_state.model_choice = c.get("model") or chat_sql.MODEL
                            st.session_state.pop("pending", None)
                            st.rerun()
                    with del_col:
                        with st.container(key=f"delwrap{c['id']}"):
                            if st.button("✕", key=f"del{c['id']}", use_container_width=True,
                                        help="Delete this chat"):
                                delete_conversation(c["id"], user["id"])
                                if active:
                                    st.session_state.messages = []
                                    st.session_state.conversation_id = None
                                st.rerun()

        # ── flexible space, then the utility rows at the floor ───────────
        st.markdown('<div class="sb-spacer"></div>', unsafe_allow_html=True)
        st.markdown('<div class="sb-divider"></div>', unsafe_allow_html=True)

        with st.container(key="upwrap"):
            up = st.file_uploader("Import a chat (.json)", type=["json"], key="upload_convo",
                                  label_visibility="collapsed")
        st.markdown('<div class="sb-uphint">JSON · max 2 MB</div>', unsafe_allow_html=True)

        cid = st.session_state.get("conversation_id")
        full = load_conversation_full(cid, user["id"]) if cid else None
        with st.container(key="dl_convo"):
            st.download_button(
                "Export conversation",
                data=json.dumps(full, indent=2) if full else "",
                file_name=f"chat_{cid}.json" if cid else "chat.json",
                mime="application/json", use_container_width=True,
                key="dl_convo_btn", disabled=not full,
                help=None if full else "Open or start a chat to export it.",
            )

        if up is not None and st.session_state.get("_last_import") != up.file_id:
            try:
                data = json.loads(up.getvalue().decode("utf-8"))
                msgs = data.get("messages", [])
                title = data.get("title") or "Imported chat"
                new_id = import_conversation(user["id"], title, msgs, model=data.get("model"))
                st.session_state._last_import = up.file_id
                st.session_state.conversation_id = new_id
                st.session_state.messages = load_messages(new_id)
                st.session_state.model_choice = get_conversation_model(new_id) or chat_sql.MODEL
                st.success(f"Imported “{title}”.")
                st.rerun()
            except (json.JSONDecodeError, KeyError, TypeError):
                st.error("That file doesn't look like an exported chat.")


def _model_picker():
    """The model control, moved out of the sidebar and into the top bar as a
    compact pill. Presentation only — the same selectbox, key and
    session_state/set_conversation_model logic as before."""
    options = chat_sql.AVAILABLE_MODELS
    current = st.session_state.get("model_choice", chat_sql.MODEL)
    idx = options.index(current) if current in options else 0
    with st.container(key="modelpill"):
        chosen = st.selectbox(
            "Model", options, index=idx, key="model_select",
            label_visibility="collapsed",
            help="Applies to this chat, from your next message on.",
        )
    if chosen != current:
        st.session_state.model_choice = chosen
        if st.session_state.get("conversation_id"):
            set_conversation_model(st.session_state.conversation_id, chosen)
        st.rerun()


def _topbar(is_admin):
    cols = (st.columns([3.1, 2.1, 1.4, 1.4, 1.4]) if is_admin
            else st.columns([3.6, 2.2, 1.5, 1.5]))
    with cols[0]:
        st.markdown(
            '<div class="topbar-title">'
            f'<div class="brand-card sm"><img src="{LOGO_URI}" alt="AYG"></div>'
            '<h1>Laynes Intelligence</h1></div>', unsafe_allow_html=True)
    with cols[1]:
        _model_picker()
    if cols[2].button("New chat", use_container_width=True):
        st.session_state.messages = []
        st.session_state.conversation_id = None
        st.session_state.view = "chat"
        st.rerun()
    if cols[3].button("Sign out", use_container_width=True):
        delete_session(st.session_state.get("session_token"))
        _clear_session_cookie()
        st.session_state.clear()
        st.rerun()
    if is_admin:
        on_users = st.session_state.get("view") == "users"
        if cols[4].button("Chat" if on_users else "Admin", use_container_width=True):
            st.session_state.view = "chat" if on_users else "users"
            st.rerun()
    st.markdown('<div class="topbar-div"></div>', unsafe_allow_html=True)


def _welcome_and_suggestions(clickable: bool):
    st.markdown(
        '<div class="welcome">'
        f'<div class="brand-card sm" style="display:inline-block"><img src="{LOGO_URI}" alt="AYG"></div>'
        '<h2>What would you like to know?</h2>'
        '<p>Ask about sales, orders, products, kitchen times or weather — '
        'across all 12 locations.</p></div>',
        unsafe_allow_html=True,
    )
    with st.container(key="sugwrap"):
        cols = st.columns(2)
        for i, s in enumerate(SUGGESTIONS):
            if cols[i % 2].button(s, use_container_width=True, key=f"sug{i}",
                                  disabled=not clickable):
                st.session_state.pending = s
                st.rerun()


def chat_screen():
    user = st.session_state.user
    is_admin = user["role"] == "super_admin"
    if st.session_state.get("session_token"):
        _set_session_cookie(st.session_state.session_token)
    history_sidebar(user)
    _topbar(is_admin)

    if st.session_state.get("view") == "users" and is_admin:
        tab_users, tab_log = st.tabs(["Users", "Query log"])
        with tab_users:
            user_admin_panel()
        with tab_log:
            query_log_panel()
        return

    configured = bool(os.getenv("ANTHROPIC_API_KEY"))

    if not st.session_state.messages:
        _welcome_and_suggestions(clickable=configured)
        if not configured:
            st.markdown(
                '<div class="notice">The assistant isn\'t switched on yet — an '
                '<code>ANTHROPIC_API_KEY</code> needs to be added to the server\'s '
                '<code>.env</code>, then the service restarted.</div>',
                unsafe_allow_html=True,
            )
            return

    for m in st.session_state.messages:
        with st.chat_message(m["role"], avatar="🧑‍💼" if m["role"] == "user" else "🐔"):
            st.markdown(m["content"])

    prompt = st.chat_input("Ask about the business…") or st.session_state.pop("pending", None)
    if not prompt:
        if not st.session_state.messages:
            st.markdown('<div class="inhint">Answers are generated from live data · '
                        'every question is logged</div>', unsafe_allow_html=True)
        return

    is_new_conversation = st.session_state.get("conversation_id") is None
    model_choice = st.session_state.get("model_choice", chat_sql.MODEL)
    if is_new_conversation:
        st.session_state.conversation_id = create_conversation(
            user["id"], _auto_title(prompt), model=model_choice)
    conversation_id = st.session_state.conversation_id

    st.session_state.messages.append({"role": "user", "content": prompt})
    save_message(conversation_id, "user", prompt)
    with st.chat_message("user", avatar="🧑‍💼"):
        st.markdown(prompt)

    with st.chat_message("assistant", avatar="🐔"):
        with st.spinner("Crunching the numbers…"):
            history = [{"role": m["role"], "content": m["content"]}
                       for m in st.session_state.messages[:-1]]
            t0 = time.time()
            err = None
            try:
                result = chat_sql.answer_question(history, prompt, model=model_choice)
                answer, steps = result.answer, result.steps
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                answer, steps = ("Something went wrong reaching the assistant. "
                                 "Try again in a moment."), []
            dur = int((time.time() - t0) * 1000)
        st.markdown(answer)

    st.session_state.messages.append({"role": "assistant", "content": answer})
    save_message(conversation_id, "assistant", answer)
    touch_conversation(conversation_id, title=None if not is_new_conversation else _auto_title(prompt))
    log_chat(user["id"], prompt, steps, answer, err, dur, conversation_id=conversation_id,
             model=model_choice)


# ─────────────────────────────── entrypoint ─────────────────────────────────
st.session_state.setdefault("messages", [])
st.session_state.setdefault("pending", None)
st.session_state.setdefault("conversation_id", None)
st.session_state.setdefault("model_choice", chat_sql.MODEL)

if "user" not in st.session_state:
    login_screen()
else:
    chat_screen()
