# Security

## Report a problem privately

If you find a security problem in the kit, please do not open a public issue.

1. Open the **Security** tab of this repository.
2. Press **Report a vulnerability**.
3. Describe what you found and how to repeat it.

## What is in scope

- `kit`, the start and stop files, and `bench/`.
- The local app: `app/server.py` and `app/index.html`.

Ollama, Kiwix, the models, and the library content are separate projects. Report problems in
them to those projects.

## How the kit protects you

- Every server listens only on `127.0.0.1`.
- The app accepts only JSON requests addressed to `127.0.0.1` or `localhost`. Other websites
  cannot send questions to it.
- Model output and library text are escaped before they go into the page.
- Library downloads are checked against the Kiwix SHA-256 checksums. The Kiwix server program
  is checked against a checksum pinned in `kit`.
- The optional weekly model watch runs the Claude Code judge with all tools turned off, so
  library text cannot make it act.
- The kit uses no accounts, keys, or tokens.
