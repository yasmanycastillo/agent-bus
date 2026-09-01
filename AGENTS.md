# Universal Agent Protocol for agent-bus

This repository is governed by the **agent-bus** autonomous multi-agent coordination protocol. Every AI coding agent (Claude Code, Antigravity/AGY, Codex, Grok, Aider, etc.) operating in this workspace **MUST** adhere to the following rules:

---

## ⚡ 1. Autonomous Background Listener (MANDATORY)

> [!IMPORTANT]
> **Never operate purely passively.**
> When starting a session in this repository, you **MUST** ensure a background listener is actively running:
> ```bash
> # For interactive sessions:
> uv run agent-bus watch --agent <your_agent_id>
> 
> # For fully autonomous headless execution:
> uv run agent-bus worker start --agent <your_agent_id>
> ```
> **Do not stop this background process** unless the human user explicitly instructs you to end the session. This guarantees that you wake up automatically when other agents send messages, assign tasks, or request code reviews.

---

## 🛡️ 2. Concurrency and File Locking

- **Never edit a file without acquiring a lock first**:
  ```bash
  uv run agent-bus work lock <file_path> --reason "<what you are doing>"
  ```
- **Check active locks before modifying shared resources**:
  ```bash
  uv run agent-bus show locks
  ```
- **Release your locks immediately when done**:
  ```bash
  uv run agent-bus work unlock <file_path>
  ```

---

## 📬 3. Inbox and Communication

- **Check inbox before and after every task**:
  ```bash
  uv run agent-bus work check
  uv run agent-bus work inbox
  ```
- **Communicate decisions, queries, and completions directly over the bus**:
  ```bash
  # Send direct message requiring response
  uv run agent-bus work msg <recipient> "<message>" --reply-needed --task <task_id>

  # Send notification / update
  uv run agent-bus work msg <recipient> "<message>"
  ```

---

## 🌿 4. Git Worktrees & Branch Isolation

- In autonomous mode, all development occurs in isolated worktrees: `.worktrees/<agent_id>` on branch `agent/<agent_id>`.
- The `BranchIntegrator` (Tech Lead) automatically runs the test suite and merges green builds to `main`.
- Never commit directly to `main` without running `uv run pytest`.

---

## 📊 5. Monitoring & Dashboard

- Check live real-time status at any time:
  ```bash
  uv run agent-bus top
  ```
