#!/bin/bash
# Pull each candidate, benchmark it, then remove it (the baseline stays until a winner is chosen).
cd "$(dirname "$0")/.."
BASELINE=$(cat model.txt 2>/dev/null || echo qwen3.5:9b)
./kit serve >/dev/null || { echo "Could not start the libraries"; exit 1; }
curl -s http://127.0.0.1:11434/api/chat -d "{\"model\":\"$BASELINE\",\"messages\":[],\"keep_alive\":0}" >/dev/null
for m in "$@"; do
  echo "== pull $m"
  ollama pull "$m" >/dev/null 2>&1 || { echo "PULL FAILED $m"; continue; }
  /usr/bin/python3 -I bench/bench.py "$m"
  [ "$m" != "$BASELINE" ] && ollama rm "$m" >/dev/null 2>&1 && echo "removed $m"
  echo "free disk: $(df -h /System/Volumes/Data | tail -1 | awk '{print $4}')"
done
echo "ALL DONE"
