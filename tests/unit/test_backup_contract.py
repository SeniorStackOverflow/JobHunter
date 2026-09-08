from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def _backup_test_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    backup_dir = tmp_path / "backups"
    script = tmp_path / "backup.sh"
    script.write_text(
        (ROOT / "deploy/backup.sh").read_text(encoding="utf-8").replace(
            "/backups", str(backup_dir)
        ),
        encoding="utf-8",
    )
    args_file = tmp_path / "pg_dump.args"
    env_file = tmp_path / "pg_dump.env"
    (fake_bin / "pg_dump").write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {args_file}\n"
        f"printf 'PGDATABASE=%s\\nPGPASSWORD=%s\\n' \"$PGDATABASE\" \"$PGPASSWORD\" > {env_file}\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in --file=*) file=${arg#--file=} ;; esac\n"
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

    assert backup["env_file"] == [
        {"path": "/etc/jobhunter/migrator.env", "required": True}
    ]
    assert backup["environment"] == {
        "POSTGRES_HOST": "",
        "POSTGRES_PORT": "",
        "POSTGRES_DB": "",
        "POSTGRES_USER": "",
        "POSTGRES_PASSWORD": "",
        "BACKUP_DIR": "/backups",
    }


def test_backup_uses_migrator_url_without_printing_secret(tmp_path: Path) -> None:
    script, fake_bin, backup_dir, args_file, env_file = _backup_test_fixture(tmp_path)

    secret = "migrator-secret-that-must-not-leak"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "MIGRATOR_DATABASE_URL": (
            f"postgresql+asyncpg://jobhunter_migrator:{secret}@postgres:5432/job_agent"
        ),
        "PGPASSWORD": "stale-password",
        "BACKUP_DIR": str(backup_dir),
    }
    result = subprocess.run(  # noqa: S603 - test controls the temporary script path
        ["/bin/sh", str(script)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout
    assert secret not in result.stderr
    args = args_file.read_text(encoding="utf-8")
    assert secret not in args
    captured_env = env_file.read_text(encoding="utf-8")
    assert (
        f"PGDATABASE=postgresql://jobhunter_migrator:{secret}@postgres:5432/job_agent"
        in captured_env
    )
    assert "PGPASSWORD=" in captured_env
    assert f"Backup created: {backup_dir}/job-agent-job_agent-" in result.stdout


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
