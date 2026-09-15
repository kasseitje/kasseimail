"""Templates: a directory per mail, managed as files on disk.

A template is a folder, not a file:

    welcome/
        meta.toml          optional -- defaults this template carries with it
        subject.j2         required -- one line
        body.html.j2       either this...
        body.txt.j2        ...or this, or both
        assets/            optional -- images and the like, for later

A folder rather than a single file with a header, because a template is rarely only text: it wants
a plain-text version next to the HTML one, an attachment pattern, a standing cc. Those belong in
files next to each other, where they can be diffed and copied as a unit, and where the window in
`ui/template_panel.py` can list directories instead of parsing a header.

`meta.toml` is where a template stops being only text and starts carrying policy -- which columns
it needs, what to attach, who is always in cc. Every one of those is overridable on the command
line; the file only says what is true by default.
"""

import re
import shutil
import tomllib

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError

from kasseimail.config import HTML_FILE, META_FILE, SUBJECT_FILE, TEXT_FILE

#: the editable parts, in the order the GUI shows them as tabs.
PARTS = (SUBJECT_FILE, HTML_FILE, TEXT_FILE, META_FILE)

#: a template name is a directory name, and it ends up in paths and in filenames of rendered
#: previews. Keeping it to this alphabet means no quoting anywhere and no surprises on Windows.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

BUILTIN_DIR = Path(__file__).resolve().parent / "templates_builtin"


class TemplateProblem(Exception):
    """A template that cannot be loaded or rendered. Carries a message meant for a person."""


# ---------------------------------------------------------------------------------------------
# the Jinja environment
# ---------------------------------------------------------------------------------------------

def _autoescape(template_name: str | None) -> bool:
    """Escape the HTML body; leave the subject and the plain text alone.

    **Deliberately not `select_autoescape(["html"])`.** That one looks at the *last* extension, and
    every file here ends in `.j2` -- so it reads `body.html.j2` as "not HTML" and renders it raw.
    The values come out of somebody's spreadsheet, and a company name with an `&` or a `<` in it
    breaks the markup of every mail in the run.

    In the subject and the plain text the escaping would be the bug instead: nobody wants to read
    `Jones &amp; Sons`.
    """
    return bool(template_name) and template_name.endswith((".html.j2", ".html"))


#: month and day names, written out rather than taken from the C library.
#:
#: **`strftime("%B")` is not safe here.** It reads the process locale, and the process locale is not
#: ours to rely on: constructing a QApplication calls `setlocale(LC_ALL, "")`, and so does anything
#: else in a large GUI stack that feels like it. The same template then renders "01 March 2026" from
#: the command line and "01 maart 2026" from the window, on the same machine, from the same file --
#: and the difference only surfaces when a recipient points at it.
#:
#: So the names live here, the language is chosen in the template, and the output is the same
#: everywhere regardless of what any library did to the locale.
MONTH_NAMES = {
    "en": ["January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"],
    "nl": ["januari", "februari", "maart", "april", "mei", "juni",
           "juli", "augustus", "september", "oktober", "november", "december"],
    "fr": ["janvier", "février", "mars", "avril", "mai", "juin",
           "juillet", "août", "septembre", "octobre", "novembre", "décembre"],
}

DAY_NAMES = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "nl": ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"],
    "fr": ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"],
}

DEFAULT_DATE_LANGUAGE = "en"


def _format_date(value, fmt: str = "%d/%m/%Y", language: str = DEFAULT_DATE_LANGUAGE) -> str:
    """`{{ due | date("%d %B %Y") }}` -- because a spreadsheet date arrives as a datetime.

    openpyxl hands back real `datetime` objects, and `{{ due }}` on one of those renders
    `2026-03-01 00:00:00` into a customer's mail. Passing anything else straight through keeps the
    filter usable on a column that is text in one file and a date in the next.

    `{{ due | date("%d %B %Y", "nl") }}` writes the month in Dutch. The names come from the table
    above rather than from the locale, so the same template gives the same text from the CLI and
    from the window -- see MONTH_NAMES for why that is not a given.
    """
    if not isinstance(value, (datetime, date)):
        return "" if value is None else str(value)

    months = MONTH_NAMES.get(language, MONTH_NAMES[DEFAULT_DATE_LANGUAGE])
    days = DAY_NAMES.get(language, DAY_NAMES[DEFAULT_DATE_LANGUAGE])

    month, weekday = months[value.month - 1], days[value.weekday()]

    # -- substituted before strftime sees them, and through a placeholder so a month name
    #    containing a literal % cannot be read as another code.
    replacements = {
        "%B": month, "%b": month[:3], "%A": weekday, "%a": weekday[:3],
        # -- locale-dependent aggregates, which would drag the names back in.
        "%c": "%Y-%m-%d %H:%M:%S", "%x": "%d/%m/%Y", "%X": "%H:%M:%S",
    }

    pieces, index = [], 0
    while index < len(fmt):
        token = fmt[index:index + 2]
        if token in replacements:
            pieces.append(replacements[token])
            index += 2
        else:
            pieces.append(fmt[index])
            index += 1

    return value.strftime("".join(pieces))


def _format_money(value, symbol: str = "€", decimals: int = 2) -> str:
    """`{{ amount | money }}` -> `€ 1.234,50`. Continental grouping, which is what the readers use."""
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)

    grouped = f"{number:,.{decimals}f}"
    # -- swap the separators: 1,234.50 -> 1.234,50, via a placeholder so the two do not collide.
    grouped = grouped.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return f"{symbol} {grouped}".strip()


def _default_blank(value) -> str:
    """`None` and the string "None" are both rendering accidents; this makes them an empty cell."""
    return "" if value is None else str(value)


def build_environment(directory: Path) -> Environment:
    """One environment per template directory.

    `StrictUndefined`, so a typo is an error instead of a blank. Without it `{{ frist_name }}`
    renders as nothing and seventy mails go out opening with "Dear ,". That failure is invisible
    until somebody replies, and by then it cannot be taken back -- which is the whole argument for
    the preflight pass in `run.py` rendering every row before anything is sent.
    """
    env = Environment(
        loader=FileSystemLoader(str(directory)),
        autoescape=_autoescape,
        undefined=StrictUndefined,
        trim_blocks=False,
        keep_trailing_newline=True,
    )
    env.filters["date"] = _format_date
    env.filters["money"] = _format_money
    env.filters["blank"] = _default_blank
    return env


# ---------------------------------------------------------------------------------------------
# a template
# ---------------------------------------------------------------------------------------------

@dataclass
class TemplateMeta:
    """`meta.toml`, parsed. Every field is a default the command line may override."""

    description: str = ""
    attachments: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    reply_to: list[str] = field(default_factory=list)
    required: list[str] = field(default_factory=list)
    #: collapse several rows into one message -- the column to group on, and how to combine the
    #: other columns. A template written to say "your stands are 12, 14 and 19" only makes sense
    #: against grouped rows, so it carries that with it rather than relying on the right flag.
    group_by: str = ""
    aggregate: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "TemplateMeta":
        if not path.is_file():
            return cls()
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as exc:
            raise TemplateProblem(f"{path.parent.name}/{META_FILE}: {exc}") from exc

        def as_list(value):
            if value is None:
                return []
            return [str(value)] if isinstance(value, str) else [str(item) for item in value]

        return cls(
            description=str(raw.get("description", "")),
            attachments=as_list(raw.get("attachments")),
            cc=as_list(raw.get("cc")),
            bcc=as_list(raw.get("bcc")),
            reply_to=as_list(raw.get("reply_to")),
            required=as_list(raw.get("required")),
            group_by=str(raw.get("group_by", "") or ""),
            aggregate={str(column): str(how)
                       for column, how in (raw.get("aggregate") or {}).items()},
        )


@dataclass
class Rendered:
    """What one row turned into."""

    subject: str
    text: str | None
    html: str | None


class Template:
    """One template directory, loaded."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.name = self.directory.name

        if not self.directory.is_dir():
            raise TemplateProblem(f"No template called '{self.name}' in {self.directory.parent}")

        self.has_subject = (self.directory / SUBJECT_FILE).is_file()
        self.has_html = (self.directory / HTML_FILE).is_file()
        self.has_text = (self.directory / TEXT_FILE).is_file()

        if not self.has_subject:
            raise TemplateProblem(f"'{self.name}' has no {SUBJECT_FILE}")
        if not (self.has_html or self.has_text):
            raise TemplateProblem(
                f"'{self.name}' has no body: add {HTML_FILE}, {TEXT_FILE}, or both"
            )

        self.meta = TemplateMeta.load(self.directory / META_FILE)
        self.env = build_environment(self.directory)

    # -- rendering ----------------------------------------------------------------------------

    def render(self, context: dict) -> Rendered:
        """Subject, plain text and HTML for one row.

        The subject is squeezed to its first line. A `.j2` file ends in a newline and an editor
        may leave a stray second line behind; a subject header containing a newline is rejected by
        Graph, and the failure names the whole message rather than the stray line.
        """
        try:
            subject_lines = self.env.get_template(SUBJECT_FILE).render(**context).strip().splitlines()
            subject = subject_lines[0].strip() if subject_lines else ""

            text = self.env.get_template(TEXT_FILE).render(**context) if self.has_text else None
            html = self.env.get_template(HTML_FILE).render(**context) if self.has_html else None
        except TemplateError as exc:
            raise TemplateProblem(f"{self.name}: {_explain(exc)}") from exc

        return Rendered(subject=subject, text=text, html=html)

    def render_string(self, source: str, context: dict, *, html: bool = False) -> str:
        """One-off render of a fragment -- an attachment pattern, or the GUI's live preview.

        The environment's filters and `StrictUndefined` apply, so a pattern misses the same way a
        body does.
        """
        try:
            template = self.env.from_string(source)
            if html:
                template.environment = self.env.overlay(autoescape=True)
            return template.render(**context)
        except TemplateError as exc:
            raise TemplateProblem(_explain(exc)) from exc

    def variables(self) -> set[str]:
        """Every name the templates reference, for the GUI to check a spreadsheet against.

        Best-effort: Jinja's static analysis does not see through `{{ row[key] }}`. Good enough to
        tell somebody which column they forgot, not good enough to be a gate -- the preflight pass
        renders for real, and that is the gate.
        """
        from jinja2 import meta as jinja_meta

        found: set[str] = set()
        for filename in (SUBJECT_FILE, HTML_FILE, TEXT_FILE):
            path = self.directory / filename
            if not path.is_file():
                continue
            try:
                ast = self.env.parse(path.read_text(encoding="utf-8"), filename=filename)
                found |= jinja_meta.find_undeclared_variables(ast)
            except TemplateError:
                continue
        return found

    # -- the files, for the editor ------------------------------------------------------------

    def read_part(self, part: str) -> str:
        path = self.directory / part
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def write_part(self, part: str, text: str) -> None:
        """Save one file and reload, so the next render sees it.

        Jinja caches compiled templates by name and mtime; `cache.clear()` removes the doubt on a
        filesystem whose timestamps are coarse enough to miss two saves in the same second.
        """
        if part not in PARTS:
            raise TemplateProblem(f"Not part of a template: {part}")

        (self.directory / part).write_text(text, encoding="utf-8")
        self.env.cache.clear()

        self.has_html = (self.directory / HTML_FILE).is_file()
        self.has_text = (self.directory / TEXT_FILE).is_file()
        if part == META_FILE:
            self.meta = TemplateMeta.load(self.directory / META_FILE)

    def describe(self) -> str:
        bodies = ", ".join(
            name for name, present in ((HTML_FILE, self.has_html), (TEXT_FILE, self.has_text))
            if present
        )
        return self.meta.description or f"({bodies})"


def _explain(exc: TemplateError) -> str:
    """A Jinja error as one line, with the file and line number.

    Only a `TemplateSyntaxError` carries `name` and `lineno`. The far more common failure is an
    `UndefinedError` -- a misspelled column -- and that one carries nothing but "'x' is undefined".
    A template set has four files, so an error without a filename sends somebody through all of
    them. Jinja does rewrite the traceback to run through the template, so the frames know which
    file it was even when the exception does not.
    """
    name = getattr(exc, "name", None)
    lineno = getattr(exc, "lineno", None)

    if not name:
        name, lineno = _from_traceback(exc)

    where = ""
    if name:
        where = f" in {name}" + (f" line {lineno}" if lineno else "")

    return f"{getattr(exc, 'message', None) or exc}{where}"


def _from_traceback(exc: BaseException) -> tuple[str | None, int | None]:
    """The innermost template frame of a rewritten Jinja traceback."""
    found = (None, None)
    frame = exc.__traceback__
    while frame is not None:
        filename = frame.tb_frame.f_code.co_filename
        if filename.endswith((".j2", ".html", ".txt")):
            found = (Path(filename).name, frame.tb_lineno)
        frame = frame.tb_next
    return found


# ---------------------------------------------------------------------------------------------
# the directory of templates
# ---------------------------------------------------------------------------------------------

class TemplateSet:
    """The local template directory: list it, load one, make one, delete one.

    This is what "manage templates from a local directory" comes down to. The CLI and the window
    both go through here, so a template made on the command line shows up in the window and the
    other way round.
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).expanduser()

    def ensure(self) -> Path:
        """Create the directory, and seed it with the starter template if it is new.

        An empty template directory is a dead end -- the window would open on an empty list with
        nothing to click. One working example is the difference between that and a starting point.
        """
        fresh = not self.directory.exists()
        self.directory.mkdir(parents=True, exist_ok=True)

        if fresh and not any(self.directory.iterdir()):
            for builtin in sorted(BUILTIN_DIR.iterdir()):
                if builtin.is_dir():
                    shutil.copytree(builtin, self.directory / builtin.name)
        return self.directory

    def names(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.directory.iterdir()
            if entry.is_dir() and (entry / SUBJECT_FILE).is_file()
        )

    def list(self) -> list["Template"]:
        """Every loadable template. One that is broken is skipped here and reported on `get`."""
        found = []
        for name in self.names():
            try:
                found.append(Template(self.directory / name))
            except TemplateProblem:
                continue
        return found

    def get(self, name: str) -> Template:
        path = self.directory / name
        if not path.is_dir():
            known = ", ".join(self.names()) or "none yet"
            raise TemplateProblem(
                f"No template called '{name}' in {self.directory}.\nAvailable: {known}"
            )
        return Template(path)

    def create(self, name: str, *, copy_from: str | None = None) -> Template:
        """A new template, from the built-in starter or as a copy of an existing one."""
        if not NAME_PATTERN.match(name):
            raise TemplateProblem(
                f"'{name}' is not a usable name: letters, digits, dot, dash and underscore, "
                "starting with a letter or a digit."
            )

        target = self.directory / name
        if target.exists():
            raise TemplateProblem(f"'{name}' already exists in {self.directory}")

        source = (self.directory / copy_from) if copy_from else (BUILTIN_DIR / "basic")
        if not source.is_dir():
            raise TemplateProblem(f"Nothing to copy from: {source}")

        self.directory.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
        return Template(target)

    def delete(self, name: str) -> None:
        target = self.directory / name
        if not target.is_dir():
            raise TemplateProblem(f"No template called '{name}' in {self.directory}")
        shutil.rmtree(target)
