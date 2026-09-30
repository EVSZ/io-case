# Cloud ticketing for Dutch public transport

Technical case. Author: Mateusz, 2026-09-30.

Every traveler has a permanent or temporary cloud account. Balances and authoritative ticket records remain in the cloud. NFC credentials (bank card, phone wallet, transit card) identify an account, while QR credentials carry signed ticket claims for offline validation.

## Core idea

The system has three paths with very different needs, and the design keeps them apart:

| Path | Question | Speed |
|---|---|---|
| Validation | May this credential board now? | Milliseconds, decided on the gate or bus reader |
| Travel processing | Which journeys happened? | Seconds to minutes |
| Billing | What does the account owe this month? | Hours to days |

**In the normal path, boarding does not wait for the cloud.** The validator decides locally, using data the cloud published in advance, and reports the tap afterwards. That local data can be out of date, so the design limits how long it is trusted and states what happens after that ([Part 1](1-architecture.md#boarding-local-validation-and-its-limits)).

## Terms

The brief says "token"; these documents use more specific terms.

| Term | Meaning |
|---|---|
| Account | The traveler's record in the cloud. Permanent (signed in) or temporary (no sign-in, limited lifetime) |
| NFC credential | Bank card, phone wallet or transit card linked to an account. Bank cards are read through a certified EMV payment kernel |
| Ticket entitlement | The right to travel, e.g. a day ticket. Its authoritative record is in the cloud database |
| QR credential | A signed copy of one ticket's product and validity claims, so a validator can check it offline |
| Travel event | A tap reported by a validator |

## Contents

| Part | Document |
|---|---|
| 1. Cloud infrastructure | [1-architecture.md](1-architecture.md) |
| 2. QR-ticket service and API | [2-qr-ticket-service.md](2-qr-ticket-service.md) |
| 3. Example code | [3-code.md](3-code.md), code in [code/](code/) |
| 4. Chatbot | [4-chatbot.md](4-chatbot.md) |

## Key decisions and trade-offs

| Decision | Alternative considered | Why this choice | Accepted downside |
|---|---|---|---|
| Validators decide locally from published data | Central lookup on every tap | Fast, and keeps working through network loss | Decisions use data up to a refresh interval old; offline risk must be bounded by policy |
| Signed QR credential (ES256) | Opaque random ID | Verifiable offline with a few public keys, no need to ship every valid ticket to every validator | Revocation needs a deny list; copies verify until expiry or revocation; key management |
| Board unknown bank cards, authorize payment afterwards | Online authorization at the gate | No queues at rush hour, works during short outages | Some journeys end unpaid (proposed operator risk policy) |
| PostgreSQL as the source of truth | Cosmos DB | Transactions and unique constraints for tickets and money | Connection limits, so pooling and backpressure are needed |
| Idempotency key plus database unique constraints | Application check before insert | Correct under retries and concurrent requests | Constraint errors must be mapped to defined responses |
| Transactional outbox and idempotent consumers | Write to the database, then publish directly | Closes the database-to-broker dual-write gap | Extra table and relay; duplicates still possible, so consumers deduplicate |
| Chatbot: public index plus read-only tools using the traveler's own token | One index that includes personal data | Access control stays in our APIs, not in the model | Personal data needed for an answer still reaches the model, so it is minimized |

## Key assumptions

- QR tickets are for fixed products with a validity window (e.g. a day ticket). Pay-as-you-go check-in/check-out uses NFC credentials.
- A paid entitlement exists before a ticket is created. Payments are out of scope.
- Offline acceptance limits and unknown-card rules are **proposed operator risk policies**, not established transport-system requirements.
- Dates follow the `Europe/Amsterdam` calendar; times are stored in UTC.
- Latency and throughput numbers are targets, not measurements.
