# Voice capture and live transcription

Use native audio capture, provisional transcripts, durable upload and explicit recovery after interruptions.

[Project overview](README.md) · [Setup](GETTING_STARTED.md) · [Verification](VERIFICATION.md)

Detailed material below is copied from the former README. Dated implementation and test notes describe their original verification stage; consult the [acceptance history](ACCEPTANCE_HISTORY.md) for later operator results.

## Incremental transcripts

## User flow

The default `teach-voice` workflow still captures and uploads recoverable audio first, then starts transcription. The optional `--live-transcript` flag starts a single Prisma realtime attempt while the microphone is active. It is available only for native microphone capture with the durable streaming workflow; file uploads and the legacy `--clip` path do not use it.

```sh
uv run --no-sync --package tacit-cli tacit teach-voice \
  --repo REPOSITORY_ID \
  --commit PUSHED_40_CHARACTER_COMMIT \
  --file path/to/source.py \
  --seconds 65 \
  --language en-IN \
  --live-transcript
```

The command asks for explicit consent before the provider stream and microphone start. Starting realtime can consume credits even when recording is cancelled or the final request fails. Provider credentials remain on the backend. A backend without the live capability is rejected before capture.

Completed provider segments are displayed incrementally on stderr as provisional text. They are not invented word-by-word partials. The live adapter requests a five-second maximum speech segment through Prisma's `x-max-speech-s` setting; this is a segmentation setting, not a measured latency guarantee. Speech activity/segment completion determines when a new segment is available; there may be a delay after speaking starts. Provider text is sanitized before terminal display. Final command JSON remains on stdout. Interim text is not a confirmed rule or a published snapshot.

Recording controls remain `p` + Enter to pause, `r` + Enter to resume, empty Enter to finish and Ctrl+C to cancel. A pause retains the same provider attempt. Tacit does not fill pauses with silence or reconnect the paid provider stream automatically; sufficiently long pauses or connection loss can end that attempt. Local transport heartbeat does not guarantee the provider will keep its session open.

## Transport and durable boundaries

`GET /v1/repos/{rid}/voice-capabilities` declares `live_transcript`. The additional WebSocket route is `/v1/repos/{rid}/voice-sessions/{sid}/live`. It requires a fresh empty session belonging to the authenticated human maintainer; agents and learners cannot start it. Existing version headers apply to its handshake.

| Direction | Frame | Meaning |
|---|---|---|
| Server → client | `{"type":"session","session":...}` | Reserved live session |
| Client → server | Binary: eight-byte big-endian sequence followed by PCM16 | One durable audio chunk, at most one second |
| Server → client | `{"type":"ack","chunks":N}` | Durable chunk count |
| Server → client | `{"type":"transcript","segment":...}` | Validated provider segment with index, ID and text |
| Client → server | `{"type":"ping"}` | Local transport heartbeat |
| Server → client | `{"type":"pong"}` | Heartbeat response |
| Client → server | `{"type":"finish","chunks":N,"audio_sha256":"...","capture_complete":true}` | End capture and bind the exact retained audio |
| Server → client | `{"type":"result","session":...}` | Final validated session result |
| Server → client | `{"type":"error","code":"..."}` | Safe failure code; no provider payload or credential |

Audio is retained locally and acknowledged only after encrypted backend storage. The backend reserves the operation before opening a provider stream and validates the final audio digest and source binding. Authorization is checked throughout the connection and before delivering transcript/final results. Resource queues, message sizes and deadlines are bounded. No remote MCP or new agent write capability is added.

The provider receives sequenced capture through one stream, with its required framing and final flush; successful live capture does not trigger a second post-recording transcription. Provider segments are validated for identity, size and ordering before display. Final transcript evidence is saved through the existing reviewable draft workflow. Explicit human `review`/`confirm` remains required to publish repository knowledge.

The shared provider parser also recognizes the documented one-based segment indexes as well as contiguous zero-based legacy indexes, without rewriting source evidence. Previously a valid one-based response could be marked incomplete. Settings and framing were checked against [Gnani's realtime documentation](https://docs.gnani.ai/api/STT/stt-websocket); that documentation does not establish live acceptance of this implementation.

## Failure and recovery

A lost live stream is not resumable as a paid stream. Preview text is not durable authoritative transcription. A failed, interrupted or ambiguous attempt retains local/server audio according to existing retention rules, but does not silently replay it. A saved final transcript can be recovered through the existing session recovery path without another provider call. Without a saved final transcript, deliberately starting a new operation is a separate potentially billable choice; export the retained WAV and inspect it first. Cancelling removes retained audio through the existing cancellation workflow; it cannot undo credits already used.


