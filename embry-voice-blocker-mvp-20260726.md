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
