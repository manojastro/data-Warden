"""Git-versioned runtime workspace holding the canonical, patchable pipeline code.

``artifacts/runtime/workspace`` is a local git repository containing ``dbt/`` (copied from
``pipelines/dbt``) and ``ingestion/mappings.yaml``. Every change to canonical code (a fault
injection or an approved, promoted repair) is a commit, so proposals can pin a
``base_commit`` and the executor can refuse stale proposals. Only fixed git sub-commands are
executed here; no user-supplied arguments reach the shell.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from datawarden.config import REPO_ROOT, get_settings

BASELINE_TAG = "baseline"
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "datawarden-runtime",
    "GIT_AUTHOR_EMAIL": "runtime@datawarden.local",
    "GIT_COMMITTER_NAME": "datawarden-runtime",
    "GIT_COMMITTER_EMAIL": "runtime@datawarden.local",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
}


class WorkspaceError(RuntimeError):
    pass


def workspace_dir() -> Path:
    return get_settings().workspace_dir


def dbt_project_dir(root: Path | None = None) -> Path:
    return (root or workspace_dir()) / "dbt"


def mappings_path(root: Path | None = None) -> Path:
    return (root or workspace_dir()) / "ingestion" / "mappings.yaml"


def _git(*args: str, cwd: Path | None = None, input_text: str | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd or workspace_dir(),
        env=_GIT_ENV,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if check and proc.returncode != 0:
        raise WorkspaceError(f"git {args[0]} failed: {proc.stderr.strip()[:500]}")
    return proc.stdout


def init_workspace() -> str:
    """(Re)create the runtime workspace from the repository baseline. Returns baseline commit."""
    ws = workspace_dir()
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    shutil.copytree(
        REPO_ROOT / "pipelines" / "dbt",
        ws / "dbt",
        ignore=shutil.ignore_patterns("target", "logs", "dbt_packages", ".user.yml"),
    )
    (ws / "ingestion").mkdir()
    shutil.copy2(REPO_ROOT / "pipelines" / "ingestion" / "mappings.yaml", mappings_path(ws))
    _git("init", "-q", "-b", "main")
    _git("add", "-A")
    _git("commit", "-q", "-m", "baseline: canonical pipeline code")
    _git("tag", "-f", BASELINE_TAG)
    registry = get_settings().contracts_registry_dir
    if registry.exists():
        shutil.rmtree(registry)
    registry.mkdir(parents=True)
    for f in (REPO_ROOT / "pipelines" / "contracts").glob("*.yaml"):
        shutil.copy2(f, registry / f.name)
    return head_commit()


def head_commit() -> str:
    return _git("rev-parse", "HEAD").strip()


def baseline_commit() -> str:
    return _git("rev-parse", BASELINE_TAG).strip()


def log(limit: int = 20) -> list[dict]:
    out = _git("log", f"-{int(limit)}", "--format=%H%x1f%an%x1f%aI%x1f%s")
    rows = []
    for line in out.splitlines():
        sha, author, when, subject = line.split("\x1f")
        rows.append({"commit": sha, "author": author, "committed_at": when, "subject": subject})
    return rows


def show_commit(commit: str) -> str:
    _validate_sha(commit)
    return _git("show", "--format=%H%n%an%n%aI%n%s%n", "--no-color", commit)


def diff(base: str, head: str = "HEAD") -> str:
    _validate_sha(base)
    if head != "HEAD":
        _validate_sha(head)
    return _git("diff", "--no-color", base, head)


def _validate_sha(value: str) -> None:
    if not (7 <= len(value) <= 40 and all(c in "0123456789abcdef" for c in value)) and value != BASELINE_TAG:
        raise WorkspaceError("invalid commit reference")


def commit_files(files: dict[str, str], message: str, root: Path | None = None) -> str:
    """Write the given relative files and commit. Returns the new commit hash."""
    base = root or workspace_dir()
    for rel, content in files.items():
        path = (base / rel).resolve()
        if base.resolve() not in path.parents:
            raise WorkspaceError(f"path escapes workspace: {rel}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git("add", "-A", cwd=base)
    _git("commit", "-q", "-m", message, cwd=base)
    return _git("rev-parse", "HEAD", cwd=base).strip()


def apply_patch(patch_text: str, root: Path | None = None, *, check_only: bool = False) -> None:
    args = ["apply", "--whitespace=nowarn"]
    if check_only:
        args.append("--check")
    _git(*args, "-", cwd=root or workspace_dir(), input_text=patch_text)


def read_file(rel: str, commit: str | None = None) -> str:
    if commit:
        _validate_sha(commit)
        return _git("show", f"{commit}:{rel}")
    path = (workspace_dir() / rel).resolve()
    if workspace_dir().resolve() not in path.parents:
        raise WorkspaceError("path escapes workspace")
    return path.read_text()


def reset_to(commit: str) -> None:
    _validate_sha(commit)
    _git("reset", "-q", "--hard", commit)


def revert_to(commit: str, message: str) -> str:
    """Create a new commit whose tree equals ``commit`` (history-preserving rollback)."""
    _validate_sha(commit)
    _git("read-tree", "-u", "--reset", commit)
    status = _git("status", "--porcelain")
    if not status.strip():
        return head_commit()
    _git("commit", "-q", "-m", message)
    return head_commit()


def clone_to(dest: Path, commit: str | None = None) -> Path:
    """Materialise a detached copy of the workspace (for shadow runs)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "-q", "--no-hardlinks", str(workspace_dir()), str(dest)],
        env=_GIT_ENV,
        check=True,
        capture_output=True,
        timeout=60,
    )
    if commit:
        _validate_sha(commit)
        _git("checkout", "-q", commit, cwd=dest)
    return dest


def tree_hash(root: Path | None = None) -> str:
    """Content hash of the dbt project + mappings (independent of git metadata)."""
    base = root or workspace_dir()
    h = hashlib.sha256()
    for path in sorted(
        p
        for p in base.rglob("*")
        if p.is_file() and ".git" not in p.parts and "target" not in p.parts and "logs" not in p.parts
    ):
        h.update(str(path.relative_to(base)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()
