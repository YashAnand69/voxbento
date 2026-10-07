from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from tenacity import AsyncRetrying, wait_none

import portal.globals as pg
from portal.transcription.providers import elevenlabs, openai
from portal.transcription.providers.base import ProviderConfig


@pytest.fixture(params=[openai, elevenlabs])
def provider(request, monkeypatch):
    module = request.param

    def retry_without_wait(**kwargs):
        kwargs["wait"] = wait_none()
        return AsyncRetrying(**kwargs)

    monkeypatch.setattr(module, "AsyncRetrying", retry_without_wait)
    cls = openai.OpenAIProvider if module is openai else elevenlabs.ElevenLabsProvider
    return cls()


@pytest.mark.anyio
@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 502, 503, 504])
async def test_http_error_is_surfaced(provider, monkeypatch, status):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={"detail": "provider error"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(pg, "shared_http_client", client)
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await provider.process_chunk(b"\0" * 3200, "en", "model", ProviderConfig(api_key="test-key"))
    assert caught.value.response.status_code == status
    assert len(calls) == (3 if status == 429 or status >= 500 else 1)


@pytest.mark.anyio
async def test_transient_error_recovers(provider, monkeypatch):
    statuses = iter([500, 200])

    def respond(request):
        return httpx.Response(next(statuses), json={"text": " recovered "})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(pg, "shared_http_client", client)
        assert (
            await provider.process_chunk(b"\0" * 3200, "en", "model", ProviderConfig(api_key="test-key")) == "recovered"
        )


@pytest.mark.anyio
async def test_empty_success_is_still_silence(provider, monkeypatch):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"text": ""}))
    ) as client:
        monkeypatch.setattr(pg, "shared_http_client", client)
        assert await provider.process_chunk(b"\0" * 3200, "en", "model", ProviderConfig(api_key="test-key")) == ""
