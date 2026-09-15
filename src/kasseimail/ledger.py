"""`report.csv` -- what happened to every row, and what a resumed run may skip.

Appended and never overwritten, so a second pass does not erase the trace of the first. That is the
point of the file: a run of three hundred that dies at ninety is *continued*, because sending ninety
people a second copy is worse than losing twenty minutes.

The CSV dialect is the one Excel on a continental machine opens without an import wizard --
semicolons, a BOM, CRLF -- because the person who reads this file reads it in Excel. Free text goes
through `defused` first: a cell starting with `=` is a formula Excel runs when the file is opened,
and these cells hold names and error messages that came from outside.
"""

import csv
import datetime

from dataclasses import dataclass, field
from pathlib import Path

REPORT_NAME = "report.csv"

COLUMNS = [
    "timestamp",
    "row",
    "key",
    "email",
    "template",
    "mode",
    "status",
    "message_id",
    "attachments",
    "note",
]

#: the statuses a row can end in. `ok` is the only one a resumed run skips.
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_NO_ADDRESS = "no address"
STATUS_RENDERED = "rendered"

MODE_DRY_RUN, MODE_DRAFTS, MODE_SEND = "dry-run", "drafts", "send"

#: the characters Excel reads as the start of a formula.
FORMULA_LEAD = ("=", "+", "-", "@")


def defused(value) -> str:
    """A cell Excel will not execute.

    A name like `=cmd|...` in a spreadsheet is a real technique, and every free-text cell here came
    out of somebody else's file or out of an error message. A leading apostrophe is what Excel
    itself uses to mean "this is text".
    """
    text = "" if value is None else str(value)
    if text[:1] in FORMULA_LEAD:
        return "'" + text
    return text


@dataclass
class Entry:
    """One row's outcome."""

    row: int
    key: str
    email: str
    template: str
    mode: str
    status: str
    message_id: str = ""
    attachments: str = ""
    note: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.datetime.now().isoformat(timespec="seconds")
    )

    def as_record(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "row": self.row,
            "key": self.key,
            "email": self.email,
            "template": self.template,
            "mode": self.mode,
            "status": self.status,
            "message_id": self.message_id,
            "attachments": self.attachments,
            "note": self.note,
        }


def report_path(out_dir: str | Path) -> Path:
    return Path(out_dir) / REPORT_NAME


def append(out_dir: str | Path, entries: list[Entry]) -> Path:
    """Write the entries, with a header if the file is new."""
    path = report_path(out_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    fresh = not path.is_file()
    with path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";", lineterminator="\r\n")
        if fresh:
            writer.writerow(COLUMNS)
        for entry in entries:
            record = entry.as_record()
            writer.writerow([defused(record[column]) for column in COLUMNS])

    return path


def already_done(out_dir: str | Path) -> set[tuple[int, str]]:
    """What an earlier run got out the door, for `--resume` to skip: `(row, key)` pairs.

    Only `ok` in a `drafts` or `send` run counts. A dry run put nothing in anybody's mailbox, and a
    failed row is exactly the one a resumed run is for.

    **The row number is half the identity, and it has to be.** The key defaults to the address, and
    an address is deliberately not unique -- a household shares a mailbox and each member gets their
    own mail. Keyed on the address alone, a resumed run would see the first one as done and the
    second person would never get theirs, silently, which is the precise failure `--resume` exists
    to prevent.

    The cost is that editing the spreadsheet between a run and its resume shifts the row numbers and
    the rows are sent again. Resume against the file you ran with; if you must edit it, use
    `--limit` or a fresh output directory instead.
    """
    path = report_path(out_dir)
    if not path.is_file():
        return set()

    done = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for record in csv.DictReader(handle, delimiter=";"):
            if record.get("status") != STATUS_OK or record.get("mode") not in (MODE_DRAFTS,
                                                                              MODE_SEND):
                continue
            key = (record.get("key") or "").strip()
            try:
                row = int(record.get("row") or 0)
            except ValueError:
                continue
            if key and row:
                done.add((row, key))
    return done
