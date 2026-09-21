"""Modal deployment wrapper for the optimizer.

Deliberately the only file that imports `modal`. `app.py` is a plain FastAPI app
with no knowledge of its host, so the same code runs under uvicorn locally and on
Modal in production with no code change:

    .venv/bin/python -m uvicorn optimizer.app:app --port 8000   # local
    modal deploy optimizer/modal_app.py                          # production

Deploying, from the repo root:

    python ingest/publish_xp.py          # write data/xp_artifact.local.json
    pip install modal
    modal token new                      # opens a browser, writes ~/.modal.toml
    modal deploy optimizer/modal_app.py

The deploy prints the service URL; set it as OPTIMIZER_URL in Vercel.

**What goes into the image, and what must not.** Only the `optimizer/` package
and the published xP artifact are copied in. Never the repo root: it holds
`.env`, and the optimizer must never receive database credentials (CLAUDE.md
invariant 5). Every path here is built from this file's location, so the command
behaves the same from any working directory.

**The xP artifact is baked in at deploy time.** That is the simplest delivery
that works today: a redeploy ships new numbers. The nightly job does not
redeploy yet, so production xP goes stale until someone runs `modal deploy`
again. Replacing this with a fetch from `OPTIMIZER_XP_URL` at container start is
the open "how does production get the artifact" decision; only this file and an
env var change when it is made.

Note on state: the in-process cache in app.py lives per container. Modal may run
several, so a cache hit is opportunistic, which is exactly the guarantee
ARCHITECTURE.md asks for. Do not reach for Redis to make it global.
"""

from __future__ import annotations

from pathlib import Path

import modal

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
ARTIFACT = REPO_ROOT / "data" / "xp_artifact.local.json"
REMOTE_ARTIFACT = "/root/data/xp_artifact.json"

# Checked only on the deploying machine. Modal re-imports this file inside the
# container, where the repo paths do not exist and the artifact is already
# mounted at REMOTE_ARTIFACT — running the check there stops every container
# at startup.
if modal.is_local() and not ARTIFACT.exists():
    raise SystemExit(
        f"No xP artifact at {ARTIFACT}. Run `python ingest/publish_xp.py` first — "
        "without it the service would silently serve synthetic seed data."
    )

image = (
    # 3.13, not the 3.14 used locally: PuLP ships its CBC solver as a binary,
    # and 3.13 is the safest runtime for a prebuilt image. Nothing in the
    # optimizer depends on 3.14.
    modal.Image.debian_slim(python_version="3.13")
    .pip_install_from_requirements(str(HERE / "requirements.txt"))
    .env({"OPTIMIZER_XP_PATH": REMOTE_ARTIFACT})
    # Copy steps last: Modal mounts them at container start, so changing code or
    # data does not rebuild the dependency layer above.
    .add_local_dir(
        str(HERE),
        remote_path="/root/optimizer",
        ignore=["tests", "**/__pycache__", "*.pyc"],
    )
    .add_local_file(str(ARTIFACT), remote_path=REMOTE_ARTIFACT)
)

# Named `app` because that is the variable `modal deploy` looks for by default.
# The FastAPI app of the same name is only imported inside the function below,
# so the two never collide.
app = modal.App("fpl-optimizer", image=image)


@app.function(
    # The MIP proves optimality on a real squad in ~6.5s on one core, inside the
    # service's own 20s solver limit. 60s leaves room for a cold start.
    cpu=1.0,
    memory=1024,
    timeout=60,
    # Scale to zero between deadlines; one warm container absorbs the burst
    # before a deadline, when essentially all traffic arrives.
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
