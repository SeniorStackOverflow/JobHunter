# Employer relationship memory

Сверено с текущей реализацией 2026-09-30.

JobHunter resolves each source vacancy to a `CanonicalEmployer` using exact source
profile identifiers, verified domains, public email addresses, and known phone
numbers. A normalized company name is review evidence only and never merges two
employers by itself. When strong identifiers disagree, the source job receives a
separate employer and an `EmployerIdentityCandidate`; no permanent suppression is
inferred from the ambiguity.

Every application, correlated Gmail reply, PhoneGate call/SMS, interview event,
and owner override writes an idempotent `EmployerInteractionEvent`. The event log
is append-only. `EmployerRelationship` is rebuilt deterministically from that log
and stores the current state plus job, role-family, or employer suppression. A
reopen action appends `relationship_reopened`; it never deletes the original
decline.

The default policy permits one active application per profile and employer. It
chooses the highest current match score with stable timestamp/ID tie-breakers.
Other applications use `deferred` and become eligible after the relationship is
closed or explicitly reopened. PostgreSQL employer-row locks serialize the final
policy check, and the same gates run during preparation, immediately before send,
and before every delivery retry.

The [daily-minimum implementation](daily-minimum-catchup.md), deployed to PROD
as `4f04db0` on 2026-09-30, enforces one active slot regardless of older configurable
limits. It conservatively guards company names against multiple same-day
sends/reservations across employer IDs without merging identities. The audited
`close-unanswered` API requires recent Gmail synchronization and an elapsed
waiting window (default 45 days), then imposes a cooldown (default 90 days from
the last send). Replies discovered later restore the active contact state.

An unanswered application does not hold the employer forever. After
`EMPLOYER_UNANSWERED_RELEASE_DAYS` (default 14) since the last application with no
employer reply, the employer may receive an application for another vacancy: the
old send no longer counts as an active conversation or as the occupied slot. This
is evaluated from timestamps at policy time; no event is written and the sent
application stays `sent`. The best-ranked deferred application of that employer
gets the slot, and its send restarts the window. A reply, an interview, a
suppression or the `close-unanswered` cooldown still block as before; the same
vacancy is never applied to twice. `0` restores the permanent freeze. In PROD
(2026-10-02) 15 of 413 contacted employers had replied, 14 of them within four
days, which is why two weeks is a conservative window.

An employer reply or verified interview proposal/confirmation freezes new
applications. An explicit decline is job-scoped unless stronger evidence exists.
Employer-scoped automatic suppression requires an explicit company-level decline
after a separately recorded `interview_attended` event. A merely scheduled or
confirmed interview is not attendance proof.

Useful operator commands are:

```text
job-agent employer-identity-audit [--company NAME]
job-agent employer-relationship-audit [--company NAME]
job-agent employer-backfill --apply
job-agent employer-remediate --apply
job-agent employer-historical-incidents --apply
job-agent email-delivery-audit [--recipient EMAIL]
```

The first two commands are read-only. `email-delivery-audit` reads Gmail only when
the stored OAuth grant includes `gmail.readonly`; it does not advance the mailbox
cursor or update delivery/contact state. Apply commands are deliberately separate
so identity and safety reports can be reviewed first.
After reviewing the A/B/C relationship audit, `employer-historical-incidents`
records each historical rapid send, post-decline send, or send during an active
conversation as an idempotent `AuditEvent`. It leaves the original `SENT`
applications untouched. Run it again after reconciling older Gmail replies if
the new evidence reveals additional historical incidents.

Production rollout keeps automatic sending paused through migration and backfill.
Run the identity dry-run first and stop if it proposes broad merges through shared
agency contacts. Apply the additive migration through the migrator role, recreate
application containers on one image digest, reauthorize Gmail for both send and
read-only scopes, apply the employer backfill, add any owner-confirmed suppression
through the audited override, then run unsent remediation. Historical `sent`
applications remain historical send attempts even when a later DSN changes their
`EmailDelivery` outcome.
