"""Compare models on the real Offline Kit pipeline: quality, speed, energy, memory.

Run: /usr/bin/python3 -I bench/bench.py <model> [<model> ...]
Each model runs on its own app instance on port 8092. The library server must be on.
Results: bench/results/<model>.json. Grade the answers by hand (see grading notes).
"""
import json
import re
import signal
import socket
import os
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OLLAMA = "http://127.0.0.1:11434"
PORT = 8092

# (question, what a good answer must contain)
QUESTIONS = [
    ("How do I make water safe to drink if I have no filter?", "rolling boil 1 min (3 at altitude); bleach about 2 drops 6% per liter, wait 30 min; SODIS/sun; cloth pre-filter"),
    ("How much household bleach do I add to 1 liter of water to disinfect it?", "about 2 drops of 6-8.25% unscented bleach per liter (8 drops per gallon), double if cloudy, wait 30 minutes"),
    ("What are the signs of a heart attack and what should I do first?", "chest pressure, arm/jaw/back pain, breathlessness, sweating, nausea; call emergency, chew aspirin if not allergic, rest, CPR/AED if unresponsive"),
    ("A deep cut on the leg will not stop bleeding. What do I do?", "firm direct pressure, do not remove soaked dressing, add layers, elevate, tourniquet above wound if life-threatening, note time, get help"),
    ("What are the signs of hypothermia and how do I treat it?", "shivering, confusion, slurred speech, drowsiness; move to warmth, remove wet clothes, insulate, warm core first, warm sweet drinks if alert, handle gently"),
    ("Can I tell if a wild mushroom is safe to eat by its smell or color?", "no; no simple rule works; deadly species look and smell fine; do not eat unless expert-identified"),
    ("My car battery is dead and nobody is around. How can I start the car?", "portable jump starter; push/bump start only for manual transmission; correct clamp order"),
    ("How do I fix a flat bicycle tire?", "remove wheel, tire levers, find hole, patch or new tube, check tire for object, reinflate"),
    ("How do I find north without a compass?", "sun rises east sets west; shadow-stick method; North Star via Big Dipper; analog watch method"),
    ("I have 20 liters of water for 3 people. Each person needs 3 liters a day to drink and 1 liter for cooking. How many days will it last?", "3 x 4 = 12 L/day, 20 / 12 = 1.67 days, about 1 day and 16 hours"),
    ("Why is the sky blue?", "Rayleigh scattering, shorter wavelengths scatter more, violet absorbed/eye sensitivity"),
    ("What were the main causes of World War I?", "militarism, alliances, imperialism, nationalism; assassination of Franz Ferdinand June 1914 as trigger"),
]


def get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def post(url, payload, timeout=900):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def gpu_util():
    out = subprocess.run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"], capture_output=True, text=True).stdout
    for part in out.split('"Device Utilization %"=')[1:]:
        digits = "".join(ch for ch in part[:4] if ch.isdigit())
        if digits:
            return int(digits)
    return 0


class GpuMeter:
    """Integrates GPU utilization over time: busy-seconds is the energy estimate."""

    def __init__(self):
        self.busy, self._stop = 0.0, threading.Event()

    def __enter__(self):
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()
        return self

    def run(self):
        last = time.time()
        while not self._stop.is_set():
            time.sleep(0.25)
            now = time.time()
            self.busy += gpu_util() / 100.0 * (now - last)
            last = now

    def __exit__(self, *a):
        self._stop.set()
        self.t.join()


def raw_speed(model):
    """Generation and prompt speed straight from Ollama, same prompt for every model."""
    prompt = "Explain in about 250 words how a lead-acid car battery works and why it can go flat in cold weather."
    with post(OLLAMA + "/api/chat", {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False,
                                     "think": False, "options": {"num_predict": 350, "temperature": 0, "num_ctx": 16384}}) as r:
        d = json.loads(r.read())
    return {
        "gen_tok_s": round(d["eval_count"] / (d["eval_duration"] / 1e9), 1),
        "prompt_tok_s": round(d.get("prompt_eval_count", 0) / max(d.get("prompt_eval_duration", 1) / 1e9, 1e-9), 1),
    }


def footprint_gb():
    """Real memory of the model process as macOS counts it (includes the MLX buffer cache)."""
    pids = subprocess.run(["pgrep", "-f", "ollama runner|llama-server"], capture_output=True, text=True).stdout.split()
    best = 0.0
    for pid in pids:
        out = subprocess.run(["footprint", "-p", pid], capture_output=True, text=True).stdout
        for num, unit in re.findall(r"phys_footprint:\s+([0-9.]+)\s*(GB|MB|KB)", out):
            best = max(best, float(num) / {"GB": 1, "MB": 1000, "KB": 1e6}[unit])
    return round(best, 2)


def loaded_size(model):
    d = json.loads(get(OLLAMA + "/api/ps"))
    for m in d.get("models", []):
        if m["name"] == model or m.get("model") == model:
            return round(m.get("size_vram", m.get("size", 0)) / 1e9, 2)
    return None


def ask(q):
    answer, sources, warnings, errors = "", [], [], []
    with post(f"http://127.0.0.1:{PORT}/api/ask", {"messages": [{"role": "user", "content": q}]}) as r:
        for line in r:
            ev = json.loads(line)
            t = ev["type"]
            if t == "token":
                answer += ev["text"]
            elif t == "clear":
                answer = ""
            elif t == "source":
                sources.append(f"{ev['id']} {ev['title']} ({ev['book']})")
            elif t == "warning":
                warnings.append(ev["text"])
            elif t == "error":
                errors.append(ev["text"])
    return answer.strip(), sources, warnings, errors


def libraries_up():
    try:
        xml = get("http://127.0.0.1:8090/search?pattern=boiling+water&format=xml&pageLength=1&books.filter.lang=eng", 5)
        return b"<item>" in xml
    except Exception:
        return False


def port_free(port):
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def run(model):
    if not libraries_up():
        raise SystemExit("The library server is off or returns no results. Run ./kit serve first.")
    # A leftover app on this port would answer with the wrong model. Refuse to start.
    if not port_free(PORT):
        raise SystemExit(f"Port {PORT} is in use (an old benchmark app?). Stop it first.")
    env = dict(os.environ, KIT_MODEL=model, PORT=str(PORT), KIWIX="http://127.0.0.1:8090")
    app = subprocess.Popen(["/usr/bin/python3", "-I", os.path.join(ROOT, "app", "server.py")], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        health = None
        for _ in range(40):
            try:
                health = json.loads(get(f"http://127.0.0.1:{PORT}/health", 1))
                break
            except Exception:
                time.sleep(0.25)
        if app.poll() is not None or not health or health.get("model") != model:
            raise SystemExit(f"The benchmark app did not start with {model} (health: {health}).")
        t0 = time.time()
        post(OLLAMA + "/api/chat", {"model": model, "messages": [], "keep_alive": "30m"}).read()
        load_s = round(time.time() - t0, 1)
        speed = raw_speed(model)
        rows, peak_mem = [], 0.0
        for q, rubric in QUESTIONS:
            with GpuMeter() as g:
                t = time.time()
                try:
                    a, s, w, e = ask(q)
                except Exception as ex:
                    a, s, w, e = "", [], [], [repr(ex)]
                secs = time.time() - t
            peak_mem = max(peak_mem, footprint_gb())
            rows.append({"q": q, "rubric": rubric, "answer": a, "sources": s, "warnings": w, "errors": e,
                         "seconds": round(secs, 1), "gpu_busy_s": round(g.busy, 1)})
            print(f"  {secs:5.0f}s gpu {g.busy:5.1f}s  src {len(s)}  warn {len(w)}  err {len(e)}  {q[:60]}", flush=True)
        result = {
            "model": model,
            "load_seconds": load_s,
            "memory_gb": peak_mem,
            "ollama_reserved_gb": loaded_size(model),
            **speed,
            "avg_seconds": round(sum(r["seconds"] for r in rows) / len(rows), 1),
            "avg_gpu_busy_s": round(sum(r["gpu_busy_s"] for r in rows) / len(rows), 1),
            "errors": sum(len(r["errors"]) for r in rows),
            "fake_citation_warnings": sum(len(r["warnings"]) for r in rows),
            "no_source_answers": sum(1 for r in rows if not r["sources"]),
            "rows": rows,
        }
        os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
        with open(os.path.join(HERE, "results", model.replace(":", "_").replace("/", "_") + ".json"), "w") as f:
            json.dump(result, f, indent=1)
        print(json.dumps({k: v for k, v in result.items() if k != "rows"}), flush=True)
    finally:
        app.terminate()
        try:
            post(OLLAMA + "/api/chat", {"model": model, "messages": [], "keep_alive": 0}).read()
        except Exception:
            pass


if __name__ == "__main__":
    # Stopping the benchmark must also stop its app (the finally block in run()).
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(1))
    for m in sys.argv[1:]:
        print(f"== {m}", flush=True)
        run(m)
