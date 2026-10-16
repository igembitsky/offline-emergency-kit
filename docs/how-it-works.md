# How it works

This page explains the design. It is also the specification that an agent can follow to
rebuild the kit on another system (Linux, Windows, or a Mac with less memory).

## The parts

```text
Browser ──> app/server.py (127.0.0.1:8091) ──> Ollama (127.0.0.1:11434)   the model
                       │
                       └────────────────────> kiwix-serve (127.0.0.1:8090)  the libraries
```

| Part | Port | Job |
|---|---|---|
| `kiwix-serve` | 8090 | Serves the `.zim` libraries: full-text search (`/search?format=xml`) and articles (`/content/...`). |
| Ollama | 11434 | Runs the model. Tool calls and JSON output. |
| `app/server.py` | 8091 | The question page, and the loop below. Python standard library only. |

## The answer loop

A small model does not search reliably on its own. In tests, a 4B model skipped the search,
answered from memory, and invented a citation. So the code does the search, not the model:

1. **Plan.** The model turns the question into 1 to 3 short keyword queries (JSON output). It
   can also say that the question needs no library (small talk, arithmetic).
2. **Search.** The code runs each query on Kiwix full-text search, across all libraries.
3. **Pick.** The model picks the 1 to 3 best results (JSON output).
4. **Read.** The code reads those articles. It keeps the lead section and the paragraphs that
   best match the question, up to about 6,000 characters per article.
5. **Answer.** The model answers from the extracts and cites them as [S1], [S2]. It can still
   call `search` or `read` if something is missing.
6. **Check.** If the answer cites an article that the code did not read, the page shows a
   warning. If the library server is off, the page says that the answer is from model memory.

Why keyword search and not a vector index: a vector index for many gigabytes of articles
takes hours to build and many gigabytes of disk, and every library update needs a rebuild.
Keyword search is built into every ZIM file. The model writes good keywords, which closes
most of the gap.

## The libraries

`catalog.tsv` lists each library: tier (`core` on the computer, `ssd` on an external drive),
the Kiwix folder, and the name without its date. The download picks the newest dated file,
checks its SHA-256 checksum from `download.kiwix.org`, and only then removes the older copy.

Some ZIM files have no full-text index (their catalog tags say `_ftindex:no`). The
`zimgit-*` prepper collections are like this, and they hold mostly PDF files. The AI cannot
search or read them, so they are for people to browse. Before you add a library for the AI,
check that its tags say `_ftindex:yes`.

## Model choice

The model is `qwen3.5:9b`. It is the largest good model that fits in 18 GB of memory with
macOS running, and it uses tools and JSON output well.

On a MacBook Pro M3 Pro with 18 GB (October 2026), it wrote about 24 to 31 tokens per
second, and an answer took 35 to 60 seconds, depending on the other load on the Mac. Other
quantizations of the same model and a 12B Gemma model wrote more slowly. A 4B model wrote a
little faster.

Some facts to know on Apple silicon:

- Ollama runs some models on Apple's MLX engine. MLX keeps a buffer cache, so macOS counts
  about 14 GB for this model while it is loaded, although the model itself needs about 8 GB.
  The kit unloads the model 5 minutes after the last question to give the memory back.
- Most of the time per answer is the model reading the article extracts, not writing.

To compare models on your own computer, use the benchmark: `bench/bench.py <model>` runs 12
real questions through the real answer loop and records time, GPU busy time (an estimate of
battery use), memory, and citation problems. `bench/compare.py` asks Claude Code to score the
answers against the current model, in blind A/B order. `./kit watch on` does this every week
for new models.

## Rebuild it on another system

An agent can rebuild the same design anywhere. Keep these rules:

1. **Same three parts.** Ollama (or llama.cpp), `kiwix-serve` (Kiwix publishes builds for
   Linux, Windows, and macOS), and a small app with the answer loop above.
2. **Local only.** Bind every server to `127.0.0.1`. In the app, accept only JSON requests
   whose `Host` is `127.0.0.1` or `localhost` on the app port.
3. **Verify downloads.** Check each ZIM file against its `.sha256` file from
   `download.kiwix.org`. Check program downloads against a known checksum.
4. **Size the model to the memory.** Rule of thumb: the model file plus 2 to 4 GB must fit in
   memory with the operating system running. 8 GB: a 4B model. 16 GB: a 9B model. 32 GB and
   more: try a 14B to 27B model. Then run the benchmark on the real computer and pick by
   result.
5. **Escape everything.** The page shows model output and article titles. Escape them before
   they go into the page.
6. **Test it.** Two real questions must come back with real sources (`app/selftest.py`). Then
   test once with the network off.
