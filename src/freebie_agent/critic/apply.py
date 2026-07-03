"""Apply/reject operator decisions on proposals.

Apply = write the diff, re-run the eval gate for prompt changes, git commit
with the rationale, mark applied with the sha. Reject = record it.
"""

from __future__ import annotations

import sqlite3

from freebie_agent import ledger
from freebie_agent.config import Config
from freebie_agent.models import ProposalState


def reject_proposal(conn: sqlite3.Connection, proposal_id: int) -> str:
    proposal = ledger.get_proposal(conn, proposal_id)
    if proposal is None:
        return f"Proposal #{proposal_id} not found."
    if proposal["state"] != ProposalState.PENDING.value:
        return f"Proposal #{proposal_id} is already {proposal['state']}."
    ledger.set_proposal_state(conn, proposal_id, ProposalState.REJECTED)
    ledger.add_event(conn, "proposal_rejected", {"proposal_id": proposal_id})
    return f"❌ Proposal #{proposal_id} rejected and recorded."


def apply_proposal(conn: sqlite3.Connection, cfg: Config, proposal_id: int) -> str:
    proposal = ledger.get_proposal(conn, proposal_id)
    if proposal is None:
        return f"Proposal #{proposal_id} not found."
    if proposal["state"] != ProposalState.PENDING.value:
        return f"Proposal #{proposal_id} is already {proposal['state']}."
    # The applier (diff writing + eval gate + git commit) lands with the
    # critic in phase 5; no proposals can exist before the critic runs.
    return (
        f"Proposal #{proposal_id} cannot be applied yet: the critic/applier "
        "is not installed in this build."
    )
