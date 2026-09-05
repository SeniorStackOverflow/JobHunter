from __future__ import annotations

import math
import os
import shlex
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field

import httpx

# The proven, immutable toolkit this harness builds on (never edited, never
# copied locally — every reference below reads it in place on the rig).
_PROVEN_DIR = "/srv/phonegate/WORKING_DO_NOT_TOUCH_PROVEN"
# Edge-TTS + ffmpeg live in PhoneGate's own venv on the rig — reused here
# rather than adding a new JobHunter dependency for a harness that only ever
# runs manually, opt-in, on this rig.
_EDGE_TTS_BIN = "/srv/phonegate/venv/bin/edge-tts"

# ReceiverRecorder (see WORKING_DO_NOT_TOUCH_PROVEN/ReceiverRecorder.java) has
# no "stop now" signal — it runs for a fixed duration and exits. Start it for
# longer than any real scenario needs; stop_downlink_recording() kills it
# early and pulls whatever was captured so far.
_DOWNLINK_RECORD_BUDGET_S = 150


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
    _downlink_remote_pcm: str = field(default="", init=False, repr=False)

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

    def _run_or_raise(
        self, cmd: str, *, what: str, timeout: int = 30
    ) -> subprocess.CompletedProcess[str]:
        """Run a command on the rig over SSH; raise with stderr on failure.

        Unlike dial()/hangup() (fire-and-forget button presses a human could
        just retry), the injection/recording pipeline is several sequential
        steps where a silent failure partway through would leave the rig in
        a half-configured state (e.g. forwarding enabled with nothing to
        stream) — surface failures immediately instead.
        """
        result = self._ssh(cmd, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"{what} failed (exit {result.returncode}): {result.stderr.strip()}")
        return result

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

    def inject_uplink_speech(self, text: str, *, duration_seconds: int | None = None) -> None:
        """Synthesize ``text`` and inject it into A06's own cellular uplink —
        i.e. what A06 "says" on the call, reaching A14's downlink where
        PhoneGate/Groq ASR picks it up as an employer turn.

        Built on the proven CallStreamer + ParamSetter primitives
        (WORKING_DO_NOT_TOUCH_PROVEN), adapted from
        ``a14_call_inject.py``'s A14-side invocation to run on A06 instead —
        Phase 2a's dial direction (A06 -> A14) is the reverse of the proven
        scripts' own A14 -> A06 flow, so the roles of "injector" and
        "recorder" both move to A06, which originates the call.

        Synthesis (edge-tts + ffmpeg) and the ADB push both run on the rig
        over the existing SSH connection, not on whatever machine invokes
        this harness — the proven toolkit and its venv only exist there.
        """
        run_id = uuid.uuid4().hex
        remote_mp3 = f"/tmp/realcall_inject_{run_id}.mp3"
        remote_pcm = f"/tmp/realcall_inject_{run_id}.pcm"
        device_pcm = "/data/local/tmp/realcall_inject.pcm"

        self._run_or_raise(
            f"{_EDGE_TTS_BIN} --voice ru-RU-DmitryNeural --text {shlex.quote(text)} "
            f"--write-media {remote_mp3}",
            what="edge-tts synthesis",
            timeout=30,
        )
        self._run_or_raise(
            f"ffmpeg -y -i {remote_mp3} -ar 48000 -ac 2 -f s16le {remote_pcm}",
            what="ffmpeg PCM conversion",
            timeout=30,
        )

        if duration_seconds is None:
            size_result = self._run_or_raise(
                f"wc -c < {remote_pcm}", what="measuring injection PCM size", timeout=10
            )
            pcm_bytes = int(size_result.stdout.strip() or 0)
            # 48000 Hz, 16-bit, stereo = 4 bytes/sample-frame. CallStreamer
            # takes an integer-second duration and LOOPS the PCM back to the
            # start if that duration exceeds the actual audio length (see
            # "Seamless loop across full speech track" in CallStreamer.java)
            # -- so truncating (int()) undershoots and clips the last word,
            # while padding with a multi-second margin overshoots and makes
            # the phrase audibly repeat from the top. ceil() is the minimum
            # integer that can't undershoot; no extra margin on top of it.
            duration_seconds = max(2, math.ceil(pcm_bytes / (48000 * 4)))

        self._run_or_raise(
            f"adb -s {self.a06_serial} push {remote_pcm} {device_pcm}",
            what="pushing injection audio to A06",
            timeout=30,
        )
        self._run_or_raise(
            f"adb -s {self.a06_serial} push {_PROVEN_DIR}/param_setter.dex "
            f"/data/local/tmp/param_setter.dex",
            what="pushing param_setter.dex to A06",
            timeout=15,
        )
        self._run_or_raise(
            f"adb -s {self.a06_serial} push {_PROVEN_DIR}/call_streamer.dex "
            f"/data/local/tmp/call_streamer.dex",
            what="pushing call_streamer.dex to A06",
            timeout=15,
        )

        def _set_param(kv: str) -> None:
            self._adb(
                self.a06_serial,
                "/system/bin/app_process "
                "-Djava.class.path=/data/local/tmp/param_setter.dex /data/local/tmp "
                f"com.callbridge.param.ParamSetter '{kv}'",
                timeout=10,
            )

        try:
            _set_param("g_call_forwarding_enable=true")
            _set_param("incall_music=1")
            self._adb(
                self.a06_serial,
                "/system/bin/app_process "
                "-Djava.class.path=/data/local/tmp/call_streamer.dex /data/local/tmp "
                f"com.callbridge.streamer.CallStreamer {device_pcm} {duration_seconds} "
                "48000 2 MEDIA",
                timeout=duration_seconds + 15,
            )
        finally:
            # Always reset the hardware mixer flag, even if streaming failed —
            # leaving it enabled would bleed media audio into every call
            # after this one.
            _set_param("g_call_forwarding_enable=false")

        self._ssh(f"rm -f {remote_mp3} {remote_pcm}", timeout=10)

    def start_downlink_recording(self) -> str:
        """Launch ReceiverRecorder on A06 (VOICE_DOWNLINK, source 3) in the
        background — what A06 hears, i.e. JobHunter's own TTS output relayed
        through A14's cellular uplink. Mirrors
        ``a06_call_record.py``'s recorder invocation."""
        remote_pcm = "/data/local/tmp/realcall_downlink.pcm"
        self._downlink_remote_pcm = remote_pcm
        # Best-effort cleanup of a stale prior run's process/file.
        self._adb(self.a06_serial, "pkill -9 -f ReceiverRecorder; true", timeout=10)
        self._adb(self.a06_serial, f"rm -f {remote_pcm}", timeout=10)
        self._run_or_raise(
            f"adb -s {self.a06_serial} push {_PROVEN_DIR}/receiver_record.dex "
            f"/data/local/tmp/receiver_record.dex",
            what="pushing receiver_record.dex to A06",
            timeout=15,
        )
        self._ssh(
            f"adb -s {self.a06_serial} shell 'nohup /system/bin/app_process "
            f"-Djava.class.path=/data/local/tmp/receiver_record.dex /data/local/tmp "
            f"ReceiverRecorder {_DOWNLINK_RECORD_BUDGET_S} {remote_pcm} 3 "
            f"</dev/null >/data/local/tmp/realcall_downlink.log 2>&1 &'",
            timeout=10,
        )
        return remote_pcm

    def stop_downlink_recording(self) -> bytes:
        """Stop the recorder started by ``start_downlink_recording`` and
        return the captured PCM (16-bit mono, 16000 Hz — see
        ``ReceiverRecorder.java``) as raw bytes."""
        if not self._downlink_remote_pcm:
            raise RuntimeError("stop_downlink_recording() called without a prior start")
        remote_pcm = self._downlink_remote_pcm
        self._adb(self.a06_serial, "pkill -9 -f ReceiverRecorder; true", timeout=10)
        time.sleep(1.0)  # let the last write flush before pulling the file

        remote_tmp = f"/tmp/realcall_downlink_{uuid.uuid4().hex}.pcm"
        self._run_or_raise(
            f"adb -s {self.a06_serial} pull {remote_pcm} {remote_tmp}",
            what="pulling downlink recording from A06",
            timeout=30,
        )

        fd, local_path = tempfile.mkstemp(suffix=".pcm")
        os.close(fd)
        try:
            scp = subprocess.run(
                [
                    "scp",
                    "-P",
                    self.ssh_port,
                    f"{self.ssh_user}@{self.ssh_host}:{remote_tmp}",
                    local_path,
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if scp.returncode != 0:
                raise RuntimeError(
                    f"scp of downlink recording failed: {scp.stderr.decode(errors='replace')}"
                )
            with open(local_path, "rb") as f:
                return f.read()
        finally:
            os.unlink(local_path)
            self._ssh(f"rm -f {remote_tmp}", timeout=10)
