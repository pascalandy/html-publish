# Agent map

- Read [docs/architecture.md](docs/architecture.md) before changing ownership or state transitions
- Run `just check` before handing off code changes
- Treat `html_publish/cli.py` as the JSON and command-line boundary
- Treat `html_publish/store.py` as the only mutation owner
- Keep controlled HTTP evidence separate from Tailscale, browser, and production evidence
- Do not describe this MVP as production-ready
