"""Exercise a built image with a disposable, isolated Postgres database and no API keys."""

import argparse
import datetime as dt
import http.cookiejar
import json
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def command(*args, timeout=60):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"{' '.join(args[:3])} exited {result.returncode}: {result.stderr[-3000:]}")
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="emer:local")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--history-scope", choices=("shared", "browser"), default="shared")
    args = parser.parse_args()
    if args.evidence.exists() and any(args.evidence.iterdir()):
        parser.error("Use a new or empty evidence directory so prior verification results are preserved.")
    args.evidence.mkdir(parents=True, exist_ok=True)
    suffix = uuid.uuid4().hex[:10]
    network, database, application = (f"emer-check-{part}-{suffix}" for part in ("net", "db", "app"))
    initializer = f"emer-check-init-{suffix}"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    report = {
        "started_at": dt.datetime.now(dt.UTC).isoformat(),
        "image": args.image,
        "classification": "local container HTTP with explicit lexical fixture; no provider, hybrid or audio proof",
        "history_scope": args.history_scope,
        "checks": [],
        "status": "failed",
    }
    owned = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        report["checks"].append(name)

    def client():
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(opener, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(
            origin + path,
            data=data,
            method=method,
            headers={"Origin": origin, "Content-Type": "application/json"},
        )
        try:
            response = opener.open(req, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "")
            parsed = (
                json.loads(body)
                if "application/json" in content_type
                else body.decode() if content_type.startswith("text/") else body
            )
            return response.status, parsed, response.headers

    def wait_until_ready(opener, seconds):
        started = time.monotonic()
        while time.monotonic() - started < seconds:
            try:
                if request(opener, "/api/v1/health/ready")[0] == 200:
                    return round(time.monotonic() - started, 3)
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(1)
        raise TimeoutError("production image did not become ready")

    try:
        report["docker_context"] = command("docker", "context", "show")
        metadata = json.loads(command("docker", "image", "inspect", args.image))[0]
        report["image_id"] = metadata["Id"]
        report["architecture"] = metadata["Architecture"]
        report["image_bytes"] = metadata["Size"]
        check("image runs as non-root", metadata["Config"]["User"] not in ("", "root", "0"))
        check(
            "production image defaults to secure cookies", "COOKIE_SECURE=true" in metadata["Config"]["Env"]
        )
        command("docker", "network", "create", network)
        owned.append(("network", network))
        command(
            "docker",
            "run",
            "-d",
            "--name",
            database,
            "--network",
            network,
            "--memory",
            "384m",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,size=256m",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "-e",
            "POSTGRES_DB=emer",
            "postgres:17-alpine",
            timeout=120,
        )
        owned.append(("container", database))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = subprocess.run(
                ["docker", "exec", database, "pg_isready", "-U", "postgres", "-d", "emer"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise TimeoutError("isolated Postgres did not become ready")
        # Initialization is a separate release operation. This isolated smoke
        # deliberately uses lexical retrieval, so no provider credentials or
        # invented embedding vectors are needed.
        # Register the initializer before dispatch: a Docker client timeout does
        # not guarantee that its container stopped. Cleanup must own it as well.
        owned.append(("container", initializer))
        command(
            "docker", "run", "--name", initializer, "--network", network,
            "--platform", "linux/amd64",
            "-e", f"DATABASE_URL=postgresql+psycopg://postgres@{database}:5432/emer",
            "--entrypoint", "bash", args.image, "-c",
            "uv run --no-sync alembic upgrade head && "
            "uv run --no-sync emer ingest --manifest config/corpus.json",
            timeout=120,
        )
        check("explicit lexical fixture initialization succeeds without provider credentials", True)
        command(
            "docker",
            "run",
            "-d",
            "--name",
            application,
            "--network",
            network,
            "--memory",
            "512m",
            "--platform",
            "linux/amd64",
            "-p",
            f"127.0.0.1:{port}:8017",
            "-e",
            f"DATABASE_URL=postgresql+psycopg://postgres@{database}:5432/emer",
            "-e",
            f"APP_ORIGIN={origin}",
            "-e",
            "COOKIE_SECURE=false",
            "-e",
            "RETRIEVAL_MODE=lexical",
            "-e",
            f"SHARED_WORKSPACE={'true' if args.history_scope == 'shared' else 'false'}",
            args.image,
        )
        owned.append(("container", application))
        check(
            "HTTP port is published only on loopback",
            command("docker", "port", application, "8017/tcp") == f"127.0.0.1:{port}",
        )
        visitor = client()
        report["startup_seconds"] = wait_until_ready(visitor, 90)
        check("migrations, index verification and HTTP startup succeed within 512 MiB", True)
        status, html, _ = request(visitor, "/")
        check("built frontend is served", status == 200 and 'id="root"' in html)
        assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', html)
        check("frontend references compiled assets", bool(assets))
        for asset in assets:
            check(f"compiled asset responds: {asset}", request(visitor, asset)[0] == 200)
        status, corpus, _ = request(visitor, "/api/v1/corpus")
        check("all 35 immutable sources are available", status == 200 and corpus["count"] == 35)
        report["corpus"] = corpus
        status, patients, _ = request(visitor, "/api/v1/patients")
        check("all eight patients are available", status == 200 and len(patients["items"]) == 8)
        status, first_session, headers = request(visitor, "/api/v1/session", "POST")
        check("session bootstraps", status == 200)
        if args.history_scope == "browser":
            check("anonymous cookie is HttpOnly", "HttpOnly" in headers.get("Set-Cookie", ""))
        status, conversation, _ = request(
            visitor, "/api/v1/conversations", "POST", {"title": "Image smoke check"}
        )
        check(
            "owned conversation persists",
            status == 201 and request(visitor, "/api/v1/conversations/" + conversation["id"])[0] == 200,
        )
        command("docker", "restart", "--time", "10", application)
        report["restart_seconds"] = wait_until_ready(visitor, 60)
        check(
            "owned conversation survives app restart",
            request(visitor, "/api/v1/conversations/" + conversation["id"])[0] == 200,
        )
        # Disable only this disposable database, preserving its bytes. This
        # exercises actual connection failure and pool recovery, not a mock.
        command(
            "docker", "exec", database, "psql", "-U", "postgres", "-d", "postgres",
            "-v", "ON_ERROR_STOP=1", "-c", "ALTER DATABASE emer ALLOW_CONNECTIONS false",
        )
        try:
            command(
                "docker", "exec", database, "psql", "-U", "postgres", "-d", "postgres",
                "-v", "ON_ERROR_STOP=1", "-c",
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='emer'",
            )
            started = time.monotonic()
            status, failure, _ = request(visitor, "/api/v1/conversations/" + conversation["id"])
            report["database_failure_response_seconds"] = round(time.monotonic() - started, 3)
            check(
                "database outage returns a retryable 503 rather than missing knowledge",
                status == 503 and failure.get("error", {}).get("code") == "DATABASE_UNAVAILABLE"
                and failure["error"]["retryable"] is True,
            )
            check("readiness fails during database outage", request(visitor, "/api/v1/health/ready")[0] == 503)
            check("process liveness survives database outage", request(visitor, "/api/v1/health/live")[0] == 200)
            check("frontend remains available during database outage", request(visitor, "/")[0] == 200)
        finally:
            command(
                "docker", "exec", database, "psql", "-U", "postgres", "-d", "postgres",
                "-v", "ON_ERROR_STOP=1", "-c", "ALTER DATABASE emer ALLOW_CONNECTIONS true",
            )
        report["database_recovery_seconds"] = wait_until_ready(visitor, 30)
        check(
            "owned history recovers after database reconnect without app restart",
            request(visitor, "/api/v1/conversations/" + conversation["id"])[0] == 200,
        )
        stranger = client()
        check("second anonymous session bootstraps", request(stranger, "/api/v1/session", "POST")[0] == 200)
        expected_read = 200 if args.history_scope == "shared" else 404
        check("history follows the configured sharing policy",
              request(stranger, "/api/v1/conversations/" + conversation["id"])[0] == expected_read)
        status, replacement, _ = request(visitor, "/api/v1/session/reset", "POST")
        check("reset follows the configured sharing policy", status == 200 and (
            (replacement["id"] == first_session["id"]) == (args.history_scope == "shared")))
        check("history after reset follows the configured sharing policy",
              request(visitor, "/api/v1/conversations/" + conversation["id"])[0] == expected_read)
        report["status"] = "passed"
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if ("container", application) in owned:
            try:
                logs = subprocess.run(
                    ["docker", "logs", application], capture_output=True, text=True, timeout=15, check=False
                )
                (args.evidence / "container.log").write_text(logs.stdout + logs.stderr)
            except subprocess.TimeoutExpired:
                report["log_error"] = "Docker logs timed out after 15 seconds"
        cleanup = []
        for kind, name in reversed(owned):
            try:
                result = subprocess.run(
                    ["docker", "rm", "-f", name]
                    if kind == "container"
                    else ["docker", "network", "rm", name],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                cleanup.append({"resource": name, "status": result.returncode})
            except subprocess.TimeoutExpired:
                cleanup.append({"resource": name, "status": "timed out"})
        if any(item["status"] != 0 for item in cleanup):
            report["status"] = "cleanup_failed"
        report["cleanup"] = cleanup
        report["completed_at"] = dt.datetime.now(dt.UTC).isoformat()
        (args.evidence / "smoke.json").write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "checks": len(report["checks"]),
                    "evidence": str(args.evidence / "smoke.json"),
                }
            )
        )
    if report["status"] != "passed":
        raise RuntimeError("Image verification did not complete successfully; inspect smoke.json")


if __name__ == "__main__":
    main()
