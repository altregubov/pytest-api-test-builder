---
name: pytest-api-test-builder
description: Generate, scaffold, or refactor deterministic REST API automated tests using pytest, httpx, and Pydantic v2 from OpenAPI/Swagger specifications, endpoint source code, or feature requirements. Do not use for pure unit tests, UI/browser tests, or test execution-only requests.
---

# Pytest API Test Builder

Generate deterministic, maintainable REST API automated tests built on `pytest`, `httpx`, and Pydantic v2 from OpenAPI/Swagger specifications, endpoint source code, or feature requirements.

All reference code examples live in [references/architecture_patterns.md](./references/architecture_patterns.md). Read that reference before creating or editing test modules.

## Core Architectural Rules

1. **HTTP Client Exclusivity:** Use `httpx` exclusively (`httpx.Client` by default; use `httpx.AsyncClient` only when async pytest execution is already configured in the repository). Never import or use `requests`, `urllib.request`, or `aiohttp`.
2. **Typed Domain API Client Layer:** Encapsulate endpoint paths, headers, and serialization in thin, typed domain client classes (e.g., `clients/<domain>_client.py`) that wrap `httpx.Client` and return `httpx.Response` (or typed response wrappers). Never invoke raw HTTP methods (`.get()`, `.post()`, `.put()`, `.patch()`, `.delete()`, `.request()`) or instantiate `httpx.Client` / `httpx.AsyncClient` inside `test_*` functions.
3. **Adapt to Existing Layout:** Inspect the repository first and reuse existing packages (such as `core/`, `models/`, or existing fixture modules) instead of creating duplicate `clients/` or `schemas/` directories.
4. **Pydantic v2 Validation:** Define Pydantic v2 `BaseModel` schemas (`model_validate`, `model_dump`, `ConfigDict`) for both 2xx success responses and standardized 4xx error payloads. Never rely on fragile nested dictionary indexing such as `resp.json()["data"][0]["id"]`.
5. **Env-Driven Config & Fixture Isolation:** Read `BASE_URL`, timeouts, and auth credentials exclusively from environment variables inside `conftest.py` fixtures (not at module import time, so `pytest --collect-only` works offline). Centralize `httpx.Client` lifecycle and auth fixtures in `conftest.py`, and use `yield` fixtures to tear down created resources.
6. **Deterministic & Parallel-Safe Data:** Generate unique values (e.g., `uuid4` suffixes) for unique fields so repeated or parallel runs never collide with `409 Conflict`. Never depend on test execution order, mutable global state, or `time.sleep()`.
7. **Endpoint Traceability & AAA Structure:** Register the `endpoint` marker and configure `pythonpath = ["."]` in `pyproject.toml` under `[tool.pytest.ini_options]`. Tag every test with `@pytest.mark.endpoint("METHOD /path")` and a concise scenario docstring. Separate positive, negative, and boundary cases, use `@pytest.mark.parametrize` for input matrices and expected status codes, and follow strict Arrange-Act-Assert (`AAA`) without over-engineered helper abstractions.

## Step-by-Step Workflow

1. **Inspect the Repository:**
   - Examine `pyproject.toml`, `uv.lock`, `conftest.py`, existing API clients, schemas/models, and test directories to detect project conventions and async configuration.
   - Read [references/architecture_patterns.md](./references/architecture_patterns.md) for canonical file layouts and Pydantic v2 + `httpx` patterns.
2. **Configure Pytest and Fixtures:**
   - Ensure `pyproject.toml` configures `pythonpath = ["."]`, `--strict-markers`, and the `endpoint` marker under `[tool.pytest.ini_options]`.
   - Create or update `conftest.py` with fixture-scoped environment configuration, the `httpx.Client` lifecycle fixture, domain client fixtures, and `yield` cleanup fixtures.
3. **Generate Schemas and Domain Clients:**
   - Create or update Pydantic v2 request, 2xx success response, and 4xx error response models in the project's schema/model package (`schemas/` or existing equivalent).
   - Create or update a typed domain API client in `clients/` (or existing equivalent) exposing explicit domain methods rather than exposing raw HTTP calls to tests.
4. **Generate Pytest Test Modules:**
   - Write `test_*.py` modules organized by domain resource, separating positive, negative, and boundary test functions.
   - Annotate every test with `@pytest.mark.endpoint("METHOD /path")`, add a clear scenario docstring, use `uuid4`-uniquified test inputs, parameterize error/boundary matrices with `@pytest.mark.parametrize`, follow strict Arrange-Act-Assert, and validate responses with `Model.model_validate(response.json())`.
5. **Validate and Fix Before Finishing:**
   - Run the bundled validator script [scripts/validate_tests.py](./scripts/validate_tests.py) resolved relative to this skill directory:
     `uv run <skill-dir>/scripts/validate_tests.py --project-dir <project-dir> --test-dir <test-dir>`
   - Fix every reported AST violation, `ruff` lint issue, or `pytest --collect-only` error and re-run until the validator exits `0`.

## Forbidden Practices / Anti-Patterns Checklist

- Importing `requests`, `aiohttp`, or `urllib.request`.
- Calling `httpx.get(...)` / `.post(...)` / etc., instantiating `httpx.Client` / `httpx.AsyncClient`, or calling raw HTTP methods (`.get`, `.post`, `.put`, `.patch`, `.delete`, `.request`) on HTTP client objects inside `test_*` functions.
- Indexing deeply nested raw JSON dictionaries (e.g., `resp.json()["data"][0]["id"]`) instead of validating with Pydantic v2 `model_validate`.
- Using legacy Pydantic v1 APIs (`.parse_obj()`, `.dict()`, inner `class Config`).
- Hardcoding base URLs, tokens, or credentials, or reading required environment variables at module import time.
- Calling `time.sleep()` for synchronization.
- Relying on test execution order, shared mutable globals, or static unique identifiers that cause `409 Conflict` across repeated runs.
- Omitting `@pytest.mark.endpoint("METHOD /path")`, leaving the `endpoint` marker unregistered, or omitting scenario docstrings.
- Duplicating existing `core/` or `models/` packages instead of adapting to the repository's structure.
- Hiding Arrange-Act-Assert inside over-engineered test helper frameworks or adding async boilerplate when the repository uses synchronous `pytest`.
