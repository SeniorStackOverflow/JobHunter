from __future__ import annotations

# ruff: noqa: S603
import os
import subprocess
from pathlib import Path

GIT = "/usr/bin/git"
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "check-code-sync.sh"


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


def _commit(repo: Path, text: str, message: str) -> str:
    (repo / "file.txt").write_text(text)
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _clone(src: Path, dst: Path) -> None:
    subprocess.run([GIT, "clone", "-q", str(src), str(dst)], check=True)
    _git(dst, "config", "user.name", "Test")
    _git(dst, "config", "user.email", "test@example.invalid")


def _run(dev: Path, prod: Path) -> tuple[int, dict[str, str]]:
    env = os.environ.copy()
    env["JOBHUNTER_DEV_REPO"] = str(dev)
    env["JOBHUNTER_PROD_REPO"] = str(prod)
    result = subprocess.run(
        [str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    parsed = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return result.returncode, parsed


def test_equal_uses_dev_main_not_current_checkout(tmp_path: Path) -> None:
    dev = tmp_path / "dev"
    prod = tmp_path / "prod"
    _init_repo(dev)
    _clone(dev, prod)

    _git(dev, "checkout", "-q", "-b", "feature")
    _commit(dev, "feature\n", "feature work")

    code, result = _run(dev, prod)

    assert code == 0
    assert result["state"] == "equal"
    assert result["dev_sha"] == _git(dev, "rev-parse", "refs/heads/main")
    assert result["prod_sha"] == _git(prod, "rev-parse", "HEAD")


def test_dev_ahead_is_healthy(tmp_path: Path) -> None:
    dev = tmp_path / "dev"
    prod = tmp_path / "prod"
    _init_repo(dev)
    _clone(dev, prod)
    _commit(dev, "dev ahead\n", "dev ahead")

    code, result = _run(dev, prod)

    assert code == 0
    assert result["state"] == "dev_ahead"


def test_dev_behind_is_alertable(tmp_path: Path) -> None:
    dev = tmp_path / "dev"
    prod = tmp_path / "prod"
    _init_repo(dev)
    _clone(dev, prod)
    _commit(prod, "prod ahead\n", "prod ahead")
    _git(dev, "fetch", "-q", str(prod), "HEAD")

    code, result = _run(dev, prod)

    assert code == 2
    assert result["state"] == "dev_behind"


def test_diverged_is_alertable(tmp_path: Path) -> None:
    dev = tmp_path / "dev"
    prod = tmp_path / "prod"
    _init_repo(dev)
    _clone(dev, prod)
    _commit(dev, "dev branch\n", "dev branch")
    _commit(prod, "prod branch\n", "prod branch")
    _git(dev, "fetch", "-q", str(prod), "HEAD")

    code, result = _run(dev, prod)

    assert code == 2
    assert result["state"] == "diverged"


def test_missing_prod_object_is_unknown_not_failure(tmp_path: Path) -> None:
    dev = tmp_path / "dev"
    prod = tmp_path / "prod"
    _init_repo(dev)
    _init_repo(prod)
    _commit(prod, "prod only\n", "prod only")

    code, result = _run(dev, prod)

    assert code == 0
    assert result["state"] == "unknown"
    assert result["detail"] == "prod_commit_missing_in_dev_object_db"
