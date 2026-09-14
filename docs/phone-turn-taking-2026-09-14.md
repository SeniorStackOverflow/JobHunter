# Phone call turn-taking hardening — 2026-09-14

## Why this change exists

The 2026-09-14 production calls showed a strong behavioral pattern: answered employer calls ended during the second assistant prompt or very shortly after it. PhoneGate A/B checks showed RX works on both daemon 0.2.36 and 0.2.37 before and after TTS, so a blanket RX/HAL failure is not a sufficient explanation.

The old JobHunter flow was also structurally hostile to callers:

```text
answer -> greeting TTS -> sleep 250 ms -> questionnaire TTS -> LISTENING
```

The 250 ms interval was not a real listening window. JobHunter did not consume RX there, so a caller starting to answer could immediately be talked over by the next TTS block.

## New dialogue contract

```text
answer
  -> post-connect recovery
  -> one transparent disclosure + simple work-related question
  -> WAIT_FIRST_RESPONSE (real transcript/status polling, default 4.5 s)
     -> RX: continue
     -> timeout: one short retry
     -> remote end: terminal remote_ended
  -> WAIT_FIRST_RESPONSE_RETRY (default 4.5 s)
     -> RX: continue
     -> timeout: no-response closing + review
  -> optional short details prompt only for terse/non-critical first replies
  -> LISTENING
  -> normal closing
```

No vacancy/date/time/address/timezone questionnaire is fired immediately after the greeting. If the employer already supplied substantive or critical details, JobHunter does not interrupt them with the details prompt.

## Remote hangup semantics

A remote/network end is not a technical orchestrator error. `remote_ended` is a terminal script stage separate from `aborted_error`.

JobHunter persists the dialogue phase where the remote end was observed (`intro_tts`, `wait_first_rx`, `retry_tts`, `wait_first_rx_retry`, `details_tts`, or `listening`).

`aborted_error` remains reserved for actual technical failures such as PhoneGate transport errors, invalid state transitions, or hard-cap failure during the opening sequence.

## PhoneGate lifecycle integration

Call sessions are linked to PhoneGate lifecycle records with a generation-scoped external identity:

```text
<phonegate_generation>:<phonegate_call_id>
```

This avoids call-ID collision across PhoneGate restarts.

`call_lifecycle` data is persisted into `CommunicationSession.diagnostics`, including:

- `phonegate_end_reason`
- `last_tts_ended_at_ms`
- `peer_hangup_ms_after_last_tts`
- `rx_audio_bytes`
- `rx_audio_duration_ms`
- `phonegate_audio_evidence_path`
- `phonegate_audio_evidence_sha256`
- `call_disposition`

A remote hangup with no employer transcript is classified as `probable_prompt_rejection` when the lifecycle timing or observed remote-end phase strongly supports that interpretation. This is deliberately a disposition, not a claim that the employer's intent is known with certainty.

## Post-call behavior

For completed auto-answered calls with zero employer turns:

- probable prompt rejection -> summary `skipped`, verification `not_applicable`, no new manual-review flag;
- unexplained missing employer transcript -> summary `skipped`, verification `needs_review`, manual review remains required;
- connected silence that reached the no-response closing remains `needs_review` and is not rewritten as a prompt rejection.

Existing confirmed facts are not downgraded by this branch.

## Reporting

Daily phone metrics now separate assistant and employer evidence. In addition to legacy `calls_with_transcript`, the report exposes:

- assistant/employer transcript turn counts;
- `calls_with_employer_transcript`;
- `assistant_only_calls`;
- calls with raw RX audio;
- calls with RX audio but no employer ASR;
- remote/network hangup counts;
- hangups within 1 s / 3 s after completed TTS;
- median post-TTS hangup delay;
- probable prompt rejections;
- per-call lifecycle evidence in `analysis_items`.

The goal is to prevent assistant-only transcripts from being reported as successful conversation evidence.
