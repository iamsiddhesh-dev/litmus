"""``python -m litmus.api`` - the dev control plane on 127.0.0.1:8000."""

from __future__ import annotations

import uvicorn

from litmus.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "litmus.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()