# Embry Voice Blocker MVP

Immutable Goal: NOT_MET

## What The Smallest MVP Proves

MVP prompt:

```text
Hey Embry, what is the capital of France?
```

MVP command:

```bash
OUT_DIR=/tmp/embry-voice-mvp-problem-20260726T2012Z/results-r8 \
  bash /tmp/embry-voice-mvp-problem-20260726T2012Z/run_mvp.sh
```

Receipt:

```text
/tmp/embry-voice-mvp-problem-20260726T2012Z/results-r8/summary.json
```

Result:

- `pass: true`
- `mocked: false`
- `live: true`
- `checks.embry_live_turn.status: ok`
- `checks.embry_live_turn.answerAuthority: wikipedia_rest`
- `checks.embry_live_turn.answerText: Wikipedia's List of capitals of France result says: The capital of France has been Paris since its liberation in 1944.`
- Chatterbox render receipt: `/tmp/chatterbox-fork-agent-out/ux-lab-embry-direct/2026-07-26T20-45-33-029Z-a912bdabfee8.json`
- Chatterbox WAV: `/tmp/chatterbox-fork-agent-out/ux-lab-embry-direct/2026-07-26T20-45-33-029Z-a912bdabfee8.wav`

This proves only the narrow typed/live-turn path:

```text
text prompt -> Memory intent says outside_memory_domains ->
SPARTA live-turn -> Wikipedia REST dynamic answer ->
Chatterbox render -> amplitude frame data exists
```

## Where Progress Is Still Not Proven

This MVP does not prove the immutable voice goal.

Missing proof:

- wake word detection from actual audio: `Hey Embry`
- Jabra microphone capture as the input source
- RealtimeSTT final transcript from that fresh Jabra capture
- first voice asks a question, Embry answers, and conversation turn state persists
- Memory/Tau-generated conversational answer; Tau still times out in the MVP diagnostics
- Chatterbox playback through Jabra speaker for the fresh turn
- SPARTA Chat UX replay sourced from the authoritative journal
- Embry orb CDP proof showing state and amplitude/frequency-driven particle motion for that fresh turn

## Ask Competition Result

Ask competition run:

```text
/mnt/storage12tb/skills/ask/outputs/embry-voice-minimum-mvp-competition-20260726T2018Z/ask-tau-minimum-mvp-competition-for-the--c8c97a5f8e87
```

DAG receipt:

```text
/mnt/storage12tb/skills/ask/outputs/embry-voice-minimum-mvp-competition-20260726T2018Z/ask-tau-minimum-mvp-competition-for-the--c8c97a5f8e87/tau-receipts/dag-receipt.json
```

Result:

- `status: BLOCKED`
- `ok: false`
- `verdict: COMMAND_FAILED`
- `mocked: false`
- `live: true`

Handler outcome:

- `webkimi`: produced a useful repair path
- `webgpt`: `repo_access_blocked`
- `webgrok`: `browser_tab_identity_mismatch`
- `webclaude`: `browser_tab_read_timeout`

Useful WebKimi artifact:

```text
/mnt/storage12tb/skills/ask/outputs/embry-voice-minimum-mvp-competition-20260726T2018Z/ask-tau-minimum-mvp-competition-for-the--c8c97a5f8e87/node-artifacts/handler-webkimi/response.md
```

## Current Blocker Statement

The blocker is not that every component is absent. The blocker is that I do not yet have a fresh deterministic receipt for the actual voice loop:

```text
audio wake -> RealtimeSTT final transcript -> SPARTA live-turn ->
dynamic answer -> Chatterbox render -> Jabra playback ->
journal replay -> orb CDP proof
```

The smallest passing MVP narrowed one bug and repaired the typed live-turn route. It is not a substitute for the actual acoustic conversation proof.

## Fresh Rerun Learning: 2026-07-26T21:35Z

Fresh MVP command:

```bash
OUT_DIR=/tmp/embry-voice-mvp-problem-20260726T2012Z/results-r9-20260726T213537Z \
  bash /tmp/embry-voice-mvp-problem-20260726T2012Z/run_mvp.sh
```

Fresh receipt:

```text
/tmp/embry-voice-mvp-problem-20260726T2012Z/results-r9-20260726T213537Z/summary.json
```

Result:

- `pass: true`
- `mocked: false`
- `live: true`
- `checks.memory_intent.action: NO_MATCH`
- `checks.memory_intent.reason: outside_memory_domains`
- `checks.memory_answer_unix.error: curl_failed_or_timed_out`
- `checks.memory_answer_http.can_answer: false`
- `checks.tau_chat_turn.error: curl_failed_or_timed_out`
- `checks.embry_live_turn.answerAuthority: wikipedia_rest`
- `checks.embry_live_turn.answerText: Wikipedia's List of capitals of France result says: The capital of France has been Paris since its liberation in 1944.`

Fresh Chatterbox render:

```text
/home/graham/workspace/experiments/chatterbox/logs/ux-lab-embry-direct/2026-07-26T21-36-48-714Z-a912bdabfee8.json
/home/graham/workspace/experiments/chatterbox/logs/ux-lab-embry-direct/2026-07-26T21-36-48-714Z-a912bdabfee8.wav
```

Render facts:

- `backend: chatterbox-direct`
- `engine: chatterbox_turbo`
- `generation_seconds: 2.27`
- `duration_seconds: 7.08`
- `realtime_factor: 0.321`
- WAV: PCM 16-bit mono, 24000 Hz, 339918 bytes
- `audio_authority.envelope.frame_count: 443`
- `audio_authority.envelope.nonzero_frames: 442`
- envelope has nonzero `level`, `rms`, `bass`, `mid`, and `treble` fields suitable for orb animation input

Jabra playback attempt:

```text
/tmp/embry-voice-mvp-problem-20260726T2012Z/results-r9-20260726T213537Z/jabra-playback-attempt.json
```

Result:

- `mocked: false`
- `live: true`
- target: `alsa_output.usb-0b0e_Jabra_SPEAK_510_USB_501AA5274B1D022000-00.analog-stereo`
- `returncode: 0`

Jabra microphone capture during playback:

```text
/tmp/embry-voice-mvp-problem-20260726T2012Z/results-r9-20260726T213537Z/jabra-mic-capture-20260726T213849Z.json
```

Result:

- `mocked: false`
- `live: true`
- record target: `alsa_input.usb-0b0e_Jabra_SPEAK_510_USB_501AA5274B1D022000-00.mono-fallback`
- capture WAV: `/tmp/embry-voice-mvp-problem-20260726T2012Z/results-r9-20260726T213537Z/jabra-mic-capture-20260726T213849Z.wav`
- `capture_bytes: 477168`
- `duration_seconds: 9.940083`
- `mean_volume_db: -50.6`
- `max_volume_db: -37.6`
- `playback_returncode: 0`
- `record_returncode: 124`, because timeout ended the bounded recording window

RealtimeSTT/Whisper check against that Jabra mic capture:

```text
/tmp/embry-voice-mvp-problem-20260726T2012Z/results-r9-20260726T213537Z/jabra-mic-capture-realtimestt-whisper-container-key.json
```

Result:

- `ok: false`
- `mocked: false`
- `live: false`
- failed gate: `realtimestt_transcript_present`
- OpenAI-compatible Whisper executor was reached with the actual container key
- `asr_executor_call_count: 1`
- ASR transcript: empty string

New learning:

The MVP answer can be dynamically generated, rendered by Chatterbox, and played to the Jabra speaker. The Jabra mic records a real but quiet signal during that playback. Feeding that captured WAV into the RealtimeSTT bridge reaches the live Whisper executor, but the executor returns an empty transcript. The current smallest blocker is therefore below SPARTA and Chatterbox: the acoustic/Jabra capture level or capture route is not producing ASR-usable speech for this playback loop.
