# JobHunter Phone Call Agent Architecture

Status: proposed architecture, benchmark-backed

Benchmark baseline: 2026-09-01

Related systems:

- JobHunter: `/home/andrei/JobHunter`
- PhoneGate: `/home/andrei/projects/PhoneGate`
- llmRouter: `/home/andrei/llmRouter`
- Production GSM gateway: Samsung Galaxy A14 (SM-A145F)
- GSM canary/test handset: Samsung Galaxy A06 (SM-A065F)

This document defines the architecture for autonomous employer-call handling in JobHunter using PhoneGate as the telephony/audio transport and llmRouter as the realtime LLM gateway.

The design is intentionally conservative around ASR errors, scheduling, facts about the candidate, and employer-facing commitments. The system is allowed to coordinate and schedule interviews on Andrei's behalf, but it must never treat a raw ASR transcript or an LLM interpretation as authoritative truth.

---

## 1. Goals

The phone subsystem should allow JobHunter to:

1. Receive employer calls through the A14 GSM SIM.
2. Answer automatically when policy permits.
3. Immediately reassure the caller that they reached the correct number.
4. Transparently disclose that the caller is speaking with Andrei's voice assistant.
5. Identify the company, vacancy, caller purpose, and relevant application.
6. Coordinate interview date, time, format, address, contact person, and required preparation.
7. Verify critical information despite imperfect ASR.
8. Check candidate availability deterministically before accepting an interview slot.
9. Avoid inventing facts about Andrei's experience, skills, salary expectations, history, documents, or availability.
10. Produce a structured post-call result and concise notification.
11. Store sufficient evidence to audit important facts later.
12. Degrade only the phone channel when PhoneGate, GSM, ASR, TTS, or the realtime LLM is unavailable; crawler, matching, Gmail, learning, and other JobHunter functions must continue.
13. Use A06 as a real GSM canary for PhoneGate deployments and periodic physical end-to-end checks.

Non-goals for the first production phase:

- Free-form autonomous job interviews.
- Salary negotiation.
- Making commitments beyond verified scheduling authority.
- Answering unverified personal questions about Andrei.
- Treating call outcomes as preference-learning labels.
- Embedding the LLM inside PhoneGate.

---

## 2. Core architectural boundary

The fundamental split is:

```text
PhoneGate = ears + mouth + hands + GSM transport
JobHunter = context + policy + dialogue state + scheduling authority
llmRouter = bounded semantic reasoning
```

### PhoneGate responsibilities

PhoneGate owns physical and media concerns:

```text
GSM telephony
call control
RX audio
20 ms audio framing
VAD
ASR
TTS
TX/uplink audio
SMS
contacts
SIM/account telemetry
A14 daemon transport
```

PhoneGate must not know anything about:

```text
job preferences
applications
vacancy meaning
interview policy
candidate availability
business outcomes
learning labels
LLM dialogue policy
```

### JobHunter responsibilities

JobHunter owns business meaning and authority:

```text
caller correlation
company/vacancy/application context
conversation state
allowed actions
verified candidate facts
scheduling policy
availability/calendar checks
ASR confidence handling
critical-fact confirmation
LLM prompt/context construction
response policy
communication history
post-call outcome
notifications
```

### llmRouter responsibilities

llmRouter is a model transport and failover layer. The realtime model should perform narrowly bounded semantic work:

```text
understand caller intent
extract candidate facts from a turn
classify ambiguity
propose dialogue act
optionally propose short wording
```

It must not directly control PhoneGate and must not be the authority on scheduling, candidate facts, or confirmation state.

Correct flow:

```text
LLM
 ↓
proposed structured action
 ↓
JobHunter deterministic policy
 ↓
approved action
 ↓
PhoneGate
```

Incorrect flow:

```text
LLM → PhoneGate dial / speak / hangup
```

---

## 3. Physical topology

```text
                         JobHunter
                            │
                    Call Orchestrator
                            │
             ┌──────────────┼──────────────┐
             │              │              │
        Context/Policy   llmRouter      JobHunter DB
             │              │              │
             └──────────────┴──────────────┘
                            │
                     PhoneGateClient
                            │
                       REST/events
                            │
                            ▼
                       PhoneGate VPS
                            │
                       Zero-ADB WS
                            │
                            ▼
                  Samsung A14 / Orange
                            │
                           GSM
                            │
                        Employer


                  Samsung A06 / test SIM
                            │
          GSM canary + auto-answer + recorder
                            │
                     tests A14/PhoneGate
```

A14 is the production modem.

A06 is not initially a second equal production gateway. It is more valuable as a physical end-to-end canary and backup/test line because it already supports proven auto-answer, digital downlink recording, direct uplink injection, SMS, and Samsung call-audio parameter control.

---

## 4. Measured baseline

All values in this section were measured on 2026-09-01 and should be treated as operational baselines, not permanent constants.

### 4.1 VPS/device transport

```text
VPS → A14 ping
avg: 61.6 ms
packet loss: 0%

VPS → A06 ping
avg: 43.7 ms
packet loss: 0%

ADB shell A14
p50: ~115 ms
p95: ~367 ms

ADB shell A06
p50: ~117 ms
p95: ~168 ms

PhoneGate local cached REST /status
p50: 1.81 ms
p95: 2.68 ms
```

Conclusion: the VPS and local REST path are not meaningful contributors to multi-second call latency.

### 4.2 GSM call establishment

Incoming A06 → A14 tests:

```text
remote dial → A14 RINGING: roughly 5.8–6.5 s
PhoneGate POST /api/call/answer: a few milliseconds
answer → A14 IN_CALL: roughly 1.0–1.35 s
```

Outgoing A14 → A06 with proven A06 auto-answer:

```text
A06 RINGING after dial: ~5.0–5.6 s
auto-answer RINGING → ACTIVE: ~1.1–1.6 s
dial → A06 ACTIVE: ~6.3–7.0 s
```

Important semantic warning:

On outbound A14 calls, PhoneGate `IN_CALL` can represent Samsung telephony OFFHOOK/DIALING before the remote party actually answers. Therefore outbound `IN_CALL` must not be treated as proof of remote connection.

### 4.3 RX audio integrity

Direct digital A06 uplink → GSM → A14 PhoneGate RX test with a known 1 kHz signal:

```text
captured_frames = 457
queued_frames   = 457
dropped_frames  = 0
RMS             ≈ 8354
peak            ≈ 13817
1 kHz energy    ≈ 99.98%
```

Long stress test:

```text
captured_frames = 1447
queued_frames   = 1447
dropped_frames  = 0
20 ms frames
≈28.94 s of RX accounting
```

Conclusion: the current Zero-ADB RX path is stable enough that JobHunter should not add a second custom jitter buffer on top of PhoneGate.

### 4.4 ASR latency and quality

PhoneGate uses Groq Whisper for cloud ASR.

Measured backend latency on real GSM speech:

```text
roughly 0.2–0.3 s in the latest full-turn tests
```

PhoneGate VAD end-of-turn silence timeout:

```text
320 ms
```

Corrected full-turn tests measured:

```text
end of remote GSM speech → transcript
626 ms
648 ms
677 ms
```

This matches the expected VAD + ASR pipeline.

ASR is not perfect. Real tests produced correct critical values such as `четверг` and `14 часов`, but also garbled noncritical words. Examples included malformed phrases similar to `среди седований` and imperfect recognition of the word `PhoneGate`.

Therefore raw ASR output must always be treated as a hypothesis, not a fact.

### 4.5 Piper TTS baseline

Installed backend:

```text
piper-tts 1.7.0
ru_RU-dmitri-medium
model path:
/home/andrei/.cache/phonegate/piper/ru_RU-dmitri-medium.onnx
```

PhoneGate live process confirmed local Piper loading through `onnxruntime` and log evidence.

Fifteen unique short employer-facing phrases:

```text
min:  586 ms
p50:  683 ms
avg:  734 ms
p95: 1035 ms
max: 1035 ms
```

Longer phrases were roughly 1.4–1.8 s.

Cached/prefetched phrases avoid synthesis and can be prepared on the device in only a few to a few tens of milliseconds, although actual device-side TX activation still has additional startup latency.

### 4.6 Corrected full conversational turn

Three corrected E2E tests used:

```text
A06 known speech
→ direct GSM uplink
→ A14 PhoneGate RX
→ VAD
→ Groq Whisper
→ transcript
→ immediate PhoneGate speak
→ Piper
→ A14 GSM TX
→ A06 digital VOICE_DOWNLINK recording
```

Measured:

```text
speech end → transcript
0.626–0.677 s

transcript → tx_active
1.66 s
2.08 s
2.56 s
```

Without LLM:

```text
speech end → start of assistant TX
approximately 2.3–3.2 s
```

With a ~0.3–0.5 s realtime LLM budget, expected normal response start is approximately:

```text
2.6–3.7 s after caller finishes speaking
```

This is the correct planning range for the initial production agent.

### 4.7 SMS

Six real SMS tests succeeded.

```text
A14 → A06
6.003 s
4.001 s
3.728 s
median ≈ 4.0 s

A06 → A14
4.074 s
3.750 s
3.610 s
median ≈ 3.75 s
```

PhoneGate REST submission itself was only several milliseconds. SMS must therefore be treated as asynchronous GSM delivery rather than synchronous request/response transport.

### 4.8 SIM telemetry / USSD

Real Orange account refresh:

```text
~8.9 s end-to-end
```

PhoneGate already maintains a 15-minute cache and avoids refresh during an active call. This is correct. SIM account refresh must never sit on the realtime call path.

---

## 5. Realtime process model

Do not execute each call turn as a Celery task.

Create a dedicated always-running asyncio service, conceptually:

```text
jobhunter-call-agent
```

It should hold persistent connections/resources:

```text
PhoneGate httpx.AsyncClient
llmRouter httpx.AsyncClient
JobHunter DB pool
optional Calendar connector/client
```

Celery remains appropriate for:

```text
post-call analysis
notifications
reports
reconciliation
periodic canary scheduling
background maintenance
```

A single A14 provides one physical GSM voice line, so a single active call session is the normal concurrency model.

---

## 6. PhoneGate integration

JobHunter should use a small direct REST client, not MCP.

Suggested module structure:

```text
app/phone/
    client.py
    service.py
    orchestrator.py
    schemas.py
    policy.py
    prompts.py
    facts.py
    availability.py
    response_renderer.py
```

PhoneGateClient responsibilities only:

```text
status()
events(after_id)
transcripts(after_id)
recent_audio(seconds)
answer()
speak(text)
hangup()
dial(number)      # later / controlled use
send_sms(...)      # later / controlled use
```

It must not know JobHunter business policy.

### Event consumption

PhoneGate already exposes durable-ish in-process event IDs through:

```text
GET /api/events?after_id=N
```

Initial implementation can poll locally because measured REST latency is negligible.

Suggested polling:

```text
IDLE:       200–300 ms
RINGING:     50–100 ms
CONNECTED:   50–100 ms for transcript/events
```

Persist the last consumed event ID associated with the running call-agent process/session where useful, but always resynchronize from current PhoneGate status after restart.

PhoneGate browser WebSocket is not required for the first production implementation. It may later replace polling if event delivery latency or efficiency becomes relevant.

---

## 7. Two independent state machines

Do not combine phone transport state and dialogue/business state.

### 7.1 Telephony state

```text
IDLE
  ↓
RINGING
  ↓
ANSWERING
  ↓
CONNECTED
  ↓
ENDING
  ↓
ENDED
```

For future outbound calls use a richer state model:

```text
OUTGOING_DIALING
REMOTE_RINGING_OR_UNKNOWN
CONNECTED
```

Do not infer remote answer solely from PhoneGate outbound `IN_CALL`.

### 7.2 Dialogue state

```text
GREETING
  ↓
IDENTIFY_CALLER
  ↓
IDENTIFY_VACANCY
  ↓
IDENTIFY_PURPOSE
  ↓
COLLECTING_DETAILS
  ↓
CONFIRMING_CRITICAL_FACTS
  ↓
CHECKING_AVAILABILITY
  ↓
COMMITTING_OR_PROPOSING_ALTERNATIVE
  ↓
FINAL_READBACK
  ↓
CLOSING
  ↓
COMPLETED
```

At any time:

```text
TelephonyState = CONNECTED
DialogueState  = CONFIRMING_CRITICAL_FACTS
```

This separation prevents telephony quirks from corrupting dialogue logic.

---

## 8. Hooks and first 10–15 seconds

The opening is too important to generate freely with an LLM.

Use deterministic, pre-cached phrases.

Recommended sequence:

### Hook 1: reassure immediately

```text
Здравствуйте. Если вы звоните по поводу вакансии или собеседования с Андреем, вы попали правильно.
```

Purpose: prevent immediate hang-up caused by an unexpected synthetic voice.

### Hook 2: state value

```text
Я помогу вам согласовать собеседование.
```

Purpose: the caller immediately understands that the system can solve their problem.

### Disclosure and authority

```text
Я голосовой ассистент Андрея и уполномочен согласовывать и назначать собеседования от его имени.
```

### ASR reassurance

Prefer:

```text
Важные дату, время и адрес я обязательно уточню.
```

Avoid opening with a long technical warning that the system may fail to recognize speech. The caller should hear a quality-control promise, not a disclaimer about broken software.

### First question

```text
Подскажите, пожалуйста, по какой вакансии вы звоните?
```

### Barge-in rule

If the caller starts speaking after any hook block, remaining greeting blocks should not continue over the caller. The dialogue should transition to listening immediately where technically possible.

The greeting should be composed from separate cached blocks, not one long generated TTS sentence.

---

## 9. Phrase bank

Pre-synthesize and prefetch common short phrases after PhoneGate/A14 reconnect so the common path avoids dynamic TTS.

Suggested initial phrase bank:

```text
HOOK_CORRECT_NUMBER
Здравствуйте. Если вы звоните по поводу вакансии или собеседования с Андреем, вы попали правильно.

HOOK_VALUE
Я помогу вам согласовать собеседование.

DISCLOSURE
Я голосовой ассистент Андрея и уполномочен согласовывать и назначать собеседования от его имени.

ASR_ASSURANCE
Важные дату, время и адрес я обязательно уточню.

ASK_VACANCY
Подскажите, пожалуйста, по какой вакансии вы звоните?

ASK_COMPANY
Подскажите, пожалуйста, название компании.

ASK_DATE
Подскажите, пожалуйста, дату собеседования.

ASK_TIME
Подскажите, пожалуйста, точное время.

ASK_FORMAT
Собеседование будет очно или онлайн?

ASK_ADDRESS
Подскажите, пожалуйста, адрес.

ASK_CONTACT_PERSON
С кем нужно будет связаться на месте?

REPAIR_GENERIC
Повторите, пожалуйста, последнее.

REPAIR_TIME
Не расслышал время. Подскажите ещё раз.

REPAIR_DATE
Уточните, пожалуйста, дату.

REPAIR_ADDRESS
Адрес я расслышал не полностью. Повторите, пожалуйста.

DEFER_UNKNOWN_FACT
У меня нет подтверждённой информации по этому вопросу. Я передам его Андрею. Сейчас могу помочь согласовать собеседование.

PROCESSING
Секунду, пожалуйста.

CLOSE_SUCCESS
Спасибо. Информация записана.

TECHNICAL_FAILURE
Возникла техническая проблема. Пожалуйста, отправьте информацию SMS на этот номер.
```

Dynamic TTS should mainly be used for normalized confirmation phrases containing specific dates, times, addresses, or names.

---

## 10. ASR distrust model

This is a core safety requirement.

The system must operate on the rule:

```text
ASR transcript != fact
```

An ASR transcript is only a hypothesis about what the caller said.

Likewise:

```text
LLM interpretation != fact
```

The LLM proposes meaning. JobHunter determines whether evidence is sufficient to accept or confirm it.

Correct pipeline:

```text
GSM audio
  ↓
ASR hypothesis
  ↓
semantic interpretation
  ↓
deterministic validation
  ↓
candidate fact
  ↓
confirmation when required
  ↓
confirmed fact
```

Never use:

```text
ASR → LLM → database truth
```

---

## 11. Confidence is multi-signal

Do not gate important decisions only on Whisper confidence.

Whisper can be confidently wrong.

Use multiple signals:

```text
ASR confidence
ASR text plausibility
LLM extraction confidence
context consistency
calendar validity
known company/vacancy context
contradictions with previous turns
explicit caller confirmation
```

Suggested trust classes:

### HIGH

Noncritical fact is semantically clear, contextually plausible, no conflicts.

May be accepted without immediate read-back.

Examples:

```text
company name
vacancy title
caller purpose
```

### MEDIUM

Interpretation is plausible but field is critical or some uncertainty exists.

Store as candidate and confirm.

### LOW

Transcript is garbled, contradictory, implausible, or confidence is poor.

Do not store a normalized business fact. Ask a targeted clarification question.

---

## 12. Critical facts

The following must normally receive explicit confirmation regardless of nominal ASR confidence:

```text
interview date
interview time
physical address or online format
material location details
mandatory documents/preparation if they affect attendance
```

Contact person should be confirmed when ambiguity matters.

Final read-back example:

```text
Подтверждаю: собеседование в четверг, третьего сентября, в четырнадцать часов, по адресу Индустриальная, двенадцать. Всё верно?
```

Only an affirmative caller response should transition critical facts from candidate to confirmed.

If corrected:

```text
caller: Нет, в пятнадцать.
```

update the candidate value and confirm again.

---

## 13. Targeted repair instead of repeating everything

Do not force the caller to repeat an entire sentence because one field was uncertain.

Bad:

```text
Повторите всё ещё раз.
```

Better:

```text
Четверг понял. Подскажите, пожалуйста, точное время.
```

Or:

```text
Адрес я расслышал не полностью. Повторите, пожалуйста, только адрес.
```

This makes the assistant appear competent even when ASR is imperfect.

Suggested limit:

```text
maximum ~2 clarification attempts per field
```

After repeated failure, prefer unresolved truth over invented precision:

```text
time = UNKNOWN
needs_review = true
```

Employer-facing response:

```text
Не хочу записать информацию неправильно. Я передам Андрею, что время нужно уточнить.
```

---

## 14. Deterministic validators

Before accepting an extracted value, apply deterministic domain validation.

Examples:

### Time

```text
hour must be 0..23
minute must be 0..59
```

### Relative date

LLM should preserve:

```text
в четверг
завтра
следующий понедельник
```

A deterministic DateResolver using `Europe/Chisinau` resolves the expression.

The LLM must not perform authoritative calendar arithmetic.

### Conflict detection

If caller speech contains both:

```text
четверг
4 сентября
```

and those are inconsistent for the relevant year, mark a conflict and ask for clarification.

### Address sanity

Do not reject unusual addresses automatically, but flag obviously malformed or contradictory normalized output for read-back rather than silently trusting it.

---

## 15. Fact model

Represent extracted information with provenance and state.

Conceptual structure:

```text
CallFact

field
raw_expression
normalized_value
asr_confidence
llm_confidence
source_turn_id
state
confirmed_by_turn_id
```

Recommended states:

```text
CANDIDATE
CONFIRMED
CONFLICT
UNKNOWN
```

Example:

```text
field          = time
raw_expression = "в два часа"
normalized     = 14:00
asr_confidence = 0.78
llm_confidence = 0.94
state          = CANDIDATE
```

After explicit read-back and caller `Да`:

```text
state = CONFIRMED
```

---

## 16. Evidence clips

PhoneGate already exposes recent call audio:

```text
GET /api/call/audio?seconds=1..10
```

A first production version does not need full-call archival to preserve useful evidence.

After each important transcript, JobHunter can immediately fetch the last several seconds and store a short evidence clip.

Example:

```text
CommunicationTurn #7
speaker = employer
text = "В четверг в четырнадцать"
asr_confidence = 0.78
audio_evidence = turn_7.wav
```

For critical facts this allows later manual verification against the original GSM audio.

Retention should be bounded and privacy-conscious. Evidence clips should be retained only as long as operationally useful.

---

## 17. Caller/application correlation

Current JobHunter source data already contains:

```text
SourceJob.public_phone
SourceJob.public_phones
```

Add:

```text
ContactType.PHONE
```

Normalize phone contacts to E.164.

On incoming call:

```text
caller number
  ↓
normalize E.164
  ↓
search EmployerContact(PHONE)
  ↓
related SourceJob / CanonicalJob
  ↓
related Application
```

A known caller number provides useful context but must not become a strict allowlist.

Employers may call from:

```text
HR personal mobile
reception
company switchboard
manager mobile
another company number
```

Unknown employer numbers should still be answered according to configured policy and identified conversationally.

---

## 18. Realtime LLM contract

The realtime model should receive narrow domain context.

Example input context:

```text
caller_number
matched_company
matched_vacancy
application_status
verified_candidate_facts
current_dialogue_state
conversation_recent_turns
known_call_facts
missing_required_facts
allowed_dialogue_acts
```

The model should return strict validated JSON.

Example:

```json
{
  "intent": "interview_invitation",
  "dialogue_act": "confirm_datetime",
  "facts": {
    "date_expression": "в четверг",
    "time_expression": "в два часа дня"
  },
  "confidence": {
    "date": 0.91,
    "time": 0.87
  },
  "should_defer": false
}
```

The realtime model should prefer semantic extraction over free prose generation.

JobHunter ResponseRenderer maps `dialogue_act` to a cached or controlled phrase whenever possible.

---

## 19. Realtime llmRouter profile

Create a dedicated logical profile:

```text
call-realtime
```

Do not use a generic dynamic `fast` tier as the sole selector.

Current benchmark-backed preference:

```text
1. Groq
   qwen/qwen3.8-27b

2. Cloudflare
   @cf/meta/llama-4-scout-17b-16e-instruct

3. Google
   models/gemini-3.1-flash-lite-preview

4. Cloudflare secondary
   @cf/aisingapore/gemma-sea-lion-v4-27b-it

5. deterministic safe fallback
```

Reasons:

- Groq Qwen was extremely fast and semantically strong, but burst quota/rate limiting can arrive abruptly.
- Cloudflare Llama 4 Scout was the most consistently correct stable fallback in deep call-specific tests.
- Google is useful as a different provider failure domain.
- Provider-aware cooldown is required so one provider-wide 429 does not trigger sequential futile attempts across many models on the same exhausted provider.

### Hedged fallback

For realtime calls, consider hedged inference:

```text
start Groq primary
  ↓
if no valid response after ~400–500 ms
  ↓
start Cloudflare fallback in parallel
  ↓
accept first valid schema-compliant answer
```

Only hedge on slow primary requests. Do not duplicate every normal request.

### Time budget

The LLM should not be allowed to create long silence.

If no usable semantic decision arrives within a small realtime budget, use a cached repair/processing phrase rather than waiting indefinitely.

Examples:

```text
Секунду, пожалуйста.
```

or:

```text
Повторите, пожалуйста, последнее.
```

---

## 20. LLM restrictions

The realtime LLM must not:

```text
invent candidate work history
invent SAP/ERP/tool experience
invent education
invent salary expectations
promise attendance without availability check
change confirmed dates silently
negotiate salary
commit to unknown documents or legal terms
invent an employer address
mark an unconfirmed fact confirmed
```

If asked an unsupported question:

```text
У меня нет подтверждённой информации по этому вопросу. Я передам его Андрею. Сейчас могу помочь согласовать собеседование.
```

This response should preferably come from a controlled template.

---

## 21. Availability and scheduling authority

The assistant is authorized to coordinate and schedule interviews on Andrei's behalf.

That authority does not mean the LLM guesses availability.

Introduce an AvailabilityService:

```text
proposed interview slot
  + UserProfile.availability
  + Calendar busy/free when available
  ↓
AVAILABLE | CONFLICT | UNKNOWN
```

Only deterministic `AVAILABLE` permits final acceptance.

If conflict:

```text
Это время занято. Могу предложить другое время.
```

If availability cannot be checked:

```text
Я записал предложенное время и передам его Андрею для подтверждения.
```

This distinction prevents the agent from making false commitments.

---

## 22. Response generation strategy

Use three levels:

### Level 1: fixed cached phrase

Use whenever a standard dialogue act exists.

Fastest and most reliable.

### Level 2: template with normalized values

Example:

```text
Правильно понимаю: {date_spoken} в {time_spoken}?
```

Date/time values come from deterministic normalization.

### Level 3: constrained dynamic wording

Use only when neither a fixed phrase nor template is appropriate.

Even then the LLM proposes wording and JobHunter may validate/shorten it before TTS.

Keep telephone replies short because Piper synthesis scales with phrase length and long spoken turns reduce conversational quality.

---

## 23. Dialogue policy example

```text
HOOK
 ↓
IDENTIFY COMPANY
 ↓
IDENTIFY VACANCY
 ↓
IDENTIFY PURPOSE
 ↓
interview invitation?
 ├─ no → bounded handling / defer
 └─ yes
      ↓
   collect date
      ↓
   collect time
      ↓
   collect format
      ↓
   collect address/link
      ↓
   collect contact person / preparation
      ↓
   confirm critical facts
      ↓
   check availability
      ↓
   accept or propose alternative
      ↓
   final read-back
      ↓
   close
```

Inside every collection step:

```text
listen
 ↓
ASR hypothesis
 ↓
trust assessment
 ├─ low → targeted repair
 └─ usable
      ↓
   extract candidate fact
      ↓
   deterministic validation
      ↓
   critical?
   ├─ yes → explicit confirmation
   └─ no  → accept
```

---

## 24. Data model

Do not overload `ApplicationStatus` with CRM/call states.

Current Application lifecycle remains delivery-oriented:

```text
PREPARED
PENDING_REVIEW
APPROVED
AUTO_APPROVED
SENDING
SENT
DELIVERY_UNKNOWN
FAILED
BLOCKED
CANCELLED
```

Phone communication should have separate entities.

### CommunicationSession

Conceptual fields:

```text
id
profile_id
application_id nullable
canonical_job_id nullable
source_job_id nullable
contact_id nullable
channel              call | sms | email
transport             phonegate | gmail | ...
direction             inbound | outbound
remote_address        normalized phone/email
started_at
ringing_at nullable
answered_at nullable
ended_at nullable
outcome nullable
needs_review
metadata
```

### CommunicationTurn

```text
id
session_id
speaker               employer | assistant | system
text
raw_text nullable
started_at nullable
created_at
asr_backend nullable
asr_confidence nullable
llm_provider nullable
llm_model nullable
dialogue_act nullable
audio_evidence_key nullable
metadata
```

### CallFact

```text
id
session_id
source_turn_id
field
raw_expression
normalized_value
asr_confidence
llm_confidence
state
confirmed_by_turn_id nullable
created_at
updated_at
```

### InterviewAppointment

```text
id
profile_id
application_id
communication_session_id
starts_at nullable
timezone
format                 onsite | remote | phone | unknown
address nullable
meeting_url nullable
contact_person nullable
preparation nullable
status                 proposed | confirmed | needs_review | cancelled
confirmed_at nullable
created_at
updated_at
```

Do not store call outcomes as review-learning preference labels. Employer response/outcome modeling, if added later, should be a separate learning domain.

---

## 25. Post-call processing

Realtime and post-call reasoning should be separate.

During call:

```text
small/fast model
recent turn context
strict schema
short response
```

After call:

```text
full transcript
confirmed facts
application/vacancy context
evidence metadata
  ↓
stronger smart/quality model
  ↓
PostCallResult
```

Example:

```json
{
  "outcome": "INTERVIEW_SCHEDULED",
  "company": "Example SRL",
  "vacancy": "Warehouse Operator",
  "date": "2026-09-03",
  "time": "14:00",
  "format": "onsite",
  "address": "...",
  "critical_facts_confirmed": true,
  "needs_review": false
}
```

The post-call model may summarize evidence but may not upgrade an unconfirmed fact into a confirmed fact.

---

## 26. Notification

High-confidence confirmed result:

```text
📞 Example SRL — Warehouse Operator
Собеседование: 3 сентября, 14:00
📍 str. Industrială 12, Chișinău
Дата и время подтверждены.
```

Uncertain result:

```text
📞 Example SRL
Вероятно пригласили на собеседование 3 сентября.
Время распознано ненадёжно.
⚠️ Требуется проверить запись.
```

Telegram is the target notification channel for post-call results. Phone handling must not depend on Telegram delivery; notification failure must not affect call completion or appointment state.

---

## 27. Failure domains

Phone functionality must be a separate health domain.

Suggested components:

```text
phonegate_transport
a14_daemon
gsm_call_control
gsm_rx
gsm_tx
asr
tts
llm_realtime
sms
sim_account
```

Examples:

### Piper unavailable, Edge fallback available

```text
tts = degraded
phone channel may continue with higher latency
```

### Groq ASR unavailable

```text
voice_ai = degraded/unavailable
SMS and other JobHunter channels continue
```

### Realtime LLM unavailable

Use bounded deterministic repair/fallback flow. Do not stall the caller indefinitely.

### A14 disconnected

```text
gsm_transport = unavailable
```

Disable automatic phone handling only.

Do not trigger:

```text
global JobHunter pause
crawler pause
matching pause
email pause
learning pause
```

---

## 28. Realtime failure UX

The caller must not sit through long backend retries.

Example policy:

```text
primary LLM slow
  ↓
hedge fallback
  ↓
no valid result in bounded budget
  ↓
cached "Секунду, пожалуйста."
  ↓
one limited retry/repair path
```

If the intelligent path remains unavailable:

```text
Возникла техническая проблема. Пожалуйста, отправьте информацию SMS на этот номер.
```

Then close gracefully and create an alert/needs-review session.

---

## 29. Audio metrics

PhoneGate RX counters may reset between calls. Do not compute integrity by subtracting arbitrary process-lifetime values across call boundaries.

Persist per-call metrics:

```text
captured_frames
queued_frames
dropped_frames
duration
```

For 20 ms frames expected rate is approximately:

```text
50 frames / second
```

Useful loss ratio:

```text
loss_ratio = dropped_frames / (queued_frames + dropped_frames)
```

Suggested initial thresholds:

```text
0%      healthy
<0.5%   warn
>=0.5%  degraded
```

Tune after collecting real production data.

---

## 30. SMS semantics

SMS should be asynchronous and idempotent.

Because GSM delivery can take several seconds and a POST failure can be ambiguous, use states similar to:

```text
SMS_SENDING
SMS_SENT_TO_DEVICE
SMS_DELIVERY_UNKNOWN
SMS_FAILED
```

Do not blindly retry an ambiguous outbound send, because duplicate employer SMS is worse than an explicit unknown state.

---

## 31. SIM balance/minutes/SMS telemetry

PhoneGate now supports Orange and Moldcell account telemetry through USSD parsing.

This belongs in operational health/telemetry, not call dialogue.

Rules:

```text
use cached value
refresh periodically
never block call handling on USSD
never refresh during active call
alert on low balance/minutes according to policy
```

Measured Orange forced refresh was roughly 8.9 seconds, confirming that it must remain off the realtime path.

---

## 32. A06 GSM canary

A06 already has proven tooling for:

```text
auto-answer
VOICE_DOWNLINK digital recording
ExactAudioPlayer
Samsung ParamSetter
direct GSM uplink injection
SMS sending
ADB/Tailscale control
```

Use it as a physical integration test, not merely a mock target.

### Post-deploy call canary

```text
A06 calls A14
  ↓
A14/PhoneGate answers
  ↓
A06 sends known tone/phrase into GSM uplink
  ↓
PhoneGate RX/VAD/ASR validates it
  ↓
PhoneGate sends known response
  ↓
A06 digitally records VOICE_DOWNLINK
  ↓
RMS/spectrum/ASR verifies response
```

This validates:

```text
real SIM
real mobile network
real call establishment
A14 RX
PhoneGate audio transport
ASR
TTS
A14 TX
remote GSM downlink
```

This is substantially more valuable than only testing HTTP endpoints.

### Frequency

Run:

```text
after meaningful PhoneGate/daemon deploy
periodically, e.g. daily or less frequently
```

Do not run frequently enough to waste included call minutes unnecessarily.

### SMS canary

Periodically perform a unique-token SMS round trip in both directions.

Canary failure should set:

```text
gsm_transport_degraded
```

but must not globally pause JobHunter.

---

## 33. Security and privacy boundaries

- PhoneGate token remains a transport credential and must not be exposed to the LLM.
- LLM receives normalized domain context, not raw PhoneGate control APIs.
- Candidate personal facts supplied to the realtime model must come from verified JobHunter profile data.
- Raw audio/evidence retention should be bounded.
- Logs must avoid unnecessary full phone numbers where masking is sufficient.
- Outbound calls/SMS remain side effects and require deterministic JobHunter authorization.
- Model output alone never authorizes a telephony side effect.

---

## 34. Suggested implementation phases

### Phase 1: data + read-only integration

Implement:

```text
ContactType.PHONE
E.164 normalization
PhoneGateClient
CommunicationSession
CommunicationTurn
CallFact
InterviewAppointment
phone health surface
```

Receive/correlate incoming call events and store transcripts/evidence, but do not autonomously speak beyond a controlled test mode.

### Phase 2: deterministic greeting and evidence capture

Enable:

```text
auto-answer policy
cached hooks
AI disclosure
guided first question
transcript ingestion
evidence clips
post-call summary
```

No autonomous interview confirmation yet.

### Phase 3: constrained realtime dialogue

Add:

```text
call-realtime llmRouter profile
strict JSON schema
dialogue acts
ASR trust assessment
targeted repair
critical-fact candidate states
controlled ResponseRenderer
```

### Phase 4: scheduling authority

Add:

```text
AvailabilityService
calendar busy/free
relative-date resolver
critical read-back
InterviewAppointment confirmation
```

Only here should the assistant autonomously confirm interview slots.

### Phase 5: operational hardening

Add:

```text
A06 automated GSM canary
per-component health metrics
latency histograms
call failure alerts
LLM/provider failover telemetry
ASR/TTS quality metrics
bounded evidence retention
```

### Phase 6: broader conversation capability

Only after production evidence shows the controlled flow is reliable should the system consider handling more employer questions beyond scheduling.

---

## 35. Production observability

Record per call:

```text
time_to_ring
time_to_answer
answer_to_connected
speech_end_to_transcript
asr_backend_latency
llm_latency
llm_provider
llm_model
tts_latency
transcript_to_tx_active
turn_total_latency
captured_frames
queued_frames
dropped_frames
clarification_count
critical_fact_confirmation_count
unknown_fact_deferrals
call_outcome
```

Useful SLO candidates after enough data exists:

```text
speech_end_to_transcript p95 < 1.2 s
realtime LLM p95 < 1.5 s including failover
short TTS p95 < 1.5 s
RX dropped frame ratio < 0.5%
critical facts confirmed before final commit = 100%
unsupported candidate facts invented = 0
```

Do not lock these exact latency SLOs until production distributions are collected.

---

## 36. Key invariants

These rules should be encoded in tests and policy, not left only in prompts.

1. Raw ASR transcript is never authoritative truth.
2. LLM interpretation is never authoritative truth.
3. Date/time/address are never committed from one unconfirmed ASR hypothesis.
4. LLM cannot create candidate personal facts.
5. LLM cannot directly operate PhoneGate.
6. Availability must be checked before autonomous acceptance of a slot.
7. Final critical read-back is required before marking an interview confirmed.
8. Unknown is preferable to invented precision.
9. Repeated ASR failure should trigger graceful defer/review, not an infinite clarification loop.
10. Phone-channel degradation must not globally pause unrelated JobHunter functions.
11. Post-call models cannot promote unconfirmed facts to confirmed.
12. Outbound ambiguous SMS failure must not cause blind duplicate retries.
13. A14 is the production modem; A06 is initially the canary/test modem.
14. Common opening/repair phrases should be cached/prefetched.
15. Employer-facing replies should stay short.

---

## 37. Final target flow

```text
Employer calls
    ↓
A14 / GSM
    ↓
PhoneGate RINGING event
    ↓
JobHunter Call Agent
    ↓
correlate caller/application if possible
    ↓
policy permits auto-answer
    ↓
PhoneGate answer
    ↓
cached hook:
"Если вы звоните по поводу вакансии... вы попали правильно."
    ↓
AI disclosure + scheduling authority
    ↓
listen
    ↓
PhoneGate RX → VAD → Groq ASR
    ↓
ASR HYPOTHESIS
    ↓
JobHunter + realtime LLM semantic extraction
    ↓
deterministic validators
    ↓
low confidence/conflict?
 ┌───────────────┴───────────────┐
 yes                             no
 ↓                               ↓
targeted repair             candidate fact
                                 ↓
                             critical?
                          ┌──────┴──────┐
                          yes           no
                          ↓             ↓
                       confirm        accept
                          ↓
                   caller confirmation
                          ↓
                    confirmed fact
                          ↓
                  availability check
                          ↓
               accept / alternative slot
                          ↓
                    final read-back
                          ↓
                       confirmed
                          ↓
                       close call
                          ↓
                  post-call analysis
                          ↓
                 structured appointment
                          ↓
                  concise notification
```

---

## 38. Architectural conclusion

The measured PhoneGate/A14 media path is already good enough for an employer-facing voice agent:

- RX stress tests showed zero dropped frames.
- Speech end to transcript is around 0.65 seconds in corrected E2E testing.
- Piper removed the previous 6–8 second Edge TTS bottleneck and short unique phrases now synthesize around 0.7 seconds median.
- Real GSM TX audio remains intelligible after the cellular codec.
- The remaining typical response-start budget, including a fast realtime LLM, is roughly around three seconds.

The primary engineering risk is therefore no longer basic telephony transport. The primary risk is semantic correctness under imperfect ASR and model uncertainty.

The architecture must optimize for:

```text
fast enough conversation
+ explicit caller hooks
+ graceful ASR repair
+ deterministic validation
+ strict confirmation of critical facts
+ narrow LLM authority
+ strong provider failover
+ evidence-backed outcomes
```

The desired system is not a free-form AI interviewer. It is a controlled, low-latency scheduling agent that can sound natural while remaining deliberately skeptical of what it thinks it heard.


---

## 39. JobHunter web panel integration

Phone Call Agent должен управляться из существующей веб-панели JobHunter, а не через отдельный второй интерфейс. Текущие верхнеуровневые разделы панели:

```text
Главная
Требуют решения
История
Настройки
Диагностика
```

Добавляется новый первичный раздел:

```text
Звонки
```

Концептуально: `/?view=calls`. Он использует существующие admin-auth, CSRF, audit log, layout, cards, badges, status dots и responsive-стили. PhoneGate Web Studio внутрь панели не встраивается, а bearer token PhoneGate никогда не попадает в браузер.

```text
Browser
  ↓
JobHunter admin routes/API
  ↓
CallAgent / DB / PhoneGateClient
  ↓
PhoneGate
```

### 39.1 Навигация и структура

В боковом меню появляется `Звонки` с badge:

```text
Звонки [1]
```

Смысл badge:

```text
0          нет активного звонка и нерешённых phone-items
1          один активный звонок
N          звонки/собеседования, требующие проверки
red badge  phone channel unavailable или есть critical phone alert
```

Внутри `Звонки` четыре вкладки:

```text
Live
История звонков
Собеседования
Evidence
```

Подробная диагностика остаётся в существующем `Диагностика`; в `Звонки` показывается только компактная health-полоска. Так не появляются две конкурирующие страницы здоровья системы.

### 39.2 Live call view

Когда звонка нет:

```text
Телефонная линия
● Готова
A14 · Orange · Zero-ADB
PhoneGate connected
ASR: healthy · TTS: Piper · LLM: healthy
Последняя проверка: 23:42
```

При входящем/активном звонке экран превращается в live workspace:

```text
┌──────────────────────────────────────────────────────────────┐
│ ВХОДЯЩИЙ ЗВОНОК / В РАЗГОВОРЕ                   00:01:24     │
│ +373••••123 · Example SRL · Warehouse Operator              │
│ Application #... · caller match: high                       │
├──────────────────────────────────────────────────────────────┤
│ Dialogue: CONFIRMING_CRITICAL_FACTS                          │
│ Telephony: CONNECTED                                         │
│                                                              │
│ Employer: В четверг в два часа, Индустриальная двенадцать.  │
│ ASR: 78% · Groq · 0.31 s                                    │
│                                                              │
│ Assistant: Правильно понимаю: в четверг в 14:00...?         │
│ LLM: qwen/qwen3.8-27b · 0.43 s · TTS: Piper 0.68 s          │
├──────────────────────────────────────────────────────────────┤
│ Facts                                                        │
│ Date       03.09.2026       CANDIDATE                        │
│ Time       14:00            CANDIDATE                        │
│ Address    Industrială 12   CONFIRMED                        │
├──────────────────────────────────────────────────────────────┤
│ [Take over] [Speak...] [Hang up]                             │
└──────────────────────────────────────────────────────────────┘
```

Live transcript append-only. Для employer-turn показываются:

```text
speaker
timestamp
ASR backend
ASR confidence
ASR latency
trust class: HIGH / MEDIUM / LOW
```

Для assistant-turn:

```text
dialogue_act
render source: cached | template | dynamic
LLM provider/model, если модель использовалась
LLM latency
TTS backend
TTS latency
```

UI должен визуально поддерживать тот же distrust-model, что backend:

```text
ASR transcript    neutral
CANDIDATE fact    warning
CONFIRMED fact    success
CONFLICT          danger
UNKNOWN           muted
```

Raw ASR нельзя оформлять так, будто это подтверждённая истина.

### 39.3 Operator takeover и live controls

Оператор может вмешаться без потери session history:

```text
Take over / Resume autonomy
Speak text
Hang up
Mark fact corrected
Mark session needs review
```

`Take over` переводит session в operator mode:

```text
autonomous LLM responses stop
PhoneGate RX/ASR continues
transcript/evidence capture continues
operator can send controlled TTS text
```

`Resume autonomy` разрешён только если dialogue state ещё recoverable и нет unresolved critical conflict.

Все live-actions идут server-side POST через JobHunter с:

```text
admin authentication
CSRF
AuditEvent
session correlation ID
```

Browser JavaScript никогда не вызывает PhoneGate напрямую.

Free-text `Speak` перед отправкой показывает точный текст, а результат сохраняется как `CommunicationTurn` с `actor=operator`, чтобы post-call analysis отличал ручное вмешательство от автономного ответа.

### 39.4 Facts panel

Постоянная панель фактов:

```text
Field        Value                 State       Source
Company      Example SRL           HIGH        turn 2
Vacancy      Warehouse Operator    HIGH        correlation
Date         03 Sep 2026           CANDIDATE   turn 5
Time         14:00                 CONFIRMED   turns 5+7
Format       onsite                CONFIRMED   turn 8
Address      Industrială 12        CONFLICT    turns 9+10
```

По клику открываются:

```text
raw expression
normalized value
ASR confidence
LLM confidence
source transcript turn
confirmation turn
evidence clip
validator notes
```

Ручное исправление факта создаёт audited revision, а не тихо переписывает исходные evidence/provenance.

### 39.5 История звонков

Одна строка/карточка на `CommunicationSession`:

```text
date/time
company / vacancy
direction
duration
outcome
appointment status
needs_review
ASR/TTS/LLM health summary
Telegram status
```

Фильтры:

```text
all
interview scheduled
proposed / needs confirmation
needs review
missed / dropped
technical failure
unknown caller
```

Поиск:

```text
company
vacancy
masked phone number
application ID
```

Session detail показывает:

```text
call timeline
full transcript
candidate/confirmed/conflicting facts
appointment
latency metrics
evidence clips
LLM route/fallback history
PhoneGate RX frame stats
post-call summary
Telegram delivery state
audit events
```

Цель: оператор должен понять, почему агент принял решение, не залезая в server logs.

### 39.6 Собеседования

Это operator-facing view таблицы `InterviewAppointment`.

Разделы:

```text
Сегодня
Предстоящие
Требуют подтверждения
Прошедшие
Отменённые
```

Карточка:

```text
company
vacancy
date/time in Europe/Chisinau
format: onsite / remote / phone
address or meeting link
contact person
status: proposed / confirmed / needs_review / cancelled
source call
related application
```

`confirmed` визуально явно отличается от `needs_review`.

Detail view хранит final read-back и подтверждающий caller-turn, чтобы календарное событие всегда можно было трассировать обратно к разговору.

### 39.7 Calendar / availability

Рекомендуемая модель доступа:

```text
Selected personal calendars → READ FREE/BUSY ONLY
JobHunter Interviews        → READ + CREATE + UPDATE + CANCEL
```

В `Собеседования` можно показывать компактную day/week availability-strip:

```text
13:00–14:00 BUSY
14:00–15:00 FREE
15:00–16:30 FREE
16:30–17:30 BUSY
```

Названия приватных событий из личных календарей по умолчанию не показываются. Для scheduling logic и обычного UI достаточно `BUSY/FREE`.

Для confirmed appointment показывать calendar sync:

```text
Calendar: synced
Calendar: pending
Calendar: failed
```

Source of truth для call semantics остаётся `InterviewAppointment`; внешний календарь является синхронизированным представлением, а не единственной копией данных.

### 39.8 Evidence tab

Evidence полезен для проверки ASR, но не должен превращаться в бесконечный архив записей.

Показывать clips по session/fact:

```text
turn 5 · date/time · 7.2 s · retained 12 days
turn 9 · address · 5.8 s · retained 12 days
```

Для каждого:

```text
Play
transcript
ASR confidence
linked facts
created_at
retention expiry
```

Retention должен быть видимым и ограниченным. Full-call recording для первой версии не требуется.

### 39.9 Главная

В `Главная` добавляется компактная сводка, без дублирования full calls UI:

```text
PhoneGate        Healthy
Calls today      4
Interviews       2 confirmed
Needs review     1
```

При активном звонке сверху показывать strip:

```text
📞 Идёт звонок · Example SRL · 01:24 · [Открыть]
```

При phone degradation/unavailable показывать owner-status warning со ссылкой на `Звонки`/`Диагностика`.

### 39.10 Диагностика

В существующую `Диагностика` добавить секцию `Phone channel`:

```text
phonegate_transport
a14_daemon
gsm_call_control
gsm_rx
gsm_tx
asr
tts
llm_realtime
sms
sim_account
Telegram notification
```

Для каждого компонента:

```text
healthy | degraded | unavailable
last successful check
last error/warning
short diagnostic
link to related recent sessions
```

Метрики:

```text
RX dropped-frame ratio
speech_end_to_transcript p50/p95
LLM latency p50/p95
Piper TTS p50/p95
provider fallback rate
ASR clarification rate
calls requiring manual review
```

Phone degradation не должен менять global JobHunter readiness, если не сломан реально общий dependency.

### 39.11 Настройки

В `Настройки` добавить блок `Телефонный агент`:

```text
Auto-answer enabled
Emergency stop
Quiet hours
Maximum call duration
Allowed scheduling autonomy
Evidence retention days
Telegram notification level
Calendar availability sources
JobHunter Interviews calendar
A06 canary enabled / schedule
```

Advanced collapsed block:

```text
call-realtime model order
hedge threshold
LLM timeout
ASR/TTS health thresholds
```

Секреты PhoneGate/Telegram/calendar после сохранения обратно в браузер не рендерятся. Только `configured / not configured`, rotate/disconnect и audit trail.

### 39.12 Telegram delivery status

Telegram не является source of truth. Source of truth — JobHunter DB/panel.

Для каждого post-call результата:

```text
Telegram: sent
Telegram: retrying
Telegram: failed
Telegram: disabled
```

Telegram failure никогда не меняет:

```text
call outcome
CallFact states
InterviewAppointment status
calendar sync state
```

### 39.13 Browser refresh model

Первая версия может использовать polling:

```text
active Live tab        500–1000 ms
idle Calls page        5–10 s
Overview phone strip   10–15 s
Diagnostics            15–30 s
```

CallAgent polling PhoneGate остаётся значительно быстрее; browser polling влияет только на presentation latency.

Позже можно добавить JobHunter WebSocket/SSE. Даже тогда browser подписывается на JobHunter, не на PhoneGate.

### 39.14 Mobile/responsive behavior

На узком экране порядок:

```text
live status
operator controls
facts
transcript
collapsed metrics
```

`Hang up` и `Emergency stop` требуют deliberate confirmation и не должны находиться рядом с безобидной навигацией. Fat-finger остаётся удивительно конкурентоспособным fault injector.

### 39.15 Suggested implementation surface

В существующей панели:

```text
app/admin/routes.py
  add view = "calls"
  phone_tab = live | history | appointments | evidence

app/admin/templates/
  dashboard_calls.html
  optional calls_* partials

app/admin/static/
  calls.js
```

JobHunter-owned API/actions, например:

```text
GET  /api/v1/phone/status
GET  /api/v1/phone/sessions/{id}
GET  /api/v1/phone/appointments
POST /api/v1/phone/sessions/{id}/takeover
POST /api/v1/phone/sessions/{id}/resume
POST /api/v1/phone/sessions/{id}/speak
POST /api/v1/phone/sessions/{id}/hangup
POST /api/v1/phone/facts/{id}/correct
POST /api/v1/phone/emergency-stop
```

Названия могут измениться. Архитектурная граница неизменна: browser action сначала проходит policy/audit JobHunter и только потом при необходимости достигает PhoneGate.

### 39.16 UI acceptance criteria

Панель готова к production, когда оператор без server-shell может:

1. Увидеть health PhoneGate/A14.
2. Увидеть ringing/active call в пределах одного browser refresh.
3. Увидеть caller, vacancy и application при успешной корреляции.
4. Следить за transcript + ASR confidence/trust.
5. Отличить candidate date/time/address от confirmed.
6. Взять управление и остановить автономные ответы.
7. Осознанно завершить звонок.
8. Открыть завершённый call и evidence.
9. Увидеть upcoming proposed/confirmed interviews.
10. Увидеть calendar sync без раскрытия лишних private event titles.
11. Увидеть Telegram delivery независимо от business state.
12. Понять причину `needs_review` без чтения raw server logs.
