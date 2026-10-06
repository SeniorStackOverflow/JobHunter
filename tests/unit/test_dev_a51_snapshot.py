"""Build snapshot security/provenance, without Docker or either real phone."""

from __future__ import annotations

import io
import subprocess
import tarfile

import pytest

from deploy import dev_a51


@pytest.fixture
def snapshot_tree(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    state = root / ".dev-a51"
    state.mkdir()
    for directory in ("app", "fixture_site", "config", "migrations", "tests", "deploy", "scripts"):
        (root / directory).mkdir()
    for name in (
        "Dockerfile",
        ".dockerignore",
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "alembic.ini",
        "docker-compose.dev-a51.yml",
        "deploy/container-entrypoint.sh",
        "deploy/dev_a51.py",
        "deploy/dev-a51-native.sh",
        "deploy/a51-native-launcher.c",
        "scripts/verify_dev_a51.py",
    ):
        (root / name).write_text("build input\n")
    (root / "app/main.py").write_text("print('DEV')\n")
    (root / ".env").write_text("SECRET=must-not-ship\n")
    (state / "credentials.json").write_text('{"password": "must-not-ship"}')
    (root / "app/ignored.json").write_text('{"secret": "must-not-ship"}')
    (root / "app/outside.py").symlink_to(root / ".env")
    monkeypatch.setattr(dev_a51, "ROOT", root)
    monkeypatch.setattr(dev_a51, "STATE", state)

    def fake_run(args, **kwargs):
        if args[1] == "ls-files":
            files = [
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_file() and path.name != "ignored.json"
            ]
            return subprocess.CompletedProcess(args, 0, "\0".join(files).encode(), b"")
        assert args == ["git", "rev-parse", "HEAD"]
        return subprocess.CompletedProcess(args, 0, b"a" * 40 + b"\n", b"")

    monkeypatch.setattr(dev_a51, "run", fake_run)
    return root


def test_snapshot_excludes_credentials_ignored_files_and_symlinks(snapshot_tree):
    manifest, archive = dev_a51.snapshot()
    with tarfile.open(fileobj=io.BytesIO(archive.read_bytes()), mode="r:gz") as tar:
        names = tar.getnames()
        assert "app/main.py" in names
        assert "deploy/container-entrypoint.sh" in names
        assert ".env" not in names
        assert ".dev-a51/credentials.json" not in names
        assert "app/ignored.json" not in names
        assert "app/outside.py" not in names
        assert all(b"must-not-ship" not in tar.extractfile(name).read() for name in names)
    assert manifest["files"] == len(names)


def test_revision_tracks_exact_source_bytes_and_repeats_idempotently(snapshot_tree):
    first, _ = dev_a51.snapshot()
    second, _ = dev_a51.snapshot()
    assert first == second
    (snapshot_tree / "app/main.py").write_text("print('new DEV behavior')\n")
    changed, _ = dev_a51.snapshot()
    assert changed["commit"] == first["commit"]
    assert changed["revision"] != first["revision"]
    assert changed["source_sha256"] != first["source_sha256"]


def test_verification_preserves_the_deployed_manifest_if_source_changed(snapshot_tree):
    first, archive = dev_a51.snapshot()
    previous_archive = archive.read_bytes()
    manifest_path = snapshot_tree / ".dev-a51/candidate.json"
    previous_manifest = manifest_path.read_bytes()
    (snapshot_tree / "app/main.py").write_text("print('unverified change')\n")
    changed, _ = dev_a51.snapshot(write=False)
    assert changed["revision"] != first["revision"]
    assert archive.read_bytes() == previous_archive
    assert manifest_path.read_bytes() == previous_manifest


def test_unignored_generated_credentials_fail_before_upload(snapshot_tree):
    (snapshot_tree / "app/client_secret.json").write_text('{"secret": "do-not-upload"}')
    with pytest.raises(RuntimeError, match="Sensitive configuration"):
        dev_a51.snapshot()
    assert not (snapshot_tree / ".dev-a51/source.tar.gz").exists()


@pytest.mark.parametrize("model,valid", [("SM-A515F", True), ("SM-A145F", False)])
def test_adb_forwarding_requires_the_actual_a51_model(monkeypatch, model, valid):
    calls = []

    def fake_adb(*args, **kwargs):
        calls.append(args)
        stdout = model.encode() + b"\r\n" if args[0] == "shell" else b""
        return subprocess.CompletedProcess(args, 0, stdout, b"")

    monkeypatch.setattr(dev_a51, "adb", fake_adb)
    monkeypatch.setattr(dev_a51, "resolve_serial", lambda: "100.123.23.6:37685")
    monkeypatch.setattr(
        dev_a51, "android", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, b"0\n")
    )
    if valid:
        dev_a51.connect()
        assert sum(args[0] == "forward" for args in calls) == 1
    else:
        with pytest.raises(RuntimeError, match="Expected Samsung A51"):
            dev_a51.connect()
        assert all(args[0] != "forward" for args in calls)


def test_adb_discovers_changed_port_only_on_dedicated_phone(monkeypatch):
    monkeypatch.delenv("JOBHUNTER_A51_ADB_SERIAL", raising=False)
    output = (
        b"List of devices attached\n100.106.163.104:39345\tdevice\n100.123.23.6:37685\tdevice\n"
    )
    monkeypatch.setattr(
        dev_a51, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, output, b"")
    )
    assert dev_a51.resolve_serial() == "100.123.23.6:37685"


def test_adb_override_cannot_select_another_phone(monkeypatch):
    monkeypatch.setenv("JOBHUNTER_A51_ADB_SERIAL", "100.106.163.104:39345")
    with pytest.raises(RuntimeError, match="dedicated A51"):
        dev_a51.resolve_serial()


def test_native_docker_pins_socket_and_preserves_binary_stdin(monkeypatch):
    calls = []
    monkeypatch.setattr(dev_a51, "adb", lambda *a, **k: calls.append((a, k)))
    dev_a51.remote("docker info", data=b"archive\x00bytes", capture=True)
    args, kwargs = calls[0]
    assert args[:2] == ("shell", "-T")
    assert "--host unix://" + dev_a51.DOCKER_SOCKET in args[2]
    assert "unset DOCKER_CONTEXT" in args[2]
    assert "chroot " + dev_a51.CLI_ROOT in args[2]
    assert kwargs["data"] == b"archive\x00bytes"


def test_cleanup_keeps_shared_daemon_cache_and_other_applications(monkeypatch):
    commands = []
    monkeypatch.setattr(dev_a51, "remote", commands.append)
    monkeypatch.setattr(dev_a51, "android", lambda command, **kwargs: commands.append(command))
    dev_a51.cleanup()
    command = commands[0]
    assert "--filter label=org.opencontainers.image.jobhunter-project=dev-a51" in command
    assert "builder prune" not in command
    assert "volume prune" not in command
    assert "system prune" not in command


@pytest.mark.parametrize("isolated", [True, False])
def test_native_exec_gate_fails_closed_and_removes_its_probe(monkeypatch, isolated):
    commands = []

    def fake_remote(command, **kwargs):
        commands.append(command)
        failed = command.startswith("docker exec") and not isolated
        return subprocess.CompletedProcess(command, int(failed), b"", b"")

    monkeypatch.setattr(dev_a51, "remote", fake_remote)
    if isolated:
        dev_a51.verify_native_exec()
    else:
        with pytest.raises(RuntimeError, match="root/PID namespace"):
            dev_a51.verify_native_exec()
    assert "--network none --read-only" in commands[0]
    assert "test ! -e /system" in commands[1]
    assert commands[-1].startswith("docker rm -f jobhunter-dev-a51-runtime-probe-")
