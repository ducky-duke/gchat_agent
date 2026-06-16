# Promo Launch Checklist

> Illustrative sample data for the gchat_agent demo — not real policy.

A promotion (welcome bonus, deposit match, free spins, cashback) cannot be
approved or scheduled until every required field below is filled in. A request
that is missing any of these is incomplete and should be sent back with the gaps
named. Track every promo in Jira under the `PROMO-` project.

## Required Fields

A complete promo brief must specify all of the following:

- **Owner:** named person accountable for the brief, plus the engineering owner
  assigned to build the bonus configuration.
- **Audience / eligibility:** which players qualify — for example new UK players
  only, first deposit, no existing customers — and any geo or segment limits.
- **Budget:** an approved marketing budget cap for the campaign (e.g. a fixed GBP
  cap), so liability is bounded.
- **Start and end dates:** explicit go-live date and end date. "Soon" or "this
  weekend" is not a date.
- **Bonus mechanics:** match percentage and cap (e.g. 100% up to 50 GBP),
  minimum deposit, and wagering requirement (e.g. 35x).
- **Terms and conditions:** bonus expiry window, per-player cap, and the excluded
  games list (e.g. slots-only contribution).
- **Compliance sign-off:** confirmation from Compliance, including KYC and
  responsible-gambling requirements (see below).
- **Acceptance criteria:** how QA confirms the bonus credits, wagers, and
  withdraws correctly before launch.

## Bonus Mechanics Defaults

When a field is unstated, these are the house defaults to confirm with the owner,
not to assume silently:

- Wagering requirement: 35x bonus amount.
- Minimum qualifying deposit: 10 GBP.
- Bonus expiry: 7 days after credit.
- Cap: one bonus per player, new customers only unless stated.

## Compliance and KYC Sign-off

- A promotion that pays out real funds cannot be withdrawn by a player until KYC
  is complete — see the KYC/AML Compliance doc. State this in the terms.
- Responsible-gambling controls (deposit limits, self-exclusion checks) must
  still apply to bonus play.
- Compliance signs off the final terms, eligibility, and any market-specific
  wording before scheduling.

## Approval and Acceptance Criteria

- Launch is approved only when all required fields are present and Compliance has
  signed off.
- QA verifies, against documented acceptance criteria, that the bonus credits on
  qualifying deposit, tracks wagering correctly, blocks withdrawal until wagering
  and KYC are met, and respects the budget cap.
- Schedule the go-live only after approval; do not soft-launch an unsigned promo.
