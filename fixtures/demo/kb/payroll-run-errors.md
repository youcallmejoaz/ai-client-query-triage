---
id: payroll-run-errors
title: Fixing a failed payroll run
category: technical_issue
url: https://help.tidewater.example/articles/payroll-run-errors
updated: 2026-08-12
---

A payroll run can fail at the payment submission step. The run stays in
"Needs attention" and no money has left the client's account.

## Common error codes

- **E-102 Bank details rejected**: one or more employee bank accounts failed
  the bank's validation check (usually a sort code or account number typo, or
  a closed account). The run report lists the affected employees. Fix their
  bank details under People → Pay details, then choose "Resubmit payments".
- **E-207 Tax code missing**: an employee has no tax code. Add it under
  People → Tax, then resubmit.
- **E-310 Funding not confirmed**: the direct debit that funds the payroll
  bounced. Contact Billing.

## Payment cut-off times

Standard payments must be submitted by 14:00 two working days before payday.
Enterprise clients can use same-day Faster Payments for runs submitted by
11:00 on payday itself. Runs submitted after these cut-offs are paid on the
next working day.

## What support can do

Support can check the bank's rejection report, confirm which employees are
affected, and, for Enterprise clients, resubmit a corrected run on the
client's behalf once the client confirms the corrected details in writing.
Support cannot change bank details for the client.
