# Security and safety model

The assistant touches client email, which is confidential by default. These
are the guarantees it gives, and where each one comes from.

## 1. It never sends email

| Layer | Gmail | Microsoft 365 |
|---|---|---|
| Interface | `MailProvider` (`src/triage/mail/base.py`) has no send method | same |
| Provider code | Calls only `messages.list/get/modify`, `threads.get`, `labels.list/create`, `drafts.create/get/delete` | Calls only message `GET`, `PATCH` (categories and draft body), `createReply`, and `DELETE` on its own unedited drafts |
| Tests | `tests/test_no_send.py` parses the provider modules and fails on any send-style call or URL | same |
| Platform permission | **Not enforceable**: Google has no scope that allows drafts but not sending, so `gmail.modify` is used | App granted `Mail.ReadWrite` only. Without `Mail.Send`, Microsoft 365 rejects any send |
| n8n workflows | Only `addLabels`, `getAll` and `create`; tests reject send, reply or forward steps and mail nodes without an explicit operation | Drafts via Graph `createReply`; the n8n "Reply" operation is not used |

The dashboard has no send button. A person opens the draft in Gmail or
Outlook, edits it and sends it.

## 2. Confidential clients never reach the AI

`src/triage/privacy.py` runs before any model call. It excludes an email when:

- the client record has `ai_excluded=true`
- the sender's domain or address is on the exclusion list
- the subject carries a marker such as `[Confidential]`
- an excluded party is in To or Cc

For an excluded email:

- Nothing from it is sent to the model.
- Only metadata is stored: sender, time, client and assignee. The subject and
  body are not stored.
- The email is labelled `AI/Excluded` and routed by rules to the account
  owner.
- The alert contains no subject or body.

`test_confidential_client_never_reaches_the_ai` checks all of this.

## 3. Drafts are grounded and reviewable

- Claude receives only the retrieved sources: knowledge-base sections, the
  client record and recent queries. It is told to state facts only from them
  and to cite each source it uses.
- `validate_citations` removes citations of sources the model was not given
  and records them. A draft with no valid citation, or with low confidence,
  is flagged.
- Missing information becomes a "check before sending" list, both in the
  dashboard and in the reviewer note at the top of the draft.
- The prompt forbids promising refunds, credits, discounts or contract changes.

## 4. Email content is untrusted input

- Client identity comes only from the sender's address, never from the email
  text. A claimed company name is shown as "unverified" and never loads that
  client's record.
- The prompts tell the model that emails and sources are data, and that it
  should ignore any instructions inside them.
- Structured outputs constrain what the model can return. The worst a prompt
  injection can do is produce a bad draft, which a person reviews before
  anything is sent.
- The dashboard renders all email content through Jinja2 autoescaping. Graph
  drafts are built with `html.escape`.

## 5. Data minimisation

- The model receives the latest message with quoted history stripped, at most
  two earlier thread messages, and attachment names only.
- Nothing is sent to the model for auto-replies, bulk mail, emails that were
  already answered, or excluded clients.
- Data sent to the Claude API is handled under Anthropic's commercial terms.
  Check that this fits your data processing agreement, and use the exclusion
  lists for anything that must stay in-house.

## 6. Access to the dashboard and API

- In production (`APP_ENV=production`) the app answers `503` until
  `BASIC_AUTH_USER` and `BASIC_AUTH_PASSWORD` are set (password at least 12
  characters). The exception is `AUTH_DISABLED=true`, meant for private
  networks.
- `/api/*` accepts `X-API-Key: $API_KEY` (for n8n and scripts) or the login.
  `/api/health` is public and returns only `{"ok": true}`.
- Dashboard forms carry a CSRF token derived from `SECRET_KEY`.
- There is one shared login, with no per-user roles. Serve it over HTTPS.

## 7. Secrets

- API keys, OAuth tokens, the Graph client secret and the Slack/Teams webhook
  URLs come from the environment or from files under `secrets/`. Both are
  git-ignored.
- A Gmail OAuth token or a service account with domain-wide delegation grants
  mailbox access: store it like a password.
- Rotate the Graph client secret before it expires.
