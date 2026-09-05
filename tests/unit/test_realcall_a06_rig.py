from __future__ import annotations

import subprocess

import pytest

from tests.realcall.a06_originate import A06Rig


def _rig() -> A06Rig:
    return A06Rig(
        ssh_host="example.invalid",
        ssh_port="22",
        ssh_user="tester",
        a14_serial="a14",
        a06_serial="a06",
        a06_number="+37300000001",
        a14_number="+37300000002",
    )


def test_injection_cleans_vps_temp_files_after_pipeline_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _rig()
    cleanup_commands: list[str] = []

    def fake_run_or_raise(
        cmd: str, *, what: str, timeout: int = 30
    ) -> subprocess.CompletedProcess[str]:
        del cmd, timeout
        if what == "ffmpeg PCM conversion":
            raise RuntimeError("ffmpeg failed")
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def fake_ssh(cmd: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        del timeout
        cleanup_commands.append(cmd)
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(rig, "_run_or_raise", fake_run_or_raise)
    monkeypatch.setattr(rig, "_ssh", fake_ssh)

    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        rig.inject_uplink_speech("Тест")

    assert len(cleanup_commands) == 1
    cleanup = cleanup_commands[0]
    assert cleanup.startswith("rm -f /tmp/realcall_inject_")
    assert ".mp3 /tmp/realcall_inject_" in cleanup
    assert cleanup.endswith(".pcm")


def test_injection_cleanup_timeout_does_not_mask_original_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _rig()

    def fake_run_or_raise(
        cmd: str, *, what: str, timeout: int = 30
    ) -> subprocess.CompletedProcess[str]:
        del cmd, timeout
        if what == "ffmpeg PCM conversion":
            raise RuntimeError("original pipeline failure")
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def cleanup_timeout(cmd: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(rig, "_run_or_raise", fake_run_or_raise)
    monkeypatch.setattr(rig, "_ssh", cleanup_timeout)

    with pytest.raises(RuntimeError, match="original pipeline failure"):
        rig.inject_uplink_speech("Тест")
