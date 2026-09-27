# Connecting a Microsoft 365 shared mailbox (Outlook)

The assistant uses Microsoft Graph with an app registration (app-only access,
no user sign-in). It reads the shared mailbox, sets Outlook **categories**,
and saves reply drafts in each conversation. It never sends email.

## What access it needs

- Application permission **`Mail.ReadWrite`** on Microsoft Graph, with admin
  consent. It covers reading messages, setting categories, and creating and
  deleting drafts.
- **Do not grant `Mail.Send`.** Without it Microsoft 365 rejects any attempt to
  send from the app, so the no-send guarantee is enforced by the platform as
  well as by the code.
- Limit the app to the shared mailbox only. By default an application
  permission applies to every mailbox in the tenant.

## Steps

1. **Register an app.** Go to Microsoft Entra admin center → App registrations
   → New registration. Name it, for example "Client query triage", and choose
   single tenant. No redirect URI is needed.
2. **Add the permission.** Go to API permissions → Add a permission → Microsoft
   Graph → Application permissions → `Mail.ReadWrite`. Then click **Grant
   admin consent**.
3. **Create a secret.** Go to Certificates & secrets → New client secret. Copy
   the value; it is shown only once.
4. **Scope it to the shared mailbox.** In Exchange Online PowerShell, use RBAC
   for Applications (recommended):

   ```powershell
   New-ServicePrincipal -AppId <client-id> -ObjectId <enterprise-app-object-id> -DisplayName "Client query triage"
   New-ManagementScope -Name "Support mailbox" -RecipientRestrictionFilter "PrimarySmtpAddress -eq 'support@yourcompany.com'"
   New-ManagementRoleAssignment -App <client-id> -Role "Application Mail.ReadWrite" -CustomResourceScope "Support mailbox"
   ```

   If you use RBAC for Applications, you can remove the tenant-wide Entra
   permission from step 2. The older alternative is an
   `ApplicationAccessPolicy` (`New-ApplicationAccessPolicy -AccessRight
   RestrictAccess`) with a mail-enabled security group that contains the
   shared mailbox.
5. **Configure** `.env`:

   ```
   MAIL_PROVIDER=graph
   GRAPH_TENANT_ID=<directory (tenant) id>
   GRAPH_CLIENT_ID=<application (client) id>
   GRAPH_CLIENT_SECRET=<secret value>
   GRAPH_MAILBOX=support@yourcompany.com
   MAILBOX_ADDRESS=support@yourcompany.com
   TEAM_DOMAINS=yourcompany.com
   ```

6. Check it:

   ```bash
   triage check
   triage poll
   ```

## What you will see in Outlook

- Categories named `AI/Urgency/Critical`, `AI/Category/Billing`,
  `AI/Assigned/…`, `AI/Draft ready` and so on. Urgency categories are
  colour-coded. Categories people add themselves are kept.
- A reply draft in the conversation (in **Drafts**, and inline in the
  conversation view), with the original message quoted underneath. It opens
  with a short "delete this block" note listing the sources used. Set
  `DRAFT_REVIEW_NOTE=false` to leave it out.
- When a teammate sends a reply from the shared mailbox, the next poll marks
  the query as replied.

## Notes

- Graph throttling (HTTP 429) is retried using the `Retry-After` header.
- Only unedited drafts are ever deleted, for example when a newer message
  arrives in the thread or a teammate presses **Regenerate**. The check uses
  the message's `changeKey`, so a draft someone has started editing is never
  removed.
- Rotate the client secret before it expires. Secrets last at most two years.
