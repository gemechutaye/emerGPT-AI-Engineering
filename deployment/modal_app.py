"""Modal deployment for EMER's persistent API and explicit ingestion job.

Run from the repository root with Modal CLI 1.5.5. This module defines resources;
importing it never deploys them. Read deployment/README.md before deployment.
"""

from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parents[1]
app = modal.App("emer-gpt")
# A run initializes every resource registered on its App. Keep the operator job
# on a separate App so migrations/ingestion cannot start the public server.
jobs = modal.App("emer-gpt-jobs")
runtime_secret = modal.Secret.from_name("emer-runtime")
image = modal.Image.from_dockerfile(
    ROOT / "deployment/Dockerfile",
    context_dir=ROOT,
    # The reranker is optional in the measured application configuration, but its
    # pinned assets are prepared here so enabling it never downloads on a request.
    build_args={"PREPARE_RERANKER": "true"},
)


@app.server(
    image=image,
    secrets=[runtime_secret],
    cpu=(0.25, 0.25),
    memory=(1024, 1024),
    min_containers=1,
    max_containers=1,
    target_concurrency=0,
    port=8017,
    startup_timeout=180,
    exit_grace_period=120,
    unauthenticated=True,
    routing_region="us-west",
)
class API:
    @modal.enter()
    def start(self):
        import subprocess

        self.process = subprocess.Popen(["bash", "deployment/start.sh"], cwd="/app")

    @modal.exit()
    def stop(self):
        import subprocess

        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)


@jobs.function(
    image=image,
    secrets=[runtime_secret],
    cpu=(0.25, 0.25),
    memory=(1024, 1024),
    timeout=300,
    retries=0,
    max_containers=1,
)
def initialize():
    """Explicit operator job: migrate, ingest real embeddings, verify the index."""
    import subprocess

    for command in (
        ["uv", "run", "--no-sync", "alembic", "upgrade", "head"],
        ["uv", "run", "--no-sync", "emer", "ingest", "--embeddings", "--manifest", "config/corpus.json"],
        ["uv", "run", "--no-sync", "python", "deployment/check_index.py"],
    ):
        subprocess.run(command, cwd="/app", check=True)
