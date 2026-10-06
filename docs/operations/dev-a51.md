# Samsung A51 DEV

The target is the rooted `SM-A515F` on Tailscale `100.123.23.6`.
The wrapper discovers its connected ADB port (currently `:37685`, originally
`:39285`) and validates the model and root. To select a changed port explicitly,
set `JOBHUNTER_A51_ADB_SERIAL=100.123.23.6:PORT`; other phone IPs are rejected.
Run every command from the DEV checkout. The lifecycle commands never use the
workstation Docker daemon: its current default connection reaches PROD.

```sh
scripts/dev-a51.sh setup      # idempotent native runtime check / HTTP relay
scripts/dev-a51.sh deploy     # snapshot, on-phone build, tests, migrations, startup, acceptance
scripts/dev-a51.sh verify     # verify the deployed candidate again
scripts/dev-a51.sh status
scripts/dev-a51.sh logs
scripts/dev-a51.sh console    # HTTP relay diagnostic log
scripts/dev-a51.sh down       # stop DEV containers; preserve DEV volumes
```

Browser access after a successful deployment:

- ADB-forwarded: `http://127.0.0.1:18881`
- Tailscale: `http://100.123.23.6:8881`
- A51 LAN, when reachable: `http://192.168.200.48:8881` (rediscover after DHCP changes).

The generated DEV administrator password and API token are in the ignored,
permission-restricted `.dev-a51/credentials.json`. Do not commit or paste that file.
It contains new DEV credentials, never copied PROD credentials. The API key is used
by the acceptance command. Login through the ordinary administrator password form.

## Runtime and isolation

The operator installed kernel `4.14.113-22755563-docker` and native Docker
27.5.1 with overlay2, based on [a51-kvm](https://github.com/SeniorStackOverflow/a51-kvm).
JobHunter uses that existing daemon at
`unix:///data/local/tmp/codex-a51-docker/docker.sock`, always with an explicit
socket and CLI. There is no QEMU VM, guest SSH, TCG or dependency on full KVM guest
support. Kernel/firmware installation and SELinux changes remain outside DEV scope.

Compose v2.32.4 and Buildx v0.20.1 are official ARM64 releases, checksum pinned
and installed under the JobHunter-owned `/data/local/jobhunter-dev/docker-config`.
The CLI runs natively in a small, checksum-pinned Alpine chroot under
`/data/local/jobhunter-dev/cli-rootfs` to give Go tools normal DNS and CA paths.
It shares Android's kernel and only mounts the native socket/binaries and owned
configuration/releases; application services run in actual Docker containers.
The Compose project `jobhunter-dev-a51` gives application containers, networks and
volumes distinct names. DEV credentials, releases and logs live under
`/data/local/jobhunter-dev`; unrelated phone services and Docker workloads are preserved.

The installed daemon has a private network namespace and the existing IPv4 uplink
`10.231.43.2`. Docker does not publish ports from the internal application bridge.
A JobHunter-owned static `docker-proxy` in the daemon's network namespace forwards
only `10.231.43.2:8882` to the current DEV API container's HTTP port. Another owned
relay forwards Android port `8881` to it. Both relays use the installed binary,
without changing Android firewall rules. The own startup hook
`/data/adb/service.d/jobhunter-dev-a51.sh` checks the existing native daemon and
starts the relay. If no Docker daemon exists, it uses the owned corrected
launcher; it refuses to replace/restart an existing unready daemon.
The existing Docker binaries, ext4 storage and uplink come from a51-kvm. Its
original chroot-only launcher allowed `docker exec`/healthchecks to enter the
Android enclosing root instead of the container root. JobHunter's standalone
`deploy/a51-native-launcher.c` derives from upstream commit
`ac9f151387808a836b684972e3a3af323641110b` (GPL-2.0-only) and corrects the
private mount namespace root before starting the same native daemon. A native
ARM64 static build uses `aarch64-linux-gnu-gcc`; install that cross-compiler on
the DEV workstation before setup. The original a51-kvm launcher/scripts are
preserved. The correction was applied only after stopping the two JobHunter
containers and verifying there were no unrelated running Docker workloads.
Every setup now requires an actual read-only, network-disabled Alpine container
whose `docker exec` sees its own Linux root and PID1, and cannot see Android's
`/system`. A failure blocks deployment before application startup.

The initial Docker ext4 test image had only 256 MiB. On 2026-10-06 its existing
filesystem was expanded online to 12 GiB using the same backing file and loop
mount, preserving existing images and the running daemon. This is a sparse file
inside encrypted Android `/data`, consuming blocks as Docker writes them.
Deployment requires at least 2 GiB free in Docker storage and checks Android
space too. Do not recreate/format its backing file. A fresh installation needs
an adequately sized native Docker filesystem before building JobHunter.

Memory/swap and process limits bound each DEV service so it cannot grow without
limit into Android's RAM or zRAM. On this Android kernel, application UID 10001
cannot create IPv4 sockets even with the inet group or additional NET_RAW capability.
The DEV image therefore builds its ordinary unprivileged user with UID/GID 9999;
an actual container verified sockets and PostgreSQL DNS with default seccomp and
no additional groups/capabilities. Production builds retain UID/GID 10001.
Before migrations, an isolated network-disabled one-shot container reconciles
ownership of only the two DEV application storage volumes to the image's user.
The application network is internal. Real email,
phone actions, Telegram, cloud LLM and live source scans are disabled for acceptance. Built-in sources are
registered by startup/seed with their paused defaults; fixture jobs are the
only active source. The deployment never acknowledges source policy or enables
an existing source. No PROD database or resume data is imported.

Snapshots contain an allowlist of application/build/test files and a SHA256 of
their names and bytes. Credentials and workstation environment files are absent.
Release-specific Compose environments preserve the running release configuration
when a later build fails. DEV migrations run before new API startup.
An acceptance failure invalidates previous acceptance evidence. New changes
require a new deployment and new evidence.

## A14 primary proxy and local network

A51 (`192.168.200.48`) and A14 (`192.168.200.39`) share Wi-Fi and are physically
adjacent. Prefer a verified LAN path for an explicitly authorized live integration
test; physical proximity alone does not prove routing or listener accessibility.

Read-only diagnostics on 2026-10-05 found A51's ARP entry for A14 in `FAILED`
state and `Destination Host Unreachable` when pinging A14. The reverse ping also
failed. A14's existing SOCKS listener is `127.0.0.1:18080`, reachable through its
existing tunnel/relay, rather than directly through either phone IP. Thus
`socks5://192.168.200.39:18080` is not currently a working DEV primary endpoint.
Neither `:39345` nor `:39285` is a proxy port; those are ADB endpoints.

Do not expose the existing unauthenticated loopback proxy on all LAN interfaces
or replace its client allowlist as part of DEV setup. A direct LAN route needs
verified Wi-Fi peer connectivity and a separately authorized, tested listener or
relay allowing A51 alone. Keep the existing PROD tunnel operational and retain
its route as the fallback if LAN is unavailable. Proxy credentials are separate
from PhoneGate credentials; DEV never rotates either. Default fixture acceptance
does not contact A14 or require live proxy egress. Live proxy testing must be an
explicit, separate operation, with the adapter's usual domain/SSRF policy intact.

## Acceptance and PROD gate

The lifecycle runs selected pipeline/catalog/identity regression tests inside a
validation image before migrating. Actual deployed acceptance checks readiness,
the exact revision, running services, identical API/worker/Beat image IDs, worker
ping, a queued fixture scan, API/OpenAPI/MCP DEV identity, source visibility in
both panels, ownership denial and three consecutive clean administrator/user
browser iterations. Screenshots and JSON evidence stay under `.dev-a51`.
User sessions in this offline check are prepared in the DEV database; this is
not live Google OAuth validation. Any OAuth change additionally needs its own
complete provider/redirect/cookie scenario in three clean contexts.

Run `scripts/verify.sh` for the complete local browser-enabled project checks.
Before requesting PROD permission, review the exact candidate, all relevant
focused/full results, A51 acceptance and screenshots, and report the proposed
rollout/rollback. A failure or unavailable A51 closes the normal PROD gate and
must be reported immediately. DEV authorization never permits PROD mutations.

The native daemon deliberately retains build cache to speed up repeated builds.
Cleanup removes only unused images carrying JobHunter's exact `dev-a51` project
label. It never prunes shared build cache, unrelated images or named volumes.
Check `docker system df`, the native ext4 filesystem and Android `/data` after
acceptance and before growing builds. The old emulated-VM startup scripts are
removed from the checkout and are no longer used.
