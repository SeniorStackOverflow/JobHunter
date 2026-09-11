from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).parents[2]


def _load_backup_entrypoint() -> ModuleType:
    path = ROOT / "deploy/backup_entrypoint.py"
    assert path.exists(), "production backup URL parser is missing"
    spec = importlib.util.spec_from_file_location("backup_entrypoint", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _backup_test_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    backup_dir = tmp_path / "backups"
    script = tmp_path / "backup.sh"
    script.write_text(
        (ROOT / "deploy/backup.sh")
        .read_text(encoding="utf-8")
        .replace("/backups", str(backup_dir)),
        encoding="utf-8",
    )
    args_file = tmp_path / "pg_dump.args"
    env_file = tmp_path / "pg_dump.env"
    (fake_bin / "pg_dump").write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {args_file}\n"
        f'printf \'PGDATABASE=%s\\nPGPASSWORD=%s\\n\' "$PGDATABASE" "$PGPASSWORD" > {env_file}\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in --file=*) file=${arg#--file=} ;; esac\n'
        "done\n"
        "printf 'fake dump\\n' > \"$file\"\n",
        encoding="utf-8",
    )
    (fake_bin / "pg_restore").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for executable in (fake_bin / "pg_dump", fake_bin / "pg_restore"):
        executable.chmod(0o755)
    return script, fake_bin, backup_dir, args_file, env_file


def test_production_backup_uses_root_only_migrator_env() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))
    backup = compose["services"]["backup"]

    assert backup["env_file"] == [{"path": "/etc/jobhunter/migrator.env", "required": True}]
    assert backup["environment"] == {
        "POSTGRES_HOST": "",
        "POSTGRES_PORT": "",
        "POSTGRES_DB": "",
        "POSTGRES_USER": "",
        "POSTGRES_PASSWORD": "",
        "BACKUP_DIR": "/backups",
    }
    assert backup["image"].startswith("jobhunter-backup-prod:")
    assert backup["build"]["dockerfile"] == "deploy/backup.Dockerfile"
    assert backup["entrypoint"] == ["python3", "/usr/local/bin/job-agent-backup-entrypoint"]
    dockerfile = (ROOT / "deploy/backup.Dockerfile").read_text(encoding="utf-8")
    assert "FROM postgres:16-alpine" in dockerfile
    assert "apk add --no-cache python3" in dockerfile


def test_backup_entrypoint_extracts_explicit_libpq_fields() -> None:
    module = _load_backup_entrypoint()

    values = module.parse_migrator_database_url(
        "postgresql+asyncpg://jobhunter%5Fmigrator:p%40ss%2Fword@postgres:55432/job%5Fagent"
    )

    assert values == {
        "POSTGRES_HOST": "postgres",
        "POSTGRES_PORT": "55432",
        "POSTGRES_USER": "jobhunter_migrator",
        "POSTGRES_PASSWORD": "p@ss/word",
        "POSTGRES_DB": "job_agent",
    }


def test_backup_entrypoint_rejects_missing_host() -> None:
    module = _load_backup_entrypoint()

    with pytest.raises(ValueError, match="host"):
        module.parse_migrator_database_url("postgresql:///job_agent")


def test_backup_entrypoint_keeps_secret_out_of_exec_arguments(monkeypatch) -> None:
    module = _load_backup_entrypoint()
    secret = "migrator-secret-that-must-not-leak"
    monkeypatch.setattr(
        module.os,
        "environ",
        {
            "MIGRATOR_DATABASE_URL": (
                f"postgresql+asyncpg://jobhunter_migrator:{secret}@postgres:5432/job_agent"
            ),
            "PGPASSWORD": "stale-password",
        },
    )
    captured: dict[str, object] = {}

    def fake_execve(path: str, argv: list[str], environment: dict[str, str]) -> None:
        captured.update(path=path, argv=argv, environment=environment)
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(module.os, "execve", fake_execve)

    with pytest.raises(RuntimeError, match="exec intercepted"):
        module.main()

    assert captured["path"] == module.BACKUP_EXECUTABLE
    assert captured["argv"] == [module.BACKUP_EXECUTABLE]
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert environment["POSTGRES_HOST"] == "postgres"
    assert environment["POSTGRES_PORT"] == "5432"
    assert environment["POSTGRES_PASSWORD"] == secret
    assert "MIGRATOR_DATABASE_URL" not in environment
    assert "PGPASSWORD" not in environment
    assert secret not in " ".join(captured["argv"])


def test_backup_keeps_dev_postgres_fallback(tmp_path: Path) -> None:
    script, fake_bin, backup_dir, args_file, _ = _backup_test_fixture(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "POSTGRES_HOST": "postgres",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "dev_db",
        "POSTGRES_USER": "dev_user",
        "POSTGRES_PASSWORD": "dev-password",
        "BACKUP_DIR": str(backup_dir),
    }
    env.pop("MIGRATOR_DATABASE_URL", None)

    try:
        result = subprocess.run(  # noqa: S603 - test controls the temporary script path
            ["/bin/sh", str(script)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        args = args_file.read_text(encoding="utf-8")
        assert "--host=postgres" in args
        assert "--username=dev_user" in args
        assert "--dbname=dev_db" in args
        assert "dev-password" not in result.stdout + result.stderr
        assert f"Backup created: {backup_dir}/job-agent-dev_db-" in result.stdout
    finally:
        for dump_file in backup_dir.glob("*.dump"):
            dump_file.unlink(missing_ok=True)


def _load_restore_entrypoint() -> ModuleType:
    path = ROOT / "deploy/restore_entrypoint.py"
    assert path.exists(), "production restore URL parser is missing"
    spec = importlib.util.spec_from_file_location("restore_entrypoint", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_production_restore_uses_root_only_migrator_env() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))
    restore = compose["services"]["restore"]

    assert restore["env_file"] == [{"path": "/etc/jobhunter/migrator.env", "required": True}]
    assert restore["environment"]["POSTGRES_HOST"] == ""
    assert restore["environment"]["POSTGRES_PORT"] == ""
    assert restore["environment"]["POSTGRES_DB"] == ""
    assert restore["environment"]["POSTGRES_USER"] == ""
    assert restore["environment"]["POSTGRES_PASSWORD"] == ""
    assert restore["entrypoint"] == ["python3", "/usr/local/bin/job-agent-restore-entrypoint"]
    assert restore["image"].startswith("jobhunter-backup-prod:")


def test_restore_entrypoint_uses_migrator_role_without_leaking_secret(monkeypatch) -> None:
    module = _load_restore_entrypoint()
    secret = "restore-secret-that-must-not-leak"
    monkeypatch.setattr(
        module.os,
        "environ",
        {
            "MIGRATOR_DATABASE_URL": (
                f"postgresql+asyncpg://jobhunter_migrator:{secret}@postgres:5432/job_agent"
            ),
            "POSTGRES_USER": "job_agent",
            "POSTGRES_PASSWORD": "stale-bootstrap-secret",
        },
    )
    captured: dict[str, object] = {}

    def fake_execve(path: str, argv: list[str], environment: dict[str, str]) -> None:
        captured.update(path=path, argv=argv, environment=environment)
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(module.os, "execve", fake_execve)

    with pytest.raises(RuntimeError, match="exec intercepted"):
        module.main()

    assert captured["path"] == module.RESTORE_EXECUTABLE
    assert captured["argv"] == [module.RESTORE_EXECUTABLE]
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert environment["POSTGRES_USER"] == "jobhunter_migrator"
    assert environment["POSTGRES_PASSWORD"] == secret
    assert environment["POSTGRES_DB"] == "job_agent"
    assert "MIGRATOR_DATABASE_URL" not in environment
    assert secret not in " ".join(captured["argv"])


def test_restore_entrypoint_drops_database_url_from_child_environment() -> None:
    module = _load_restore_entrypoint()
    environment = module.build_restore_environment(
        {
            "DATABASE_URL": "postgresql+asyncpg://wrong:wrong@wrong/wrong",
            "MIGRATOR_DATABASE_URL": (
                "postgresql+asyncpg://jobhunter_migrator:secret@postgres:5432/job_agent"
            ),
        }
    )

    assert "DATABASE_URL" not in environment
    assert "MIGRATOR_DATABASE_URL" not in environment
    assert environment["POSTGRES_USER"] == "jobhunter_migrator"
