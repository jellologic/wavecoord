# EARS statements

A requirement's Statement is one sentence with `shall` exactly once. `wavecoord spec check` rejects an approved requirement whose statement fits none of these patterns.

| Pattern | Form | Example |
|---|---|---|
| Ubiquitous | The `<system>` shall `<response>`. | The session store shall keep each session until it is revoked. |
| Event | When `<trigger>`, the `<system>` shall `<response>`. | When an access token is within 60 s of expiry, the auth service shall issue a new token. |
| State | While `<state>`, the `<system>` shall `<response>`. | While the store is read-only, the API shall answer writes with 503. |
| Unwanted | If `<condition>`, then the `<system>` shall `<response>`. | If a refresh token is revoked, then the auth service shall reject it. |
| Optional | Where `<feature>`, the `<system>` shall `<response>`. | Where SSO is enabled, the login page shall offer the SSO button. |
| Combined | While `<state>`, when `<trigger>`, the `<system>` shall `<response>`. | While offline, when the user saves, the app shall queue the change. |

Rules:

- One behaviour per statement. "The X shall A and B" is two requirements.
- Name a measurable response: a value, a status code, a time, a file, an event. "Fast", "secure" and "user-friendly" are not testable.
- The trigger and the state are observable from outside the code under test.
- Put the reason in Rationale, not in the statement.
