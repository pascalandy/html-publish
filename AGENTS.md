# Agent map

- Treat [docs/contract.md](docs/contract.md) as the behavior contract; a behavior change starts there
- Read [docs/architecture.md](docs/architecture.md) before changing ownership or state transitions
- Run `just check` before handing off code changes
- Treat `html_publish/cli.py` as the JSON and command-line boundary
- Treat `html_publish/store.py` as the only mutation owner
- Keep controlled HTTP evidence separate from Tailscale, browser, and production evidence
- Do not describe this MVP as production-ready
- Prove CLI and HTTP behavior with the [verify-html-publish skill](.agents/skills/verify-html-publish/SKILL.md) before claiming a fix or feature works
- Keep every GitHub workflow on a manual dispatch trigger only; the candidate check and stable-tag promotion procedure lives in [docs/operations.md](docs/operations.md)
- Use [docs/operations.md](docs/operations.md) for the controlled `om1` deployment workflow, the [installed verification handoff](docs/evidence/deployment/2026-09-22-om1-installed.md) for the reviewed wheel and startup evidence, and the [installed skills record](docs/evidence/skills/2026-09-22-installed-skills-and-authoring.md) for receipts and the authoring cutover
