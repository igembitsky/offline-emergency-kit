"""Judge every benchmarked model against the current one and print a table.

Run: /usr/bin/python3 -I bench/compare.py   (needs bench/results/*.json and a signed-in claude)
"""
import glob
import importlib.util
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("mw", os.path.join(HERE, "model-watch.py"))
mw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mw)

cur_name = mw.current_model()
cur = json.load(open(mw.results_path(cur_name)))
print("| Model | Quality vs current | Seconds per answer | GPU busy s per answer | Memory GB | Gen tok/s | Fake cites | No sources | Better? |")
print("|---|---|---|---|---|---|---|---|---|")
print(f"| {cur_name} (current) | ref | {cur['avg_seconds']} | {cur['avg_gpu_busy_s']} | {cur['memory_gb']} | {cur['gen_tok_s']} | {cur['fake_citation_warnings']} | {cur['no_source_answers']} | |")
for path in sorted(glob.glob(os.path.join(HERE, "results", "*.json"))):
    c = json.load(open(path))
    if c["model"] == cur_name:
        continue
    q_cur, q_cand, notes = mw.judge(cur, c)
    win = mw.better(cur, c, q_cur, q_cand)
    print(f"| {c['model']} | {q_cand} vs {q_cur} | {c['avg_seconds']} | {c['avg_gpu_busy_s']} | {c['memory_gb']} | {c['gen_tok_s']} | {c['fake_citation_warnings']} | {c['no_source_answers']} | {'yes' if win else 'no'} |", flush=True)
    print(f"  Judge: {notes}", flush=True)
