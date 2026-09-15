"""Which files go with which mail.

Three sources, combined per row:

1. **A column in the spreadsheet** -- `--attachment-column attachment`. One or more paths in the
   cell, separated by `;` or `|`.
2. **The same file on every mail** -- `--attach handbook.pdf`, repeatable.
3. **A pattern** -- `--attach-pattern "invoices/{{ invoice_no }}.pdf"`, rendered per row like the
   body is, then matched on disk as a glob. `meta.toml`'s `attachments` list feeds this one, so a
   template can carry its own.

Relative paths resolve against `--attachment-root`, which defaults to the directory the spreadsheet
is in -- the arrangement people actually have, a folder with the list and the PDFs next to it.

**A missing file is a preflight failure, not a mid-run surprise.** Finding out at message 40 that
the PDFs stop at 39 leaves you with 39 mails you cannot recall and a decision to make in a hurry.
`--allow-missing-attachments` downgrades it to a per-row skip for the cases where a blank really is
allowed.
"""

from dataclasses import dataclass, field
from pathlib import Path

from kasseimail.config import MAX_ATTACHMENT_BYTES, MAX_INLINE_TOTAL_BYTES
from kasseimail.templates import Template, TemplateProblem

#: what separates two paths inside one spreadsheet cell. A comma is not among them on purpose:
#: filenames contain commas far more often than people expect.
CELL_SEPARATORS = (";", "|")


@dataclass
class Attachment:
    path: Path
    size: int

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class Resolution:
    """The attachments for one row, and what could not be found."""

    attachments: list[Attachment] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_size(self) -> int:
        return sum(item.size for item in self.attachments)

    @property
    def names(self) -> list[str]:
        return [item.name for item in self.attachments]

    @property
    def needs_upload_session(self) -> bool:
        """Too big to ride along in the request body; it has to go up separately.

        The threshold is on the files rather than the encoded request because that is the number a
        person can check against what is on disk.
        """
        return self.total_size > MAX_INLINE_TOTAL_BYTES


@dataclass
class AttachmentSpec:
    """How to find the attachments, before any row is looked at."""

    columns: list[str] = field(default_factory=list)
    common: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    root: Path = field(default_factory=Path.cwd)
    allow_missing: bool = False

    @property
    def is_empty(self) -> bool:
        return not (self.columns or self.common or self.patterns)

    def resolve(self, recipient, template: Template) -> Resolution:
        """The files for one row. Never raises for a missing file -- that is what `missing` is."""
        found = Resolution()
        seen: set[Path] = set()

        for raw in self._candidates(recipient, template, found):
            for path in self._expand(raw, found):
                if path in seen:
                    continue
                seen.add(path)
                self._take(path, found)

        return found

    # -- the three sources --------------------------------------------------------------------

    def _candidates(self, recipient, template: Template, found: Resolution) -> list[str]:
        """Every path-ish string this recipient asks for, patterns already rendered.

        **Columns and patterns are read per member row, not off the aggregated value.** A grouped
        recipient is several spreadsheet rows, and both of those things are per-row facts: three
        stands booked on three rows want three PDFs, and `invoices/{{ stand_number }}.pdf` has to
        render once per stand to name them. Rendered against the group it would be handed
        "12, 14, 19" and go looking for `invoices/12, 14, 19.pdf`.

        For an ungrouped recipient there is exactly one member, so this is the same work it always
        did.
        """
        candidates = list(self.common)

        for member, context in _member_contexts(recipient):
            for column in self.columns:
                cell = member.get(column)
                if cell in (None, ""):
                    continue
                candidates.extend(_split_cell(str(cell)))

            for pattern in self.patterns:
                try:
                    rendered = template.render_string(pattern, context).strip()
                except TemplateProblem as exc:
                    # -- a pattern naming a column that is not there. Reported per row rather than
                    #    raised, so the preflight can list every row it affects in one pass.
                    message = f"attachment pattern {pattern!r}: {exc}"
                    if message not in found.errors:
                        found.errors.append(message)
                    continue
                if rendered:
                    candidates.append(rendered)

        return candidates

    def _expand(self, raw: str, found: Resolution) -> list[Path]:
        """One candidate to zero or more real files, expanding a glob if there is one."""
        text = raw.strip().strip('"').strip("'")
        if not text:
            return []

        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate

        if any(char in text for char in "*?["):
            matches = sorted(p for p in candidate.parent.glob(candidate.name) if p.is_file())
            if not matches:
                found.missing.append(str(candidate))
            return matches

        if not candidate.is_file():
            found.missing.append(str(candidate))
            return []

        return [candidate]

    def _take(self, path: Path, found: Resolution) -> None:
        size = path.stat().st_size
        if size == 0:
            found.errors.append(f"{path.name} is empty (0 bytes)")
            return
        if size > MAX_ATTACHMENT_BYTES:
            found.errors.append(
                f"{path.name} is {_mb(size)}, over the {_mb(MAX_ATTACHMENT_BYTES)} Graph accepts"
            )
            return
        found.attachments.append(Attachment(path=path.resolve(), size=size))


def _member_contexts(recipient):
    """Each spreadsheet row behind this recipient, with a context to render a pattern against.

    The context is the member's own fields over the group's, so `{{ stand_number }}` in a pattern
    is *this* stand while `{{ first_name }}` still resolves even if only the group carries it.
    """
    group_context = recipient.context()

    for member in recipient.members:
        yield member, group_context | dict(member)


def _split_cell(cell: str) -> list[str]:
    parts = [cell]
    for separator in CELL_SEPARATORS:
        parts = [piece for part in parts for piece in part.split(separator)]
    return [part.strip() for part in parts if part.strip()]


def _mb(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def describe_size(size: int) -> str:
    """A size a person reads, for a table cell and a log line."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} kB"
    return _mb(size)
