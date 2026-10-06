"""A51-only DEV lifecycle. Never uses the workstation's default Docker daemon."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".dev-a51"
PHONE_IP = "100.123.23.6"
SERIAL = PHONE_IP + ":39285"
REMOTE = "/data/local/jobhunter-dev"
RELEASES = "/opt/jobhunter-dev"
CLI_ROOT = REMOTE + "/cli-rootfs"
WEB_PORT = 18881
DOCKER_BIN = "/data/local/tmp/codex-a51-docker-bin/docker"
DOCKER_SOCKET = "/data/local/tmp/codex-a51-docker/docker.sock"
DOCKER_DATA = "/data/local/tmp/codex-a51-docker/data"
PLUGINS = {
    "docker-compose": (
        "https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-aarch64",
        "0c4591cf3b1ed039adcd803dbbeddf757375fc08c11245b0154135f838495a2f",
    ),
    "docker-buildx": (
        "https://github.com/docker/buildx/releases/download/v0.20.1/buildx-v0.20.1.linux-arm64",
        "f7d867e9f1a3c00b32dd580f56594e229df05e3fb1b083b7099c91c2e7d2ce1e",
    ),
}
MINIROOT = "https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/aarch64/alpine-minirootfs-3.22.6-aarch64.tar.gz"
MINIROOT_SHA256 = "821565fa8f3953eefd12497b166b4b50add2f7c57fb312e75862f5867e06fefe"


def run(args, *, data=None, capture=False, check=True, timeout=None):
    return subprocess.run(  # noqa: S603 - argv is assembled by this A51-only lifecycle tool
        args,
        cwd=ROOT,
        input=data,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=check,
        timeout=timeout,
    )


def adb(*args, **kwargs):
    return run(["adb", "-s", SERIAL, *args], **kwargs)


def android(script, **kwargs):
    # ADB sends one shell command; quote the complete su argument, not separate argv.
    return adb("shell", "su -c " + shlex.quote(script), **kwargs)


def initialize_state():
    STATE.mkdir(mode=0o700, exist_ok=True)
    STATE.chmod(0o700)
    run(["git", "check-ignore", "--quiet", ".dev-a51/id_ed25519"])


def resolve_serial():
    override = os.environ.get("JOBHUNTER_A51_ADB_SERIAL")
    if override:
        if not re.fullmatch(re.escape(PHONE_IP) + r":[0-9]{1,5}", override):
            raise RuntimeError("ADB override must use the dedicated A51 Tailscale IP")
        return override
    output = run(["adb", "devices"], capture=True).stdout.decode()
    candidates = [
        line.split()[0]
        for line in output.splitlines()
        if line.startswith(PHONE_IP + ":") and line.split()[1:] == ["device"]
    ]
    if len(candidates) > 1:
        raise RuntimeError("Multiple A51 ADB endpoints; set JOBHUNTER_A51_ADB_SERIAL explicitly")
    return candidates[0] if candidates else SERIAL


def connect():
    global SERIAL
    SERIAL = resolve_serial()
    adb("connect", SERIAL, capture=True, check=False)
    model = adb("shell", "getprop ro.product.model", capture=True).stdout.decode().strip()
    if model != "SM-A515F":
        raise RuntimeError(f"Expected Samsung A51 SM-A515F at {SERIAL}, got {model!r}")
    if android("id -u", capture=True).stdout.decode().strip() != "0":
        raise RuntimeError("A51 root access is unavailable")
    adb("forward", f"tcp:{WEB_PORT}", "tcp:8881", capture=True)


def remote(script, **kwargs):
    # Always pin the socket and CLI on the verified phone, regardless of host Docker settings.
    prefix = (
        "set -eu; unset DOCKER_CONTEXT; "
        f"export DOCKER_HOST=unix://{DOCKER_SOCKET} DOCKER_CONFIG={REMOTE}/docker-config; "
        "export PATH=/data/local/tmp/codex-a51-docker-bin:/usr/sbin:/usr/bin:/sbin:/bin; "
        f'docker() {{ {DOCKER_BIN} --host unix://{DOCKER_SOCKET} "$@"; }}; '
    )
    # Shell protocol without a PTY transports binary stdin and its EOF correctly.
    command = f"chroot {CLI_ROOT} /bin/sh -c " + shlex.quote(prefix + script)
    return adb("shell", "-T", "su -c " + shlex.quote(command), **kwargs)


def push_root(local: Path, destination: str):
    staging = "/data/local/tmp/jobhunter-dev-" + secrets.token_hex(6)
    adb("push", str(local), staging, capture=True)
    android(
        f"mkdir -p {shlex.quote(str(Path(destination).parent))}; "
        f"mv {staging} {shlex.quote(destination)}"
    )


def setup():
    initialize_state()
    connect()
    # Native Docker and its kernel/network launcher were installed by the operator.
    # Install only JobHunter-owned CLI plugins and HTTP forwarding.
    android(f"mkdir -p {REMOTE}/docker-config/cli-plugins {REMOTE}/releases; chmod 700 {REMOTE}")
    if android(f"chroot {CLI_ROOT} /bin/sh -c true", capture=True, check=False).returncode:
        archive = STATE / "minirootfs.tar.gz"
        with urllib.request.urlopen(MINIROOT, timeout=120) as response:
            archive.write_bytes(response.read())
        if hashlib.sha256(archive.read_bytes()).hexdigest() != MINIROOT_SHA256:
            raise RuntimeError("Native CLI rootfs checksum mismatch")
        push_root(archive, REMOTE + "/minirootfs.tar.gz")
        android(
            f"mkdir -p {CLI_ROOT}; /data/adb/magisk/busybox tar "
            f"-xzf {REMOTE}/minirootfs.tar.gz -C {CLI_ROOT}"
        )
    for name, (url, checksum) in PLUGINS.items():
        target = f"{REMOTE}/docker-config/cli-plugins/{name}"
        existing = android(f"sha256sum {shlex.quote(target)}", capture=True, check=False)
        if existing.returncode == 0 and existing.stdout.decode().split()[0] == checksum:
            continue
        downloaded = STATE / name
        if not url.startswith("https://github.com/"):
            raise RuntimeError("Docker plugins must come from the pinned official HTTPS release")
        with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - pinned HTTPS above
            downloaded.write_bytes(response.read())
        if hashlib.sha256(downloaded.read_bytes()).hexdigest() != checksum:
            raise RuntimeError(f"Official Docker plugin checksum mismatch: {name}")
        push_root(downloaded, target)
        android(f"chmod 700 {target}")
    launcher = STATE / "native-launcher"
    run(
        [
            "aarch64-linux-gnu-gcc",
            "-O2",
            "-static",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-o",
            str(launcher),
            str(ROOT / "deploy/a51-native-launcher.c"),
        ],
        capture=True,
    )
    push_root(launcher, REMOTE + "/bin/native-launcher")
    android(f"chmod 700 {REMOTE}/bin/native-launcher")
    push_root(ROOT / "deploy/dev-a51-native.sh", REMOTE + "/native-start.sh")
    android(f"chmod 700 {REMOTE}/native-start.sh; /system/bin/sh {REMOTE}/native-start.sh")
    info = json.loads(remote("docker info --format '{{json .}}'", capture=True).stdout)
    if info["Architecture"] != "aarch64" or info["Driver"] != "overlay2":
        raise RuntimeError("Expected native ARM64 Docker with overlay2")
    if info["DockerRootDir"] != DOCKER_DATA:
        raise RuntimeError("Unexpected A51 Docker storage; refuse to deploy")
    verify_native_exec()
    remote("docker compose version; docker buildx version")
    boot_hook = STATE / "boot.sh"
    boot_hook.write_text(f"#!/system/bin/sh\nsleep 30\n/system/bin/sh {REMOTE}/native-start.sh\n")
    push_root(boot_hook, "/data/adb/service.d/jobhunter-dev-a51.sh")
    android("chmod 700 /data/adb/service.d/jobhunter-dev-a51.sh")
    print("A51 native Docker ready:", info["ServerVersion"], flush=True)


def verify_native_exec():
    name = "jobhunter-dev-a51-runtime-probe-" + secrets.token_hex(6)
    remote(
        f"docker run -d --name {name} --network none --read-only "
        "--memory 32m --memory-swap 32m --pids-limit 32 "
        "--label org.opencontainers.image.jobhunter-project=dev-a51 "
        "alpine:3.22 /bin/sleep 120",
        capture=True,
    )
    try:
        result = remote(
            f"docker exec {name} /bin/sh -c "
            + shlex.quote(
                "test -s /etc/alpine-release && test ! -e /system && "
                "test -e /proc/1/exe && /bin/cat /etc/alpine-release"
            ),
            check=False,
            capture=True,
            timeout=20,
        )
        if result.returncode:
            raise RuntimeError(
                "Native Docker exec does not preserve the container root/PID namespace; "
                "refuse deployment. Check the a51-kvm launcher namespace root boundary."
            )
    finally:
        remote(f"docker rm -f {name}", capture=True)


def credentials():
    from app.security.auth import hash_api_key, hash_password

    path = STATE / "credentials.json"
    if path.exists():
        return json.loads(path.read_text())
    result = {
        "admin_username": "admin",
        "admin_password": secrets.token_urlsafe(24),
        "api_token": secrets.token_urlsafe(32),
        "database_password": secrets.token_urlsafe(24),
        "secret_key": secrets.token_urlsafe(48),
    }
    result["admin_password_hash"] = hash_password(result["admin_password"])
    result["api_token_hash"] = hash_api_key(result["api_token"])
    path.write_text(json.dumps(result, indent=2) + "\n")
    path.chmod(0o600)
    return result


def snapshot(*, write=True):
    listed = (
        run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], capture=True)
        .stdout.decode()
        .split("\0")
    )
    visible = set(listed)
    paths = []
    for directory in ("app", "fixture_site", "config", "migrations", "tests"):
        for path in (ROOT / directory).rglob("*"):
            if (
                path.is_file()
                and not path.is_symlink()
                and path.relative_to(ROOT).as_posix() in visible
                and "__pycache__" not in path.parts
                and path.suffix
                in {".py", ".html", ".css", ".js", ".json", ".svg", ".yaml", ".mako"}
            ):
                if path.suffix in {".json", ".yaml"} and any(
                    marker in path.name.casefold()
                    for marker in ("credential", "client_secret", "private_key", "admin-password")
                ):
                    raise RuntimeError(f"Sensitive configuration is not a build input: {path.name}")
                paths.append(path)
    paths.extend(
        ROOT / name
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
        )
    )
    digest = hashlib.sha256()
    contents = []
    for path in sorted(paths):
        name = path.relative_to(ROOT).as_posix()
        data = path.read_bytes()
        digest.update(name.encode() + b"\0" + data + b"\0")
        contents.append((name, data, path.stat().st_mode & 0o777))
    commit = run(["git", "rev-parse", "HEAD"], capture=True).stdout.decode().strip()
    revision = commit[:12] + "-" + digest.hexdigest()[:12]
    archive = STATE / "source.tar.gz"
    if write:
        with tarfile.open(archive, "w:gz") as tar:
            for name, data, mode in contents:
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), mode
                tar.addfile(info, io.BytesIO(data))
    manifest = {
        "revision": revision,
        "source_sha256": digest.hexdigest(),
        "commit": commit,
        "files": len(contents),
    }
    if write:
        (STATE / "candidate.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest, archive


def compose(script, *, revision=None, **kwargs):
    directory = f"{RELEASES}/releases/{revision}" if revision else f"{RELEASES}/current"
    command = f"cd {directory}; docker compose --env-file .env -f docker-compose.dev-a51.yml "
    return remote("set -eu; " + command + script, **kwargs)


def deploy():
    (STATE / "acceptance.json").unlink(missing_ok=True)
    (STATE / "browser-evidence.json").unlink(missing_ok=True)
    setup()
    manifest, archive = snapshot()
    revision = manifest["revision"]
    creds = credentials()
    env = {
        "JOBHUNTER_DEV_REVISION": revision,
        "DEV_SECRET_KEY": creds["secret_key"],
        "DEV_DATABASE_PASSWORD": creds["database_password"],
        "DEV_ADMIN_PASSWORD_HASH": creds["admin_password_hash"],
        "DEV_MCP_API_KEYS_HASHED": json.dumps([creds["api_token_hash"]]),
    }
    # Single quotes preserve literal dollar signs in Argon2 hashes for Compose interpolation.
    payload = "".join(f"{key}='{value}'\n" for key, value in env.items()).encode()
    remote(
        f"mkdir -p {RELEASES}/releases/{revision}; tar -xz -C {RELEASES}/releases/{revision}",
        data=archive.read_bytes(),
    )
    remote(f"umask 077; cat > {RELEASES}/releases/{revision}/.env", data=payload)
    android(
        "pid=$(cat /data/local/tmp/codex-a51-docker/dockerd.pid); "
        f"available=$(nsenter -t \"$pid\" -m -- df -Pk {DOCKER_DATA} | awk 'NR==2 {{print $4}}'); "
        'test "$available" -ge 2097152 || '
        "{ echo 'A51 native Docker has less than 2 GiB free before build' >&2; exit 1; }; "
        "df -h /data"
    )
    remote("docker system df")
    print("Building ARM64 DEV candidate on A51:", revision, flush=True)
    compose("build api", revision=revision)
    compose("--profile validation build validation", revision=revision)
    compose("up -d redis", revision=revision)
    compose("--profile validation run --rm validation", revision=revision)
    # Migrations run before replacing application processes; failed builds keep the old app running.
    compose("up -d postgres redis", revision=revision)
    compose("run --rm --no-deps init-storage", revision=revision)
    compose("run --rm migrate", revision=revision)
    compose("run --rm --no-deps api job-agent seed --include-fixture", revision=revision)
    compose("up -d --no-build api worker beat fixture-site", revision=revision)
    android(f"/system/bin/sh {REMOTE}/native-start.sh")
    remote(f"ln -sfn {RELEASES}/releases/{revision} {RELEASES}/current")
    print("DEV deployed. Browser:", f"http://127.0.0.1:{WEB_PORT}", flush=True)
    verify()


def cleanup():
    # This native daemon can be shared: remove only unused JobHunter DEV application images.
    # Deliberately retain its build cache; do not globally prune another application's cache.
    remote(
        "docker image prune --all --force "
        "--filter label=org.opencontainers.image.jobhunter-project=dev-a51; "
        "docker system df"
    )
    android(
        "df -h /data; "
        "pid=$(cat /data/local/tmp/codex-a51-docker/dockerd.pid); "
        f'nsenter -t "$pid" -m -- df -h {DOCKER_DATA}'
    )


def wait_ready(revision):
    url = f"http://127.0.0.1:{WEB_PORT}/ready"
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=15) as response:
                body = json.load(response)
            if (
                body.get("deployment", {}).get("revision") == revision
                and body.get("status") == "ready"
            ):
                return
        except (OSError, ValueError):
            pass
        time.sleep(10)
    raise RuntimeError("A51 DEV did not become ready with the expected revision")


def verify():
    (STATE / "acceptance.json").unlink(missing_ok=True)
    initialize_state()
    connect()
    candidate = json.loads((STATE / "candidate.json").read_text())
    revision = candidate["revision"]
    # Source snapshot must still match the tree the operator is reviewing.
    current, _ = snapshot(write=False)
    if current != candidate:
        raise RuntimeError("Working tree changed after deployment; deploy the current candidate")
    wait_ready(revision)
    output = compose("ps --format json", capture=True).stdout.decode()
    try:
        services = json.loads(output)
    except json.JSONDecodeError:
        services = [json.loads(line) for line in output.splitlines() if line.strip()]
    if isinstance(services, dict):
        services = [services]
    required = {"api", "worker", "beat", "postgres", "redis", "fixture-site"}
    running = {service["Service"] for service in services if service["State"] == "running"}
    if not required <= running:
        raise RuntimeError(f"DEV services are missing: {sorted(required - running)}")
    unhealthy = [
        item["Service"]
        for item in services
        if item["Service"] in {"api", "postgres", "redis"} and item.get("Health") != "healthy"
    ]
    if unhealthy:
        raise RuntimeError(f"DEV services are not healthy: {unhealthy}")
    image_ids = (
        remote(
            "docker inspect --format '{{.Image}}' "
            "jobhunter-dev-a51-api-1 jobhunter-dev-a51-worker-1 jobhunter-dev-a51-beat-1",
            capture=True,
        )
        .stdout.decode()
        .splitlines()
    )
    if len(image_ids) != 3 or len(set(image_ids)) != 1:
        raise RuntimeError("DEV api/worker/beat image digests do not agree")
    image_revision = (
        remote(
            "docker image inspect "
            + shlex.quote(image_ids[0])
            + " --format '{{index .Config.Labels \"org.opencontainers.image.revision\"}}'",
            capture=True,
        )
        .stdout.decode()
        .strip()
    )
    if image_revision != revision:
        raise RuntimeError("DEV application image contains a different revision")
    ping = compose(
        "exec -T worker celery -A app.scheduler.celery_app:celery_app inspect ping --timeout=20",
        capture=True,
    )
    if "pong" not in ping.stdout.decode():
        raise RuntimeError("Actual DEV worker did not answer Celery ping")
    run([sys.executable, "scripts/verify_dev_a51.py"])
    evidence = {
        **candidate,
        "image_id": image_ids[0],
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "browser_contexts": 3,
        "services": sorted(running),
        "target": SERIAL,
        "runtime": "native-docker",
    }
    (STATE / "acceptance.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print("DEV acceptance saved:", STATE / "acceptance.json", flush=True)
    cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["setup", "deploy", "verify", "status", "logs", "console", "down"]
    )
    args = parser.parse_args()
    initialize_state()
    try:
        if args.command == "setup":
            setup()
        elif args.command == "deploy":
            deploy()
        elif args.command == "verify":
            verify()
        else:
            connect()
            if args.command == "console":
                android(f"tail -50 {REMOTE}/relay.log")
            elif args.command == "status":
                compose("ps")
            elif args.command == "logs":
                compose("logs --tail=100 api worker beat")
            else:
                compose("down")  # Deliberately preserves every DEV named volume and database.
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        # Do not echo command arguments or credentials from CalledProcessError.
        reason = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__
        print(
            f"A51 DEV deployment/verification unavailable: {reason}. "
            "Notify the operator; PROD gate remains closed.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
