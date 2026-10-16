"""End-to-end check: ask real questions, require sources and an answer.

Run: ./kit doctor   (the app must be on)
"""
import json
import sys
import time
import urllib.request

APP = "http://127.0.0.1:" + (sys.argv[1] if len(sys.argv) > 1 else "8091")
QUESTIONS = [
    "How do I make water safe to drink if I have no filter?",
    "What are the signs of a heart attack and what should I do first?",
]


def ask(q):
    req = urllib.request.Request(
        APP + "/api/ask",
        data=json.dumps({"messages": [{"role": "user", "content": q}]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    answer, sources, errors, t0 = "", [], [], time.time()
    with urllib.request.urlopen(req, timeout=900) as r:
        for line in r:
            ev = json.loads(line)
            if ev["type"] == "token":
                answer += ev["text"]
            elif ev["type"] == "clear":
                answer = ""
            elif ev["type"] == "source":
                sources.append(f"{ev['id']} {ev['title']} ({ev['book']})")
            elif ev["type"] == "warning":
                errors.append(ev["text"])
            elif ev["type"] == "error":
                errors.append(ev["text"])
    return answer.strip(), sources, errors, time.time() - t0


failed = 0
for q in QUESTIONS:
    answer, sources, errors, secs = ask(q)
    ok = answer and sources and not errors and "[S" in answer
    failed += not ok
    print(f"\n{'PASS' if ok else 'FAIL'}  {q}  ({secs:.0f} s)")
    print("  sources: " + ("; ".join(sources) or "none"))
    if errors:
        print("  errors: " + "; ".join(errors))
    print("  answer: " + answer[:400].replace("\n", " ") + ("..." if len(answer) > 400 else ""))
sys.exit(1 if failed else 0)
