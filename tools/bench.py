#!/usr/bin/env python3
"""Benchmark: direct agy calls vs. agy2api HTTP endpoint.

Usage:
    python bench.py [--runs N] [--prompt TEXT] [--model MODEL]

Requirements: agy2api server must already be running on localhost:7862.
"""

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

AGY_PATH = os.environ.get(
    "AGY_PATH",
    r"C:\Users\Administrator\AppData\Local\agy\bin\agy.EXE",
)
API_URL = os.environ.get(
    "AGY2API_URL", "http://127.0.0.1:7862/v1/chat/completions"
)
API_KEY = os.environ.get("AGY2API_KEY", "pwd")

DEFAULT_PROMPT = "قل كلمة واحدة فقط: مرحبا"
DEFAULT_MODEL = "claude-sonnet-4.6"
DEFAULT_RUNS = 3


# ── helpers ───────────────────────────────────────────────────────────────────

def _run_direct(prompt: str, model: str, timeout: int) -> tuple[float, str, str]:
    """Call agy directly via subprocess. Returns (elapsed, answer_snippet, error)."""
    cmd = [AGY_PATH, f"--print={prompt}", "--disable-slash-commands",
           "--model", model, "--print-timeout", f"{timeout}s"]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + 30,
            env={**os.environ, "HOME": os.environ.get("USERPROFILE", "")},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            return elapsed, "", (proc.stderr or proc.stdout)[:200]
        snippet = (proc.stdout or "").strip()[:120]
        return elapsed, snippet, ""
    except subprocess.TimeoutExpired:
        return time.monotonic() - t0, "", "TIMEOUT"
    except Exception as exc:
        return time.monotonic() - t0, "", str(exc)


def _run_api(prompt: str, model: str, timeout: int) -> tuple[float, str, str]:
    """Call agy2api HTTP endpoint. Returns (elapsed, answer_snippet, error)."""
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {API_KEY}"},
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout + 30) as resp:
            elapsed = time.monotonic() - t0
            data = json.loads(resp.read())
            snippet = data["choices"][0]["message"]["content"][:120]
            return elapsed, snippet, ""
    except urllib.error.HTTPError as exc:
        return time.monotonic() - t0, "", f"HTTP {exc.code}: {exc.read()[:200]}"
    except Exception as exc:
        return time.monotonic() - t0, "", str(exc)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="agy vs agy2api latency benchmark")
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                    help=f"عدد التشغيلات لكل طريقة (افتراضي: {DEFAULT_RUNS})")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT, help="نص الطلب")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="النموذج")
    ap.add_argument("--timeout", type=int, default=300, help="مهلة كل طلب بالثواني")
    args = ap.parse_args()

    print("=" * 62)
    print("  Benchmark: agy مباشر vs. agy2api")
    print(f"  النموذج : {args.model}")
    print(f"  الطلب   : {args.prompt[:60]}")
    print(f"  عدد التشغيلات: {args.runs}")
    print("=" * 62)

    direct_times: list[float] = []
    api_times: list[float] = []

    for i in range(1, args.runs + 1):
        print(f"\n── التشغيل {i}/{args.runs} ───────────────────────────")

        # Direct agy
        print("  [1] agy مباشر ...", end=" ", flush=True)
        elapsed, snippet, err = _run_direct(args.prompt, args.model, args.timeout)
        if err:
            print(f"فشل: {err}")
        else:
            direct_times.append(elapsed)
            print(f"{elapsed:.2f}s  ← {snippet!r}")

        # API
        print("  [2] agy2api   ...", end=" ", flush=True)
        elapsed, snippet, err = _run_api(args.prompt, args.model, args.timeout)
        if err:
            print(f"فشل: {err}")
        else:
            api_times.append(elapsed)
            print(f"{elapsed:.2f}s  ← {snippet!r}")

    # ── Summary ──
    print("\n" + "=" * 62)
    print("  النتائج النهائية")
    print("=" * 62)

    def _stats(label: str, times: list[float]) -> None:
        if not times:
            print(f"  {label:20s}: لا بيانات")
            return
        avg = sum(times) / len(times)
        mn, mx = min(times), max(times)
        print(f"  {label:20s}: avg={avg:.2f}s  min={mn:.2f}s  max={mx:.2f}s  "
              f"(n={len(times)})")

    _stats("agy مباشر", direct_times)
    _stats("agy2api", api_times)

    if direct_times and api_times:
        overhead = (sum(api_times) / len(api_times)) - (sum(direct_times) / len(direct_times))
        sign = "+" if overhead >= 0 else ""
        print(f"\n  الفارق (overhead agy2api): {sign}{overhead:.2f}s")
        pct = overhead / (sum(direct_times) / len(direct_times)) * 100
        print(f"  النسبة المئوية           : {sign}{pct:.1f}%")

    print("=" * 62)


if __name__ == "__main__":
    main()
