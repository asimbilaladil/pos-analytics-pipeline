#!/usr/bin/env python3
"""The chat page on a phone that locks, sleeps, goes offline or is killed.

Run:  venv/bin/python tests/test_chat_mobile_frontend.py     (exit 0 = pass)
Needs: frontend/node_modules (npm ci) and Playwright's Chromium.

The REAL built frontend runs in Chromium against a mocked API whose job state
the test controls, so every case is deterministic and makes no model call.
A sleeping phone is simulated the way it really behaves: document hidden and
the page FROZEN through the DevTools lifecycle API (JS, timers and fetch all
suspended), then woken. Nothing here keeps a page awake.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
os.chdir(ROOT)
from playwright.async_api import async_playwright  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Build the current frontend into a temp dir and serve it (SPA fallback).
DIST = tempfile.mkdtemp(prefix="laynes-dist-")
b = subprocess.run(["npx", "vite", "build", "--outDir", DIST, "--emptyOutDir"],
                   cwd=os.path.join(ROOT, "frontend"), capture_output=True, text=True)
if b.returncode != 0:
    print("frontend build failed:\n" + (b.stderr or b.stdout)[-2000:])
    sys.exit(1)


class Static(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if not os.path.isfile(os.path.join(DIST, self.path.split("?")[0].lstrip("/"))):
            self.path = "/index.html"
        return super().do_GET()


httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Static, directory=DIST))
threading.Thread(target=httpd.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{httpd.server_address[1]}"

VIS = """(() => { let v = 'visible';
  Object.defineProperty(Document.prototype, 'visibilityState', {get() { return v }});
  Object.defineProperty(Document.prototype, 'hidden', {get() { return v !== 'visible' }});
  window.__setVis = (x) => { v = x; document.dispatchEvent(new Event('visibilitychange')); };
})();"""


class MockApi:
    """Server-side state the test drives. Mirrors the real API contract:
    a key identifies one submission; resends return its job."""

    def __init__(self):
        self.convs, self.jobs, self.keys = {}, {}, {}
        self.next_id, self.polls, self.posts, self.conv_gets = 100, 0, [], 0
        self.lose_ack = None          # coroutine run between server work and the reply
        self.models = ["m"]
        self.options = None           # server-approved options incl. provider (new API)

    def complete(self, job_id, text="FINAL ANSWER"):
        j = self.jobs[job_id]
        self.convs[j["conversation_id"]]["messages"].append(
            {"role": "assistant", "content": text, "model": j.get("model"),
             "provider": ("openai" if (j.get("model") or "").startswith("gpt-") else "anthropic")})
        j["status"] = "completed"

    async def handle(self, route):
        req = route.request
        path = req.url.split("/api", 1)[1].split("?")[0]
        m = req.method

        def ok(body, status=200):
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        if path == "/auth/me":
            return await ok({"id": 7, "email": "t@x", "full_name": "T", "role": "user", "is_admin": False})
        if path == "/models":
            if self.options is not None:
                provs = {o["provider"] for o in self.options}
                return await ok({"models": [o["id"] for o in self.options], "default": self.models[0],
                                 "options": self.options,
                                 "providers": [{"id": "anthropic", "label": "Claude \u2014 Anthropic",
                                                "enabled": True},
                                               {"id": "openai", "label": "GPT \u2014 OpenAI",
                                                "enabled": "openai" in provs}]})
            return await ok({"models": self.models, "default": self.models[0]})
        if path == "/conversations" and m == "GET":
            return await ok([{"id": c["id"], "title": c["title"], "model": "m",
                              "updated_at": "2026-10-18T00:00:00Z"} for c in self.convs.values()])
        parts = path.strip("/").split("/")
        if parts[0] == "chat" and parts[1] == "jobs":
            self.polls += 1
            j = self.jobs.get(parts[2])
            return await (ok(self.public(j)) if j else ok({"detail": "Job not found"}, 404))
        if parts[0] == "conversations" and len(parts) == 2 and m == "GET":
            self.conv_gets += 1
            c = self.convs.get(int(parts[1]))
            return await (ok(c) if c else ok({"detail": "Conversation not found"}, 404))
        if parts[0] == "conversations" and len(parts) == 3 and parts[2] == "active-job":
            active = [j for j in self.jobs.values() if j["conversation_id"] == int(parts[1])
                      and j["status"] in ("queued", "processing")]
            return await ok(self.public(active[-1]) if active else None)
        if parts[0] == "conversations" and len(parts) == 3 and parts[2] == "messages" and m == "POST":
            body = json.loads(req.post_data or "{}")
            self.posts.append(body)
            key = body.get("idempotency_key")
            if key in self.keys:
                j = self.jobs[self.keys[key]]
            else:
                cid = int(parts[1])
                if cid == 0:
                    cid = self.next_id
                    self.next_id += 1
                    self.convs[cid] = {"id": cid, "title": body["question"][:40], "model": "m", "messages": []}
                self.convs[cid]["messages"].append({"role": "user", "content": body["question"]})
                jid = f"job-{len(self.jobs) + 1}"
                j = {"job_id": jid, "conversation_id": cid, "status": "processing", "attempt": 1,
                     "model": body.get("model")}
                self.jobs[jid], self.keys[key] = j, jid
            acc = {"conversation_id": j["conversation_id"], "title": self.convs[j["conversation_id"]]["title"],
                   "job_id": j["job_id"], "status": j["status"], "user_message_id": 1}
            if self.lose_ack:
                hook, self.lose_ack = self.lose_ack, None
                await hook()
                return await route.abort("connectionreset")
            return await ok(acc, 202)
        return await ok({"detail": "unmocked " + path}, 404)

    @staticmethod
    def public(j):
        j = {k: v for k, v in j.items() if k != "model"}
        return {**j, "created_at": None, "completed_at": None, "duration_ms": None,
                "error_code": j.get("error_code"), "error_message": j.get("error_message")}


async def ui(p):
    # Counted inside the transcript only: the sidebar lists the title too.
    return await p.evaluate("""() => { const t = (document.querySelector('.space-y-7') || {}).innerText || ''; return {
      thinking: t.includes('Analyzing your data'),
      answers: (t.match(/FINAL ANSWER/g) || []).length,
      questions: (t.match(/How did P10 go\\?/g) || []).length,
      error: (document.querySelector('.bg-amber-50') || {}).innerText || null,
      retry: !!Array.from(document.querySelectorAll('button')).find(b => b.textContent === 'Retry') } }""")


async def until(p, pred, secs=8):
    t = time.time()
    while time.time() - t < secs:
        u = await ui(p)
        if pred(u):
            return u
        await asyncio.sleep(0.1)
    return await ui(p)


Q = "How did P10 go?"


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()

        async def fresh(api, ctx=None):
            ctx = ctx or await browser.new_context(viewport={"width": 390, "height": 844},
                                                   is_mobile=True, has_touch=True)
            await ctx.add_init_script(VIS)
            await ctx.route("**/api/**", api.handle)
            p = await ctx.new_page()
            await p.goto(BASE + "/")
            await p.wait_for_selector("textarea")
            return ctx, p, await ctx.new_cdp_session(p)

        async def sleep_phone(cdp, p):
            await p.evaluate("window.__setVis('hidden')")
            await cdp.send("Page.setWebLifecycleState", {"state": "frozen"})

        async def wake_phone(cdp, p):
            await cdp.send("Page.setWebLifecycleState", {"state": "active"})
            await p.evaluate("window.__setVis('visible')")

        async def ask(p):
            await p.fill("textarea", Q)
            await p.keyboard.press("Enter")

        # C / D: sleep after the 202, job completes while asleep
        api = MockApi()
        ctx, p, cdp = await fresh(api)
        await ask(p)
        await until(p, lambda u: u["thinking"])
        await asyncio.sleep(3)
        await sleep_phone(cdp, p)
        polls0 = api.polls
        await asyncio.sleep(6)
        check("C. a hidden/frozen page sends no polls", api.polls == polls0, f"{api.polls - polls0}")
        api.complete("job-1")
        await wake_phone(cdp, p)
        u = await until(p, lambda u: u["answers"] == 1 and not u["thinking"], 3)
        check("D. wake after completion: the answer appears at once, once",
              u["answers"] == 1 and not u["thinking"] and u["questions"] == 1, str(u))
        await ctx.close()

        # E: wake while still processing -> polling resumes, then completes
        api = MockApi()
        ctx, p, cdp = await fresh(api)
        await ask(p)
        await until(p, lambda u: u["thinking"])
        await sleep_phone(cdp, p)
        await asyncio.sleep(3)
        await wake_phone(cdp, p)
        polls0 = api.polls
        await asyncio.sleep(6)
        u = await ui(p)
        check("E. wake while processing: still 'Analyzing' and polling resumed",
              u["thinking"] and api.polls - polls0 >= 2, f"polls={api.polls - polls0}")
        api.complete("job-1")
        u = await until(p, lambda u: u["answers"] == 1 and not u["thinking"])
        check("E. ... then the answer arrives", u["answers"] == 1 and not u["thinking"])
        await ctx.close()

        # I: visibilitychange / focus / pageshow storm -> one polling loop
        api = MockApi()
        ctx, p, cdp = await fresh(api)
        await ask(p)
        await until(p, lambda u: u["thinking"])
        for _ in range(8):
            await p.evaluate("window.__setVis('hidden'); window.__setVis('visible');"
                             "window.dispatchEvent(new Event('focus'));"
                             "window.dispatchEvent(new Event('pageshow'))")
        await asyncio.sleep(1)
        polls0 = api.polls
        await asyncio.sleep(10)
        n = api.polls - polls0
        check("I. many wake-up events -> a single 2.5 s polling loop", 3 <= n <= 5, f"{n} polls in 10 s")
        await ctx.close()

        # 11: wake exactly as the job finishes -> one answer, no stale badge
        api = MockApi()
        ctx, p, cdp = await fresh(api)
        await ask(p)
        await until(p, lambda u: u["thinking"])
        await sleep_phone(cdp, p)
        await asyncio.sleep(1)
        api.complete("job-1")
        await wake_phone(cdp, p)
        await p.evaluate("window.dispatchEvent(new Event('focus')); window.dispatchEvent(new Event('online'))")
        u = await until(p, lambda u: u["answers"] >= 1 and not u["thinking"])
        await asyncio.sleep(3)
        u = await ui(p)
        check("11. waking as the job finishes: one answer, no stale 'Analyzing'",
              u["answers"] == 1 and not u["thinking"] and u["questions"] == 1, str(u))
        check("11. ... and the job was settled once (one transcript reload after completion)",
              api.conv_gets <= 3, f"conversation GETs={api.conv_gets}")
        await ctx.close()

        # G: the phone locks before the 202 is read (connection dies)
        api = MockApi()
        ctx, p, cdp = await fresh(api)

        async def lost():
            await sleep_phone(cdp, p)
            await asyncio.sleep(2)
            api.complete("job-1")          # server finished while the phone slept
            asyncio.get_running_loop().call_later(0.5, lambda: asyncio.ensure_future(wake_phone(cdp, p)))
        api.lose_ack = lost
        await ask(p)
        u = await until(p, lambda u: u["answers"] == 1 and not u["thinking"], 12)
        keys = [x.get("idempotency_key") for x in api.posts]
        check("G. lost 202: the client re-sends with the SAME key",
              len(keys) == 2 and keys[0] == keys[1], str(keys))
        check("G. ... and shows the existing answer: no error, no Retry, no duplicate",
              u["answers"] == 1 and u["questions"] == 1 and not u["retry"] and not u["error"]
              and len(api.jobs) == 1 and len(api.convs) == 1, str(u))
        await ctx.close()

        # F: app killed after the 202 -> reopen shows the answer
        api = MockApi()
        ctx, p, cdp = await fresh(api)
        await ask(p)
        await until(p, lambda u: u["thinking"])
        await p.close()
        api.complete("job-1")
        p2 = await ctx.new_page()
        await p2.goto(BASE + "/")
        u = await until(p2, lambda u: u["answers"] == 1)
        check("F. killed after the 202: reopening the app shows the answer, once",
              u["answers"] == 1 and u["questions"] == 1 and not u["thinking"], str(u))
        await ctx.close()

        # F2: killed BEFORE the 202 was read -> reopen recovers the send by key
        api = MockApi()
        ctx, p, cdp = await fresh(api)

        async def killed():
            await p.close()
        api.lose_ack = killed
        await ask(p)
        await asyncio.sleep(1)
        api.complete("job-1")
        p2 = await ctx.new_page()
        await p2.goto(BASE + "/")
        u = await until(p2, lambda u: u["answers"] == 1)
        keys = [x.get("idempotency_key") for x in api.posts]
        check("F2. killed before the 202: reopening re-sends the same key and shows the answer",
              u["answers"] == 1 and u["questions"] == 1 and len(set(keys)) == 1 and len(api.jobs) == 1,
              f"{u} keys={keys}")
        await ctx.close()

        # H / 12: Wi-Fi -> offline -> online while analyzing
        api = MockApi()
        ctx, p, cdp = await fresh(api)
        await ask(p)
        await until(p, lambda u: u["thinking"])
        await ctx.set_offline(True)
        await asyncio.sleep(4)
        api.complete("job-1")
        await asyncio.sleep(1)
        await ctx.set_offline(False)
        u = await until(p, lambda u: u["answers"] == 1 and not u["thinking"], 8)
        check("H. network drop while analyzing: back online -> answer shown",
              u["answers"] == 1 and not u["thinking"], str(u))
        await ctx.close()

        # K: server restarted mid-job -> failed/interrupted -> message + Retry path
        api = MockApi()
        ctx, p, cdp = await fresh(api)
        await ask(p)
        await until(p, lambda u: u["thinking"])
        api.jobs["job-1"].update(status="failed", error_code="interrupted",
                                 error_message="The server restarted while this answer was being generated. Please try again.")
        u = await until(p, lambda u: u["error"] is not None, 8)
        check("K. an interrupted job (API restart) shows the restart message, not a spinner",
              "server restarted" in (u["error"] or "") and not u["thinking"], str(u))
        await ctx.close()

        # Reopening a past conversation with no active job is not an error
        api = MockApi()
        api.convs[50] = {"id": 50, "title": "Past chat", "model": "m",
                         "messages": [{"role": "user", "content": Q}, {"role": "assistant", "content": "FINAL ANSWER"}]}
        ctx, p, cdp = await fresh(api, await browser.new_context(viewport={"width": 1280, "height": 900}))
        await p.get_by_text("Past chat").first.click()
        u = await until(p, lambda u: u["answers"] == 1, 5)
        await asyncio.sleep(1)
        u = await ui(p)
        check("an idle conversation opens without an error banner (active-job null)",
              u["answers"] == 1 and u["error"] is None, str(u))
        await ctx.close()

        # Model picker on a phone: visible, on screen, and it changes the model sent
        for width in (390, 320):
            api = MockApi()
            api.models = ["claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5-20251001"]
            ctx, p, cdp = await fresh(api, await browser.new_context(
                viewport={"width": width, "height": 800}, is_mobile=True, has_touch=True))
            sel = p.locator("select[aria-label='Assistant model']")
            box = await sel.bounding_box() if await sel.is_visible() else None
            check(f"model picker is visible on a {width}px phone",
                  box is not None and box["x"] >= 0 and box["x"] + box["width"] <= width, str(box))
            boxes = await p.evaluate("""() => Array.from(document.querySelectorAll('header button, header select'))
                .filter(e => e.offsetParent !== null).map(e => { const r = e.getBoundingClientRect();
                  return [r.left, r.right] })""")
            boxes.sort()
            overlap = any(a[1] > b[0] + 0.5 for a, b in zip(boxes, boxes[1:]))
            scroll_w = await p.evaluate("document.documentElement.scrollWidth")
            check(f"{width}px header: controls do not overlap and the page does not scroll sideways",
                  not overlap and scroll_w <= width, f"{boxes} scrollWidth={scroll_w}")
            fs = await sel.evaluate("e => parseFloat(getComputedStyle(e).fontSize)")
            check(f"{width}px: picker text is >= 16px (no iOS zoom on tap)", fs >= 16, str(fs))
            await sel.select_option("claude-opus-5")
            await ask(p)
            await until(p, lambda u: u["thinking"])
            check(f"{width}px: the chosen model is sent with the question",
                  api.posts and api.posts[-1].get("model") == "claude-opus-5",
                  str(api.posts[-1].get("model") if api.posts else None))
            await ctx.close()

        # ── providers: Claude + OpenAI in one picker ──────────────────────
        CLAUDE = [{"id": "claude-sonnet-5", "label": "Sonnet 5", "provider": "anthropic"},
                  {"id": "claude-opus-5", "label": "Opus 5", "provider": "anthropic"},
                  {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5", "provider": "anthropic"}]
        GPT = [{"id": "gpt-6-luna", "label": "GPT-6 Luna", "provider": "openai"},
               {"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol", "provider": "openai"}]

        def provider_api(with_openai=True):
            api = MockApi()
            api.models = ["claude-sonnet-5"]
            api.options = CLAUDE + (GPT if with_openai else [])
            return api

        api = provider_api(with_openai=False)
        ctx, p, cdp = await fresh(api)
        await p.wait_for_selector("header select option", state="attached")
        groups = await p.evaluate("document.querySelectorAll('header select optgroup').length")
        opts = await p.evaluate("Array.from(document.querySelectorAll('header select option')).map(o => o.value)")
        check("OpenAI disabled: picker is the plain Claude list (no groups, no GPT option)",
              groups == 0 and opts == [o["id"] for o in CLAUDE], f"{groups} {opts}")
        check("OpenAI disabled: Claude stays the default selection",
              await p.locator("select[aria-label='Assistant model']").input_value() == "claude-sonnet-5")
        await ctx.close()

        for width in (390, 320, 1280):
            api = provider_api()
            mobile = width < 640
            ctx, p, cdp = await fresh(api, await browser.new_context(
                viewport={"width": width, "height": 800}, is_mobile=mobile, has_touch=mobile))
            sel = p.locator("select[aria-label='Assistant model']")
            # /api/models is fetched after first paint: wait for the options.
            await p.wait_for_selector("header select optgroup", state="attached")
            labels = await p.evaluate("Array.from(document.querySelectorAll('header select optgroup')).map(g => g.label)")
            check(f"{width}px: picker grouped 'Claude \u2014 Anthropic' / 'GPT \u2014 OpenAI'",
                  labels == ["Claude \u2014 Anthropic", "GPT \u2014 OpenAI"], str(labels))
            check(f"{width}px: Claude is still the default", await sel.input_value() == "claude-sonnet-5")
            await sel.select_option("gpt-6-luna")
            box = await sel.bounding_box()
            boxes = await p.evaluate("""() => Array.from(document.querySelectorAll('header button, header select, header [data-testid=selected-provider]'))
                .filter(e => e.offsetParent !== null).map(e => { const r = e.getBoundingClientRect();
                  return [r.left, r.right] })""")
            boxes.sort()
            overlap = any(a[1] > b[0] + 0.5 for a, b in zip(boxes, boxes[1:]))
            scroll_w = await p.evaluate("document.documentElement.scrollWidth")
            check(f"{width}px: GPT selected, header fits (no overlap, no sideways scroll)",
                  box and box["x"] + box["width"] <= width and not overlap and scroll_w <= width,
                  f"{boxes} scrollWidth={scroll_w}")
            title = await sel.get_attribute("title")
            check(f"{width}px: selection names model and provider", title == "GPT-6 Luna \u2014 OpenAI", title)
            tag = p.locator("[data-testid=selected-provider]")
            if width >= 768:
                check(f"{width}px: provider shown next to the picker",
                      await tag.is_visible() and (await tag.inner_text()) == "OpenAI")
            await ask(p)
            await until(p, lambda u: u["thinking"])
            check(f"{width}px: the OpenAI model is sent with the question",
                  api.posts and api.posts[-1].get("model") == "gpt-6-luna")
            # Switching the picker while the job runs must not touch that job.
            await sel.select_option("claude-opus-5")
            await asyncio.sleep(3)
            api.complete("job-1")
            u = await until(p, lambda u: u["answers"] == 1 and not u["thinking"])
            cap = await p.locator("[data-testid=answer-model]").all_inner_texts()
            check(f"{width}px: answer labelled with the model that wrote it",
                  u["answers"] == 1 and cap == ["GPT-6 Luna \u00b7 OpenAI"], str(cap))
            check(f"{width}px: switching mid-job sent nothing new for that job",
                  len(api.posts) == 1 and api.jobs["job-1"]["model"] == "gpt-6-luna")
            await ctx.close()

        # Lost 202 while the picker is changed: the resend keeps the ORIGINAL model
        api = provider_api()
        ctx, p, cdp = await fresh(api)
        sel = p.locator("select[aria-label='Assistant model']")
        await sel.select_option("gpt-6-luna")

        async def lost_and_switch():
            await sleep_phone(cdp, p)
            await asyncio.sleep(1.5)
            api.complete("job-1")
            asyncio.get_running_loop().call_later(0.5, lambda: asyncio.ensure_future(wake_phone(cdp, p)))
        api.lose_ack = lost_and_switch
        await ask(p)
        await asyncio.sleep(0.2)
        try:
            await sel.select_option("claude-haiku-4-5-20251001", timeout=1000)
        except Exception:                     # frozen page: the switch may land after wake
            pass
        u = await until(p, lambda u: u["answers"] == 1 and not u["thinking"], 12)
        sent = [(x.get("idempotency_key"), x.get("model")) for x in api.posts]
        check("lost 202 + picker switched: resend uses the same key AND the original model",
              len(sent) == 2 and sent[0] == sent[1] and sent[0][1] == "gpt-6-luna"
              and u["answers"] == 1 and len(api.jobs) == 1, str(sent))
        await ctx.close()

        # Reopening an OpenAI conversation after OpenAI was disabled
        api = provider_api(with_openai=False)
        api.convs[60] = {"id": 60, "title": "GPT chat", "model": "gpt-6-luna",
                         "messages": [{"role": "user", "content": Q},
                                      {"role": "assistant", "content": "FINAL ANSWER",
                                       "model": "gpt-6-luna", "provider": "openai"}]}
        ctx, p, cdp = await fresh(api, await browser.new_context(viewport={"width": 1280, "height": 900}))
        await p.get_by_text("GPT chat").first.click()
        await until(p, lambda u: u["answers"] == 1, 5)
        val = await p.locator("select[aria-label='Assistant model']").input_value()
        cap = await p.locator("[data-testid=answer-model]").all_inner_texts()
        check("old GPT conversation, OpenAI now off: history intact, picker falls back to a sendable model",
              val == "claude-sonnet-5" and cap == ["GPT-6 Luna \u00b7 OpenAI"], f"{val} {cap}")
        await ctx.close()
        await browser.close()


try:
    asyncio.run(main())
finally:
    httpd.shutdown()
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
