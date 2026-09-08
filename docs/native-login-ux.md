# Native login UX

Open App Home, click the Google account link button, finish browser login, then
return to Home to confirm the displayed account once

The native Home handler prepares a ten-minute PKCE authorization URL for the
authenticated Slack workspace/member. Existing pending URLs are reused on Home
refresh. No new public redirect endpoint or OAuth provider is needed
Preparing the URL does not make a network request to Cognito or authorize a user

The Slack URL button opens the browser directly. Its interaction is acknowledged
and consumed by native middleware, never forwarded to the conversation listener
No model tool receives login URLs, codes or tokens

After a valid callback, the server-resolved Slack identity selects a bounded,
short-lived native UI destination. The callback schedules a Home update without
waiting for Slack. Failed UI delivery does not undo login or replay the callback
The backup refresh button remains available for expired links or delivery failure

The callback does not activate the login. A separate nonce and authenticated
Slack confirmation are still required. The account label is plain text and the
confirmation cannot be performed by another Slack user
There is no additional confirmation dialog after that explicit button click

Container restarts still invalidate memory-only login sessions. This UX change
does not introduce credential persistence or alter third-party grants

Tested locally with runtime, callback and native UI tests. Real desktop/mobile
Slack browser behavior and push delivery still require deployment acceptance
