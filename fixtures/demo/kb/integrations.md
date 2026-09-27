---
id: integrations
title: Accounting integrations (Xero and QuickBooks)
category: technical_issue
url: https://help.tidewater.example/articles/integrations
updated: 2026-09-18
---

## How syncing works

Journals for each finalised payroll run sync to Xero or QuickBooks every
night at 02:00 UK time. Admins can also press "Sync now" under Settings →
Integrations.

## QuickBooks sync failures

QuickBooks connections expire after 100 days, or earlier if the QuickBooks
admin's password changes. When that happens the nightly sync fails with
"Authorization expired". Reconnect under Settings → Integrations →
QuickBooks → Reconnect, then press "Sync now". Journals from the failed
nights are sent on the next successful sync, so nothing is lost.

## Recent incident

On 16-17 September a QuickBooks API change caused failed syncs for some
clients. It was fixed on 17 September; affected journals were re-sent
automatically.
