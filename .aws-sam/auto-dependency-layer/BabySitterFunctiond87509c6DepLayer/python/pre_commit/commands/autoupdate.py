from __future__ import annotations

import concurrent.futures
import os.path
import re
import tempfile
from typing import Any
from typing import NamedTuple
from typing import Sequence

import pre_commit.constants as C
from pre_commit import git
from pre_commit import output
from pre_commit import xargs
from pre_commit.clientlib import InvalidManifestError
from pre_commit.clientlib import load_config
from pre_commit.clientlib import load_manifest
from pre_commit.clientlib import LOCAL
from pre_commit.clientlib import META
from pre_commit.commands.migrate_config import migrate_config
from pre_commit.util import CalledProcessError
from pre_commit.util import cmd_output
from pre_commit.util import cmd_output_b
from pre_commit.yaml import yaml_dump
from pre_commit.yaml import yaml_load


class RevInfo(NamedTuple):
    repo: str
    rev: str
    frozen: str | None = None
    hook_ids: frozenset[str] = frozenset()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> RevInfo:
        return cls(config["repo"], config["rev"])

    def update(self, tags_only: bool, freeze: bool) -> RevInfo:
        with tempfile.TemporaryDirectory() as tmp:
            _git = ("git", *git.NO_FS_MONITOR, "-C", tmp)

            if tags_only:
                tag_opt = "--abbrev=0"
            else:
                tag_opt = "--exact"
            tag_cmd = (*_git, "describe", "FETCH_HEAD", "--tags", tag_opt)

            git.init_repo(tmp, self.repo)
            cmd_output_b(*_git, "config", "extensions.partialClone", "true")
            cmd_output_b(
                *_git,
                "fetch",
                "origin",
                "HEAD",
                "--quiet",
                "--filter=blob:none",
                "--tags",
            )

            try:
                rev = cmd_output(*tag_cmd)[1].strip()
            except CalledProcessError:
                rev = cmd_output(*_git, "rev-parse", "FETCH_HEAD")[1].strip()
            else:
                if tags_only:
                    rev = git.get_best_candidate_tag(rev, tmp)

            frozen = None
            if freeze:
                exact = cmd_output(*_git, "rev-parse", rev)[1].strip()
                if exact != rev:
                    rev, frozen = exact, rev

            try:
                # workaround for windows -- see #2865
                cmd_output_b(*_git, "show", f"{rev}:{C.MANIFEST_FILE}")
                cmd_output(*_git, "checkout", rev, "--", C.MANIFEST_FILE)
            except CalledProcessError:
                pass  # this will be caught by manifest validating code
            try:
                manifest = load_manifest(os.path.join(tmp, C.MANIFEST_FILE))
            except InvalidManifestError as e:
                raise RepositoryCannotBeUpdatedError(f"[{self.repo}] {e}")
            else:
                hook_ids = frozenset(hook["id"] for hook in manifest)

        return self._replace(rev=rev, frozen=frozen, hook_ids=hook_ids)


class RepositoryCannotBeUpdatedError(RuntimeError):
    pass


def _check_hooks_still_exist_at_rev(
    repo_config: dict[str, Any],
    info: RevInfo,
) -> None:
    # See if any of our hooks were deleted with the new commits
    hooks = {hook["id"] for hook in repo_config["hooks"]}
    hooks_missing = hooks - info.hook_ids
    if hooks_missing:
        raise RepositoryCannotBeUpdatedError(
            f"[{info.repo}] Cannot update because the update target is "
            f'missing these hooks: {", ".join(sorted(hooks_missing))}',
        )


def _update_one(
    i: int,
    repo: dict[str, Any],
    *,
    tags_only: bool,
    freeze: bool,
) -> tuple[int, RevInfo, RevInfo]:
    old = RevInfo.from_config(repo)
    new = old.update(tags_only=tags_only, freeze=freeze)
    _check_hooks_still_exist_at_rev(repo, new)
    return i, old, new


REV_LINE_RE = re.compile(r'^(\s+)rev:(\s*)([\'"]?)([^\s#]+)(.*)(\r?\n)$')


def _original_lines(
    path: str,
    rev_infos: list[RevInfo | None],
    retry: bool = False,
) -> tuple[list[str], list[int]]:
    """detect `rev:` lines or reformat the file"""
    with open(path, newline="") as f:
        original = f.read()

    lines = original.splitlines(True)
    idxs = [i for i, line in enumerate(lines) if REV_LINE_RE.match(line)]
    if len(idxs) == len(rev_infos):
        return lines, idxs
    elif retry:
        raise AssertionError("could not find rev lines")
    else:
        with open(path, "w") as f:
            f.write(yaml_dump(yaml_load(original)))
        return _original_lines(path, rev_infos, retry=True)


def _write_new_config(path: str, rev_infos: list[RevInfo | None]) -> None:
    lines, idxs = _original_lines(path, rev_infos)

    for idx, rev_info in zip(idxs, rev_infos):
        if rev_info is None:
            continue
        match = REV_LINE_RE.match(lines[idx])
        assert match is not None
        new_rev_s = yaml_dump({"rev": rev_info.rev}, default_style=match[3])
        new_rev = new_rev_s.split(":", 1)[1].strip()
        if rev_info.frozen is not None:
            comment = f"  # frozen: {rev_info.frozen}"
        elif match[5].strip().startswith("# frozen:"):
            comment = ""
        else:
            comment = match[5]
        lines[idx] = f"{match[1]}rev:{match[2]}{new_rev}{comment}{match[6]}"

    with open(path, "w", newline="") as f:
        f.write("".join(lines))


def autoupdate(
    config_file: str,
    tags_only: bool,
    freeze: bool,
    repos: Sequence[str] = (),
    jobs: int = 1,
) -> int:
    """Auto-update the pre-commit config to the latest versions of repos."""
    migrate_config(config_file, quiet=True)
    changed = False
    retv = 0

    config_repos = [
        repo
        for repo in load_config(config_file)["repos"]
        if repo["repo"] not in {LOCAL, META}
    ]

    rev_infos: list[RevInfo | None] = [None] * len(config_repos)
    jobs = jobs or xargs.cpu_count()  # 0 => number of cpus
    jobs = min(jobs, len(repos) or len(config_repos))  # max 1-per-thread
    jobs = max(jobs, 1)  # at least one thread
    with concurrent.futures.ThreadPoolExecutor(jobs) as exe:
        futures = [
            exe.submit(
                _update_one,
                i,
                repo,
                tags_only=tags_only,
                freeze=freeze,
            )
            for i, repo in enumerate(config_repos)
            if not repos or repo["repo"] in repos
        ]
        for future in concurrent.futures.as_completed(futures):
            try:
                i, old, new = future.result()
            except RepositoryCannotBeUpdatedError as e:
                output.write_line(str(e))
                retv = 1
            else:
                if new.rev != old.rev:
                    changed = True
                    if new.frozen:
                        new_s = f"{new.frozen} (frozen)"
                    else:
                        new_s = new.rev
                    msg = f"updating {old.rev} -> {new_s}"
                    rev_infos[i] = new
                else:
                    msg = "already up to date!"

                output.write_line(f"[{old.repo}] {msg}")

    if changed:
        _write_new_config(config_file, rev_infos)

    return retv
