"""Async git subprocess wrappers (stdlib only, no GitPython).

Security: every call uses explicit argv with ``asyncio.create_subprocess_exec``.
``shell=True`` is never used. All refs are passed as positional arguments,
never interpolated into a command string.

EXEC-04 security root of trust:
    ``is_sha_on_main`` runs ``git merge-base --is-ancestor <sha>
    refs/remotes/origin/main``. Only SHAs already reachable from
    ``origin/main`` can be installed. A compromised main service (pv-proxy)
    cannot request an arbitrary SHA.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger(component="updater_root.git_ops")

GIT_DEFAULT_TIMEOUT_S = 60.0
GIT_FETCH_TIMEOUT_S = 120.0


class GitTimeoutError(Exception):
    """Raised when a git subprocess exceeds its timeout."""


class GitOpsError(Exception):
    """Raised on unexpected git invocation shapes."""


@dataclass
class GitResult:
    """Result of a ``git`` subprocess invocation.

    Non-zero ``returncode`` is NOT an exception — the caller decides
    whether a non-zero exit is a normal "no" answer (e.g. merge-base
    --is-ancestor returning 1 for "not an ancestor") or a hard failure.
    """

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


async def run_git(
    *args: str,
    cwd: Path,
    timeout_s: float = GIT_DEFAULT_TIMEOUT_S,
) -> GitResult:
    """Run ``git <args>`` as a subprocess and capture output.

    Uses ``asyncio.create_subprocess_exec`` with explicit argv — no shell,
    no string interpolation. On timeout, the process is killed and
    :class:`GitTimeoutError` is raised. Non-zero exit codes are returned
    in the result for the caller to inspect.
    """
    log.info("git_exec", args=args, cwd=str(cwd))
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )
    except asyncio.TimeoutError as e:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        raise GitTimeoutError(
            f"git {' '.join(args)} timed out after {timeout_s}s"
        ) from e
    return GitResult(
        returncode=proc.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


async def git_fetch(repo_dir: Path, remote: str = "origin") -> GitResult:
    """Run ``git -C <repo_dir> fetch --tags --quiet <remote>``."""
    return await run_git(
        "-C",
        str(repo_dir),
        "fetch",
        "--tags",
        "--quiet",
        remote,
        cwd=repo_dir,
        timeout_s=GIT_FETCH_TIMEOUT_S,
    )


async def git_set_origin(repo_dir: Path, url: str) -> GitResult:
    """Run ``git -C <repo_dir> remote set-url origin <url>``.

    DESIGN v8.3.1: ``git clone --shared`` points the new release's origin at
    the LOCAL previous-release directory, so ``git fetch origin`` never
    reaches GitHub and the ``is_sha_on_main`` security anchor validates
    against a frozen ref (live failure 2026-06-11: ``sha_not_on_main`` for a
    SHA that was on real main for a day). Rewiring origin to the canonical
    repo URL right after the clone keeps the anchor anchored. The URL comes
    from root-owned config only (constant + root-owned env override), never
    from format-sniffing an existing origin — see the design doc's F3 note.
    """
    return await run_git(
        "-C",
        str(repo_dir),
        "remote",
        "set-url",
        "origin",
        url,
        cwd=repo_dir,
    )


async def git_dissociate(repo_dir: Path) -> GitResult:
    """Make a ``--shared`` clone self-contained: repack all objects locally,
    then drop the alternates pointer.

    DESIGN v8.3.1/F-live: release clones share their object store with the
    PREVIOUS release dir via ``objects/info/alternates``. Release retention
    later deletes old release dirs — which silently corrupts every child
    clone (live failure 2026-06-11: ``fatal: unresolved deltas`` on fetch,
    update permanently wedged). ``repack -a -d`` copies every reachable
    object into the clone itself; afterwards the alternates file is removed.
    Costs a few seconds + the repo's pack size per release, buys retention
    safety by construction.
    """
    result = await run_git(
        "-C",
        str(repo_dir),
        "repack",
        "-a",
        "-d",
        cwd=repo_dir,
        timeout_s=GIT_FETCH_TIMEOUT_S,
    )
    if result.ok:
        alternates = repo_dir / ".git" / "objects" / "info" / "alternates"
        try:
            alternates.unlink(missing_ok=True)
        except OSError:
            pass  # repack already made us self-sufficient; pointer is moot
    return result


async def git_fetch_sha(repo_dir: Path, sha: str) -> GitResult:
    """Run ``git -C <repo_dir> fetch --quiet origin <sha>``.

    DESIGN v8.3.5: a ``--shared`` clone inherits only the SOURCE's stale
    ``refs/heads`` — the target sha was fetched earlier into the SOURCE's
    ``refs/remotes/origin/main`` and is unreachable from any cloned ref.
    ``git_dissociate``'s ``repack -a -d`` is reachability-based, so it drops
    the target sha's objects entirely and the subsequent checkout fails
    (live failure 2026-06-11 11:17Z). Fetching the raw sha from origin works
    because the target is always reachable from a branch head — anchored by
    the earlier ``is_sha_on_main`` merge-base check against origin/main.
    By construction: the clone owns the target objects regardless of
    alternates/reachability state.
    """
    return await run_git(
        "-C",
        str(repo_dir),
        "fetch",
        "--quiet",
        "origin",
        sha,
        cwd=repo_dir,
        timeout_s=GIT_FETCH_TIMEOUT_S,
    )


async def git_rev_parse(repo_dir: Path, ref: str) -> str | None:
    """Resolve ``ref`` to a SHA. Returns ``None`` on non-zero exit."""
    r = await run_git("-C", str(repo_dir), "rev-parse", ref, cwd=repo_dir)
    if not r.ok:
        return None
    return r.stdout.strip() or None


async def is_sha_on_main(
    repo_dir: Path,
    sha: str,
    main_ref: str = "refs/remotes/origin/main",
) -> bool:
    """EXEC-04: security root of trust.

    Returns True iff ``sha`` is an ancestor of ``main_ref``. This is the
    only reason a compromised pv-proxy cannot install arbitrary code: the
    updater only accepts SHAs already in ``origin/main``'s history.

    Any non-zero exit code (including "not an ancestor" = 1 and "bad
    object" = 128) results in ``False`` — the function is conservative
    by design.
    """
    r = await run_git(
        "-C",
        str(repo_dir),
        "merge-base",
        "--is-ancestor",
        sha,
        main_ref,
        cwd=repo_dir,
    )
    return r.ok


async def git_clone_shared(source: Path, dest: Path) -> GitResult:
    """Run ``git clone --shared --no-checkout <source> <dest>``.

    ``--shared`` reuses the object store from the existing release, which
    makes local release-to-release clones roughly an order of magnitude
    faster than a full clone. ``--no-checkout`` defers working tree
    population to a later ``git checkout --detach`` step.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    return await run_git(
        "clone",
        "--shared",
        "--no-checkout",
        str(source),
        str(dest),
        cwd=source.parent,
        timeout_s=GIT_FETCH_TIMEOUT_S,
    )


async def git_checkout_detach(repo_dir: Path, sha: str) -> GitResult:
    """Run ``git -C <repo_dir> checkout --detach --quiet <sha>``.

    Detached HEAD avoids any branch state on the new release dir — the
    release identity IS the SHA.
    """
    return await run_git(
        "-C",
        str(repo_dir),
        "checkout",
        "--detach",
        "--quiet",
        sha,
        cwd=repo_dir,
    )


async def git_status_porcelain(repo_dir: Path) -> str:
    """Run ``git -C <repo_dir> status --porcelain`` and return stdout.

    Returns the raw porcelain output (empty string == clean tree). Used
    by the migration path to refuse dirty-tree installs.
    """
    r = await run_git(
        "-C",
        str(repo_dir),
        "status",
        "--porcelain",
        cwd=repo_dir,
    )
    return r.stdout
