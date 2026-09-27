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

## Option A: OAuth for a single mailbox (simplest)

Use this when the shared inbox is an ordinary account, such as a Workspace
user `support@yourcompany.com` that the team signs in to or has delegated
access to.

1. In [Google Cloud Console](https://console.cloud.google.com/), create a
   project and enable the **Gmail API**.
2. Under **APIs & Services → OAuth consent screen**, choose **Internal** for
   Workspace, or **External** in testing mode with the mailbox added as a test
   user. Add the `gmail.modify` scope.
3. Under **Credentials → Create credentials → OAuth client ID**, choose
   **Desktop app**. Download the JSON file to `secrets/gmail-client.json`.
4. Run this and sign in **as the shared mailbox**:

   ```bash
   MAIL_PROVIDER=gmail triage gmail-auth
   ```

   The token is saved to `secrets/gmail-token.json` and refreshed
   automatically. Keep it secret: it grants access to the mailbox.
5. Set in `.env`:

   ```
   MAIL_PROVIDER=gmail
   MAILBOX_ADDRESS=support@yourcompany.com
   TEAM_DOMAINS=yourcompany.com
   ```

On a server (Render, Docker), copy `gmail-token.json` onto a persistent disk
and point `GMAIL_TOKEN_FILE` at it.

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
