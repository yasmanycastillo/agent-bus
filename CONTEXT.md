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

**Instruction**: A request the human has already confirmed, stored with `submit_instruction`. The agent that stores it is its coordinator; storing it does not start the work.
_Avoid_: prompt, plan

**Coordinator**: The agent that owns an instruction. Only it assigns that instruction's work and may close an implementation it assigned once the latest verdict on its latest candidate commit approves it. It does not do the assigned work itself.
_Avoid_: admin, orchestrator

**Assignment**: A part of an instruction given to one agent with `assign_work`, as an implementation or a review. A reviewer cannot implement the work it reviews. Self-assignments send no message.
_Avoid_: claim, handoff

**Review**: An assigned task that judges named implementations. It stays blocked until those implementations are submitted and closes once it approves every covered candidate commit.
_Avoid_: code review comment

**Verdict**: One reviewer's judgment (`approve` or `changes_requested`) on one implementation's candidate commit. `changes_requested` reopens the implementation as pending and free.
_Avoid_: review result, LGTM

**Agent pane**: An agent TUI that the coordinator opens, drives and closes in the project's private tmux server. Its state (`idle`, `working`, `blocked`, `dead`) is read from the last screen lines; a person answers `blocked` permission questions. Still a prototype.
_Avoid_: worker, muxel panel

**Watcher**: The process that wakes an agent already open in a TUI (muxel or agent pane) when the bus has work for it, deferring while the TUI is busy. One per agent identity.
_Avoid_: worker, headless turn

**Nudge**: A short notice the watcher types into a TUI when the agent's tasks change, asking it to run `my_pending_items`. It carries no task content and is sent once per change.
_Avoid_: message, notification
