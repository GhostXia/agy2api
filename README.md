# agy2api

OpenAI-compatible wrapper around the logged-in official Antigravity CLI.

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

## Compliance / Acceptable Use

This is an **unofficial** personal-interop tool. It is not affiliated with,
endorsed by, or supported by Google. You are responsible for using it within
the Antigravity / Gemini Terms of Service. To stay on the safe side:

- **Personal, local, single-user only.** Do not share the endpoint, share the
  Bearer key, or run it as a public proxy. Doing so redistributes your personal
  Google quota to third parties — the clearest ToS violation.
- **No commercial use or resale** of the free-tier quota.
- **Do not expose it to the network.** The server refuses to bind a non-loopback
  address unless you set `AGY2API_ALLOW_REMOTE=1` (don't, unless you fully
  understand the consequences).
- **Human-paced volume.** Concurrency is capped at 1 by default
  (`AGY2API_MAX_CONCURRENCY`). Don't drive high-volume or mass-parallel traffic.
- **Don't use outputs to train competing models**, and don't strip safety
  filtering — pass prompts through as-is.
- The tool only reads *your own* local conversation database on *your own*
  machine; it does not break encryption or access anyone else's data. Keep it
  that way.

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
  -d '{"model":"gemini-3.5-flash","messages":[{"role":"user","content":"reply with exactly PONG"}]}'
```

## Configuration

- `AGY_PATH`: CLI executable path, default `agy`
- `AGY_WORKDIR`: working directory for `agy`, default project parent
- `AGY_CONVERSATIONS_DIR`: Antigravity conversations directory
- `AGY_TIMEOUT`: per-request budget seconds, default `300` (also passed to agy as `--print-timeout`; the subprocess is killed only 20s later as a hard backstop)
- `AGY2API_KEY`: bearer token, default `pwd` (empty disables auth)
- `AGY_MODELS`: comma-separated model list
- `AGY_POLL_INTERVAL`: DB-readiness poll seconds, default `0.25`
- `AGY2API_CHUNK_SIZE`: fake-stream chunk size in chars, default `10`
- `AGY2API_STREAM_DELAY`: inter-chunk delay seconds for typing effect, default `0.03`
- `AGY2API_EXPOSE_REASONING`: emit `reasoning_content`, default `true`
- `AGY2API_MAX_CONCURRENCY`: max concurrent agy runs, default `3`
- `AGY2API_CLEANUP_DB`: delete each run's conversation DB + brain dir after reading, default `true`
- `AGY2API_ALLOW_REMOTE`: allow binding a non-loopback host, default `false`
- `HOST`: bind address, default `127.0.0.1` (see Auth & Privacy / Compliance)
- `PORT`: server port, default `7862`

## Known Limits

- No true streaming: the CLI writes the final result into SQLite after completion.
- Latency is usually several seconds because each request starts an `agy` run.
- Antigravity is an agent and may use tools for complex prompts.
- The protobuf fields are reverse-engineered and may need updates if `agy` changes its cache schema.

## License

Apache-2.0. This project is written from scratch and does not copy `gcli2api` code.
