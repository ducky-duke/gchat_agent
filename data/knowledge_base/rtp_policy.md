# RTP Policy

> Illustrative sample data for the gchat_agent demo — not real policy.

This document defines the return-to-player (RTP) policy for casino and slots
content, the approved RTP ranges, who signs off on deviations, and the
escalation path when configured RTP drifts outside the allowed band.

## Scope

Applies to all real-money slots, table games, and instant-win titles in the UK
and EU-regulated markets. Does not apply to free-play or demo modes. Promotional
mechanics (deposit-match bonuses, free spins) do not change a game's configured
RTP — they are governed by the Promo Launch Checklist instead.

## Approved RTP Ranges

| Game category        | Minimum RTP | Target RTP | Maximum RTP |
|----------------------|-------------|------------|-------------|
| Slots                | 92%         | 95%        | 97%         |
| Table games          | 97%         | 98%        | 99%         |
| Instant-win / scratch| 88%         | 90%        | 94%         |

- The configured RTP for every live title must fall within its category band.
- New slots ship at the target RTP (95%) unless a commercial exception is signed.
- A single title must not be configured below 92% RTP in regulated markets.

## Deviation Sign-off

Any RTP set outside the target — for example a slot below 94% or above 96.5% —
is a deviation and requires written sign-off before going live:

- **Below target (92%–94%):** Head of Casino approves, with a commercial
  rationale attached to the change ticket.
- **At or above 97%:** Finance plus Head of Casino approve jointly (margin impact).
- **Below the 92% category floor:** prohibited; not approvable in regulated markets.

Record every approved deviation in the game configuration ticket with the
approver, date, and the agreed RTP value.

## Monitoring and Escalation

Actual measured RTP is reconciled weekly against the configured value:

- **Drift under 1.0 percentage point:** within tolerance, no action.
- **Drift of 1.0–2.0 points:** Casino Operations investigates within 48 hours.
- **Drift over 2.0 points, or any title measuring below 92%:** raise a P2
  incident, freeze the title if player-facing, and escalate to the Head of
  Casino and Compliance on-call.

Escalation owner: Casino Operations on-call. Compliance must be notified for any
sustained sub-floor RTP, as it is a regulatory reporting trigger.
