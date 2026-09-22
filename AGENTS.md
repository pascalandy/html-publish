# Agent map

- Treat [docs/contract.md](docs/contract.md) as the behavior contract; a behavior change starts there
- Read [docs/architecture.md](docs/architecture.md) before changing ownership or state transitions
- Run `just check` before handing off code changes
- Treat `html_publish/cli.py` as the JSON and command-line boundary
- Treat `html_publish/store.py` as the only mutation owner
- Keep controlled HTTP evidence separate from Tailscale, browser, and production evidence
- Do not describe this MVP as production-ready
- Prove CLI and HTTP behavior with the [verify-html-publish skill](.agents/skills/verify-html-publish/SKILL.md) before claiming a fix or feature works
- Use [docs/operations.md](docs/operations.md) for the controlled `om1` deployment workflow and [docs/evidence/deployment/2026-09-21-om1-controlled-mvp.md](docs/evidence/deployment/2026-09-21-om1-controlled-mvp.md) for its live verification record
