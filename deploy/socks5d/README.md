# JobHunter A14 SOCKS proxy

The phone daemon forwards HTTP (80) and HTTPS (443) for arbitrary public domains.
It no longer contains an adapter-specific Rabota allowlist. The client allowlist,
loopback listener, bounded connections/timeouts and resolved public IP checks remain.
JobHunter independently applies each adapter's URL/domain and redirect policy.
Internal/private destinations, including Tailscale and metadata IPs, remain blocked.

The existing reverse Chisel tunnel and host relay do not change. PhoneGate is an
independent service: never change its credentials or restart it during this rollout.
`100.106.163.104:39345` is an ADB connection, not a SOCKS endpoint.

Validate locally before any production mutation:

```sh
cd deploy/socks5d
GOMAXPROCS=1 go test -p 1 ./...
GOMAXPROCS=1 go vet ./...
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 GOMAXPROCS=1 go build -p 1 -trimpath -ldflags='-s -w' -o /tmp/jobhunter-socks5d-v5 .
```

After JobHunter project checks and three clean local browser contexts pass, report
evidence and rollout/rollback plan and obtain deployment authorization as required
by AGENTS.md. Record the running daemon version and SHA256. Keep one previous binary
in `/data/adb/jobhunter-proxy/` for a defined rollback window, push the new binary to
a staging file, verify its checksum/version, atomically replace `socks5d`, and restart
only that daemon (the existing supervisor restarts it). Do not restart the tunnel,
PhoneGate, or the phone. Verify both sites and a third public domain through the
existing configured primary SOCKS endpoint; confirm private IPs remain denied.
On failure atomically restore the previous binary and restart only `socks5d`.

The generic HTTP crawler uses `CRAWLER_PROXY_PRIMARY_URL` when configured. For
existing installations it also accepts `RABOTA_PROXY_PRIMARY_URL` when
`RABOTA_PROXY_POOL_ENABLED=true`; no environment migration is required. Explicit
source transports/proxies and injected offline fetchers keep their own routing.
Proxy credentials remain SecretStr values and must never appear in logs or fixtures.
The shared client does not silently fall back to a direct connection when the primary
proxy fails. Rabota keeps its existing WAF-aware proxy pool and fallback behavior.
