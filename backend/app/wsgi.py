"""WSGI entry point: HOST and PORT configurable by the deployment entrypoint."""

from __future__ import annotations

import os

from .api import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    app.run(host=host, port=port)
