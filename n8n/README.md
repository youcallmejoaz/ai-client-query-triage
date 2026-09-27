# n8n workflows

The same pipeline, driven by [n8n](https://n8n.io) instead of the built-in
poller. n8n watches the mailbox and does the mailbox writes. The Python service
is the brain: `POST /api/triage` classifies the email with Gemini, looks up the
client and the knowledge base, and returns labels, a cited draft and a routing
decision. Every query still appears in the dashboard.

![The Gmail triage workflow in n8n](../docs/media/n8n-workflow.png)

| File | What it does |
|---|---|
| `inbox-triage-gmail.json` | Gmail Trigger (every minute) → triage → add labels, save a reply draft in the thread, post urgent or complex queries to Slack |
| `setup-gmail-labels.json` | Run once: creates every `AI/…` label (`GET /api/labels`) so the triage workflow can apply them by ID |
| `inbox-triage-outlook.json` | Outlook Trigger → triage → set categories, create a reply draft in the conversation (Graph `createReply`), post to Teams |
| `daily-digest.json` | Weekdays at 08:45: `GET /api/digest` → Slack |

## No sending, by construction

None of these workflows sends email:

- Gmail steps use only `addLabels`, `getAll`, and `create` (for drafts and
  labels).
- Outlook drafts are created with Graph `createReply`, which only saves a
  draft, and then filled in with `PATCH`. The n8n Outlook "Reply" operation is
  deliberately not used, because it sends unless an option is set.

`tests/test_no_send.py` checks every workflow file for send, reply or forward
steps, and for mail nodes that do not name an operation. n8n's Gmail and
Outlook message nodes default to "send", so an unnamed operation would send.

## Setup

1. Run the triage service with an API key (`API_KEY=…`). With
   `docker compose --profile n8n up`, n8n reaches it at `http://triage:8000`,
   the URL already set in the workflows. Otherwise change the URL in the
   *Triage with Gemini* node.
2. In n8n, create the credentials the workflows reference:
   - **Triage API key**: a *Header Auth* credential with name `X-API-Key` and
     your `API_KEY` as the value.
   - **Gmail**: *Gmail OAuth2* for the shared inbox. For Outlook, use
     *Microsoft Outlook OAuth2* for the shared mailbox, and change `/me/` in
     the three Graph URLs to `/users/support@yourcompany.com/`.
   - **Slack** or **Microsoft Teams** for alerts. Pick the channel in the
     alert node.
3. Import the files (**Workflows → Import from file**), open each one, select
   the credentials, and activate *Inbox triage* and *Daily summary*.
4. Gmail only: run *Setup: create Gmail labels* once, and again after adding
   clients or team members.

Set `SCHEDULER_ENABLED=false` on the triage service when n8n drives it, so the
mailbox is not polled twice. When n8n does the mailbox writes, the service does
not need its own mailbox access. Set `MAIL_PROVIDER=gmail` or `graph` only if
you also want the service to spot replies sent from the mailbox and close
queries automatically. Otherwise close them from the dashboard or with
`POST /api/queries/{id}/status`.

## Verified with

n8n 2.40.7. All four files import with `n8n import:workflow`. n8n's parameter
validation reports no issues, apart from the Teams team and channel, which you
pick after import.
