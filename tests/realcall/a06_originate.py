from __future__ import annotations

import subprocess
from dataclasses import dataclass

import httpx


@dataclass
class A06Rig:
    ssh_host: str
    ssh_port: str
    ssh_user: str
    a14_serial: str  # e.g. "100.106.163.104:43369"; "" -> auto-detect
    a06_serial: str  # e.g. "100.100.224.9:38557"
    a06_number: str  # A06's number (for incoming calls)
    a14_number: str = ""  # A14's number (for A06 to dial)
    phonegate_url: str = ""
    phonegate_token: str = ""

    def _ssh(self, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["ssh", "-p", self.ssh_port, f"{self.ssh_user}@{self.ssh_host}", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def _adb(self, serial: str, cmd: str, timeout: int = 25) -> str:
        return self._ssh(f"adb -s {serial} shell '{cmd}'", timeout).stdout.strip()

    def check_preconditions(self) -> list[str]:
        reasons: list[str] = []
        try:
            devs = self._ssh("adb devices -l", timeout=15)
        except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
            return [f"ssh/adb unreachable: {exc}"]
        if devs.returncode != 0:
            reasons.append(f"adb devices failed: {devs.stderr.strip()[:200]}")
        if self.a14_serial and self.a14_serial not in devs.stdout:
            reasons.append(f"A14 {self.a14_serial} not in adb devices")
        if self.a06_serial and self.a06_serial not in devs.stdout:
            reasons.append(f"A06 {self.a06_serial} not in adb devices")
        if self.phonegate_url and self.phonegate_token:
            try:
                r = httpx.get(
                    f"{self.phonegate_url}/api/device/status",
                    headers={"Authorization": f"Bearer {self.phonegate_token}"},
                    timeout=10,
                )
                st = r.json()
                if not st.get("connected") or st.get("mode") != "Zero-ADB":
                    reasons.append(
                        f"PhoneGate not ready: connected={st.get('connected')} "
                        f"mode={st.get('mode')}"
                    )
            except (httpx.HTTPError, ValueError) as exc:
                reasons.append(f"PhoneGate status unreachable: {exc}")
        if not self.a14_number:
            reasons.append("a14_number not configured (A06 needs it to dial A14)")
        return reasons

    def dial(self, number: str) -> None:
        self._adb(self.a06_serial, f"am start -a android.intent.action.CALL -d tel:{number}")

    def hangup(self) -> None:
        self._adb(self.a06_serial, "input keyevent KEYCODE_ENDCALL")

    def inject_uplink_wav(self, remote_wav_path: str) -> None:
        # Uses the proven CallStreamer/ParamSetter primitives; the exact invocation
        # mirrors WORKING_DO_NOT_TOUCH_PROVEN/a14_call_inject.py adapted for A06.
        # IMPLEMENTER: fill in from that reference during the real-hardware bring-up.
        raise NotImplementedError("uplink injection — wire from the proven toolkit on the rig")

    def start_downlink_recording(self) -> str:
        # ReceiverRecorder src 3 (VOICE_DOWNLINK) on A06, mirroring a06_call_record.py.
        raise NotImplementedError("downlink recording — wire from the proven toolkit on the rig")

    def stop_downlink_recording(self) -> bytes:
        raise NotImplementedError("downlink recording — wire from the proven toolkit on the rig")
