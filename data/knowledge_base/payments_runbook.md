# Payout Webhook Runbook

> Illustrative sample data for the gchat_agent demo — not real policy.

Operational runbook for payout/withdrawal payment webhook failures. Use it when
provider callbacks to `/webhooks/payout` time out, fail signature checks, or
payouts are stuck in a non-final state. Covers the Skrill, Neteller, and card
payout PSPs.

## Symptoms

- Provider POST to `/webhooks/payout` returns a 504 (gateway timeout) and the
  payout is never marked settled.
- Payouts stuck in `pending` long past the provider's stated settlement window.
- Rising rate of timed-out callbacks (baseline is under 1%; over 5% is an
  incident).
- Players report withdrawals that show as pending but never complete; support
  ticket volume climbs.

## First Checks

1. Identify the affected PSP and method. Skrill, Neteller, and card payouts run
   on independent handlers — confirm whether deposits and card payouts are also
   affected or only one provider's payouts.
2. Check the callback timeout rate and 5xx rate per provider in the dashboard.
3. Check the database connection pool utilization for the payout service. A
   saturated pool under load is the most common cause of callback 504s.
4. Confirm the provider is not itself degraded (status page / provider SLA).

## Common Failure Causes

- **Connection-pool exhaustion:** the webhook handler holds a DB transaction
  open while making the outbound provider call, so connections are not released
  under load and callbacks queue until they hit the 30s gateway timeout (504).
  Fix: do not call external providers inside an open transaction; commit or
  release the connection first.
- **Signature mismatch:** callback rejected as unauthenticated. Usually a rotated
  signing secret or clock skew. Verify the active webhook secret matches the
  provider portal and that the server clock is in sync.
- **Provider timeout / degradation:** upstream PSP is slow or down; rely on retry
  and reconciliation rather than code changes.
- **Idempotency key collision:** a retried callback is dropped as a duplicate
  when it should update state.

## Retry and Idempotency

- Every payout callback carries a provider idempotency key; handlers must be
  idempotent — replaying the same callback must not double-pay.
- Failed or timed-out callbacks are retried with exponential backoff for up to
  24 hours.
- For callbacks that never arrive, the reconciliation job polls the provider
  payout status API every 15 minutes and settles or fails stuck payouts.
- A stuck payout that cannot be auto-reconciled within 2 hours is settled
  manually by Payments on-call after confirming provider-side status.

## On-call Owner and Escalation

- **First responder:** Payments on-call engineer.
- **P2 (degraded, payouts stuck but recoverable):** Payments on-call triages and
  drives the fix; target mitigation same day.
- **P1 (payouts failing broadly or funds at risk):** page the Payments
  engineering lead and notify Finance and Compliance.
- Track every payout incident in Jira under the `PAY-` project and link the
  affected provider, error rate, and number of stuck payouts.
