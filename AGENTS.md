# Independent iOS History App

This directory is self-contained. Do not use or modify the old WordPress project or its production server to build/run/test it.

- Web: `web/`; API/catalog: `app/`; CLI: `ios_download.py`; Go/Node adapters: `bridge/`; upstream version/checksum manifests: `dependencies/`; licenses: `licenses/`.
- Start with `docker compose up -d --build`; default URL `http://localhost:8088`.
- Support Linux containers on amd64/arm64. Do not claim native Windows or all architectures were tested.
- Apple password must be submitted before asking for OTP. Preserve the same auth process, cookies and signer through OTP.
- Official mode returns Apple's raw link without storing IPA; package mode writes account authorization data and expires files. Do not treat raw URLs as equivalent to complete IPA.
- Never copy or commit Apple credentials, production keys, sessions, runtime caches or IPA files. Credentials travel through encrypted HTTP envelopes/private process pipes, not argv/logs.
- Keep upstream licenses/checksums. Do not edit fetched upstream source to bypass account licenses or platform checks; adapters/build patches must be documented and tested.
- Tests may use synthetic fixtures, but do not describe them as real Apple login/download/installation.
- Changes to API schemas, origin/cookie policy, deployment or dependencies need corresponding docs/tests. Do not automatically publish images or deploy externally.
