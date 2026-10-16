"""Offline Kit question app.

A local model (Ollama) answers questions. It first searches the offline
libraries (kiwix-serve), reads the best articles, and cites them.
Python standard library only. Binds to 127.0.0.1.
"""
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
KIWIX = os.environ.get("KIWIX", "http://127.0.0.1:8090")
OLLAMA = os.environ.get("OLLAMA", "http://127.0.0.1:11434")
MODEL = os.environ.get("KIT_MODEL", "qwen3.5:9b")
PORT = int(os.environ.get("PORT", "8091"))
NUM_CTX = int(os.environ.get("KIT_CTX", "16384"))
MAX_STEPS = 6
# Free the model memory 5 minutes after the last question (reload takes about 4 seconds).
KEEP_ALIVE = os.environ.get("KIT_KEEP_ALIVE", "5m")
ARTICLE_CHARS = 6000

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Full-text search across all offline libraries (Wikipedia, medicine, repair, survival guides). "
            "Use short keyword queries, like an encyclopedia search. Returns titles, ids and snippets.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "2 to 6 keywords"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read one article from a search result. Returns the parts of the article most relevant to the focus.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "The id from a search result, like S3"},
                    "focus": {"type": "string", "description": "What you need from the article, in a few words"},
                },
                "required": ["id"],
            },
        },
    },
]

SYSTEM = """You are Offline Kit, a careful assistant on a laptop with no internet.
You have offline libraries: {books}.

How to answer:
1. The app has already searched the libraries and put the best extracts in the user message. Base your answer on them.
2. If the extracts miss something important, call `read` on another result id, or `search` with other words.
3. Answer in plain language. Lead with what to do or the direct answer. Use short numbered steps for procedures.
4. Cite sources inline as [S1], [S2] with the ids you read. Cite only what you read.
5. If the libraries do not cover it, say so in one line, then answer from your own knowledge and mark that part "(from model memory, not verified)".
6. For medical emergencies, give the first-aid steps from the sources, and say when to get professional help if it is reachable.
For small talk or rewriting text, answer directly without tools."""


def http_json(url, payload=None, timeout=600):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def get_text(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


class TextExtractor(HTMLParser):
    """Turn an article page into paragraphs of plain text."""

    SKIP = {"script", "style", "noscript", "sup", "nav", "footer", "table", "figure", "math"}
    BLOCK = {"p", "li", "h1", "h2", "h3", "h4", "dd", "dt", "pre", "blockquote", "tr", "div", "br"}
    STOP_SECTIONS = {"references", "external links", "further reading", "notes", "see also", "bibliography", "sources", "citations"}

    def __init__(self):
        super().__init__()
        self.parts, self.buf, self.skip, self.stopped, self.in_h = [], [], 0, False, None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag in self.BLOCK:
            self.flush()
        if tag in ("h2", "h3"):
            self.in_h = tag

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag in self.BLOCK:
            heading = self.in_h == tag
            text = self.flush(heading=heading)
            if heading:
                self.in_h = None
                if text and text.strip("# ").lower() in self.STOP_SECTIONS:
                    self.stopped = True

    def handle_data(self, data):
        if not self.skip and not self.stopped:
            self.buf.append(data)

    def flush(self, heading=False):
        text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        self.buf = []
        if text and not self.stopped:
            text = "## " + text if heading else text
            self.parts.append(text)
            return text
        return None


def article_text(path):
    page = get_text(KIWIX + path)
    p = TextExtractor()
    p.feed(page)
    p.flush()
    return [t for t in p.parts if len(t) > 1]


def pick_relevant(paras, focus, limit=ARTICLE_CHARS):
    """Keep the lead section, then the paragraphs that best match the focus words."""
    words = {w for w in re.findall(r"[a-z0-9]{3,}", (focus or "").lower())}
    lead, rest, seen_h = [], [], False
    for i, t in enumerate(paras):
        if t.startswith("## "):
            seen_h = True
        (rest if seen_h else lead).append((i, t))
    out, size = [], 0
    for i, t in lead:
        if size > limit * 0.4:
            break
        out.append((i, t))
        size += len(t)
    scored = sorted(rest, key=lambda it: -sum(it[1].lower().count(w) for w in words)) if words else rest
    heading_for = {}
    current = None
    for i, t in rest:
        if t.startswith("## "):
            current = t
        heading_for[i] = current
    for i, t in scored:
        if size >= limit:
            break
        if t.startswith("## "):
            continue
        out.append((i, t))
        size += len(t)
    out.sort()
    lines, last_h = [], None
    for i, t in out:
        h = heading_for.get(i)
        if h and h != last_h:
            lines.append(h)
            last_h = h
        lines.append(t)
    text = "\n".join(lines)
    return text[:limit] + (" ..." if len(text) > limit else "")


def books():
    """Names and titles of the libraries kiwix-serve has loaded."""
    try:
        xml = get_text(KIWIX + "/catalog/v2/entries?count=-1", timeout=5)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(xml)
        out = []
        for e in root.findall("a:entry", ns):
            name = e.findtext("a:name", default="", namespaces=ns)
            title = e.findtext("a:title", default=name, namespaces=ns)
            out.append((name, title))
        return out
    except Exception:
        return []


class Session:
    """One question: holds the search results and the sources that were read."""

    def __init__(self, emit):
        self.emit = emit
        self.results = {}  # id -> {title, path, book}
        self.read_ids = []
        self.n = 0

    def search(self, query):
        q = urllib.parse.urlencode({"pattern": query, "format": "xml", "pageLength": 8, "books.filter.lang": "eng"})
        self.emit({"type": "status", "text": f"Searching the libraries: {query}"})
        try:
            xml = get_text(f"{KIWIX}/search?{q}")
            root = ET.fromstring(xml)
        except Exception as e:
            if not getattr(self, "warned_off", False):
                self.warned_off = True
                self.emit({"type": "warning", "text": "The library server is off, so this answer is from model memory only. Run ./kit start to turn the libraries on."})
            return f"Search failed: {e}. The library server may be off."
        lines = []
        for item in root.iter("item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            snippet = re.sub(r"<[^>]+>", "", html.unescape(item.findtext("description", "") or ""))
            book = item.findtext("book/title", "") or ""
            if not link:
                continue
            known = next((k for k, v in self.results.items() if v["path"] == link), None)
            if known:
                continue
            self.n += 1
            sid = f"S{self.n}"
            self.results[sid] = {"title": title, "path": link, "book": book}
            lines.append(f"{sid} | {title} | {book} | {snippet[:240]}")
        return "\n".join(lines) if lines else "No results. Try other keywords."

    def read(self, sid, focus=""):
        r = self.results.get(sid.strip())
        if not r:
            return f"Unknown id {sid}. Use an id from the search results."
        self.emit({"type": "status", "text": f"Reading: {r['title']}"})
        try:
            text = pick_relevant(article_text(r["path"]), focus)
        except Exception as e:
            return f"Could not read the article: {e}"
        if sid not in self.read_ids:
            self.read_ids.append(sid)
            self.emit({"type": "source", "id": sid, "title": r["title"], "book": r["book"], "url": KIWIX + r["path"]})
        return f"[{sid}] {r['title']} ({r['book']})\n{text}"


def chat_once(messages, schema=None, think=False):
    """One non-streamed model call. With a schema, the reply is JSON that fits it."""
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "think": bool(think),
        "keep_alive": KEEP_ALIVE,
        "options": {"num_ctx": NUM_CTX, "temperature": 0},
    }
    if schema:
        payload["format"] = schema
    with http_json(OLLAMA + "/api/chat", payload) as r:
        out = json.loads(r.read())
    if out.get("error"):
        raise RuntimeError(out["error"])
    text = out.get("message", {}).get("content", "")
    if not schema:
        return text
    try:
        return json.loads(text)
    except ValueError:
        return {}


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "needs_library": {"type": "boolean"},
        "queries": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    },
    "required": ["needs_library", "queries"],
}

PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "read": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "focus": {"type": "string"},
    },
    "required": ["read", "focus"],
}


def transcript(messages):
    return "\n".join(f"{m['role'].upper()}: {m['content'][:1500]}" for m in messages[-6:])


def plan(messages):
    """Stage 1: the model turns the question into encyclopedia search terms."""
    prompt = (
        "Conversation so far:\n" + transcript(messages) + "\n\n"
        "Decide if the LAST user message needs facts from an offline encyclopedia and practical guides "
        "(any factual, medical, technical, scientific, historical or how-to question needs it; "
        "small talk, rewriting or arithmetic does not). "
        "If it does, write 1 to 3 short keyword search queries (2 to 5 words each, like encyclopedia titles), "
        "the most specific first. Return JSON."
    )
    out = chat_once([{"role": "user", "content": prompt}], PLAN_SCHEMA)
    queries = [q.strip() for q in out.get("queries", []) if isinstance(q, str) and q.strip()][:3]
    needs = out.get("needs_library", True)
    if needs and not queries:
        queries = [messages[-1]["content"][:80]]
    return needs, queries


def pick(s, messages, listing):
    """Stage 3: the model picks the articles worth reading."""
    prompt = (
        "Question: " + messages[-1]["content"][:1500] + "\n\nSearch results (id | title | library | snippet):\n"
        + listing + "\n\nPick the 1 to 3 ids whose full articles best answer the question. "
        "Prefer practical guides and medical libraries for how-to and health questions. "
        "Also say in a few words what to look for in them (focus). Return JSON."
    )
    out = chat_once([{"role": "user", "content": prompt}], PICK_SCHEMA)
    ids = [i.strip() for i in out.get("read", []) if isinstance(i, str) and i.strip() in s.results][:3]
    if not ids:
        ids = list(s.results)[:2]
    return ids, str(out.get("focus", ""))


def answer(messages, think, emit):
    s = Session(emit)
    names = ", ".join(t for _, t in books()) or "none loaded"
    emit({"type": "status", "text": "Planning the search..."})
    needs, queries = plan(messages)
    context = ""
    if needs:
        listing = "\n".join(s.search(q) for q in queries)
        if s.results:
            emit({"type": "status", "text": "Choosing articles..."})
            ids, focus = pick(s, messages, listing)
            focus = focus + " " + messages[-1]["content"]
            extracts = [s.read(i, focus) for i in ids]
            context = (
                "Library extracts for this question (cite them as [S#]):\n\n" + "\n\n---\n\n".join(extracts)
                + "\n\nOther search results you may `read` if the extracts miss something:\n" + listing
            )
        else:
            context = "The library search found nothing for: " + "; ".join(queries) + ". You may `search` with other words."

    chat = [{"role": "system", "content": SYSTEM.format(books=names)}] + messages[:-1]
    last = messages[-1]["content"]
    chat.append({"role": "user", "content": (context + "\n\nQuestion: " + last) if context else last})

    for step in range(MAX_STEPS + 1):
        payload = {
            "model": MODEL,
            "messages": chat,
            "stream": True,
            "think": bool(think),
            "keep_alive": KEEP_ALIVE,
            "options": {"num_ctx": NUM_CTX, "temperature": 0.3},
        }
        if step < MAX_STEPS:
            payload["tools"] = TOOLS
        content, calls = [], []
        emit({"type": "status", "text": "Writing the answer..."})
        with http_json(OLLAMA + "/api/chat", payload) as r:
            for line in r:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise RuntimeError(chunk["error"])
                msg = chunk.get("message", {})
                if msg.get("thinking"):
                    emit({"type": "thinking", "text": msg["thinking"]})
                if msg.get("content"):
                    content.append(msg["content"])
                    emit({"type": "token", "text": msg["content"]})
                calls += msg.get("tool_calls") or []
        if not calls:
            fake = sorted(set(re.findall(r"\[(S\d+)\]", "".join(content))) - set(s.read_ids))
            if fake:
                emit({"type": "warning", "text": "The answer cites " + ", ".join(fake) + ", which the model did not read. Treat those parts as unverified."})
            return
        if content:
            emit({"type": "clear"})  # text before a tool call is a plan, not the answer
        chat.append({"role": "assistant", "content": "".join(content), "tool_calls": calls})
        for c in calls:
            fn = c.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            if fn.get("name") == "search":
                out = s.search(str(args.get("query", "")))
            elif fn.get("name") == "read":
                out = s.read(str(args.get("id", "")), str(args.get("focus", "")))
            else:
                out = "Unknown tool."
            chat.append({"role": "tool", "tool_name": fn.get("name", ""), "content": out})


MAX_BODY = 1_000_000  # bytes
MAX_MESSAGE = 20_000  # characters per message


class Handler(BaseHTTPRequestHandler):
    def allowed(self):
        """Only this machine, by name. Blocks other websites (DNS rebinding) from using the app."""
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def log_message(self, fmt, *args):
        sys.stderr.write("%s\n" % (fmt % args))

    def send(self, code, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if not self.allowed():
            return self.send(403, "forbidden", "text/plain")
        if self.path == "/health":
            return self.send(200, json.dumps({"ok": True, "model": MODEL}), "application/json")
        if self.path == "/api/info":
            return self.send(200, json.dumps({"model": MODEL, "kiwix": KIWIX, "books": books()}), "application/json")
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        self.send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/ask":
            return self.send(404, "not found", "text/plain")
        # JSON only: a browser cannot send JSON to another site without permission (CORS), so other
        # websites cannot make this Mac run the model.
        if not self.allowed() or not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send(403, "forbidden", "text/plain")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self.send(413, "too large", "text/plain")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            messages = [
                {"role": m["role"], "content": str(m.get("content", ""))[:MAX_MESSAGE]}
                for m in body.get("messages", [])
                if isinstance(m, dict) and m.get("role") in ("user", "assistant")
            ][-8:]
        except (ValueError, AttributeError, TypeError):
            return self.send(400, "bad request", "text/plain")
        if not messages or messages[-1]["role"] != "user":
            return self.send(400, "bad request", "text/plain")
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def emit(ev):
            self.wfile.write((json.dumps(ev) + "\n").encode())
            self.wfile.flush()

        try:
            answer(messages, body.get("think", False), emit)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            try:
                emit({"type": "error", "text": f"{type(e).__name__}: {e}"})
            except Exception:
                return
        try:
            emit({"type": "done"})
        except Exception:
            pass


if __name__ == "__main__":
    print(f"Offline Kit on http://127.0.0.1:{PORT} (model {MODEL}, library {KIWIX})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
