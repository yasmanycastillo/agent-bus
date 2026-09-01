"""Gestión de git worktrees por agente (Pilar 1 de la arquitectura).

Cada agente opera aislado en ``.worktrees/<agent_id>`` vinculado a su rama
``agent/<agent_id>``; los cambios se commitean ahí y un integrador los fusiona
a ``main`` (ver ``integrator.py``, T6).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("agent_bus.worker.worktrees")

DEFAULT_BASE_DIR = ".worktrees"


@dataclass
class WorktreeInfo:
    agent_id: str
    path: Path
    branch: str

    def to_dict(self) -> dict:
        return {"agent_id": self.agent_id, "path": str(self.path), "branch": self.branch}


class WorktreeManager:
    """Crea, sincroniza y consulta los worktrees aislados por agente."""

    def __init__(self, repo_root: Path | None = None, base_dir: str = DEFAULT_BASE_DIR) -> None:
        self.repo_root = (repo_root or Path.cwd()).resolve()
        self.base_dir = self.repo_root / base_dir

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        import subprocess

        result = subprocess.run(
            ["git", *args],
            cwd=cwd or self.repo_root,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def branch_for(self, agent_id: str) -> str:
        return f"agent/{agent_id}"

    def path_for(self, agent_id: str) -> Path:
        return self.base_dir / agent_id

    def exists(self, agent_id: str) -> bool:
        path = self.path_for(agent_id)
        return path.is_dir() and (path / ".git").exists()

    def is_repo(self) -> bool:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def create(self, agent_id: str, base_ref: str = "main") -> WorktreeInfo:
        """Crea el worktree del agente sobre ``agent/<agent_id>``.

        Si la rama no existe, nace de ``base_ref``. Idempotente: si el
        worktree ya existe, solo se devuelve su info.
        """
        branch = self.branch_for(agent_id)
        path = self.path_for(agent_id)
        if self.exists(agent_id):
            logger.info("Worktree for '%s' already exists at %s", agent_id, path)
            return WorktreeInfo(agent_id, path, branch)

        if not self.is_repo():
            raise RuntimeError(f"{self.repo_root} is not a git repository")

        existing_branches = self._git("branch", "--list", branch)
        if existing_branches:
            self._git("worktree", "add", str(path), branch)
        else:
            self._git("worktree", "add", "-b", branch, str(path), base_ref)
        logger.info("Created worktree for '%s' at %s (branch %s)", agent_id, path, branch)
        return WorktreeInfo(agent_id, path, branch)

    def sync_with_base(self, agent_id: str, base_ref: str = "main") -> str:
        """Rebasea la rama del agente contra ``base_ref`` (para minimizar conflictos)."""
        if not self.exists(agent_id):
            raise RuntimeError(f"No worktree for agent '{agent_id}'")
        return self._git("rebase", base_ref, cwd=self.path_for(agent_id))

    def current_diff(self, agent_id: str, base_ref: str = "main") -> str:
        """Diff resumido de la rama del agente vs ``base_ref`` (para compactación de prompts)."""
        if not self.exists(agent_id):
            return ""
        return self._git("diff", "--stat", f"{base_ref}...HEAD", cwd=self.path_for(agent_id))

    def has_changes(self, agent_id: str) -> bool:
        """True si el worktree tiene cambios sin commitear."""
        if not self.exists(agent_id):
            return False
        status = self._git("status", "--porcelain", cwd=self.path_for(agent_id))
        return bool(status)

    def commit_all(self, agent_id: str, message: str) -> str | None:
        """Commitea todos los cambios pendientes del worktree; None si no había nada."""
        if not self.exists(agent_id):
            raise RuntimeError(f"No worktree for agent '{agent_id}'")
        if not self.has_changes(agent_id):
            return None
        self._git("add", "-A", cwd=self.path_for(agent_id))
        self._git("commit", "-m", message, cwd=self.path_for(agent_id))
        return self._git("rev-parse", "HEAD", cwd=self.path_for(agent_id))

    def remove(self, agent_id: str, force: bool = False) -> None:
        """Elimina el worktree y su rama local."""
        if not self.exists(agent_id):
            return
        args = ["worktree", "remove"]
        if force or self.has_changes(agent_id):
            args.append("--force")
        args.append(str(self.path_for(agent_id)))
        self._git(*args)
        try:
            self._git("branch", "-D", self.branch_for(agent_id))
        except RuntimeError:
            pass  # rama ya fusionada/borrada
        logger.info("Removed worktree for '%s'", agent_id)

    def list_all(self) -> list[WorktreeInfo]:
        """Worktrees de agentes registrados bajo ``.worktrees/``."""
        result: list[WorktreeInfo] = []
        if not self.base_dir.is_dir():
            return result
        for entry in sorted(self.base_dir.iterdir()):
            if entry.is_dir() and (entry / ".git").exists():
                agent_id = entry.name
                result.append(WorktreeInfo(agent_id, entry, self.branch_for(agent_id)))
        return result
