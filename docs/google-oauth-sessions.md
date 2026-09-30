# Browser login and Gmail authorization

The account and operator panels use persistent, signed HttpOnly cookies with a
30-day inactivity window by default. Successful authenticated requests renew a
session after one day (or half its configured lifetime for shorter lifetimes).
Renewal retains the session nonce, so current CSRF forms in other tabs remain
valid. Logout deletes the cookie and cannot be undone by renewal. Expired or
tampered cookies never renew; account suspension and session-version changes
still invalidate account sessions. Keep the deployment's `SECRET_KEY` stable
across normal restarts. `SESSION_TTL_SECONDS` and `USER_SESSION_TTL_SECONDS`
configure each role's inactivity window.

Sign in with Google requests only `openid` and `email`. It creates a JobHunter
session without requesting Gmail consent or replacing an existing Gmail
credential. Gmail is connected separately and its encrypted refresh token is
stored per account. Gmail's SDK refreshes short-lived access tokens on demand
without opening a browser. Closing the panel or logging out does not disconnect
Gmail, and refreshing a panel cookie does not affect background email workers.

## Google's seven-day rule

Google documents a **seven-day** refresh-token lifetime for External OAuth apps
with publishing status **Testing**, except requests limited to basic identity
scopes. JobHunter requests `gmail.send` and `gmail.readonly` for delivery and
bounce reconciliation, so that exception does not apply to Gmail authorization.

The correct configuration for ongoing delivery is **In production** in
Google Auth Platform → Audience. Publishing status and verification are separate:
`gmail.send` is sensitive and `gmail.readonly` is restricted. Personal use by a
limited group can qualify for Google's verification exception; a public product
must meet the applicable verification requirements. Publishing does not promise
that every token is permanent: revocation, Gmail password changes, unused tokens,
token limits and account policy can still require consent again.

On 2026-09-30, a read-only check matched the deployed JobHunter OAuth client to its
Google Cloud project and confirmed **In production**. No Cloud configuration was
changed. If a previously issued Testing token expires, reconnect Gmail once after
the app is published. Do not repeatedly mint refresh tokens on ordinary login.

An expired or revoked refresh token cannot be silently replaced: Google requires
the user's authorization. The app automatically refreshes valid access tokens,
retries safely rejected temporary refresh failures, and shows **Reconnect Gmail**
only for a permanent refresh rejection. Delivery monitoring records that condition
for the affected account without advancing the mailbox cursor or interfering with
other accounts. Reconnection restores only applications known not to have been
sent; messages with uncertain delivery outcomes are never automatically resent.

Sources, checked 2026-09-30:

- [OAuth refresh-token expiration](https://developers.google.com/identity/protocols/oauth2#expiration)
- [Publishing status and Testing exception](https://support.google.com/cloud/answer/15549945?hl=en)
- [Verification exceptions](https://support.google.com/cloud/answer/13464323?hl=en)
- [Gmail scope classifications](https://developers.google.com/workspace/gmail/api/auth/scopes)
