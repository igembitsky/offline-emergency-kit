"""The weekly model watch for the Offline Emergency Kit.

Finds new local models that fit this Mac, tests them on the real pipeline against the current
model, and writes a one-line summary (for a daily brief, a note, or an agent to read). Also
checks the libraries for newer versions.

  /usr/bin/python3 -I bench/model-watch.py              the weekly sweep (./kit watch on: Mondays 06:30)
  /usr/bin/python3 -I bench/model-watch.py --dry        discover only, no download or test
  /usr/bin/python3 -I bench/model-watch.py --model <m>  test one named model now

1. Discover (free): ollama.com newest models with tool use. Keep tags that fit memory
   (MAX_GB download), skip cloud, full-precision and NVIDIA-only formats. One tag per family.
2. Test (about 10 minutes of GPU each, at most SCREENS a week): pull, run bench.py, remove.
3. Judge: Claude Code (`claude -p`, all tools off) scores each candidate answer against the
   current model's answer on the same question, in blind A/B order. Without Claude Code the
   watch reports speed and energy only and never recommends a switch.
4. Report: bench/model-watch/<date>.md, and status.md with one "Brief line" summary.
It never switches the model. Switching is your call: ./kit use <model>.
"""
import datetime
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "model-watch")
RESULTS = os.path.join(HERE, "results")
STATUS = os.path.join(OUT, "status.md")
SEEN = os.path.join(OUT, "seen.json")
MAX_GB = float(os.environ.get("WATCH_MAX_GB", "10"))   # fits 18 GB with macOS and a 16k context
MIN_GB = float(os.environ.get("WATCH_MIN_GB", "2"))    # smaller models are too weak to bother
SCREENS = int(os.environ.get("WATCH_SCREENS", "2"))
KEEP_FREE_GB = 30
JUDGE = os.environ.get("JUDGE", "sonnet")
SKIP_TAG = re.compile(r"cloud|bf16|fp16|fp32|q8|int8|q2_|q3_|iq\d|coding|embed", re.I)
SKIP_FAMILY = re.compile(r"embed|rerank|guard|ocr|vision-only|whisper|tts|code", re.I)
TODAY = datetime.date.today().isoformat()
log = []


def say(s):
    print(s, flush=True)
    log.append(s)


def fetch(url, timeout=30):
    for i in range(3):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            if i == 2:
                raise
            time.sleep(2 * (i + 1))


def current_model():
    p = os.path.join(ROOT, "model.txt")
    return open(p).read().strip() if os.path.exists(p) else "qwen3.5:9b"


def write_status(brief, verdict):
    os.makedirs(OUT, exist_ok=True)
    with open(STATUS, "w") as f:
        f.write(f"# Offline Kit model watch\n\nLast sweep: {TODAY}\n\nBrief line: {brief}\n\nVerdict: {verdict}\n")


# ---------- 1. discover ----------

def families():
    """Newest Ollama model families that support tool calls."""
    html = fetch("https://ollama.com/search?c=tools&o=newest")
    names = re.findall(r'href="/library/([a-z0-9._-]+)"', html)
    seen, out = set(), []
    for n in names:
        if n not in seen and not SKIP_FAMILY.search(n):
            seen.add(n)
            out.append(n)
    return out[:30]


def fitting_tags(family):
    """Tags of a family that fit this Mac: (tag, size GB, is_mlx), one name per set of weights."""
    html = fetch(f"https://ollama.com/library/{family}/tags")
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    pattern = (rf"({re.escape(family)}:[a-z0-9._-]+)\s+((?:latest\s+|MLX\s+)*)([0-9a-f]{{12}})\s+•\s+"
               r"([0-9.]+)\s*(GB|MB)(?:\s*-\s*([0-9.]+)\s*(GB|MB))?")
    tags = {}
    for tag, labels, digest, s1, u1, s2, u2 in re.findall(pattern, text):
        size, unit = (s2, u2) if s2 else (s1, u1)
        gb = float(size) / (1000 if unit == "MB" else 1)
        if SKIP_TAG.search(tag.split(":", 1)[1]) or not (MIN_GB <= gb <= MAX_GB):
            continue
        prev = tags.get(digest)
        if prev is None or prev[0].endswith(":latest"):  # prefer an explicit name over "latest"
            tags[digest] = (tag, gb, "MLX" in labels)
    return sorted(tags.values(), key=lambda t: -t[1])


def params_b(tag):
    """Parameter count in billions from the tag name: 9b -> 9, e4b -> 4, 26b-a4b -> 26. None if absent."""
    m = re.search(r"(?<![a-z0-9])e?(\d+(?:\.\d+)?)b(?![a-z])", tag.split(":", 1)[1])
    return float(m.group(1)) if m else None


def pick_tags(tags):
    """Per family: the largest parameter count that fits, then its smallest MLX and smallest standard file
    (smallest is usually 4-bit: the best speed and energy for the same model)."""
    named = [t for t in tags if params_b(t[0])]
    pool = named or tags
    if not pool:
        return []
    top = max(params_b(t[0]) or t[1] for t in pool)
    same = [t for t in pool if (params_b(t[0]) or t[1]) == top]
    out = []
    for want_mlx in (True, False):
        fmt = sorted((t for t in same if t[2] == want_mlx), key=lambda t: t[1])
        if fmt:
            out.append((fmt[0][0], fmt[0][1]))
    return out


def discover(seen):
    cands = []
    for fam in families():
        try:
            choices = pick_tags(fitting_tags(fam))
        except Exception as e:
            say(f"- {fam}: could not read tags ({e})")
            continue
        cands += [c for c in choices if c[0] not in seen and c[0] != current_model()]
    return cands


# ---------- 2. test ----------

def free_gb():
    return shutil.disk_usage("/System/Volumes/Data").free / 1e9


def on_battery():
    return "Battery Power" in subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout


def wait_for_other_heavy_jobs():
    """One heavy job at a time: wait up to two hours for the virtual patient watch to finish."""
    for _ in range(24):
        busy = subprocess.run(["pgrep", "-f", "model-watch.mjs|bench/eval.py|blender"], capture_output=True).returncode == 0
        if not busy:
            return
        time.sleep(300)


def results_path(model):
    return os.path.join(RESULTS, model.replace(":", "_").replace("/", "_") + ".json")


def bench(model):
    p = subprocess.run(["/usr/bin/python3", "-I", os.path.join(HERE, "bench.py"), model],
                       capture_output=True, text=True, timeout=3 * 3600)
    if p.returncode != 0 or not os.path.exists(results_path(model)):
        raise RuntimeError(f"benchmark of {model} failed: {(p.stderr or p.stdout)[-300:]}")
    return json.load(open(results_path(model)))


def pull(model):
    p = subprocess.run(["ollama", "pull", model], capture_output=True, text=True, timeout=3 * 3600)
    if p.returncode != 0:
        raise RuntimeError(f"pull of {model} failed: {p.stderr[-200:]}")


def remove(model):
    subprocess.run(["ollama", "rm", model], capture_output=True)


# ---------- 3. judge ----------

def judge(cur, cand):
    """Blind A/B scores (0 to 10) per question from claude -p. Returns (cur_avg, cand_avg, notes)."""
    items, keys = [], []
    for i, (a, b) in enumerate(zip(cur["rows"], cand["rows"])):
        flip = random.random() < 0.5
        x, y = (b, a) if flip else (a, b)
        keys.append(flip)
        items.append(
            f"### Q{i + 1}: {a['q']}\nWhat a good answer must contain: {a['rubric']}\n\n"
            f"Answer A:\n{x['answer'][:3000] or '(empty)'}\n\nAnswer B:\n{y['answer'][:3000] or '(empty)'}\n"
        )
    prompt = (
        "You grade answers from an offline emergency and general-knowledge assistant. For each question, "
        "score Answer A and Answer B from 0 to 10: correct facts against the rubric, safe advice, complete "
        "enough to act on, clear. A dangerous error caps the score at 2. Ignore citation markers like [S1].\n"
        'Return only JSON: {"scores": [{"q": 1, "a": <0-10>, "b": <0-10>}, ...], "notes": "<one sentence on the main difference>"}\n\n'
        + "\n".join(items)
    )
    if not shutil.which("claude"):
        return None, None, "no judge: install Claude Code to grade quality"
    # All tools off: the answers contain library text, which must not be able to make the judge act.
    p = subprocess.run(["claude", "-p", "--model", JUDGE, "--tools", "", "--strict-mcp-config",
                        "--no-session-persistence", "--output-format", "json"],
                       input=prompt, capture_output=True, text=True, timeout=1800)
    out = p.stdout + p.stderr
    if p.returncode != 0 or '"result"' not in p.stdout:
        if re.search(r"not logged in|login|OAuth|authenticate", out, re.I):
            raise RuntimeError('the judge (claude) is not signed in. Fix: run "claude auth login" once in a terminal')
        raise RuntimeError(f"the judge failed: {out[-200:]}")
    text = json.loads(p.stdout)["result"]
    data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    cur_s, cand_s = [], []
    for s, flip in zip(data["scores"], keys):
        a, b = float(s["a"]), float(s["b"])
        cur_s.append(b if flip else a)
        cand_s.append(a if flip else b)
    n = len(cur_s)
    return round(sum(cur_s) / n, 2), round(sum(cand_s) / n, 2), data.get("notes", "")


def better(cur, cand, q_cur, q_cand):
    """A switch is worth it if quality holds and energy or memory drops, or quality rises clearly
    at similar energy. No quality grade means no recommendation."""
    if q_cur is None or q_cand is None:
        return False
    if cand["errors"] or cand["fake_citation_warnings"] > cur["fake_citation_warnings"] or cand["no_source_answers"] > cur["no_source_answers"]:
        return False
    mem_cur, mem_cand = cur.get("memory_gb") or 0, cand.get("memory_gb") or 0
    if mem_cand > max(mem_cur, 12) + 1:
        return False
    saves_energy = cand["avg_gpu_busy_s"] <= 0.85 * cur["avg_gpu_busy_s"]
    saves_memory = mem_cur and mem_cand and mem_cand <= mem_cur - 3 and cand["avg_gpu_busy_s"] <= 1.1 * cur["avg_gpu_busy_s"]
    clearly_better = q_cand >= q_cur + 0.5 and cand["avg_gpu_busy_s"] <= 1.15 * cur["avg_gpu_busy_s"]
    return bool((q_cand >= q_cur - 0.3 and (saves_energy or saves_memory)) or clearly_better)


# ---------- libraries ----------

def library_check():
    """Newer library versions on Kiwix, and the age of the oldest download."""
    lib = os.path.join(ROOT, "library")
    local = {}
    for f in os.listdir(lib):
        m = re.match(r"(.+)_(\d{4}-\d{2})\.zim$", f)
        if m:
            local[m.group(1)] = (m.group(2), os.path.getmtime(os.path.join(lib, f)))
    newer = 0
    listings = {}
    for line in open(os.path.join(ROOT, "catalog.tsv")):
        if line.startswith("#") or not line.strip():
            continue
        tier, folder, name = line.split("\t")[:3]
        if tier != "core" or name not in local:
            continue
        if folder not in listings:
            try:
                listings[folder] = fetch(f"https://download.kiwix.org/zim/{folder}/")
            except Exception:
                listings[folder] = ""
        dates = re.findall(rf'"{re.escape(name)}_(\d{{4}}-\d{{2}})\.zim"', listings[folder])
        if dates and max(dates) > local[name][0]:
            newer += 1
    oldest_days = int((time.time() - min(t for _, t in local.values())) / 86400) if local else 0
    return newer, oldest_days


# ---------- main ----------

def main():
    args = sys.argv[1:]
    dry = "--dry" in args
    only = args[args.index("--model") + 1] if "--model" in args else None
    os.makedirs(OUT, exist_ok=True)
    seen = json.load(open(SEEN)) if os.path.exists(SEEN) else []
    cur_name = current_model()
    say(f"# Offline Kit model watch {TODAY}\nCurrent model: {cur_name}. Fits: {MIN_GB} to {MAX_GB} GB download.")

    newer, oldest = library_check()
    lib_note = ""
    if oldest > 180:
        lib_note = f" Libraries are {oldest} days old: refresh with ./kit download core."
    elif newer:
        lib_note = f" {newer} libraries have newer versions (refresh due at 6 months)."
    say(f"Libraries: {newer} newer versions on Kiwix, oldest download {oldest} days.")

    cands = [(only, None)] if only else discover(seen)
    if not cands:
        write_status(f"Offline Kit model watch ran {TODAY}: no new local model that fits this Mac. Keep {cur_name}.{lib_note}", "No change")
        say("No new candidates.")
        return finish()
    say("New candidates: " + ", ".join(f"{t} ({g:.1f} GB)" if g else t for t, g in cands))
    if dry:
        return finish()

    if on_battery():
        write_status(f"Offline Kit model watch skipped on {TODAY}: the Mac was on battery. It tries again next Monday, or run it by hand.{lib_note}", "Skipped")
        return finish()
    wait_for_other_heavy_jobs()
    subprocess.run([os.path.join(ROOT, "kit"), "serve"], capture_output=True)

    cur = json.load(open(results_path(cur_name))) if os.path.exists(results_path(cur_name)) else None
    if cur is None:
        say(f"Benchmarking the current model {cur_name} first.")
        cur = bench(cur_name)

    rows, best = [], None
    for tag, gb in cands[:SCREENS]:
        if gb and free_gb() < gb + KEEP_FREE_GB:
            say(f"- {tag}: skipped, not enough free disk ({free_gb():.0f} GB free).")
            continue
        try:
            pull(tag)
            cand = bench(tag)
            q_cur, q_cand, notes = judge(cur, cand)
        except Exception as e:
            say(f"- {tag}: {e}")
            if "not signed in" in str(e):
                remove(tag)
                write_status(f"Offline Kit model watch ran {TODAY} but stopped: {e}.{lib_note}", f"Error: {e}")
                return finish()
            remove(tag)
            continue
        finally:
            if tag not in seen:
                seen.append(tag)
        remove(tag)  # keep only the model in use
        win = better(cur, cand, q_cur, q_cand)
        rows.append((tag, cand, q_cur, q_cand, notes, win))
        if win and (best is None or q_cand > best[3]):
            best = rows[-1]

    say("\n| Model | Quality (0-10) | Seconds per answer | GPU busy s per answer | Memory GB | Gen tok/s | Better? |\n|---|---|---|---|---|---|---|")
    say(f"| {cur_name} (current) | ref | {cur['avg_seconds']} | {cur['avg_gpu_busy_s']} | {cur.get('memory_gb')} | {cur.get('gen_tok_s')} | |")
    for tag, c, qc, qn, notes, win in rows:
        say(f"| {tag} | {qn} vs {qc} | {c['avg_seconds']} | {c['avg_gpu_busy_s']} | {c.get('memory_gb')} | {c.get('gen_tok_s')} | {'yes' if win else 'no'} |")
        if notes:
            say(f"  Judge: {notes}")

    if best:
        tag, c, qc, qn, notes, _ = best
        energy = round(100 * (1 - c["avg_gpu_busy_s"] / cur["avg_gpu_busy_s"]))
        brief = (f"Offline Kit: {tag} beats {cur_name} on this Mac (quality {qn} vs {qc}, about {energy}% less GPU time per answer, "
                 f"memory {c.get('memory_gb')} vs {cur.get('memory_gb')} GB). Switch with: ./kit use {tag}.{lib_note}")
        write_status(brief, "Recommend a switch")
    else:
        tested = ", ".join(r[0] for r in rows) or "none (see report)"
        write_status(f"Offline Kit model watch ran {TODAY}: tested {tested}; none beats {cur_name}. No change.{lib_note}", "No change")
    finish(seen)


def finish(seen=None):
    if seen is not None:
        json.dump(seen, open(SEEN, "w"), indent=1)
    with open(os.path.join(OUT, f"{TODAY}.md"), "w") as f:
        f.write("\n".join(log) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        msg = str(e)[:200]
        write_status(f"Offline Kit model watch ran {TODAY} but stopped on an error: {msg}. It runs again next Monday.", f"Error: {msg}")
        traceback.print_exc()
        sys.exit(1)
