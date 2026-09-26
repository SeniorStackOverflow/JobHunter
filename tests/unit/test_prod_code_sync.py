from __future__ import annotations

# ruff: noqa: S603
import os
import subprocess
from pathlib import Path

GIT = "/usr/bin/git"
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "sync-prod-code.sh"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [GIT, "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    subprocess.run([GIT, "init", "-q", str(path)], check=True)
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.invalid")
    (path / "file.txt").write_text("base\n")
    _git(path, "add", "file.txt")
    _git(path, "commit", "-q", "-m", "base")
    _git(path, "branch", "-M", "main")


def _clone(src: Path, dst: Path) -> None:
    subprocess.run([GIT, "clone", "-q", str(src), str(dst)], check=True)
    _git(dst, "config", "user.name", "Test")
    _git(dst, "config", "user.email", "test@example.invalid")


def _commit(repo: Path, text: str, message: str) -> str:
    (repo / "file.txt").write_text(text)
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _run(dev: Path, prod: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["JOBHUNTER_DEV_REPO"] = str(dev)
    env["JOBHUNTER_PROD_REPO"] = str(prod)
    return subprocess.run(
        [str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_noop_when_prod_matches_dev_main(tmp_path: Path) -> None:
    dev = tmp_path / "dev"
    prod = tmp_path / "prod"
    _init_repo(dev)
    _clone(dev, prod)

    result = _run(dev, prod)

    assert result.returncode == 0
    assert "state=already_synced" in result.stdout


def test_fast_forwards_prod_to_dev_main(tmp_path: Path) -> None:
    dev = tmp_path / "dev"
    prod = tmp_path / "prod"
    _init_repo(dev)
    _clone(dev, prod)
    expected = _commit(dev, "next\n", "next")

    result = _run(dev, prod)

    assert result.returncode == 0
    assert "state=fast_forwarded" in result.stdout
    assert _git(prod, "rev-parse", "HEAD") == expected


def test_refuses_diverged_prod(tmp_path: Path) -> None:
    dev = tmp_path / "dev"
    prod = tmp_path / "prod"
    _init_repo(dev)
    _clone(dev, prod)
    _commit(dev, "dev\n", "dev")
    _commit(prod, "prod\n", "prod")

    result = _run(dev, prod)

    assert result.returncode == 66
    assert "refusing non-fast-forward deployment" in result.stderr


def test_refuses_dirty_prod(tmp_path: Path) -> None:
    dev = tmp_path / "dev"
    prod = tmp_path / "prod"
    _init_repo(dev)
    _clone(dev, prod)
    (prod / "dirty.txt").write_text("dirty\n")

    result = _run(dev, prod)

    assert result.returncode == 64
    assert "not clean" in result.stderr
