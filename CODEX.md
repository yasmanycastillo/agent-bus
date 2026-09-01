# agent-bus protocol for codex

You are codex, a participating agent in this project coordinated via **agent-bus**.
Read these instructions at the start of every session and follow them strictly.

## Session startup

1. Run `agent-bus work as codex` to set yourself as the active agent.
2. Run `agent-bus work check` to see if you have pending messages. **If you have pending messages, read them FIRST before doing anything else.**
3. Run `agent-bus show dashboard` to see current project state: tasks, inbox, locks, active agents.
4. Review the plan: read `.agent-bus/plan.md` if it exists.
5. Claim a task or continue one assigned to you before starting work.

## When to check inbox

**You MUST check for messages at these moments:**
- At the start of every session (`agent-bus work check`)
- After completing a task (`agent-bus work check` before `work done`)
- Before claiming a new task (`agent-bus work check`)
- After sending a message to another agent (`agent-bus work check`)
- When the user or another agent mentions they sent you a message
- Any time you're about to make a decision that affects other agents

Use `agent-bus work check` for a quick check. Use `agent-bus work inbox` for full details.

## Daily workflow

### Before working on a task
- `agent-bus work check` — check for new messages first.
- `agent-bus work claim <task_id>` — claim it before touching any code.
- `agent-bus work lock <file_path>` — lock every file you will modify. Always lock before editing.

### While working
- Use `agent-bus work msg <other_agent> "<message>"` to communicate with other agents.
- Use `agent-bus work msg <other_agent> "<question>" --reply-needed --task <task_id>` when you need a response.
- Register important decisions: `agent-bus work decide "<title>" "<what>"`.

### After finishing a task
- `agent-bus work check` — check if anyone sent you messages while you were working.
- Release all your locks: `agent-bus work unlock <file_path>`.
- Mark the task done: `agent-bus work done <task_id>`.

### Handing off to another agent
If you need to transfer a task mid-progress:
```bash
agent-bus work handoff <task_id> <to_agent> \
  --summary "What you did and current status" \
  --files "src/file1.py,src/file2.py" \
  --questions "Open question 1,Open question 2"
```

## Rules

- **Never** edit a file locked by another agent. Check with `agent-bus show locks`.
- **Always** lock files before editing them.
- **Always** communicate via `agent-bus work msg` — never assume the other agent knows what you are doing.
- **Always** register decisions that affect architecture or scope.
- **Always** hand off tasks properly with context if you cannot finish them.
- **Always** check inbox (`agent-bus work check`) before starting new work and after finishing tasks.
- If another agent sends you a message requiring reply (`reply_needed`), respond promptly.
- If a user tells you another agent sent you a message, run `agent-bus work inbox` immediately.

## Useful commands

| Command | Description |
|---|---|
| `agent-bus work check` | Quick check for pending messages |
| `agent-bus work inbox` | Full inbox listing |
| `agent-bus show dashboard` | Full project overview |
| `agent-bus show tasks` | List all tasks |
| `agent-bus show tasks --status in_progress` | Filter tasks by status |
| `agent-bus show inbox <agent>` | View agent inbox |
| `agent-bus show locks` | Active file locks |
| `agent-bus show decisions` | Registered decisions |
| `agent-bus show agents` | Registered agents |
| `agent-bus work context` | View shared project context |
| `agent-bus work context --update tech_stack "fastapi,postgres"` | Update project context |
