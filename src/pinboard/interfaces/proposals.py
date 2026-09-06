import msgspec

from pinboard.domain.errors import DecisionFailureCode
from pinboard.interfaces.errors import ProposalFailure, ProposalResult
from pinboard.interfaces.proposal_models import Proposal


def parse_proposal(data: bytes | str) -> ProposalResult[Proposal]:
    try:
        return msgspec.json.decode(data, type=Proposal)
    except msgspec.DecodeError as error:
        return ProposalFailure(DecisionFailureCode.PROPOSAL_INVALID, f"Cannot decode proposal JSON: {error}", None)
