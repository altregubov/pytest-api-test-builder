# Architecture Patterns for `pytest-api-test-builder`

> **Adapting to Existing Repositories:** The examples below use `clients/`, `schemas/`, and `tests/api/` as default locations. When a repository already defines equivalent packages (such as `core/`, `api_clients/`, `models/`, or `tests/integration/`), reuse those existing directories and naming conventions instead of creating duplicate packages.

---

## 1. `pyproject.toml` Pytest Configuration

Configure `pythonpath = ["."]`, strict marker checking, and the `endpoint` traceability marker so tests resolve local imports cleanly and every `@pytest.mark.endpoint(...)` tag is validated at collection time:

```toml
[tool.pytest.ini_options]
pythonpath = ["."]
addopts = ["--strict-markers"]
markers = [
    "endpoint(route): Tag test with target API endpoint 'METHOD /path'",
]
```

---

## 2. `conftest.py` (Environment Config, Client Lifecycle, Yield Cleanup)

Read environment variables **inside fixtures** rather than at module import time. That keeps `pytest --collect-only` working offline without requiring live credentials or environment variables during static discovery.

```python
from __future__ import annotations

import os
from collections.abc import Generator
from uuid import uuid4

import httpx
import pytest

from clients.items_client import ItemsClient
from schemas.items import ItemCreate, ItemResponse


@pytest.fixture(scope="session")
def api_base_url() -> str:
    """Resolve the target API base URL from the environment."""
    return os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")


@pytest.fixture(scope="session")
def api_timeout() -> float:
    """Resolve the HTTP request timeout in seconds from the environment."""
    return float(os.environ.get("API_TIMEOUT", "10.0"))


@pytest.fixture(scope="session")
def auth_headers() -> dict[str, str]:
    """Build optional authorization headers from environment credentials."""
    token = os.environ.get("API_AUTH_TOKEN", "")
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def http_client(
    api_base_url: str,
    api_timeout: float,
    auth_headers: dict[str, str],
) -> Generator[httpx.Client, None, None]:
    """Manage a shared synchronous httpx.Client for the test session."""
    with httpx.Client(
        base_url=api_base_url,
        timeout=api_timeout,
        headers=auth_headers,
    ) as client:
        yield client


@pytest.fixture(scope="session")
def items_client(http_client: httpx.Client) -> ItemsClient:
    """Provide the typed Items domain API client."""
    return ItemsClient(http_client)


@pytest.fixture()
def created_item(items_client: ItemsClient) -> Generator[ItemResponse, None, None]:
    """Create a unique item for read/update tests and delete it in teardown."""
    unique_suffix = uuid4().hex[:8]
    payload = ItemCreate(
        name=f"widget-{unique_suffix}",
        sku=f"SKU-{unique_suffix}",
        price=19.99,
    )
    response = items_client.create_item(payload)
    assert response.status_code == 201, response.text
    item = ItemResponse.model_validate(response.json())

    yield item

    cleanup_response = items_client.delete_item(item.id)
    assert cleanup_response.status_code in (200, 204, 404)


@pytest.fixture()
def item_cleanup_ids(items_client: ItemsClient) -> Generator[list[str], None, None]:
    """Track item IDs created during a test and delete them after the test."""
    created_ids: list[str] = []
    yield created_ids

    for item_id in reversed(created_ids):
        cleanup_response = items_client.delete_item(item_id)
        assert cleanup_response.status_code in (200, 204, 404)
```

> **Async Projects:** Switch `httpx.Client` to `httpx.AsyncClient` and `async with httpx.AsyncClient(...) as client: yield client` **only** when the repository already uses `pytest-asyncio` or `anyio` for async tests.

---

## 3. `schemas/items.py` (Pydantic v2 Request, Success, and Error Models)

Use Pydantic v2 (`BaseModel`, `ConfigDict`, `Field`, `model_validate`, `model_dump`) for both 2xx success responses and standardized 4xx error payloads.

```python
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ItemCreate(BaseModel):
    """Request payload for creating an item."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    sku: str = Field(min_length=1, max_length=64)
    price: float = Field(gt=0)


class ItemUpdate(BaseModel):
    """Request payload for partially updating an item."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    price: float | None = Field(default=None, gt=0)


class ItemResponse(BaseModel):
    """2xx response schema for a single item resource."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    sku: str
    price: float


class ItemListResponse(BaseModel):
    """2xx response schema for a paginated list of items."""

    model_config = ConfigDict(extra="ignore")

    items: list[ItemResponse]
    total: int = Field(ge=0)


class ErrorDetail(BaseModel):
    """Field-level validation error entry returned in 4xx responses."""

    model_config = ConfigDict(extra="ignore")

    field: str | None = None
    message: str


class ErrorResponse(BaseModel):
    """Standardized 4xx error response payload."""

    model_config = ConfigDict(extra="ignore")

    code: str
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)
```

---

## 4. `clients/items_client.py` (Typed Domain API Client)

Encapsulate endpoint paths, headers, and Pydantic serialization inside a domain client so tests never call raw HTTP methods on `httpx`.

```python
from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel


class ItemsClient:
    """Typed domain API client for /api/v1/items endpoints."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self._base_path = "/api/v1/items"

    def create_item(
        self,
        payload: BaseModel | dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send POST /api/v1/items with a schema or raw negative-test payload."""
        body = (
            payload.model_dump(mode="json", exclude_none=True)
            if isinstance(payload, BaseModel)
            else payload
        )
        return self._client.post(self._base_path, json=body, headers=headers)

    def get_item(self, item_id: str) -> httpx.Response:
        """Send GET /api/v1/items/{item_id}."""
        return self._client.get(f"{self._base_path}/{item_id}")

    def list_items(self, *, limit: int = 20, offset: int = 0) -> httpx.Response:
        """Send GET /api/v1/items with pagination query parameters."""
        return self._client.get(
            self._base_path,
            params={"limit": limit, "offset": offset},
        )

    def update_item(
        self,
        item_id: str,
        payload: BaseModel | dict[str, Any],
    ) -> httpx.Response:
        """Send PATCH /api/v1/items/{item_id}."""
        body = (
            payload.model_dump(mode="json", exclude_none=True)
            if isinstance(payload, BaseModel)
            else payload
        )
        return self._client.patch(f"{self._base_path}/{item_id}", json=body)

    def delete_item(self, item_id: str) -> httpx.Response:
        """Send DELETE /api/v1/items/{item_id}."""
        return self._client.delete(f"{self._base_path}/{item_id}")
```

---

## 5. `tests/api/test_items.py` (Positive, Negative, and Boundary Tests)

Keep every test strictly structured as Arrange-Act-Assert (`AAA`), tagged with `@pytest.mark.endpoint("METHOD /path")`, documented with a scenario docstring, and validated through Pydantic v2 models.

```python
from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from clients.items_client import ItemsClient
from schemas.items import ErrorResponse, ItemCreate, ItemResponse


@pytest.mark.endpoint("POST /api/v1/items")
def test_create_item_returns_201_and_valid_schema(
    items_client: ItemsClient,
    item_cleanup_ids: list[str],
) -> None:
    """Verify creating a unique item returns 201 and matches ItemResponse."""
    # Arrange
    unique_suffix = uuid4().hex[:8]
    payload = ItemCreate(
        name=f"sensor-{unique_suffix}",
        sku=f"SKU-{unique_suffix}",
        price=42.5,
    )

    # Act
    response = items_client.create_item(payload)

    # Assert
    assert response.status_code == 201
    created = ItemResponse.model_validate(response.json())
    item_cleanup_ids.append(created.id)
    assert created.name == payload.name
    assert created.sku == payload.sku
    assert created.price == payload.price


@pytest.mark.endpoint("GET /api/v1/items/{item_id}")
def test_get_item_returns_existing_resource(
    items_client: ItemsClient,
    created_item: ItemResponse,
) -> None:
    """Verify fetching an existing item by ID returns 200 and matching fields."""
    # Arrange
    target_id = created_item.id

    # Act
    response = items_client.get_item(target_id)

    # Assert
    assert response.status_code == 200
    fetched = ItemResponse.model_validate(response.json())
    assert fetched.id == created_item.id
    assert fetched.sku == created_item.sku


@pytest.mark.endpoint("POST /api/v1/items")
@pytest.mark.parametrize(
    ("invalid_payload", "expected_status"),
    [
        pytest.param(
            {"sku": f"SKU-{uuid4().hex[:8]}", "price": 10.0},
            422,
            id="missing_required_name",
        ),
        pytest.param(
            {"name": "invalid-price", "sku": f"SKU-{uuid4().hex[:8]}", "price": -1.0},
            422,
            id="negative_price_boundary",
        ),
        pytest.param(
            {"name": "x" * 101, "sku": f"SKU-{uuid4().hex[:8]}", "price": 10.0},
            422,
            id="name_exceeds_max_length",
        ),
    ],
)
def test_create_item_rejects_invalid_payload(
    items_client: ItemsClient,
    invalid_payload: dict[str, Any],
    expected_status: int,
) -> None:
    """Verify invalid or out-of-bounds item payloads return 4xx ErrorResponse."""
    # Arrange
    payload = dict(invalid_payload)

    # Act
    response = items_client.create_item(payload)

    # Assert
    assert response.status_code == expected_status
    error = ErrorResponse.model_validate(response.json())
    assert error.code
    assert error.message
```
