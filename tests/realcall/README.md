# Real-call harness (Phase 2a merge gate)

`pytest -m realcall` places REAL GSM calls A06 -> A14. Never runs in CI.

## Prerequisites

- SSH to the VPS reachable; `adb` there sees A14 and A06 over Tailscale.
- A06: active SIM with minutes, screen unlocked, proven `.dex` present.
- A14: PhoneGate running, daemon connected (`connected=true`, `mode=Zero-ADB`).
- JobHunter `call-agent` running against the same PhoneGate with
  `PHONE_AUTO_ANSWER_ENABLED=true`.

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

## Incomplete pieces to finish on the rig

`a06_originate.py` `inject_uplink_wav` / `start_downlink_recording` / `stop_downlink_recording`
raise NotImplementedError — wire them from
`/srv/phonegate/WORKING_DO_NOT_TOUCH_PROVEN/` (a06_call_record.py, a14_call_inject.py,
CallStreamer, ParamSetter, ReceiverRecorder) for the A06->A14 direction. Do NOT edit
that protected directory.
