set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

# Run every local check
default: check

# Run the CLI behavior tests
test:
  uv run python -m unittest discover -s tests -v

# Check formatting and lint rules
lint:
  uv run ruff format --check .
  uv run ruff check .

# Check Python types
typecheck:
  uv run pyright

# Run lint, types, and tests
check: lint typecheck test

# Install or update the controlled om1 deployment from this checkout
deploy-om1:
  uv run python -m html_publish.deploy install --source .

# Check the installed service and private HTTPS route
health-om1:
  uv run python -m html_publish.deploy health

# Switch om1 back to the previously installed application release
rollback-om1:
  uv run python -m html_publish.deploy rollback
