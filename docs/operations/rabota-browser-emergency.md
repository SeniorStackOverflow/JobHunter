# Rabota.md browser fallback: optional emergency profile

Production normally runs Rabota.md with `waf_http`, the A14 primary egress and the validated free-proxy reserve. Chromium is not installed in the standard image and `RABOTA_BROWSER_FALLBACK_MODE=none` is forced by `docker-compose.prod.yml`.

The browser fallback code is retained for emergency use. `docker-compose.browser.yml` replaces only the crawler `worker` with `jobhunter-prod-browser:<revision>`, builds that image with the `playwright` extra plus Chromium, and sets `RABOTA_BROWSER_FALLBACK_MODE=stealth_browser`. Other services keep the normal production image.

Enable the emergency crawler profile with:

```sh
sudo ./deploy/prod-browser-compose.sh up -d --build worker
```

Return to the standard browser-free crawler with:

```sh
sudo ./deploy/prod-compose.sh up -d --build worker
```

The source configuration should remain `transport: waf_http` and `fallback_transport: none`; the emergency profile is a runtime override, so activation does not require mutating the source row. Free proxies never receive Chromium fallback; their transport remains pure HTTP/WAF solver only.

Before enabling the emergency profile, check that the A14 egress is still reachable and inspect the Rabota/WAF diagnostics. Browser mode is intentionally not the default because a single Chromium session can add several hundred MiB of RAM and materially increase image/build size.
