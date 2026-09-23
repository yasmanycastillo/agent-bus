# Agent Bus domain language

Agent Bus records and governs work performed by independent agents and runtimes.

## Language

**Task plan**: A validated proposal of tasks and dependencies for one objective. It does not assign execution authority or prove that any task was completed.
_Avoid_: provider response, Hermes plan

**Capability**: A project-approved ability that makes an agent eligible for a class of tasks. A participant's self-description alone does not grant permission to claim work.
_Avoid_: model name, provider name

**Runtime attempt**: One identifiable execution of a task by a runtime. A retry is a new attempt linked to the same task.
_Avoid_: task, agent session

**Candidate commit**: The immutable Git commit submitted for tests and review.
_Avoid_: candidate branch, merge commit

**Integration commit**: The commit created on the target branch when an approved candidate is merged. Its identity normally differs from the candidate commit.
_Avoid_: reviewed SHA

**Artifact**: An immutable output of work, addressable by an Agent Bus identifier and bound to its producer and task.
_Avoid_: message attachment

**Evidence**: A claim about an acceptance criterion backed by a verifiable artifact, commit, or recorded review result.
_Avoid_: agent assertion
