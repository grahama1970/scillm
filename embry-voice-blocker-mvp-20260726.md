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

## Fresh Acoustic Debugger Proof: 2026-07-26T22:56Z

Debugger script committed in Chatterbox:

```text
/home/graham/workspace/experiments/chatterbox/scripts/debug_embry_jabra_acoustic_mvp.sh
```

Chatterbox branch/commit:

```text
persona-dream-emotion-render-endpoint ac3291c7ad622b4eb95db576b206b28cf82e3e9d
```

Remote ref verification:

```text
ac3291c7ad622b4eb95db576b206b28cf82e3e9d refs/heads/persona-dream-emotion-render-endpoint
```

Live command after restarting the local Whisper container:

```bash
OUT_DIR=/tmp/embry-jabra-acoustic-debug-whisper-up-20260726T225605Z \
  /home/graham/workspace/experiments/chatterbox/scripts/debug_embry_jabra_acoustic_mvp.sh
```

Summary receipt:

```text
/tmp/embry-jabra-acoustic-debug-whisper-up-20260726T225605Z/summary.json
```

Result:

- `pass: false`
- `mocked: false`
- `live: true`
- Jabra playback target: `alsa_output.usb-0b0e_Jabra_SPEAK_510_USB_501AA5274B1D022000-00.analog-stereo`
- Jabra record target: `alsa_input.usb-0b0e_Jabra_SPEAK_510_USB_501AA5274B1D022000-00.mono-fallback`
- capture WAV: `/tmp/embry-jabra-acoustic-debug-whisper-up-20260726T225605Z/jabra-mic-capture.wav`
- `capture_bytes: 477168`
- `duration_seconds: 9.940083`
- `mean_volume_db: -42.6`
- `max_volume_db: -17.2`
- `playback_returncode: 0`
- `record_returncode: 124`, from the bounded recording timeout
- RealtimeSTT fed `498` audio chunks and `70` trailing-silence chunks
- live OpenAI-compatible Whisper executor was reached once
- failed gate: `realtimestt_transcript_present`
- transcript: empty string

Direct ASR separation:

```text
/tmp/embry-jabra-acoustic-debug-whisper-up-20260726T225605Z/direct-whisper-source.json
/tmp/embry-jabra-acoustic-debug-whisper-up-20260726T225605Z/direct-whisper-captured.json
```

Result:

- original Chatterbox source WAV -> Whisper HTTP 200 and non-empty transcript:
  `Wikipedia's list of capitals of France's results says the capital of France has been Paris since its liberation in 1944.`
- Jabra-mic-captured WAV -> Whisper HTTP 200 and empty transcript: `{"text":""}`
- gain/filter attempts at `volume=12dB`, `volume=20dB`, `highpass/lowpass + 20dB`, and `afftdn + 18dB` still returned empty transcripts

Direct RealtimeSTT bridge separation:

```text
/tmp/embry-realtimestt-source-bridge-20260726T230101Z/realtimestt-source-asr.json
```

Result:

- `ok: true`
- `mocked: false`
- `live: true`
- failed gates: `[]`
- live OpenAI-compatible Whisper executor call count: `1`
- transcript:
  `Wikipedia's list of capitals of France's result says the capital of France has been Paris since its liberation in 1944.`

This proves the RealtimeSTT bridge and Whisper executor can produce text when the input WAV is intelligible.

Device-state checks:

- `wpctl get-volume 33`: `Volume: 1.00`
- `wpctl get-volume 58`: `Volume: 0.90`
- `pw-cli enum-params 33 Props`: `mute false`, `softMute false`, source volume `1.000000`
- `amixer -c 1 scontents`: Jabra `Mic` capture `7 [100%] [9.00dB] [on]`

Direct ALSA duplex check:

```text
/tmp/embry-jabra-alsa-duplex-20260726T225734Z/summary.json
```

Result:

- ALSA playback return code: `0`
- ALSA mic capture WAV: `/tmp/embry-jabra-alsa-duplex-20260726T225734Z/alsa-jabra-mic-capture.wav`
- duration: `10.000000`
- mean volume: `-42.9 dB`
- max volume: `-29.6 dB`
- direct Whisper response: `{"text":""}`

Non-Jabra speaker output check:

```text
/tmp/embry-jabra-acoustic-debug-usb-speakers-20260726T230028Z/summary.json
```

Result:

- playback target: `alsa_output.usb-Generic_USB_Audio-00.HiFi__hw_ALC1220VBDT__sink`
- record target: `alsa_input.usb-0b0e_Jabra_SPEAK_510_USB_501AA5274B1D022000-00.mono-fallback`
- capture WAV existed, `478192` bytes, `9.961417` seconds
- mean volume: `-42.5 dB`
- max volume: `-28.6 dB`
- live Whisper executor reached once through RealtimeSTT
- failed gate: `realtimestt_transcript_present`
- transcript: empty string

Current conclusion:

Memory is not the current acoustic blocker. The Whisper ASR service is not generally broken because it transcribes the original Chatterbox WAV. The RealtimeSTT bridge is not generally broken because it also transcribes the original Chatterbox WAV through the same live executor. PipeWire is not showing the Jabra source as muted, and ALSA also reports the mic capture switch as on. The failing layer is the physical/acoustic capture path: Jabra mic recordings of speaker playback are not ASR-usable in the current workstation setup.

## Ask/Surf Competition Failure Ticket

The MVP `$ask competition` for this acoustic blocker produced zero candidate implementations because browser-oracle transport failed before reviewer output.

Competition DAG receipt:

```text
/mnt/storage12tb/skills/ask/outputs/embry-jabra-acoustic-mvp-competition-20260726T2259Z/ask-tau-embry-jabra-acoustic-mvp-competi-fa2dece807a5/tau-receipts/dag-receipt.json
```

Result:

- `status: BLOCKED`
- `ok: false`
- `verdict: COMMAND_FAILED`
- `provider_live: false`
- `handler-webclaude`: invalid tab id
- `handler-webkimi`: invalid tab id
- `handler-webgrok`: missing live tab
- `handler-webgpt`: local-path preflight rejection; requested concatenated text or small zip

Filed ticket:

```text
https://github.com/grahama1970/agent-skills/issues/1024
```
