# Real-call harness (Phase 2a merge gate)

`pytest -m realcall` places REAL GSM calls A06 -> A14. Never runs in CI.

## Prerequisites

- SSH to the VPS reachable; `adb` there sees A14 and A06 over Tailscale
  (`adb devices -l` — both serials present).
- A06: active SIM with minutes, screen unlocked. `check_preconditions()`
  cannot detect this — pkill/keyevent/app_process all "succeed" against a
  locked screen too.
- A14: PhoneGate running, daemon connected (`connected=true`, `mode=Zero-ADB`).
- JobHunter `call-agent` running against the same PhoneGate with
  `PHONE_AUTO_ANSWER_ENABLED=true`.
- PhoneGate's own venv on the rig has `edge-tts` and `ffmpeg` available
  (`/srv/phonegate/venv/bin/edge-tts`) — `inject_uplink_speech` synthesizes
  on the rig itself, not on whatever machine invokes the harness.

## Run

```bash
ENABLE_REALCALL_TESTS=true \
PHONEGATE_URL=https://phonegate.46-225-103-75.sslip.io \
PHONEGATE_AUTH_TOKEN=... \
REALCALL_A14_NUMBER=... REALCALL_A06_NUMBER=... \
REALCALL_A14_SERIAL=... REALCALL_A06_SERIAL=... \
uv run pytest -q -m realcall tests/realcall/
```

## 2a "done" checklist

- [ ] `pytest -m realcall` green against the live A14 gate
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

This has been implemented and exercised for code correctness (unit tests,
lint, type-check) but the actual `pytest -m realcall` live-hardware run
against a real GSM call is a separate, explicit step — see the checklist
above.
