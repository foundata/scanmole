"""Advisory inspection of the output directory for the filename preview.

GTK-free and non-mutating: this only looks at the candidate names the
engine's :func:`~scanmole.naming.output_candidates` yields and reports the
first one that appears free. It never creates a file or a directory, so it
can never consume a counter value or leave an empty reservation behind.

The result is advisory by construction. Only the CLI's exclusive-create
reservation at scan start decides an output name; between a preview and
that reservation another process can take the name, and the CLI then moves
on to the next candidate in this same sequence.
"""

from __future__ import annotations

import os
import stat as stat_module
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

CANDIDATE_LIMIT = 500
"""How many names one look inspects before giving up.

A bound on the number of filesystem lookups, so an advisory preview
cannot walk a directory indefinitely. It does not make an individual
lookup interruptible, and it says nothing about the run: the CLI keeps
searching past this point, so reaching it means the preview has no answer,
never that no free name exists."""

Stat = Callable[[Path], os.stat_result]


@dataclass(frozen=True)
class PreviewOutcome:
    """What one look at the output directory found.

    ``path`` is the first candidate that appeared free; ``reason`` names
    why nothing could be offered instead. Exactly one of them is set, so
    the GTK layer renders a name or a short unavailable state and never
    has to handle an exception.
    """

    path: Path | None = None
    reason: str = ""

    @property
    def available(self) -> bool:
        """Whether a name could be offered."""
        return self.path is not None


def first_free_candidate(
    candidates: Iterable[Path],
    *,
    stat: Stat = os.lstat,
    limit: int = CANDIDATE_LIMIT,
) -> PreviewOutcome:
    """The first candidate that looks free, or why none could be offered.

    ``stat`` defaults to :func:`os.lstat` on purpose: exclusive creation
    fails on a dangling symlink, so following links would promise a name
    the reservation cannot have. A candidate counts as free only once its
    parent has been established as a readable directory in the same look,
    because a ``FileNotFoundError`` from a vanished parent says nothing
    about the name itself.
    """
    parents: dict[Path, str] = {}
    for index, candidate in enumerate(candidates):
        if index >= limit:
            break
        problem = _directory_problem(candidate.parent, stat, parents)
        if problem:
            return PreviewOutcome(reason=problem)
        try:
            stat(candidate)
        except FileNotFoundError:
            # Free, unless the parent went away between the check above
            # and this one, in which case nothing here is knowable.
            parents.pop(candidate.parent, None)
            gone = _directory_problem(candidate.parent, stat, parents)
            return PreviewOutcome(reason=gone) if gone else PreviewOutcome(candidate)
        except OSError:
            return PreviewOutcome(reason="unreadable")
    # Only the preview stopped looking. The reservation walks the same
    # sequence without a bound, so claiming there is no free name here
    # would be a statement about the run that nothing established.
    return PreviewOutcome(reason="search-limit")


def _directory_problem(parent: Path, stat: Stat, cache: dict[Path, str]) -> str:
    """Why ``parent`` cannot hold an output file, or an empty string."""
    if parent in cache:
        return cache[parent]
    try:
        mode = stat(parent).st_mode
    except FileNotFoundError:
        problem = "missing-directory"
    except OSError:
        problem = "unreadable"
    else:
        # A symlink to a directory is a fine place to write; only a
        # non-directory target is not, so this one resolves the link.
        if stat_module.S_ISLNK(mode):
            try:
                mode = os.stat(parent).st_mode
            except OSError:
                cache[parent] = "missing-directory"
                return "missing-directory"
        problem = "" if stat_module.S_ISDIR(mode) else "not-a-directory"
    cache[parent] = problem
    return problem
