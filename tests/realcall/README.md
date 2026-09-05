# Real-call harness (Phase 2a merge gate)

`pytest -m realcall` places REAL GSM calls A06 -> A14. Never runs in CI.

## Prerequisites

- SSH to the VPS reachable; `adb` there sees A14 and A06 over Tailscale
  (`adb devices -l` — both serials present).
- A06: active SIM with minutes, screen unlocked, and a **default app set for
  `tel:` intents**. `check_preconditions()` cannot detect either of these —
  pkill/keyevent/app_process all "succeed" against a locked screen too, and
  `am start -a android.intent.action.CALL` against no default just opens an
  app chooser and silently never dials. Fix once per device:
  `adb shell cmd telecom set-default-dialer <package>`, or tap through the
  chooser once and choose "Always".
- A14: PhoneGate running, daemon connected (`connected=true`, `mode=Zero-ADB`).
- JobHunter `call-agent` running against the same PhoneGate with
  `PHONE_AUTO_ANSWER_ENABLED=true`, pointed at a database that has at least
  one `UserProfile` with `is_default=True` — `IngestLoop` logs
  `phone_no_default_profile` and drops the call otherwise.
- PhoneGate's own venv on the rig has `edge-tts` and `ffmpeg` available
  (`/srv/phonegate/venv/bin/edge-tts`) — `inject_uplink_speech` synthesizes
  on the rig itself, not on whatever machine invokes the harness.
- `REALCALL_GROQ_API_KEY` for the happy-path scenario's downlink transcript
  check — reuse one of PhoneGate's own Groq keys (`/srv/phonegate/.env`,
  `GROQ_API_KEY`/`GROQ_API_KEYS`). Deliberately NOT a local
  faster-whisper/WhisperModel load: this rig runs the full production
  JobHunter stack alongside PhoneGate on a memory-constrained VPS, and
  loading a local ASR model here once got the test process OOM-killed
  mid-run. Without this var set, that one scenario skips (the two
  lighter-assertion scenarios don't need it).
- If running the `call-agent` as a bare process rather than via
  `docker compose` (e.g. no dedicated Redis container exposed on the host),
  a disposable Redis is enough — its keys are fully namespaced
  (`job-agent:phone:*`) and don't collide with anything else:
  `docker run --rm -d -p <port>:6379 redis:7-alpine`.

## Run

```bash
ENABLE_REALCALL_TESTS=true \
PHONEGATE_URL=https://phonegate.46-225-103-75.sslip.io \
PHONEGATE_AUTH_TOKEN=... \
REALCALL_GROQ_API_KEY=... \
REALCALL_A14_NUMBER=... REALCALL_A06_NUMBER=... \
REALCALL_A14_SERIAL=... REALCALL_A06_SERIAL=... \
uv run pytest -q -m realcall tests/realcall/
```

## 2a "done" checklist

- [x] `pytest -m realcall` green against the live A14 gate — all three
      scenarios have passed against real hardware in the same session:
      `test_realcall_per_call_hangup` and `test_realcall_runtime_stop_aborts`
      (real dial, real auto-answer, real recording, real command
      consumption, real hangup, matching DB state), and the happy path
      `test_realcall_greeting_and_capture` end to end — all 4 greeting
      blocks + the closing recorded `delivered`, the injected employer
      phrase ("Звоню по вакансии грузчика на склад") captured verbatim as
      an `employer` turn, `script_stage=greeting_completed`,
      `outcome=completed`. The word-level Groq transcript match scored 0.96
      against the script.
- [ ] Manual acceptance call passed (operator calls A14, hears the greeting,
      speaks, hears the closing, call ends — sounds acceptable to a real employer)
- [ ] Full CI sweep green (ruff, mypy, pytest, alembic check on DEV Postgres, docker compose config)

## Hardware notes

`A06Rig`'s `inject_uplink_speech` / `start_downlink_recording` /
`stop_downlink_recording` are wired to the proven, immutable toolkit at
`/srv/phonegate/WORKING_DO_NOT_TOUCH_PROVEN/` (`ReceiverRecorder` for
recording, `CallStreamer` + `ParamSetter` for injection, edge-tts + ffmpeg
for synthesis) — that directory is never edited, only read in place over
SSH. The dial direction here (A06 -> A14) is the reverse of the proven
scripts' own A14 -> A06 flow, so both the recorder and the injector run on
A06 instead of A14.

`ReceiverRecorder` has no "stop now" signal — it records for a fixed
duration and exits on its own; `stop_downlink_recording()` kills the
process early and pulls whatever was captured. Every multi-step call
(injection, recording) raises `RuntimeError` with the failing step's
stderr on the first non-zero exit, rather than continuing silently —
unlike `dial()`/`hangup()`, which a human could just retry by hand.

`CallStreamer` takes an integer-second duration and loops the injected PCM
back to the start if that duration exceeds the actual audio length —
`inject_uplink_speech` computes it as `ceil(exact_duration)` with no extra
margin: truncating undershoots and clips the last word, padding with a
multi-second margin overshoots and makes the phrase audibly repeat.

Neither the audio it records nor the audio it injects is saved anywhere
persistent — both round-trip through temp files that get deleted (or, for
the downlink recording, straight into the Groq transcription call) within
the same test run. If you need to listen back to a run's audio for manual
judgment, that's a small addition to make before relying on it.
