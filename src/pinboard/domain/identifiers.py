from typing import NewType

ActionId = NewType("ActionId", str)
ArtifactRefId = NewType("ArtifactRefId", int)
AttemptId = NewType("AttemptId", str)
CandidateId = NewType("CandidateId", str)
CheckpointId = NewType("CheckpointId", str)
HostId = NewType("HostId", str)
HistoryId = NewType("HistoryId", int)
HistorySubjectId = NewType("HistorySubjectId", str)
WorkItemId = NewType("WorkItemId", str)
LeaseId = NewType("LeaseId", str)
LedgerId = NewType("LedgerId", str)
ProposalId = NewType("ProposalId", str)
ReviewId = NewType("ReviewId", str)
TaskId = NewType("TaskId", str)

type SubjectId = WorkItemId | AttemptId | ProposalId | LedgerId
