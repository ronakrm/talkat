# Talkat — Cleanup & Improvement Notes

Findings from a deep pass over the codebase. Grouped by impact: real bugs, interface-level fixes, the big refactor, and minor polish. Line references are to the state of the tree at the time of review.

---

## Bugs hurting reliability today

### 1. Two-parser CLI flow is broken for top-level flags
`cli.py:222` parses `-v/--verbose/--debug/--quiet` and dispatches `listen`/`long`/`calibrate` into `main.py:main()`, which then calls `parser.parse_args()` again on the same `sys.argv` (`main.py:755`). The inner parser doesn't know about `-v`, so `talkat -v listen` will hit argparse's "unrecognized arguments" exit.

There's also redundant duplication: `--background` is declared in both `cli.py:175` and `main.py:751`.

**Fix:** drop the inner argparse in `main.py` entirely; pass values as function arguments from `cli.py`.

### 2. Signal handler does unsafe work
`main.py:184-190` logs and calls `cleanup_pid()` from inside the SIGINT handler. Python's `logging` module isn't async-signal-safe and can deadlock. Same pattern in `run_long_dictation_command` (`main.py:406`).

**Fix:** set the `Event` and return; let the main loop do logging and cleanup.

### 3. `sanitize_text_for_typing` double-escapes shell characters when there's no shell
`security.py:218-221` adds backslashes before `\ " ' $ \``, but `safe_subprocess_run` runs with `shell=False` (`security.py:347`), so those backslashes get *typed* literally. Dictating "what's up" produces `what\'s up` in the editor.

**Fix:** drop the escaping; with `shell=False` arguments are passed directly to `execve` and don't need shell escaping.

### 4. Vosk `/transcribe` endpoint has shared mutable state
`model_server.py:238` calls `MODEL_REC.AcceptWaveform` on a module-global recognizer. Flask's default server is threaded; two concurrent requests corrupt state.

The streaming endpoint correctly creates a local recognizer (`model_server.py:308`).

**Fix:** apply the same per-request pattern to `/transcribe`, or delete `/transcribe` since the client only calls `/transcribe_stream`.

### 5. Stale `record.py.bak` checked into source tree
Delete `src/talkat/record.py.bak`.

### 6. Module-level side effects on import
`config.py:19` runs `ensure_user_directories()` at import. `paths.py:19-21` creates a fallback runtime dir at import. Importing for tests/docs/packaging creates directories on disk.

**Fix:** move directory creation into lazy entry points (server start, first config load).

### 7. Doc/code drift on PID paths
`CLAUDE.md` says PID files live at `~/.cache/talkat/*.pid`, but `paths.py:43-45` puts them in `$XDG_RUNTIME_DIR/talkat`. Either update the docs or change the path back.

---

## Interface-level fixes

### 8. The "yields-int-then-bytes" generator is a sharp edge
`record.py:392` types as `Generator[int | bytes, None, None]`. Every caller has to `next()` once, runtime-check `isinstance(sample_rate, int)`, then iterate the rest. The exception flow when no speech is detected (`main.py:244`, `main.py:506`) is awkward.

**Fix:** replace with a function returning `tuple[int, Generator[bytes, None, None]]` — or better, an `AudioSession` object with `sample_rate` attribute and `__iter__`.

### 9. `ProcessManager` lock acquisition fails silently
`__enter__` (`process_manager.py:304`) calls `acquire_lock()` but discards the return value. If the lock can't be acquired within the timeout, the `with pm:` block proceeds anyway as if locked.

**Fix:** raise on failure, or make the context manager genuinely fail-closed.

### 10. `safe_subprocess_run` uses a fixed 30s timeout
`security.py:347`. For `ydotool type` of a long transcript with `--key-delay=1`, you can exceed that.

**Fix:** let callers pass `timeout=None` through.

### 11. Background log file handle leaked in parent
`process_manager.py:251` opens `log_file` and passes it to `Popen`, but never closes its own copy.

**Fix:** close the parent's handle after `Popen` returns (Popen keeps its own).

---

## The big refactor — pull out a transcription client

`run_listen_command` (`main.py:157-371`) and `run_long_dictation_command` (`main.py:374-631`) are 200+ lines each with ~70% overlap:
- PID setup
- signal handlers
- threshold reporting
- generator-with-metadata
- POST to `/transcribe_stream`
- error handling on `ConnectionError` / `Timeout` / `JSONDecodeError`
- notification helpers

The two functions diverge only in (a) what to do with the recognized text and (b) one-shot vs. loop.

### Suggested shape

```python
class TranscriptionClient:
    def __init__(self, config): ...
    def transcribe_one_utterance(
        self,
        threshold: float,
        stop_event: threading.Event | None = None,
    ) -> str | None:
        # Opens audio, streams to server, returns text or None.
        # All HTTP error handling lives here.
        # Audio stream is a context manager.
        ...

def listen_once(client, output_file): ...           # short mode
def listen_continuous(client, transcript_path): ... # long mode
```

Plus:
- `AudioSession` (in `record.py`) as a context manager that owns PyAudio + stream lifecycle. The current `try/finally` inside the generator works in CPython but breaks if the caller is killed mid-iteration or refactored to async.
- A `ServerClient` class wrapping `requests.Session` + URL + error mapping, so the noisy six-`except` block exists once.
- Drop the inner argparse in `main.py` entirely; let `cli.py` be the single CLI surface and call typed entry points.

After this, `run_listen_command` becomes ~30 lines and `run_long_dictation_command` becomes a loop around `transcribe_one_utterance`.

---

## Minor polish

- **`model_server.py` globals** (`MODEL`, `MODEL_TYPE`, `MODEL_REC`, `DICTIONARY_WORDS`) — wrap in a `ModelService` class held on `app.config`. Also add a request-level lock for Vosk if you keep a shared recognizer.
- **Flask dev server** for production-ish use: at minimum set `threaded=False`, or move to `waitress` (one-line change, sync, fine for single-user).
- **Duplicate start logic in `cli.py`:** `start_long_background` (lines 24-54) and the start branch of `toggle_long_background` (lines 94-116) are nearly identical. Extract `_start_long(pm, debug)`.
- **`validate_json_config`** (`security.py:261`) validates only a handful of keys; ports/timeouts/host fall through. Either validate them or stop pretending it's a full validator.
- **No tests despite pytest in deps.** `pyproject.toml` has pytest configured but no test files. Add a small smoke suite covering:
  - config load/save round-trip
  - `ProcessManager` PID lifecycle with a dummy `sleep` subprocess
  - `sanitize_text_for_typing` (would have caught bug #3)
  - `validate_json_config` rejects bad input

---

## Suggested order

1. **Bugs #1, #2, #3** are surgical wins — afternoon each, easy to verify.
2. **The big refactor** — durably improves reliability by eliminating the duplicated error-handling paths where bugs hide.
3. Everything else can ride along.

Quickest first action: **#3** (3-line fix, immediately observable improvement). Then **#1** (unblocks cleaner argument flow for the refactor).
