# agent-bus protocol for antigravity

You are antigravity, a participating agent in this project coordinated via **agent-bus**.
Read these instructions at the start of every session and follow them strictly.

## Session startup

1. Run `agent-bus work as antigravity` to set yourself as the active agent.
2. **Ensure background listener is active**: Keep a background listener running (`agent-bus watch --agent antigravity` or daemon `agent-bus worker start --agent antigravity`) so you receive incoming SSE notifications and wake up automatically without waiting for a human prompt. Do not stop it unless the human explicitly asks to end the session.
3. Run `agent-bus work check` to see if you have pending messages. **If you have pending messages, read them FIRST before doing anything else.**
4. Run `agent-bus show dashboard` to see current project state: tasks, inbox, locks, active agents.
5. Review the plan: read `.agent-bus/plan.md` if it exists.
6. Claim a task or continue one assigned to you before starting work.

## When to check inbox

**You MUST check for messages at these moments:**
- At the start of every session (`agent-bus work check`)
- After completing a task (`agent-bus work check` before `work done`)
- Before claiming a new task (`agent-bus work check`)
- After sending a message to another agent (`agent-bus work check`)
- When the user or another agent mentions they sent you a message
- Any time you're about to make a decision that affects other agents

Use `agent-bus work check` for a quick check. Use `agent-bus work inbox` for full details.

## Autonomous Operation Rules

- **Autonomous Background Listener**: You MUST have a background watcher/worker running during the entire session to reactively receive requests from other agents.
- **Never** edit a file locked by another agent. Check with `agent-bus show locks`.
- **Always** lock files before editing them (`agent-bus work lock <file>`).
- **Always** communicate via `agent-bus work msg` — never assume the other agent knows what you are doing.
- **Always** register decisions that affect architecture or scope (`agent-bus work decide`).
- **Always** hand off tasks properly with context if you cannot finish them.
- **Always** check inbox (`agent-bus work check`) before starting new work and after finishing tasks.
- If another agent sends you a message requiring reply (`reply_needed`), respond promptly.

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
