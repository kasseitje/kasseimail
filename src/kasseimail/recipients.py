"""The recipient list: a CSV or an XLSX, turned into rows a template can render.

One row is one mail. Every column becomes a variable under a name a template can actually type --
a column headed "First Name" is `{{ first_name }}` -- and the values are cleaned up on the way in,
because a spreadsheet hands back things that render badly:

- **An integral float becomes an int.** Excel stores an invoice number as `1001.0`, and
  `{{ invoice_no }}` then puts *1001.0* in front of a customer. This is the single most likely way
  a run goes out looking wrong while every other check passes.
- A `datetime` stays a `datetime`, for the `date` filter to format. Rendering one raw gives
  `2026-03-01 00:00:00`.
- Trailing whitespace goes; a cell that is only whitespace becomes empty.

Nothing here validates a template or touches the network. What it does do is say which rows have no
usable address, because that is a property of the file and not of the run.
"""

import csv
import re

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from loguru import logger

#: names the run puts into the context itself. A column normalising to one of these would be
#: overwritten, so it is reported rather than silently lost.
RESERVED_FIELDS = ("row", "attachments")

#: deliberately loose. This is a typo check -- a missing `@`, a stray comma, a space in the middle
#: -- not an RFC 5322 parser. Graph is the authority on whether an address exists; the job here is
#: to catch the ones that were never going to work before a run starts.
EMAIL_PATTERN = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]{2,}$")

#: a file this large is a mistake -- the wrong file, or a sheet with a million empty rows. Refusing
#: beats spending four minutes reading it and then asking about sending 900 000 mails.
MAX_ROWS = 100_000


class RecipientProblem(Exception):
    """A file that cannot be read, or one with no usable columns."""


def normalise_header(header: str, index: int) -> str:
    """"First Name " -> "first_name". An unnamed column becomes `column_3`.

    Lowercase because a template should not have to remember whether the spreadsheet said "Email"
    or "email"; underscores because `{{ first name }}` is not valid Jinja and the person editing
    the template should not have to know that `{{ row['First Name'] }}` is the way round it.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "_", str(header or "").strip().lower()).strip("_")
    if not cleaned:
        return f"column_{index + 1}"
    if cleaned[0].isdigit():
        cleaned = f"c_{cleaned}"
    return cleaned


def clean_value(value):
    """One cell, as a template should see it. See the module docstring for why each case is here."""
    if value is None:
        return ""

    if isinstance(value, bool):
        return value

    if isinstance(value, float):
        # -- Excel stores every number as a float; `1001.0` is an invoice number, not a quantity.
        #    `is_integer` is exact, so 1001.5 is left alone.
        if value.is_integer():
            return int(value)
        return value

    if isinstance(value, (int, datetime, date)):
        return value

    text = str(value).strip()
    return text


@dataclass
class Recipient:
    """One row of the file."""

    row: int
    email: str
    key: str
    fields: dict = field(default_factory=dict)

    def context(self, *, attachments: list[str] | None = None) -> dict:
        """What a template renders against: the columns, plus what the run knows.

        `email` is the *resolved* address, which under `--test-to` is not the one in the
        spreadsheet. A template printing it in a footer then says where the mail actually went,
        which is what somebody reading a rehearsal wants to see.
        """
        return dict(self.fields) | {
            "row": self.row,
            "email": self.email,
            "attachments": list(attachments or []),
        }


@dataclass
class RecipientTable:
    """The loaded file: the rows, and what was noticed while reading it."""

    path: Path
    headers: list[str]
    original_headers: list[str]
    rows: list[Recipient]
    email_column: str
    key_column: str
    without_email: list[int] = field(default_factory=list)
    invalid_email: list[tuple[int, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rows)

    def duplicate_keys(self) -> list[tuple[str, list[int]]]:
        """Keys appearing on more than one row.

        Not an error and not merged into one mail: households share a mailbox, and sending three
        people one mail that is mostly about somebody else is worse than sending three mails. It
        *is* reported, so three mails to one address do not read as a bug.
        """
        seen: dict[str, list[int]] = {}
        for recipient in self.rows:
            seen.setdefault(recipient.key.lower(), []).append(recipient.row)
        return sorted((key, rows) for key, rows in seen.items() if len(rows) > 1)


# ---------------------------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------------------------

def sheet_names(path: str | Path) -> list[str]:
    """The sheets in an XLSX, for the window's dropdown. A CSV has none."""
    target = Path(path)
    if target.suffix.lower() not in (".xlsx", ".xlsm"):
        return []

    import openpyxl

    book = openpyxl.load_workbook(target, read_only=True, data_only=True)
    try:
        return list(book.sheetnames)
    finally:
        book.close()


def _read_csv(path: Path) -> tuple[list, list[list]]:
    """Header row and data rows out of a delimited text file.

    `utf-8-sig` because Excel writes a BOM and `csv` would otherwise fold it into the first header,
    leaving a column named `_email` that no template matches. The delimiter is sniffed because a
    Belgian Excel writes `;` and an export from anywhere else writes `,`, and asking the user which
    one their file uses is asking them to open it in a text editor.
    """
    with path.open(encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(8192)
        handle.seek(0)

        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        except csv.Error:
            # -- a single-column file has nothing to sniff, and that is not an error.
            dialect = csv.get_dialect("excel")

        rows = [row for row in csv.reader(handle, dialect) if any(str(cell).strip() for cell in row)]

    if not rows:
        raise RecipientProblem(f"{path.name} is empty")
    return rows[0], rows[1:]


def _read_xlsx(path: Path, sheet: str | None) -> tuple[list, list[list]]:
    import openpyxl

    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet:
            if sheet not in book.sheetnames:
                raise RecipientProblem(
                    f"{path.name} has no sheet called '{sheet}'. "
                    f"It has: {', '.join(book.sheetnames)}"
                )
            worksheet = book[sheet]
        else:
            worksheet = book.worksheets[0]

        rows = [
            list(values)
            for values in worksheet.iter_rows(values_only=True)
            if any(value is not None and str(value).strip() for value in values)
        ]
    finally:
        book.close()

    if not rows:
        raise RecipientProblem(f"{path.name} is empty")
    return rows[0], rows[1:]


def load(
    path: str | Path,
    *,
    sheet: str | None = None,
    email_column: str = "email",
    key_column: str | None = None,
) -> RecipientTable:
    """Read a CSV or XLSX into rows. Raises `RecipientProblem` on a file that cannot be used."""
    target = Path(path).expanduser()
    if not target.is_file():
        raise RecipientProblem(f"No such file: {target}")

    suffix = target.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        header_row, data_rows = _read_xlsx(target, sheet)
    elif suffix in (".csv", ".tsv", ".txt"):
        header_row, data_rows = _read_csv(target)
    else:
        raise RecipientProblem(
            f"Cannot read {target.name}: expected .csv, .tsv or .xlsx. "
            "An .xls from an old Excel has to be saved as .xlsx first."
        )

    if len(data_rows) > MAX_ROWS:
        raise RecipientProblem(
            f"{target.name} has {len(data_rows)} rows, more than the {MAX_ROWS} this will read. "
            "That is usually the wrong file or a sheet with empty rows below the data."
        )

    original_headers = [str(cell).strip() if cell is not None else "" for cell in header_row]
    headers = [normalise_header(cell, index) for index, cell in enumerate(header_row)]

    warnings = _header_warnings(headers, original_headers)

    email_key = normalise_header(email_column, 0)
    if email_key not in headers:
        raise RecipientProblem(
            f"{target.name} has no column '{email_column}'.\n"
            f"Columns found: {', '.join(headers) or 'none'}\n"
            "Name the right one with --email-column."
        )

    key_key = normalise_header(key_column, 0) if key_column else email_key
    if key_key not in headers:
        raise RecipientProblem(
            f"{target.name} has no column '{key_column}' to key the run on.\n"
            f"Columns found: {', '.join(headers)}"
        )

    rows, without_email, invalid_email = [], [], []

    for offset, values in enumerate(data_rows):
        # -- 1-based and counting the header, so the number matches what the spreadsheet shows in
        #    its row gutter. An off-by-one here sends somebody to the wrong line of a 400-row file.
        row_number = offset + 2

        padded = list(values) + [None] * (len(headers) - len(values))
        fields = {name: clean_value(value) for name, value in zip(headers, padded)}

        address = str(fields.get(email_key, "")).strip()
        if not address:
            without_email.append(row_number)
        elif not EMAIL_PATTERN.match(address):
            invalid_email.append((row_number, address))

        key = str(fields.get(key_key, "")).strip() or address or f"row-{row_number}"
        rows.append(Recipient(row=row_number, email=address, key=key, fields=fields))

    logger.debug(
        "read {} rows from {} ({} columns: {})",
        len(rows), target.name, len(headers), ", ".join(headers),
    )

    return RecipientTable(
        path=target,
        headers=headers,
        original_headers=original_headers,
        rows=rows,
        email_column=email_key,
        key_column=key_key,
        without_email=without_email,
        invalid_email=invalid_email,
        warnings=warnings,
    )


def _header_warnings(headers: list[str], original: list[str]) -> list[str]:
    """Two columns that collapse to one name, and columns the run would shadow."""
    warnings = []

    seen: dict[str, list[str]] = {}
    for name, source in zip(headers, original):
        seen.setdefault(name, []).append(source or "(unnamed)")

    for name, sources in seen.items():
        if len(sources) > 1:
            warnings.append(
                f"columns {', '.join(repr(s) for s in sources)} all become '{name}'; "
                "only the last one is readable in a template"
            )

    for name in RESERVED_FIELDS:
        if name in seen:
            warnings.append(
                f"column '{name}' is overwritten by the run's own value for {{{{ {name} }}}}"
            )

    return warnings
