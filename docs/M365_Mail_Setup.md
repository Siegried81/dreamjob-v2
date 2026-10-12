# Microsoft 365 mail — the app registration to create

Dream Job reads a Microsoft 365 (work or school) or Outlook.com / Hotmail
(personal) inbox through Microsoft Graph, **read-only**, to detect replies and
bounces (FR-326). It never sends from it, and the permission it asks for
(`Mail.Read`) cannot send, move, delete or mark a message read. The code is in
`backend/dreamjob/mail/m365.py`; this page is what an administrator has to do
once, in the Microsoft Entra admin centre, before the connection works. Nothing
here is created by Dream Job itself.

## 1. Register the application

1. Sign in to <https://entra.microsoft.com> with an account allowed to register
   applications (for a personal Outlook.com mailbox, any Microsoft account works
   at <https://portal.azure.com> > *Microsoft Entra ID*).
2. *Identity* > *Applications* > *App registrations* > **New registration**.
3. **Name**: `Dream Job (local)` — any name; the job seeker sees it on the
   consent screen.
4. **Supported account types** — choose one, and set `M365_TENANT` to match:

   | Choice in the portal | `M365_TENANT` |
   |---|---|
   | Accounts in any organizational directory and personal Microsoft accounts | `common` (the default) |
   | Accounts in any organizational directory only | `organizations` |
   | Personal Microsoft accounts only | `consumers` |
   | Accounts in this organizational directory only | the Directory (tenant) ID |

5. **Redirect URI**: platform **Public client/native (mobile & desktop)**, value
   `http://localhost:8000/api/mail/m365/callback`.
   If the API runs on another host or port, use that instead and set the same
   value in `M365_REDIRECT_URI`; the two must match character for character.
6. **Register**. Copy the **Application (client) ID** from the overview page.

## 2. API permissions

*API permissions* > **Add a permission** > *Microsoft Graph* > **Delegated
permissions**, and tick exactly:

- `Mail.Read`
- `User.Read` (usually present already)
- `offline_access`

Do **not** add `Mail.ReadWrite`, `Mail.Send` or `MailboxSettings.ReadWrite`:
Dream Job refuses a token that carries any of them, so that "read-only" stays
true even if the registration is widened later. No admin consent is needed for
these three in most tenants; if the tenant restricts user consent, an
administrator presses **Grant admin consent** on this page.

## 3. Authentication settings

*Authentication*:

- the redirect URI from step 1.5 is listed under *Mobile and desktop
  applications*;
- **Allow public client flows**: leave at *No* (the authorisation-code flow
  with PKCE does not need it).

No client secret is needed. Only if the app was registered under the **Web**
platform instead does Microsoft require one: then create it under
*Certificates & secrets* > *New client secret* and put its **Value** in
`M365_CLIENT_SECRET`.

## 4. Configure Dream Job

In `.env`:

```bash
M365_CLIENT_ID=<Application (client) ID>
M365_TENANT=common
M365_REDIRECT_URI=http://localhost:8000/api/mail/m365/callback
M365_CLIENT_SECRET=          # empty for the public-client registration above
```

Restart the API. `GET /api/mail/status` then lists `m365_graph` as
"not connected" instead of "not set up".

## 5. Connect a mailbox

The job seeker calls `GET /api/mail/m365/authorize` (signed in), opens the
returned `authorization_url`, signs in to Microsoft and accepts the three
permissions. Microsoft redirects to `/api/mail/m365/callback`, which stores the
refresh token encrypted with the job seeker's key (NFR-204). From then on the
inbox poller (`POST /api/mail/poll`, and the scheduler) reads new inbox
messages through Graph.

## 6. Disconnect

`POST /api/mail/accounts/{id}/revoke` wipes the stored token. Microsoft has no
endpoint to revoke a single refresh token from an installed app, so the job
seeker finishes by removing the app's consent at
<https://myapps.microsoft.com> (work or school) or
<https://account.live.com/consent/Manage> (personal).

## What it does not do

- It does not send. Sending still needs Gmail, an IMAP mailbox with an
  application password, or the relay; with only a Microsoft mailbox connected,
  the dispatcher uses the relay.
- It reads the **Inbox** folder only; a reply filtered into *Junk* or a custom
  folder is not seen.
- Job-alert e-mails (`mail/job_alerts.py`) are still read from Gmail only.
- The Microsoft 365 **calendar** client (`postapp/calendar_sync.py`) is
  separate and still has no connection flow.
