"""Canonical resource identities for physical and shared-project file locks."""
from __future__ import annotations

from pathlib import Path

from agent_bus.project import _git_roots, resolve_project_root

_MAX_PATH_LENGTH = 4096


def _path(value: str, scope: str) -> Path:
    if scope not in ("checkout", "project"):
        raise ValueError("Unknown lock scope")
    if not isinstance(value, str) or not value.strip() or "\0" in value or len(value) > _MAX_PATH_LENGTH:
        raise ValueError("Lock path must be a nonempty string of at most 4096 characters without NUL")
    return Path(value)


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("Cannot resolve lock path") from exc


def _within(path: Path, root: Path) -> Path:
    if not path.is_relative_to(root):
        raise ValueError("Project lock path escapes the project root")
    return path


def server_lock_path(file_path: str, scope: str = "checkout", *, project_root: Path | None,
                     base_dir: Path) -> str:
    """Resolve a wire path using server state frozen when the hub starts."""
    path = _path(file_path, scope)
    if scope == "checkout":
        return str(_resolve(path if path.is_absolute() else Path(base_dir) / path))
    if project_root is None:
        raise ValueError("Project scope requires a project root")
    if path.is_absolute():
        raise ValueError("Project scope requires a project-relative path")
    root = _resolve(Path(project_root))
    return str(_within(_resolve(root / path), root))


def _existing_directory(path: Path) -> Path:
    while not path.is_dir() and path != path.parent:
        path = path.parent
    return path


def client_lock_path(file_path: str, scope: str = "checkout", *, cwd: Path | None = None,
                     project_root: Path | None = None) -> str:
    """Return an absolute checkout path or a project-relative wire path.

    Explicit cwd/project_root let long-lived clients keep their launch context.
    Git mapping is based on the target's checkout, so absolute paths from another
    linked checkout are accepted while unrelated repositories are rejected.
    """
    path = _path(file_path, scope)
    base = _resolve(Path.cwd() if cwd is None else Path(cwd))
    physical = _resolve(path if path.is_absolute() else base / path)
    if scope == "checkout":
        return str(physical)
    selected = project_root if project_root is not None else resolve_project_root(base)
    if selected is None:
        raise ValueError("Project scope requires a project root")
    root = _resolve(Path(selected))
    roots = _git_roots(_existing_directory(physical))
    if roots:
        checkout, common = roots
        if not root.is_relative_to(common):
            raise ValueError("Lock path belongs to another project")
        actual_root = checkout / root.relative_to(common)
        relative = _within(physical, actual_root).relative_to(actual_root)
    else:
        relative = _within(physical, root).relative_to(root)
    # Also reject aliases that escape only in the canonical checkout.
    server_lock_path(str(relative), "project", project_root=root, base_dir=root)
    return str(relative)
