"""Live Phase 4B verification — the builder against real subprocesses, a real HTTP
server, real document rendering, and (if hosted is on) a real Groq code-gen round-trip.

Run:
    PYTHONPATH=. uv run python scripts/verify_phase4b.py
"""

import os
import shutil
import urllib.request

from scripts._verify_lib import bootstrap_env, check, skip, summarize_and_exit

bootstrap_env()
os.environ["SECRET_CANARY"] = "should-not-leak"

from app.agent import router  # noqa: E402
from app.builder import codegen, docs, sandbox, serve, workspace  # noqa: E402


def main() -> None:
    sb = sandbox.SubprocessSandbox()
    proj = workspace.create_project("verify-4b")
    try:
        # 1. Sandbox runs the real toolchain.
        node = sb.run(["node", "-v"], cwd=proj)
        check("sandbox runs node", node.ok and node.stdout.strip().startswith("v"), node.stdout.strip() or node.stderr[:60])
        py = sb.run(["python3", "-c", "print(6*7)"], cwd=proj)
        check("sandbox runs python", py.ok and py.stdout.strip() == "42", py.stdout.strip())

        # 2. Secrets are scrubbed from the child env.
        canary = sb.run(["python3", "-c", "import os;print('LEAK' if 'SECRET_CANARY' in os.environ else 'clean')"], cwd=proj)
        check("sandbox scrubs secret env vars", canary.stdout.strip() == "clean", canary.stdout.strip())

        # 3. sandbox-exec denies reading the real .env (best-effort FS jail).
        env_path = (workspace.REPO_ROOT / ".env").as_posix()
        script = f"try:\n d=open({env_path!r}).read();print('READ',len(d))\nexcept Exception as e:\n print('DENIED',type(e).__name__)"
        deny = sb.run(["python3", "-c", script], cwd=proj)
        check("sandbox-exec denies reading data/.env", deny.stdout.strip().startswith("DENIED"),
              deny.stdout.strip() + (" (fs_deny/sandbox-exec off?)" if not deny.stdout.strip().startswith("DENIED") else ""))

        # 4. Real static serve → HTTP 200 with our content.
        workspace.write_file(proj, "index.html", "<h1>verify-ok</h1>")
        url = serve.serve_static(proj, name="verify-4b")
        port = url.rsplit(":", 1)[1]
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/index.html", timeout=5).read().decode()
        check("static server returns the page", "verify-ok" in body, f"{url} -> {body[:40]!r}")
        serve.stop_server("verify-4b")

        # 5. Real PDF renders.
        pdf = docs.generate_pdf("Verify", "# Heading\n\n- one\n- two\n\nBody text.", proj / "out.pdf")
        check("PDF renders", pdf.read_bytes()[:5] == b"%PDF-", f"{pdf.stat().st_size} bytes")

        # 6. Real hosted code-gen (only if opted in).
        if router.hosted_available():
            try:
                gp = codegen.generate_project("a simple hello-world landing page with a heading and a button")
                has_html = any(f.path.endswith(".html") for f in gp.files) and any("<" in f.content for f in gp.files)
                check("hosted code-gen returns a web project", bool(gp.files) and has_html,
                      f"{len(gp.files)} files: {[f.path for f in gp.files][:5]}")
            except Exception as exc:
                check("hosted code-gen returns a web project", False, str(exc)[:100])
        else:
            skip("hosted code-gen", "hosting not enabled (would run on the local model)")
    finally:
        shutil.rmtree(proj, ignore_errors=True)

    summarize_and_exit()


if __name__ == "__main__":
    main()
