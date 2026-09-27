set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

# Run every local check
default: check

# Run the CI verdict in order; --fast skips E2E, --list names each check, --only NAME reruns one
check *args:
  uv run python scripts/check.py {{args}}

# Install or update the controlled om1 deployment from this checkout
deploy-om1:
  uv run python -m html_publish.deploy install --source .

# Check the installed service and private HTTPS route
health-om1:
  uv run python -m html_publish.deploy health

# Switch om1 back to the previously installed application release
rollback-om1:
  uv run python -m html_publish.deploy rollback
