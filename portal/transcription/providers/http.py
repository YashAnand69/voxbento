from __future__ import annotations

import httpx


def is_retryable_transcription_error(error: BaseException) -> bool:
    if isinstance(error, (httpx.ReadTimeout, httpx.ConnectError)):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return status == 429 or 500 <= status < 600
    return False
