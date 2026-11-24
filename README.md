# pytest-api-test-builder

An Agent Skill (following the [Agent Skills specification](https://agentskills.io)) that guides AI coding agents to generate, scaffold, and refactor deterministic REST API automated tests using `pytest`, `httpx`, and Pydantic v2 from OpenAPI/Swagger specifications, endpoint source code, or feature requirements.

## Key Architectural Guarantees

- **Exclusive `httpx` Client:** Uses `httpx.Client` by default (`httpx.AsyncClient` only when async pytest execution is already configured). Forbids `requests`, `urllib.request`, and `aiohttp`.
- **Typed Domain API Client Layer:** Encapsulates endpoint paths, headers, and serialization in thin domain wrapper classes (e.g., `clients/<domain>_client.py`), forbidding raw HTTP method calls or `httpx.Client` instantiation inside `test_*` functions.
- **Repository-Aware Layout:** Reuses existing project packages (such as `core/` or `models/`) instead of creating duplicate `clients/` or `schemas/` directories.
- **Pydantic v2 Validation:** Validates both 2xx success payloads and standardized 4xx error responses via `BaseModel`, `model_validate`, `model_dump`, and `ConfigDict`, eliminating fragile nested dictionary indexing.
- **Isolated Config & Teardown:** Loads `BASE_URL`, timeouts, and auth credentials from environment variables inside `conftest.py` fixtures (preserving offline `pytest --collect-only`), cleans up created resources with `yield` fixtures, and generates `uuid4`-uniquified data to avoid `409 Conflict` collisions—with no `time.sleep()` or test order dependencies.
- **Traceability & Structure:** Configures `pythonpath = ["."]` and `--strict-markers` in `pyproject.toml`, tags every test with `@pytest.mark.endpoint("METHOD /path")` and a scenario docstring, uses `@pytest.mark.parametrize` for negative/boundary matrices, and follows strict Arrange-Act-Assert (`AAA`).

## Repository Structure

```text
pytest-api-test-builder/
├── SKILL.md                             # Main Agent Skill instructions & workflow
├── references/
│   └── architecture_patterns.md         # Idiomatic httpx + Pydantic v2 + pytest patterns
├── scripts/
│   └── validate_tests.py                # PEP 723 validator (AST + ruff + pytest collection)
├── README.md
└── LICENSE
```

## Installation & Usage

Clone or copy this repository into your project's skills directory (for example, `.agents/skills/pytest-api-test-builder/`) or your global skills directory.

### Running the Bundled Validator

The validation script uses PEP 723 inline metadata and runs directly via [`uv`](https://docs.astral.sh/uv/):

```bash
uv run scripts/validate_tests.py --project-dir <project-dir> --test-dir <test-dir>
```

`validate_tests.py` runs three checks and exits non-zero if any fail:

1. **AST Static Check:** Scans target Python files (`--test-dir`, plus `clients/`, `schemas/`, and `conftest.py` when present) for forbidden imports (`requests`, `aiohttp`, `urllib.request`), `time.sleep()` calls, and raw `httpx` usage or client instantiation inside `test_*` functions.
2. **Ruff Lint Check:** Runs `ruff check` across all target paths.
3. **Pytest Discovery Check:** Runs `pytest --collect-only` with `PYTHONPATH` including `--project-dir` (using `uv run --no-sync --directory <project-dir> pytest ...` when `uv.lock` is present).

## License

[MIT](LICENSE)
