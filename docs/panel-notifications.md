# Panel status and notifications

Сверено с текущей реализацией 2026-09-30.

The account and operator panels share their navigation, status header and notification
menu. The operator has two extra pages: calls and accounts. There is no diagnostics page.
Authenticated legacy `view=diagnostics` URLs redirect to the alerts tab in History,
preserving the selected profile. Account users cannot access operator history or calls.

Useful information is available at the point of action:

- Header: current tasks and today's sent applications against the profile's daily limit.
- Sidebar: profile search mode, selected source health, Gmail and running checks.
- Overview: actual tasks and important events, with links to the relevant controls.
- Settings: Gmail and source checks, profile configuration and resumes.
- Calls: telephone status, auto-answer controls and current call actions.
- History: operator notification archive and system journal, alongside search history.

Tasks are computed from current, authorized profile state. Reviewing all pending
applications, confirming an active resume, connecting Gmail, selecting sources or
resuming sending removes the corresponding task after the successful action.
Opening a link does not complete a task. A failed action leaves it visible.
Operator alerts and profile tasks use the same collector on Overview, Settings,
application details and Accounts. The count includes all current items; the menu
shows at most ten and links to the full overview/history.

Alerts have distinct presentation states:

- **Unread**: still needs acknowledgement or a confirmed repair.
- **Read**: manually acknowledged; this alone does not imply recovery.
- **Resolved**: a successful source scan started after the warning proves recovery.

Successful source scans acknowledge only known source-recovery warnings for that
source, created before the scan started. They preserve the original diagnostics and
append a `resolution` object (reason, scan ID and time) to the existing JSON field.
Queueing a scan, partial/failed results, a different source and concurrent newer
warnings never close the original warning. No database schema migration is needed.
If a partially completed logical scan is successfully retried, its own warnings
are also resolved by matching the source and scan ID, even if its original start
time precedes the warning. The successful finish must follow the warning.

Historical warnings repaired before this change also appear resolved when an immutable
successful scan proves recovery. This classification does not write during page reads,
and a later failure does not reopen an old warning. Unknown warnings remain unread
until acknowledgement; age alone never counts as repair. Source problems remain in
the live tasks even when the corresponding warning is manually read.

Browser checks cover both roles on desktop/mobile, three clean contexts per role,
task completion and manual acknowledgement. The recovery scenario additionally
executes the actual local scan pipeline against the in-process fixture site three
times, ensuring no live crawling or real email delivery is needed.

Telephone review uses the same predicate as the Calls queue: `needs_review` or
`verification_status=needs_review`. Successful manual fact confirmation removes
the task even when an earlier automatic summary still has technical `failed`
state. That failure stays in call history rather than reopening completed work.
