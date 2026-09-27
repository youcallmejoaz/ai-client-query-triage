# Connecting a Gmail or Google Workspace shared inbox

The assistant reads the inbox, adds `AI/…` labels and saves reply drafts in
the same threads. It never sends email: the code has no send call, and
`tests/test_no_send.py` fails the build if one is added.

## What access it needs

One OAuth scope: `https://www.googleapis.com/auth/gmail.modify`. It covers
reading messages, adding labels, and creating and deleting drafts.

Google has no narrower scope that allows creating drafts without also
allowing sending. `gmail.compose` and `gmail.modify` both technically permit
sending. So for Gmail the no-send guarantee rests on this code and its tests,
not on the permission. Microsoft 365 can enforce it at the permission level;
see [SETUP-M365.md](SETUP-M365.md).

## Option A: Connect Gmail from the dashboard (recommended, works on Render)

Everything happens in the browser: no command line, no token file.

1. In [Google Cloud Console](https://console.cloud.google.com/), create a
   project and enable the **Gmail API**.
2. Under **APIs & Services → OAuth consent screen**:
   - choose **Internal** for Google Workspace, or **External** for a personal
     @gmail.com account;
   - add the `gmail.modify` scope;
   - for External, add the mailbox's address as a test user, then press
     **Publish app**. An app left in Testing issues access that expires after
     7 days.
3. Under **Credentials → Create credentials → OAuth client ID**, choose **Web
   application**. Under **Authorized redirect URIs** add your app's callback:

   ```
   https://<your-service>.onrender.com/oauth/google/callback
   ```

   The dashboard's **Settings** page shows the exact address to paste. It is
   built from `PUBLIC_URL`, which on Render defaults to the service's own URL.
4. Set these environment variables (in Render: **Environment**), then deploy:

   ```
   MAIL_PROVIDER=gmail
   GOOGLE_OAUTH_CLIENT_ID=<client id>
   GOOGLE_OAUTH_CLIENT_SECRET=<client secret>
   ```

5. Open the dashboard → **Settings** → **Connect Gmail**, and sign in as the
   mailbox. For a personal account, Google shows an "unverified app" warning;
   choose **Advanced → Go to …**. That is expected for an app only you use.

The app stores the connection in its own database, encrypted with a key
derived from `SECRET_KEY`. It reconnects by itself after restarts, as long as
the database is on a persistent disk (see `render.yaml`) and `SECRET_KEY`
stays the same. Until the mailbox is connected, the dashboard runs normally
and shows a "not connected" banner. If Google later revokes access, the
banner comes back and asks you to connect again.
**Disconnect** on the Settings page revokes the access at Google and deletes
the stored copy.

The mailbox's own address is taken from the connected account, so
`MAILBOX_ADDRESS` doesn't need to be set.

## Option A2: OAuth from the command line (a token file)

Use this if you'd rather not expose the dashboard's callback URL, or for
local development.

1. Follow steps 1–2 above, but in step 3 choose **Desktop app** and download
   the JSON file to `secrets/gmail-client.json`.
2. Run this on your own computer and sign in **as the shared mailbox**:

   ```bash
   MAIL_PROVIDER=gmail triage gmail-auth
   ```

   The token is saved to `secrets/gmail-token.json`. Keep it secret: it grants
   access to the mailbox.
3. Set `MAIL_PROVIDER=gmail`, and `MAILBOX_ADDRESS` to the mailbox's address.

### Running it on a server (Render, Docker)

This applies to the command-line token (Option A2). The sign-in opens a
browser, so it can't run on the server. Run it on your own computer, then copy
the token to the server:

- **Render:** go to the service → **Environment** → **Secret Files** → **Add
  Secret File**. Name it `gmail-token.json` and paste the file's contents.
  Then set `GMAIL_TOKEN_FILE=/etc/secrets/gmail-token.json`.
- **Docker:** mount `secrets/` into the container (docker-compose already
  does) and set `GMAIL_TOKEN_FILE=/app/secrets/gmail-token.json`.

The server refreshes the access token itself; the file can be read-only.

### Personal Gmail accounts (@gmail.com)

- Use Option A (or A2). Option B needs Google Workspace.
- An OAuth app left in **Testing** issues refresh tokens that expire after
  **7 days**, after which the service can no longer read the mailbox. For
  anything longer than a trial, go to **OAuth consent screen → Publish app**,
  then connect again. Google shows an "unverified app" warning
  during your own sign-in; that is expected for an app only you use.
- Do **not** put `gmail.com` in `TEAM_DOMAINS`. Every sender from that domain
  would then be treated as your own team and skipped. Leave `TEAM_DOMAINS`
  empty; replies you send from the mailbox are still recognised.

## Option B: Workspace service account with domain-wide delegation

Use this for unattended servers in a Google Workspace organisation.

1. Create a service account in the Cloud project and download its JSON key.
2. In the Workspace Admin console, go to **Security → Access and data control →
   API controls → Domain-wide delegation**. Add the service account's client
   ID with the scope `https://www.googleapis.com/auth/gmail.modify`.
3. Set:

   ```
   MAIL_PROVIDER=gmail
   GMAIL_SERVICE_ACCOUNT_FILE=secrets/service-account.json
   GMAIL_DELEGATED_USER=support@yourcompany.com
   MAILBOX_ADDRESS=support@yourcompany.com
   ```

Domain-wide delegation lets the service account act as any user it is pointed
at. Store the key carefully and restrict who can change `GMAIL_DELEGATED_USER`.

## What you will see in Gmail

- Labels under **AI**, for example `AI/Urgency/Critical`,
  `AI/Category/Billing`, `AI/Client/Acme Logistics`, `AI/Assigned/Lena Novak`,
  `AI/Draft ready`, `AI/Needs human` and `AI/Excluded`. Urgency labels are
  coloured.
- A draft reply in each thread that needs one. It opens with a short
  "delete this block" note listing the sources used, which you remove before
  sending. Set `DRAFT_REVIEW_NOTE=false` to leave the note out and rely on the
  dashboard instead.
- Nothing is ever sent. When someone sends a reply from the mailbox, the next
  poll marks the query as replied.

Google Groups collaborative inboxes have no API for drafts. Use a real mailbox
(a Workspace user or a delegated account) as the shared inbox.

## Checking the connection

```bash
MAIL_PROVIDER=gmail triage check   # validates config files
MAIL_PROVIDER=gmail triage poll    # triages new mail once and prints a report
```
