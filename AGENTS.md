# Rules for agents

This repository is the Offline Emergency Kit: a local model (Ollama), offline libraries
(kiwix-serve), and a small Python app. Read README.md and docs/how-it-works.md first.

## When you set it up for a person

- Read `kit`, `app/server.py` and `app/index.html` before you run anything. Tell the person
  what each step downloads, from where, and how big.
- Check memory and free disk first. Pick the model size from docs/how-it-works.md.
- Ask before any download over 1 GB and before you delete anything.
- On a system that is not macOS, rebuild the design from docs/how-it-works.md. Do not run
  `kit` there.
- Finish with `./kit doctor`. Both questions must PASS. Then help the person test one
  question with the network off.

## When you change the code

- No dependencies beyond the Python that ships with macOS and the Kiwix binaries in `bin/`.
  No build step.
- All servers bind to 127.0.0.1. Ports: Ollama 11434, kiwix 8090, app 8091.
- Never commit `library/`, `logs/`, `bin/`, `model.txt`, `bench/results/` or
  `bench/model-watch/` output. They are in .gitignore.
- Change the library set in `catalog.tsv`, not in the script.
- Change the model only with `./kit use <model>`. It tests first.
- Escape all model output and library text before it goes into the page.
- After any change to `app/`, run `./kit stop && ./kit start && ./kit doctor`. Both
  questions must PASS.
- Keep the writing simple: short sentences, common words.
