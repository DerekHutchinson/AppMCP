"""Optional git-backed audit trail of apps as agents build them.

When ``settings.git_history_enabled`` is true, every lifecycle change to an app
(create / update / publish / unpublish / delete) writes that app's HTML and a
small JSON metadata file into a git working tree, commits the change, and
optionally pushes it to a remote branch. This gives a reviewable history of what
the AI agent has produced over time.

Design notes:
  * This is a side-channel. Every operation is wrapped so a git failure is
    logged and swallowed — it must NEVER break app create/update/publish.
  * All work runs against an existing clone at ``git_history_repo_path``. We do
    not init or clone; the operator sets up that clone once with whatever push
    credentials its remote needs (token in the URL or a mounted SSH key).
  * Layout inside the repo:
        <subdir>/<slug>/index.html   # the published HTML
        <subdir>/<slug>/app.json     # title/datasource/status/who/when
  * git operations are synchronous (subprocess); async callers use to_thread.
"""
import json
import logging
import os
import subprocess
from datetime import datetime, timezone

from config import settings

log = logging.getLogger("appmcp.history")


def _run(args: list[str], cwd: str, *, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _commit_env() -> dict:
    env = dict(os.environ)
    name = settings.git_history_author_name
    email = settings.git_history_author_email
    env["GIT_AUTHOR_NAME"] = name
    env["GIT_AUTHOR_EMAIL"] = email
    env["GIT_COMMITTER_NAME"] = name
    env["GIT_COMMITTER_EMAIL"] = email
    return env


def _ensure_branch(repo: str, branch: str, env: dict) -> bool:
    """Make `branch` the checked-out branch. Returns False on failure."""
    if _run(["checkout", branch], repo, env=env).returncode == 0:
        return True
    if _run(["checkout", "-b", branch], repo, env=env).returncode == 0:
        return True
    log.warning("git history: could not checkout/create branch %s", branch)
    return False


def _record(action: str, slug: str, app: dict | None) -> None:
    """Synchronous core: write files, commit, optionally push. Never raises."""
    repo = settings.git_history_repo_path.strip()
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        log.warning("git history enabled but %r is not a git working tree", repo)
        return

    env = _commit_env()
    branch = settings.git_history_branch
    if not _ensure_branch(repo, branch, env):
        return

    rel_dir = os.path.join(settings.git_history_subdir, slug)
    abs_dir = os.path.join(repo, rel_dir)

    if action == "delete":
        if os.path.isdir(abs_dir):
            _run(["rm", "-r", "--ignore-unmatch", "--", rel_dir], repo, env=env)
    else:
        os.makedirs(abs_dir, exist_ok=True)
        with open(os.path.join(abs_dir, "index.html"), "w", encoding="utf-8") as fh:
            fh.write((app or {}).get("html", ""))
        meta = {
            k: (app or {}).get(k)
            for k in (
                "slug", "title", "description", "datasource", "status",
                "created_by", "created_at", "updated_at",
                "published_by", "published_at",
            )
        }
        meta["history_action"] = action
        meta["history_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        with open(os.path.join(abs_dir, "app.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, sort_keys=True)
        _run(["add", "--", rel_dir], repo, env=env)

    who = (app or {}).get("created_by") or "unknown"
    title = (app or {}).get("title") or slug
    msg = f"{action} app: {title} ({slug}) by {who}"
    commit = _run(["commit", "-m", msg], repo, env=env)
    if commit.returncode != 0:
        # rc!=0 usually means "nothing to commit" — informational, not an error.
        log.info("git history: no commit for %s/%s (%s)", action, slug,
                 (commit.stdout or commit.stderr).strip()[:200])
        return

    if settings.git_history_push:
        push = _run(["push", settings.git_history_remote, branch], repo, env=env)
        if push.returncode != 0:
            log.warning("git history: push failed for %s: %s", slug,
                        (push.stderr or push.stdout).strip()[:300])


def record(action: str, slug: str, app: dict | None) -> None:
    """Entry point. Guarded + fully isolated: logs and swallows everything."""
    if not settings.git_history_enabled:
        return
    try:
        _record(action, slug, app)
    except Exception as exc:  # noqa: BLE001 - history must never break the app op
        log.warning("git history failed for %s/%s: %s", action, slug, exc)
