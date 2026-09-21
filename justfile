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
