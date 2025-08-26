# Owner Flows + Finalize Fallback

New:
- `GET /created-pots?session_id=...`
- `POST /finalize-create?session_id=...`  ← creates the pots if webhook didn't
- Owner endpoints for listing entries and manual paid.

Call `POST /finalize-create` from the success page if `/created-pots` returns empty.
