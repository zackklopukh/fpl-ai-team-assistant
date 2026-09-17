"""Modal deployment wrapper for the optimizer.

Deliberately the only file that imports `modal`. `app.py` is a plain FastAPI app
with no knowledge of its host, so the same code runs under uvicorn locally and on
Modal in production with no code change:

    .venv/bin/uvicorn optimizer.app:app --port 8000     # local
    modal deploy optimizer/modal_app.py                 # production

Deploying for the first time, once a Modal account exists:

    pip install modal
    modal token new                    # opens a browser, writes ~/.modal.toml
    modal deploy optimizer/modal_app.py

`modal serve optimizer/modal_app.py` gives a hot-reloading ephemeral deployment
for checking the container image without publishing.

This file has never been run -- no Modal account exists yet. Treat it as the
intended shape rather than as tested code.

Note on state: the in-process cache in app.py lives per container. Modal may run
several, so a cache hit is opportunistic, which is exactly the guarantee
ARCHITECTURE.md asks for. Do not reach for Redis to make it global.
"""

from __future__ import annotations

import modal

# The optimizer's dependency set is deliberately small and holds no database
# driver. Installing from requirements.txt keeps the deployed image identical to
# the local venv rather than a second list that drifts.
image = (
    modal.Image.debian_slim(python_version="3.14")
    .pip_install_from_requirements("requirements.txt")
    # The xP snapshot ships in the image for now. When the production provider in
    # pool.py lands, this becomes a fetch at container start and the file goes.
    .add_local_dir(".", remote_path="/root/optimizer")
)

modal_app = modal.App("fpl-optimizer", image=image)


@modal_app.function(
    # Free-tier sizing. The greedy solver is milliseconds; the MIP will need more
    # CPU and a longer timeout, which is a change to these numbers only.
    cpu=1.0,
    memory=1024,
    timeout=60,
    # One warm container absorbs the burst around a gameweek deadline, which is
    # when essentially all traffic arrives.
    min_containers=0,
    scaledown_window=300,
)
@modal.asgi_app()
def fastapi_app():
    # Imported inside the function so this module is importable without the
    # service's dependencies present -- Modal builds the image before it needs
    # them, and a local `import modal_app` should not drag in FastAPI.
    import sys

    sys.path.insert(0, "/root")
    from optimizer.app import app

    return app
