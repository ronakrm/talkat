# Future work — ideas surveyed from speech-note

This document is the parking lot for ideas we surveyed from
[eiis1000/speech-note](https://github.com/eiis1000/speech-note) but decided
not to ship right now. The first section is the one big idea we *do* want
to revisit (multi-engine cross-check); the rest is a record of items we
deliberately declined and why, so future contributors don't relitigate
the same questions.

## 1. Multi-engine ASR cross-check (deferred)

Speech-note's headline trick is running two independent ASR engines on
the same audio and feeding both transcripts to a cleanup LLM as named
peer sources. The LLM can then *vote against hallucinations* — a word
or phrase that only one engine produced is treated as suspect; one that
both produced is trusted. This is meaningfully better than any single
engine, especially against Whisper's well-known looping-repeat failure
mode on long clips.

### Why we didn't do it now

* **Listen mode is latency-critical.** Snap dictation into a focused
  text field is the most common Talkat use, and users notice anything
  past ~500 ms. Two ASR passes plus an LLM cleanup is multi-second.
* **Long mode is closer to the right fit, but still needs care.** Users
  who run `talkat long` for note-taking would tolerate a 2–3 s post-stop
  delay, but the current architecture transcribes per-utterance, not
  per-session. A cross-check at session end means re-transcribing the
  whole audio at the end — which requires keeping the raw audio buffer
  around, which we currently don't.
* **The infrastructure is half there.** `backends.py` is a Protocol-based
  registry; `postprocess.py` is an OpenAI-compatible LLM cleanup pass.
  What's missing is (a) a way to run two backends concurrently, (b) a
  way to keep the raw audio around for a second pass, and (c) a prompt
  template that names the sources and asks the LLM to dedupe vs. cross-
  check.

### Sketch of how it would land

Adding a `cross_check` flag (config + per-invocation CLI) to long mode.
When set:

1. `listen_continuous` keeps each utterance's raw float32 buffer in
   memory (or, for very long sessions, in a temp file under
   `XDG_RUNTIME_DIR/talkat/`).
2. At session end (after stop, before clipboard copy), spin up a second
   backend instance — likely a different model family (Vosk against
   Faster-Whisper, or two Whisper sizes) so they make uncorrelated
   errors — and run it against the same audio.
3. Build a cleanup prompt that includes both transcripts as named
   peers, with a system message describing the cross-check role.
   `postprocess_text` already calls an OpenAI-compatible endpoint;
   extend it (or add a sibling `cross_check_text`) that takes a list
   of `(source_name, transcript)` peers instead of a single string.
4. Save a `.cross-check.txt` alongside the raw and `.processed.txt`
   transcripts, with provenance.

Open questions to resolve before shipping:

* **Backend pairing.** Two Faster-Whisper sizes on the same architecture
  share failure modes; two architectures (Whisper + Vosk, or Whisper +
  a Parakeet variant) give genuine error diversity but Vosk's accuracy
  ceiling is lower. Pick deliberately, don't default.
* **Memory for raw audio.** A 30-minute session at float32 16 kHz is
  ~115 MB. Acceptable as RAM; better as a streaming temp file.
* **Prompt format.** Speech-note's prompt names each source and includes
  reliability notes (e.g. "this model is known to loop on long
  inputs"). Worth borrowing the exact framing instead of inventing.
* **Fall-open behavior.** If the second backend or the LLM fails, the
  primary single-engine transcript must still be returned. Match
  `postprocess.py`'s fail-open contract.

### Why this matters more than the other deferred items

The four small ideas we *did* ship — segmentation, gain normalization,
diagnostics — each pay off without needing a behavioral change from
the user. Cross-check is the next non-trivial leap in transcription
quality the Talkat architecture can absorb, and the only one on this
list that's worth a real design pass before implementation.

## 2. Items deliberately skipped

### OpenRouter / provider fallback chains

Speech-note has a configurable preferred-model list filtered against a
live `/models` catalog, with separate free-tier and paid-tier flags.

**Why we skipped it:** Talkat's `postprocess.py` deliberately uses one
OpenAI-compatible endpoint and treats provider selection as the user's
problem (point `base_url` at Ollama, OpenRouter, OpenAI itself,
whatever). Multi-provider fallback ladders solve a problem Talkat
doesn't have — Ollama doesn't 503 the way free OpenRouter endpoints
do — and they add a lot of config-validation surface for marginal gain.
The right path if a user needs fallback is to put their gateway in
front of the endpoint, not to teach Talkat about providers.

### `--full-auto` strict exit-code semantics

Speech-note has an explicit non-interactive mode that exits nonzero on
any cleanup failure and writes a diagnostics file matching the input
name.

**Why we skipped it:** Talkat's primary use is interactive dictation,
not file-processing pipelines. The file-processor path *does* already
have a sensible exit-code contract (`process_audio_file_command`
returns 1 on failure, 0 on success); the gap is the lack of a
"diagnostics file matching the input name" convention. The new
diagnostics module records the input filename in `extra.input_file`,
which is enough for the scripting use case. If users start running
Talkat in CI/cron, revisit.

### External transcript merging

Speech-note accepts `.srt` / `.vtt` / `.txt` / `.json` from other tools
(Google Recorder, etc.) and feeds them to the cleanup LLM as additional
sources.

**Why we skipped it:** This is a transcript-curation feature, not a
dictation feature. Anyone with a Google Recorder export and a desire
to clean it up has tools better suited to the task than Talkat.

### Multi-backend Vulkan / hardware tuning

Speech-note ships a Nix flake that pins a whole AMD-Vulkan toolchain
(whisper.cpp, llama.cpp, ROCm PyTorch, sherpa-onnx) and offers seven
ASR backends.

**Why we skipped it:** Talkat targets per-user installs on Wayland
Linux; the existing two-backend (Faster-Whisper + Vosk) choice covers
the user base. Adding Vulkan/ROCm/CUDA configuration would balloon the
install surface and the support load. If someone files an issue
wanting GPU acceleration, the answer is to set `fw_device=cuda` in
config — Faster-Whisper already supports it.

### Dual small/large-model live preview

Speech-note streams a small Faster-Whisper model's output to the user
as a live preview while a larger model transcribes in the background.

**Why we skipped it:** Talkat already streams audio to a single model
via `/transcribe_stream`. Adding a second concurrent model for preview
would (a) double server-side memory, (b) require a notion of "preview
vs. final" the typing layer doesn't have, and (c) the latency win is
modest because users see typed characters once final ASR completes —
the preview would be discarded. The right place to spend complexity
here is multi-engine cross-check (above), which has a real quality
payoff, not preview UX.

### Consent-gated model downloads

Speech-note prompts the user before downloading any model on first
use, with an `--auto-download` opt-in for non-interactive runs.

**Why we skipped it:** Faster-Whisper handles its own model downloads
on first inference, transparently. Talkat's `model_manager.py` covers
the explicit-download case. A consent prompt at first transcribe would
be a UX regression — the user already clicked "install Talkat";
that's the consent. If model download size becomes a complaint, add a
config flag rather than a prompt.

### Side-by-side multi-output review (`1`/`2`/`3` → copy)

Speech-note shows two ASR outputs and a cleaned version side-by-side
after each run and asks the user which to copy.

**Why we skipped it:** Doesn't fit the dictation use case at all —
users are mid-flow, they want text typed into the focused window, not
a review screen.

## 3. What we did ship

For the record, the changes that motivated this document landed in the
same PR:

* **Server-side gain normalization** (`audio_utils.normalize_gain`,
  config-gated, returned in the response).
* **Long-form segmentation** at energy minima (both backends,
  configurable via `max_segment_seconds`).
* **Per-run diagnostics JSON** at
  `$XDG_DATA_HOME/talkat/diagnostics/diagnostics.latest.json` plus
  timestamped copies, recording duration, RTF, gain, model, errors.

The remaining ideas on this page either need a design pass (#1) or
deliberately don't fit Talkat (#2).
