from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from intent_engineering.capture.github.auth import GitHubCredentials


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def github_secret() -> str:
    return "gh" + "p_runtime-only-client-credential"


@pytest.fixture
def github_credentials(github_secret: str) -> GitHubCredentials:
    return GitHubCredentials.resolve({"GH_TOKEN": github_secret}, lambda _: "unused")


@pytest.fixture
def transport_factory() -> Callable[
    [Callable[[httpx.Request], httpx.Response]], httpx.MockTransport
]:
    return httpx.MockTransport
