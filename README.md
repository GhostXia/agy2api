# agy2api

MIT-friendly OpenAI-compatible wrapper around the logged-in official Antigravity CLI.

## What It Does

- Calls `agy --print <prompt>` through `subprocess.run`.
- Reads the newest Antigravity conversation SQLite DB because `agy` stdout is empty in current builds.
- Decodes protobuf `steps.step_payload` with a small dependency-free wire decoder.
- Exposes:
  - `POST /v1/chat/completions`
  - `GET /v1/models`
- Supports fake streaming by sending heartbeats while `agy` runs, then chunking the completed answer into OpenAI SSE events.

## Prerequisites

This wrapper does **not** handle Google authentication. You must set up the
official CLI yourself before running it:

1. **Install the Antigravity CLI (`agy`) yourself.** This project does not bundle,
   download, or redistribute it.
2. **Log in yourself** (`agy` Google OAuth). The login lives entirely inside
   `agy`'s own local state.
3. The wrapper only ever runs `agy --print <prompt>` as a subprocess and reads the
   conversation SQLite file that `agy` writes locally. It never reads, writes,
   stores, transmits, or validates your Google credentials or OAuth tokens.

## Auth & Privacy

- **Two unrelated layers — don't confuse them:**
  - *Google OAuth* — owned 100% by `agy`. This project has zero OAuth code.
  - `AGY2API_KEY` — this wrapper's **own** Bearer password, only gating who may
    call your local HTTP endpoint. Unrelated to Google.
- **Quota is yours.** Every request consumes *your* logged-in `agy` quota. Anyone
  who can reach the endpoint with a valid Bearer key spends your quota.
- **Keep it local.** The server binds `127.0.0.1` by default. Do not set
  `HOST=0.0.0.0` / expose it publicly unless you set a strong `AGY2API_KEY` and
  accept that callers run prompts under your Google account.

## Install

```powershell
cd agy2api
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

Make sure `agy` is installed and already logged in (see Prerequisites).

## Run

```powershell
$env:AGY2API_KEY="pwd"
python server.py
```

Default URL: `http://127.0.0.1:7862`.

## Request

```bash
curl http://127.0.0.1:7862/v1/chat/completions \
  -H "Authorization: Bearer pwd" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-2.5-pro","messages":[{"role":"user","content":"reply with exactly PONG"}]}'
```

## Configuration

- `AGY_PATH`: CLI executable path, default `agy`
- `AGY_WORKDIR`: working directory for `agy`, default project parent
- `AGY_CONVERSATIONS_DIR`: Antigravity conversations directory
- `AGY_TIMEOUT`: subprocess timeout seconds, default `180`
- `AGY2API_KEY`: bearer token, default `pwd` (empty disables auth)
- `AGY_MODELS`: comma-separated model list
- `AGY_POLL_INTERVAL`: DB-readiness poll seconds, default `0.25`
- `AGY2API_CHUNK_SIZE`: fake-stream chunk size in chars, default `50`
- `AGY2API_EXPOSE_REASONING`: emit `reasoning_content`, default `true`
- `HOST`: bind address, default `127.0.0.1` (see Auth & Privacy)
- `PORT`: server port, default `7862`

## Known Limits

- No true streaming: the CLI writes the final result into SQLite after completion.
- Latency is usually several seconds because each request starts an `agy` run.
- Antigravity is an agent and may use tools for complex prompts.
- The protobuf fields are reverse-engineered and may need updates if `agy` changes its cache schema.

## License

MIT. This project is written from scratch and does not copy `gcli2api` code.
