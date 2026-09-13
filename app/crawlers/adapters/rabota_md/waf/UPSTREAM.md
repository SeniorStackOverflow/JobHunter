# Upstream provenance for the Rabota.md WAF solver

The files `solver.py`, `crypto.py`, `signals.py`, `metrics.py`, and `webgl.json` were derived from:

- Repository: `https://github.com/Switch3301/Aws-Waf-Solver`
- Pinned commit used by the 2026-09-12 spike: `fed489c54fe2eb10a6dfac5b4d4c5dfcb06b8808`

The pinned Git tree does **not** contain a `LICENSE` or `NOTICE` file. Do not describe the vendored code as MIT-licensed unless a valid license grant for this pinned source is independently verified. Production/distribution approval for the vendored implementation therefore requires an explicit licensing review or replacement with an independently implemented solver.

Local modifications include replacing `rnet` with guarded `httpx` transport, replacing `pyscrypt` with `hashlib.scrypt`, bounded proof-of-work execution, WAF error taxonomy, and JobHunter-specific rate-limit/SSRF/canary integration.
