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
RESERVED_FIELDS = ("row", "attachments", "rows", "count")

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
    #: the spreadsheet rows this recipient was built from -- one, unless it is a group. Kept whole
    #: rather than only aggregated, because a template that wants a table of the stands somebody
    #: booked needs the rows, and because attachments resolve per row and not per group.
    members: list[dict] = field(default_factory=list)
    source_rows: list[int] = field(default_factory=list)

    def __post_init__(self):
        if not self.members:
            self.members = [dict(self.fields)]
        if not self.source_rows:
            self.source_rows = [self.row]

    @property
    def count(self) -> int:
        """How many spreadsheet rows went into this message."""
        return len(self.members)

    @property
    def grouped(self) -> bool:
        return self.count > 1

    def context(self, *, attachments: list[str] | None = None) -> dict:
        """What a template renders against: the columns, plus what the run knows.

        `email` is the *resolved* address, which under `--test-to` is not the one in the
        spreadsheet. A template printing it in a footer then says where the mail actually went,
        which is what somebody reading a rehearsal wants to see.

        `rows` and `count` are what make grouping useful. The aggregated fields give you
        `{{ stand_number }}` as "12, 14, 19", which is the common case; `rows` gives you the
        rows behind it, so a template can lay them out as a list or a table with each stand's own
        size and price beside it. Without them, grouping could only ever produce joined strings.
        """
        return dict(self.fields) | {
            "row": self.row,
            "email": self.email,
            "attachments": list(attachments or []),
            "rows": [dict(member) for member in self.members],
            "count": self.count,
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
    #: the column rows were collapsed on, empty when each row is its own message.
    group_by: str = ""

    @property
    def grouped(self) -> bool:
        return bool(self.group_by)

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


# ---------------------------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------------------------
#
# Several rows, one message. A flea market books stands one row at a time, so the same person turns
# up three times with three stand numbers -- and they should get one mail listing all three, not
# three mails each mentioning one.
#
# Grouping collapses those rows into a single recipient and *aggregates* the other columns. What the
# template then sees is both: `{{ stand_number }}` as "12, 14, 19", and `rows` as the three rows
# themselves, for a template that wants to lay them out with each stand's size and price beside it.

#: how a column's values are combined across the rows of a group.
AGGREGATORS = ("auto", "first", "list", "unique", "sum", "count")

DEFAULT_AGGREGATOR = "auto"
DEFAULT_SEPARATOR = ", "

#: what each one does, for `--help` and for the window's dropdown.
AGGREGATOR_HELP = {
    "auto": "one value if every row agrees, otherwise the distinct values joined",
    "first": "the first row's value",
    "list": "every row's value, joined, in order",
    "unique": "the distinct values, joined, in order of first appearance",
    "sum": "the numbers added up",
    "count": "how many rows had a value",
}


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d") if value.time() == datetime.min.time() else str(value)
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value).strip()


def _numeric(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def aggregate_values(values: list, how: str = DEFAULT_AGGREGATOR,
                     separator: str = DEFAULT_SEPARATOR):
    """Combine one column's values across the rows of a group.

    `auto` is the default because it needs no configuration and is what people mean: a column that
    is the same on every row of the group -- the name, the address -- collapses to that one value,
    and a column that differs -- the stand number -- becomes the list. Grouping a flea market
    spreadsheet by e-mail therefore does the right thing with nothing configured, and the explicit
    aggregators are there for when it does not.
    """
    if how not in AGGREGATORS:
        raise RecipientProblem(
            f"Unknown aggregator '{how}'. Use one of: {', '.join(AGGREGATORS)}"
        )

    present = [value for value in values if _as_text(value) != ""]

    if how == "count":
        return len(present)

    if how == "sum":
        numbers = [number for number in (_numeric(value) for value in present)
                   if number is not None]
        if not numbers:
            return ""
        total = sum(numbers)
        # -- whole numbers stay whole: a total of 3 stands must not read as 3.0.
        return int(total) if float(total).is_integer() else total

    if not present:
        return ""

    if how == "first":
        return present[0]

    texts = [_as_text(value) for value in present]

    if how == "list":
        return separator.join(texts)

    distinct = list(dict.fromkeys(texts))

    if how == "unique":
        return separator.join(distinct)

    # -- auto
    if len(distinct) == 1:
        # -- the original value, not its text: a date stays a date so `| date(...)` still works.
        return present[0]
    return separator.join(distinct)


@dataclass
class GroupSpec:
    """How to collapse several rows into one message."""

    by: str = ""
    aggregators: dict = field(default_factory=dict)
    separator: str = DEFAULT_SEPARATOR

    @property
    def enabled(self) -> bool:
        return bool(self.by)

    def how(self, column: str) -> str:
        return self.aggregators.get(column, DEFAULT_AGGREGATOR)

    @classmethod
    def parse(cls, by: str | None, pairs=(), separator: str = DEFAULT_SEPARATOR) -> "GroupSpec":
        """From the command line: `--group-by email --aggregate stand_number=list`."""
        aggregators = {}
        for pair in pairs or ():
            column, _, how = str(pair).partition("=")
            column, how = column.strip(), how.strip().lower()
            if not column or not how:
                raise RecipientProblem(
                    f"--aggregate wants COLUMN=HOW, not {pair!r}. "
                    f"HOW is one of: {', '.join(AGGREGATORS)}"
                )
            if how not in AGGREGATORS:
                raise RecipientProblem(
                    f"Unknown aggregator '{how}' for column '{column}'. "
                    f"Use one of: {', '.join(AGGREGATORS)}"
                )
            aggregators[normalise_header(column, 0)] = how

        return cls(by=normalise_header(by, 0) if by else "", aggregators=aggregators,
                   separator=separator)


def group(table: RecipientTable, spec: GroupSpec) -> RecipientTable:
    """One recipient per distinct value of the group column. Returns a new table.

    **A blank group value is never grouped.** Rows with no value in that column would otherwise all
    collapse into a single recipient keyed on the empty string -- which is one mail standing for
    everybody the file failed to identify, and the worst possible way to lose them.

    The address is taken from the first row of the group rather than aggregated: a joined list of
    addresses is not something Graph can send to. A group that spans two different addresses is a
    warning, because it means the group column is not the one you wanted.
    """
    if not spec.enabled:
        return table

    if spec.by not in table.headers:
        raise RecipientProblem(
            f"Cannot group by '{spec.by}': {table.path.name} has "
            f"{', '.join(table.headers)}"
        )

    for column in spec.aggregators:
        if column not in table.headers:
            raise RecipientProblem(
                f"Cannot aggregate '{column}': {table.path.name} has "
                f"{', '.join(table.headers)}"
            )

    buckets: dict[object, list[Recipient]] = {}
    for recipient in table.rows:
        value = _as_text(recipient.fields.get(spec.by, ""))
        # -- a blank groups only with itself; the row number makes the bucket unique.
        bucket = value.lower() if value else ("", recipient.row)
        buckets.setdefault(bucket, []).append(recipient)

    grouped, warnings = [], list(table.warnings)

    for members in buckets.values():
        first = members[0]
        fields = {}
        for column in table.headers:
            values = [member.fields.get(column, "") for member in members]
            fields[column] = aggregate_values(values, spec.how(column), spec.separator)

        addresses = list(dict.fromkeys(
            _as_text(member.email) for member in members if _as_text(member.email)
        ))
        if len(addresses) > 1:
            warnings.append(
                f"the group '{_as_text(first.fields.get(spec.by))}' spans "
                f"{len(addresses)} addresses ({', '.join(addresses)}); it goes to the first"
            )

        # -- the address is never the aggregated value: it has to stay one address.
        fields[table.email_column] = addresses[0] if addresses else ""

        address = addresses[0] if addresses else ""
        # -- the key names this message in the report and is half of what --resume matches on, so
        #    it has to be there even for a group with no address at all.
        key = _as_text(fields.get(table.key_column, "")) or address or f"row-{first.row}"

        grouped.append(Recipient(
            row=first.row,
            email=address,
            key=key,
            fields=fields,
            members=[dict(member.fields) for member in members],
            source_rows=[member.row for member in members],
        ))

    collapsed = len(table.rows) - len(grouped)
    if collapsed:
        logger.info("grouped {} rows into {} message(s) by '{}'",
                    len(table.rows), len(grouped), spec.by)

    return RecipientTable(
        path=table.path,
        headers=table.headers,
        original_headers=table.original_headers,
        rows=grouped,
        email_column=table.email_column,
        key_column=table.key_column,
        without_email=[r.row for r in grouped if not r.email],
        invalid_email=[(r.row, r.email) for r in grouped
                       if r.email and not EMAIL_PATTERN.match(r.email)],
        warnings=warnings,
        group_by=spec.by,
    )
