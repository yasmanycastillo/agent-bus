from __future__ import annotations

import subprocess

import pytest

from agent_bus.core.lock_paths import client_lock_path, server_lock_path


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "core.hooksPath=/dev/null",
                    "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
                   check=True, capture_output=True)


@pytest.fixture
def checkouts(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    git(primary, "init", "-q")
    git(primary, "commit", "--allow-empty", "-m", "base")
    linked = tmp_path / "linked"
    git(primary, "worktree", "add", "--detach", str(linked), "HEAD")
    for checkout in (primary, linked):
        (checkout / "src" / "deep").mkdir(parents=True)
    try:
        yield primary, linked
    finally:
        git(primary, "worktree", "remove", "--force", str(linked))


def test_checkout_is_physical_and_worktrees_are_distinct(checkouts):
    primary, linked = checkouts
    first = client_lock_path("../a.py", cwd=primary / "src/deep")
    second = client_lock_path("src/a.py", cwd=linked)
    assert first == str(primary / "src/a.py")
    assert second == str(linked / "src/a.py")
    assert first != second
    assert server_lock_path(first, project_root=primary, base_dir=primary) == first
    assert server_lock_path("src/./a.py", project_root=primary, base_dir=primary) == first


def test_project_scope_maps_worktrees_and_subdirectories(checkouts):
    primary, linked = checkouts
    for checkout in (primary, linked):
        for path, cwd in (("src/a.py", checkout), ("../a.py", checkout / "src/deep"),
                          (str(checkout / "src/a.py"), primary)):
            logical = client_lock_path(path, "project", cwd=cwd, project_root=primary)
            assert logical == "src/a.py"
            physical = server_lock_path(logical, "project", project_root=primary, base_dir=linked)
            assert physical == client_lock_path("src/a.py", cwd=primary)


def test_symlink_aliases_collapse_and_nonexistent_files_work(checkouts):
    primary, linked = checkouts
    (linked / "alias").symlink_to(linked / "src", target_is_directory=True)
    assert client_lock_path("alias/new.py", cwd=linked) == str(linked / "src/new.py")
    assert client_lock_path("alias/new.py", "project", cwd=linked, project_root=primary) == "src/new.py"
    (primary / "alias").symlink_to(primary / "src", target_is_directory=True)
    assert server_lock_path("alias/new.py", "project", project_root=primary, base_dir=primary) == str(primary / "src/new.py")


def test_project_scope_rejects_outside_paths_and_symlinks(checkouts, tmp_path):
    primary, linked = checkouts
    outside = tmp_path / "outside"
    outside.mkdir()
    (primary / "escape").symlink_to(outside, target_is_directory=True)
    (linked / "escape").symlink_to(outside, target_is_directory=True)
    for path in ("../outside/a", str(outside / "a"), "escape/a"):
        with pytest.raises(ValueError):
            server_lock_path(path, "project", project_root=primary, base_dir=primary)
        with pytest.raises(ValueError):
            client_lock_path(path, "project", cwd=linked, project_root=primary)
    # A path may be safe in a linked checkout but escape in the primary checkout.
    (linked / "canonical-only-escape").mkdir()
    (primary / "canonical-only-escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        client_lock_path("canonical-only-escape/a", "project", cwd=linked, project_root=primary)


def test_project_scope_rejects_nested_and_unrelated_repositories(checkouts, tmp_path):
    primary, linked = checkouts
    for root in (linked / "vendor", tmp_path / "unrelated"):
        root.mkdir()
        git(root, "init", "-q")
        with pytest.raises(ValueError, match="another project"):
            client_lock_path(str(root / "new.py"), "project", cwd=linked, project_root=primary)


def test_plain_project_and_frozen_cwd(tmp_path, monkeypatch):
    project = tmp_path / "plain"
    (project / "src").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    assert client_lock_path("new.py", cwd=project / "src") == str(project / "src/new.py")
    assert client_lock_path("new.py", "project", cwd=project / "src", project_root=project) == "src/new.py"
    assert server_lock_path("new.py", project_root=None, base_dir=other) == str(other / "new.py")
    with pytest.raises(ValueError, match="requires a project root"):
        server_lock_path("new.py", "project", project_root=None, base_dir=other)


@pytest.mark.parametrize("path", ["", "  ", "a\x00b", "a" * 4097, None])
def test_invalid_paths(path, tmp_path):
    for scope in ("checkout", "project"):
        with pytest.raises(ValueError):
            client_lock_path(path, scope, cwd=tmp_path, project_root=tmp_path)
        with pytest.raises(ValueError):
            server_lock_path(path, scope, project_root=tmp_path, base_dir=tmp_path)


def test_unknown_scope_and_absolute_project_wire_path(tmp_path):
    with pytest.raises(ValueError, match="Unknown lock scope"):
        client_lock_path("a", "invalid", cwd=tmp_path)
    with pytest.raises(ValueError, match="Unknown lock scope"):
        server_lock_path("a", "invalid", project_root=tmp_path, base_dir=tmp_path)
    with pytest.raises(ValueError, match="project-relative"):
        server_lock_path(str(tmp_path / "a"), "project", project_root=tmp_path, base_dir=tmp_path)
