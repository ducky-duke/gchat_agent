# KYC / AML Compliance

> Illustrative sample data for the gchat_agent demo — not real policy.

Know-Your-Customer (KYC) and Anti-Money-Laundering (AML) requirements: the
verification tiers, the transaction thresholds that trigger enhanced checks, and
who signs off. These controls gate withdrawals — including bonus winnings — and
apply across deposits, withdrawals, and promotions.

## Verification Tiers

| Tier | Trigger                                          | Checks required                                  |
|------|--------------------------------------------------|--------------------------------------------------|
| 0    | Registration, before first deposit               | Email + age/identity declaration                 |
| 1    | Standard play, cumulative activity under 2,000 EUR | ID document + proof of address                  |
| 2 (Enhanced) | Cumulative deposits or withdrawals over 2,000 EUR, or a single transaction over 2,000 EUR | Tier 1 plus source-of-funds evidence |
| 3 (EDD) | Cumulative activity over 10,000 EUR, or any high-risk flag | Tier 2 plus enhanced due diligence + MLRO review |

## Transaction Thresholds

- **Withdrawal gate:** no withdrawal — including bonus winnings — is released
  until at least Tier 1 KYC is complete. Bonuses are not withdrawable before KYC.
- **Enhanced due diligence (EDD):** required once a player's cumulative deposits
  or withdrawals exceed **2,000 EUR**, or on any single transaction over
  **2,000 EUR**. EDD requires documented source-of-funds.
- **Heightened EDD + MLRO review:** required over **10,000 EUR** cumulative
  activity within a rolling 30 days, or on any AML red flag.
- **Velocity check:** more than 5,000 EUR withdrawn in 24 hours is held for
  manual review regardless of tier.

## AML Red Flags

Escalate immediately, independent of value thresholds, on any of:

- Rapid deposit then withdrawal with little or no play ("pass-through").
- Mismatched payment instruments or third-party funding.
- Use of payout/deposit methods that obscure source of funds.
- Player in a sanctioned or restricted jurisdiction.

## Sign-off and Escalation

- **Tier 1–2 verification:** Payments / Risk operations team approves.
- **EDD (over 2,000 EUR) and source-of-funds acceptance:** Compliance analyst
  signs off.
- **Over 10,000 EUR, MLRO review, or any AML red flag:** the Money Laundering
  Reporting Officer (MLRO) reviews and decides on a Suspicious Activity Report.
- Record the tier, evidence, approver, and date against the player's KYC record.
