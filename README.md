# Litmus

**Litmus** is a litmus test for mobile bugs: it explores an Android app, raises
candidate issues, then **replays each finding three times on a clean app state**
and reports only what **survives replay**—with evidence (trace, repro script,
logcat, screenshots).

## Why it exists

Most QA automation stops at “we saw something once.” Litmus separates **confirmed**
findings from **flaky** noise and **filtered** environment issues (ABI, pinning, etc.).

## How it works (high level)

1. Install APK on an emulator and explore with a bounded step budget.
2. Detect crashes, HTTP errors, dead taps, and broken UI.
3. Replay every candidate **3×** with **no LLM** in the verifier.
4. Ship a report plus `repro.py` and artifacts.

## Benchmark

Ground-truth evaluation uses the open **Bazaar** fixture app (separate repo).
See [docs/BENCHMARK.md](docs/BENCHMARK.md) for scoring (recall on B1–B4, B5 flaky lane).

```bash
mqa bench --fixture bazaar --runs 3
```

## Quick start

```powershell
python -m venv .venv; .venv\Scripts\pip install -e ".[dev]"
.\.venv\Scripts\mqa.exe doctor          # python, adb, AVD, Postgres, Redis

.\.venv\Scripts\python.exe -m litmus.api             # dashboard + REST + SSE on 127.0.0.1:8000
.\.venv\Scripts\arq.exe litmus.worker.WorkerSettings # the run state machine (needs Redis)
```

Open `http://127.0.0.1:8000`, upload an APK, start a run and watch the steps
stream. The CLI stays available: `mqa run --apk <path> --profile bazaar --steps 25`.

`POST /v1/runs` queues the job on Arq. If Redis is down it still runs the
lifecycle inline in the API process (single-process dev fallback); Arq is the
supported path and the only one that keeps the API stateless.

## License

_(MIT or your choice — add LICENSE before submission.)_
