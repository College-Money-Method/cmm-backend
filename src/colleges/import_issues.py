"""Bounded error/warning collector for the import (response size stays small)."""

from __future__ import annotations

from src.colleges.admin_schemas import ImportIssue, ImportWarning

ISSUE_CAP = 100


class IssueLog:
    """Keeps at most ``ISSUE_CAP`` errors and warnings; ``truncated`` says something was dropped.

    Warnings flagged ``important`` (manual-edit overwrites, renames) are listed before the
    routine ones, so a flood of "new college" lines can never hide them.
    """

    def __init__(self) -> None:
        self.errors: list[ImportIssue] = []
        self.important: list[ImportWarning] = []
        self.routine: list[ImportWarning] = []
        self.truncated = False

    def error(self, row: int | None, column: str | None, message: str) -> None:
        if len(self.errors) < ISSUE_CAP:
            self.errors.append(ImportIssue(row=row, column=column, message=message))
        else:
            self.truncated = True

    def warn(self, row: int | None, college: str | None, message: str, important: bool = False) -> None:
        (self.important if important else self.routine).append(ImportWarning(row=row, college=college, message=message))

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    def warnings(self) -> list[ImportWarning]:
        merged = self.important + self.routine
        if len(merged) > ISSUE_CAP:
            self.truncated = True
        return merged[:ISSUE_CAP]
