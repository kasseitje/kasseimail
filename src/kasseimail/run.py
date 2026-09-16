"""The run: check everything first, then send one mail per row.

Two phases, and the split is the whole design.

`preflight()` touches no network. It renders every row, resolves every attachment and checks every
address, and it collects what it finds instead of stopping at the first problem. That matters
because the failures worth catching are the ones that repeat: a column misspelled in the template is
not one broken mail, it is all of them, and finding out at row one is worth the second it costs to
find out at row three hundred too.

`execute()` refuses to start when preflight found errors and the mode is not a dry run. Then it
walks the rows, pausing between messages, writing the ledger as it goes.

Neither imports Qt, and nothing here prints. Progress arrives through a callback and everything else
through loguru, which is what lets the CLI and the window drive the same object: the CLI's callback
writes a line, the window's emits a signal.
"""

import re
import time

from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from kasseimail import ledger
from kasseimail.attachments import AttachmentSpec, Resolution, describe_size
from kasseimail.graph import GraphMailer, GraphProblem, SignInProblem
from kasseimail.ledger import (
    MODE_DRAFTS, MODE_DRY_RUN, MODE_SEND, STATUS_FAILED, STATUS_NO_ADDRESS, STATUS_OK,
    STATUS_RENDERED, STATUS_SKIPPED,
)
from kasseimail.message import plan_delivery
from kasseimail.recipients import EMAIL_PATTERN, RecipientTable
from kasseimail.templates import Rendered, Template, TemplateProblem

MODES = (MODE_DRY_RUN, MODE_DRAFTS, MODE_SEND)


class RunProblem(Exception):
    """The run cannot start. Distinct from one row failing, which is recorded and carried on from."""


# ---------------------------------------------------------------------------------------------
# what preflight found
# ---------------------------------------------------------------------------------------------

@dataclass
class RowPlan:
    """One row, checked. Everything `execute` needs, so nothing is computed twice."""

    recipient: object
    address: str
    rendered: Rendered | None = None
    resolution: Resolution = field(default_factory=Resolution)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skip: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors and not self.skip

    @property
    def row(self) -> int:
        return self.recipient.row

    @property
    def key(self) -> str:
        return self.recipient.key


@dataclass
class Preflight:
    """The whole file, checked, before anything leaves."""

    rows: list[RowPlan] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def sendable(self) -> list[RowPlan]:
        return [plan for plan in self.rows if plan.ok]

    @property
    def failing(self) -> list[RowPlan]:
        return [plan for plan in self.rows if plan.errors]

    @property
    def skipped(self) -> list[RowPlan]:
        return [plan for plan in self.rows if plan.skip and not plan.errors]

    @property
    def ok(self) -> bool:
        return not self.errors and not self.failing

    def summary_lines(self) -> list[tuple[str, str]]:
        """The report a person reads, as (level, text).

        The level travels with the line so the caller does not have to guess it back out of the
        wording. A preflight that found nothing wrong reads as information; one that did has to
        look different in a terminal and in the window's log pane, and they should not each decide
        that for themselves.
        """
        lines = [("info", f"{len(self.rows)} rows, {len(self.sendable)} ready to send")]

        if self.skipped:
            lines.append(("info", f"{len(self.skipped)} skipped: " + _reasons(self.skipped)))

        if self.failing:
            lines.append(("error", f"{len(self.failing)} with errors:"))
            for plan in self.failing[:20]:
                lines.append(("error", f"    row {plan.row} ({plan.key}): "
                                       f"{'; '.join(plan.errors)}"))
            if len(self.failing) > 20:
                lines.append(("error", f"    ... and {len(self.failing) - 20} more"))

        for problem in self.errors:
            lines.append(("error", problem))
        for warning in self.warnings:
            lines.append(("warning", warning))

        return lines


def _reasons(plans: list[RowPlan]) -> str:
    counted: dict[str, int] = {}
    for plan in plans:
        counted[plan.skip] = counted.get(plan.skip, 0) + 1
    return ", ".join(f"{reason} ({count})" for reason, count in sorted(counted.items()))


@dataclass
class Progress:
    """Handed to the progress callback once per row, and again when a large upload advances."""

    index: int
    total: int
    row: int
    key: str
    address: str
    status: str
    note: str = ""


@dataclass
class RunSummary:
    """How it went."""

    mode: str
    out_dir: Path
    total: int = 0
    delivered: int = 0
    skipped: int = 0
    failed: int = 0
    no_address: int = 0
    cancelled: bool = False
    report: Path | None = None
    failures: list[tuple[int, str]] = field(default_factory=list)
    duplicate_keys: list[tuple[str, list[int]]] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if (self.failed or self.cancelled) else 0

    def summary_lines(self) -> list[tuple[str, str]]:
        """How it went, as (level, text). See `Preflight.summary_lines` for why the level is here."""
        verb = {MODE_DRY_RUN: "rendered", MODE_DRAFTS: "left as drafts", MODE_SEND: "sent"}[self.mode]
        lines = [("info", f"{self.delivered} of {self.total} {verb}")]

        if self.skipped:
            lines.append(("info", f"{self.skipped} skipped (already done, or not selected)"))
        if self.no_address:
            lines.append(("info", f"{self.no_address} without a usable address"))
        if self.cancelled:
            lines.append(("warning", "cancelled part-way; --resume continues where it stopped"))

        if self.duplicate_keys:
            lines.append((
                "info",
                f"{len(self.duplicate_keys)} address(es) got more than one mail, each with its "
                "own row -- households share a mailbox, so this is usually right:",
            ))
            for key, rows in self.duplicate_keys[:10]:
                lines.append(("info", f"    {key}: rows {', '.join(str(r) for r in rows)}"))

        if self.failed:
            lines.append(("error", f"{self.failed} failed:"))
            for row, reason in self.failures[:20]:
                lines.append(("error", f"    row {row}: {reason}"))
            if len(self.failures) > 20:
                lines.append(("error", f"    ... and {len(self.failures) - 20} more"))

        if self.report:
            lines.append(("info", f"report: {self.report}"))
        return lines


# ---------------------------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------------------------

class SendRun:
    """One template, one spreadsheet, one delivery mode."""

    def __init__(
        self,
        *,
        template: Template,
        table: RecipientTable,
        spec: AttachmentSpec | None = None,
        mode: str = MODE_DRY_RUN,
        out_dir: str | Path = "out",
        test_to: str | None = None,
        resume: bool = False,
        pause: float = 2.5,
        limit: int | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to: list[str] | None = None,
        mailbox: str | None = None,
        settings=None,
        mailer: GraphMailer | None = None,
    ):
        if mode not in MODES:
            raise RunProblem(f"Unknown mode {mode!r}; expected one of {', '.join(MODES)}")

        self.template = template
        self.table = table
        self.spec = spec or AttachmentSpec(root=table.path.parent)
        self.mode = mode
        self.out_dir = Path(out_dir)
        self.test_to = (test_to or "").strip() or None
        self.resume = resume
        self.pause = max(0.0, float(pause))
        self.limit = limit
        self.settings = settings

        # -- which mailbox this leaves from; empty is the signed-in user's own.
        #
        #    **`None` and `""` are different on purpose.** `None` is "this run did not say", and
        #    the settings decide. `""` is a front end saying *my own mailbox*, and it has to win
        #    over a configured one -- the window's Send-from box is emptied to mean exactly that,
        #    and a fallback there would quietly send from the shared mailbox anyway.
        if mailbox is None:
            mailbox = getattr(settings, "mailbox", "") or ""
        self.mailbox = mailbox.strip()

        # -- meta.toml says what this template always does; the flags say what this run does on top
        #    of it. Added rather than replaced: a standing cc in the template is a policy, and a
        #    --cc on the command line is one more person on this particular run.
        self.cc = _merge(template.meta.cc, cc)
        self.bcc = _merge(template.meta.bcc, bcc)
        self.reply_to = _merge(template.meta.reply_to, reply_to)

        self._mailer = mailer
        self._preflight: Preflight | None = None

    # -- phase one ----------------------------------------------------------------------------

    def preflight(self, force: bool = False) -> Preflight:
        """Render and resolve every row. No network, no files written, nothing sent."""
        if self._preflight is not None and not force:
            return self._preflight

        found = Preflight()
        found.warnings.extend(self.table.warnings)

        # -- a mistyped sender is the repeating failure this phase is for: Graph answers the same
        #    403 to every row, so the run reads as seventy problems rather than one.
        if self.mailbox and not EMAIL_PATTERN.match(self.mailbox):
            found.errors.append(
                f"'{self.mailbox}' is not a usable mailbox to send from; it has to be a full "
                "address, like info@example.be"
            )

        missing_columns = [
            column for column in self.template.meta.required if column not in self.table.headers
        ]
        if missing_columns:
            found.errors.append(
                f"the template requires column(s) {', '.join(missing_columns)}, and "
                f"{self.table.path.name} has {', '.join(self.table.headers)}"
            )

        for column in self.spec.columns:
            if column not in self.table.headers:
                found.errors.append(f"no attachment column '{column}' in {self.table.path.name}")

        done = ledger.already_done(self.out_dir) if self.resume else set()
        if self.resume:
            logger.debug("resuming: {} keys already recorded as sent", len(done))

        for recipient in self.table.rows:
            found.rows.append(self._plan(recipient, done))

        if self.limit is not None:
            self._apply_limit(found)

        for key, rows in self.table.duplicate_keys():
            found.warnings.append(
                f"{key} appears on rows {', '.join(str(r) for r in rows)} and gets one mail each"
            )

        self._preflight = found
        return found

    def _plan(self, recipient, done: set[str]) -> RowPlan:
        address = self._address_for(recipient)
        plan = RowPlan(recipient=recipient, address=address)

        if not recipient.email:
            # -- kept as a skip and not an error: a row with no address is a fact about the file,
            #    not a mistake in the run, and the ledger records it by name so nobody disappears.
            plan.skip = "no address"
            return plan

        if not EMAIL_PATTERN.match(recipient.email):
            plan.errors.append(f"'{recipient.email}' is not a usable address")

        if self.resume and (recipient.row, recipient.key) in done:
            plan.skip = "already sent"

        try:
            resolution = self.spec.resolve(recipient, self.template)
        except Exception as exc:  # pragma: no cover -- a filesystem that refuses, not a bad path
            plan.errors.append(f"resolving attachments: {exc}")
            return plan

        plan.resolution = resolution
        plan.errors.extend(resolution.errors)

        if resolution.missing:
            named = ", ".join(Path(path).name for path in resolution.missing)
            if self.spec.allow_missing:
                plan.warnings.append(f"missing attachment(s): {named}")
            else:
                plan.errors.append(f"missing attachment(s): {named}")

        try:
            plan.rendered = self.template.render(recipient.context(attachments=resolution.names))
        except TemplateProblem as exc:
            plan.errors.append(str(exc))
            return plan

        if not plan.rendered.subject:
            plan.errors.append("the subject renders empty")

        return plan

    def _apply_limit(self, found: Preflight) -> None:
        """Keep the first N rows that would actually go out; mark the rest as not selected.

        Counted over the sendable rows rather than over the file, so `--limit 3` on a file whose
        first ten rows have no address still gives three mails -- which is what somebody testing
        with it means by three.
        """
        taken = 0
        for plan in found.rows:
            if not plan.ok:
                continue
            if taken < self.limit:
                taken += 1
            else:
                plan.skip = "over --limit"

    def _address_for(self, recipient) -> str:
        """Where this mail actually goes.

        `--test-to` redirects everything to one mailbox but leaves the row otherwise untouched, so
        the subject still names the real person and a row with no address is *still* skipped. A
        rehearsal that quietly fixes the empty cells is not a rehearsal of the real run.
        """
        if not recipient.email:
            return ""
        return self.test_to or recipient.email

    # -- phase two ----------------------------------------------------------------------------

    def execute(self, progress=None, should_cancel=None, on_device_code=None) -> RunSummary:
        """Do the run. Raises `RunProblem` if preflight says it should not start."""
        found = self.preflight()

        if not found.ok and self.mode != MODE_DRY_RUN:
            raise RunProblem(
                "Refusing to send: preflight found problems.\n"
                + "\n".join(text for _, text in found.summary_lines())
                + "\n\nFix them, or use a dry run to look at what would go out."
            )

        self.out_dir.mkdir(parents=True, exist_ok=True)
        summary = RunSummary(mode=self.mode, out_dir=self.out_dir)
        summary.duplicate_keys = self.table.duplicate_keys()

        if self.mode != MODE_DRY_RUN:
            self._sign_in(on_device_code)
            # -- so Graph stops waiting out a Retry-After when the run is called off. A throttled
            #    run can be told to wait minutes, and that wait is otherwise invisible to cancel.
            if self._mailer is not None:
                self._mailer.should_cancel = should_cancel

        work = [plan for plan in found.rows if plan.ok]
        summary.total = len(work)
        summary.no_address = sum(1 for plan in found.rows if plan.skip == "no address")
        summary.skipped = sum(1 for plan in found.rows if plan.skip and plan.skip != "no address")

        entries = [
            ledger.Entry(
                row=plan.row, key=plan.key, email=plan.recipient.email,
                template=self.template.name, mode=self.mode,
                status=STATUS_NO_ADDRESS if plan.skip == "no address" else STATUS_SKIPPED,
                note=plan.skip,
            )
            for plan in found.rows if plan.skip
        ]

        logger.info(
            "{} {} message(s) from template '{}'{}",
            {MODE_DRY_RUN: "rendering", MODE_DRAFTS: "drafting", MODE_SEND: "sending"}[self.mode],
            len(work), self.template.name,
            f", all to {self.test_to}" if self.test_to else "",
        )

        for index, plan in enumerate(work, start=1):
            if should_cancel is not None and should_cancel():
                summary.cancelled = True
                logger.warning("cancelled after {} of {}", index - 1, len(work))
                break

            entry = self._deliver(plan, index, len(work), summary, progress)
            entries.append(entry)

            if self.pause and self.mode != MODE_DRY_RUN and index < len(work):
                interruptible_sleep(self.pause, should_cancel)

        summary.report = ledger.append(self.out_dir, entries)
        for level, text in summary.summary_lines():
            logger.log(level.upper(), text)

        return summary

    def _deliver(self, plan: RowPlan, index: int, total: int, summary: RunSummary,
                 progress) -> ledger.Entry:
        """One row. Never raises: a failure is recorded and the run goes on to the next person."""
        entry = ledger.Entry(
            row=plan.row, key=plan.key, email=plan.address, template=self.template.name,
            mode=self.mode, status=STATUS_OK,
            attachments=", ".join(plan.resolution.names),
        )

        def report(status: str, note: str = "") -> None:
            if progress is not None:
                progress(Progress(index=index, total=total, row=plan.row, key=plan.key,
                                  address=plan.address, status=status, note=note))

        try:
            self._write_preview(plan)

            if self.mode == MODE_DRY_RUN:
                entry.status = STATUS_RENDERED
                summary.delivered += 1
                size = describe_size(plan.resolution.total_size)
                note = f"{len(plan.resolution.attachments)} attachment(s), {size}"
                logger.info("  · row {} {} — {}", plan.row, plan.key, note)
                report("rendered", note)
                return entry

            delivery = plan_delivery(
                plan.resolution,
                subject=plan.rendered.subject,
                text=plan.rendered.text,
                html=plan.rendered.html,
                to=[plan.address],
                cc=self.cc,
                bcc=self.bcc,
                reply_to=self.reply_to,
            )

            def on_chunk(sent, size):
                report("uploading", f"{describe_size(sent)} of {describe_size(size)}")

            entry.message_id = self._mailer.deliver(
                delivery, send=(self.mode == MODE_SEND), on_chunk=on_chunk
            )
            summary.delivered += 1

            verb = "sent to" if self.mode == MODE_SEND else "draft for"
            logger.info("  ✓ row {} {} {}", plan.row, verb, plan.address)
            report("ok")

        except (GraphProblem, SignInProblem, TemplateProblem, OSError, ValueError) as exc:
            entry.status = STATUS_FAILED
            entry.note = str(exc)
            summary.failed += 1
            summary.failures.append((plan.row, str(exc)))
            logger.error("  ✗ row {} {}: {}", plan.row, plan.key, exc)
            report("failed", str(exc))

        return entry

    def _write_preview(self, plan: RowPlan) -> None:
        """The rendered bodies on disk, for every mode.

        Not only for a dry run: when somebody asks a week later what exactly went to row 112, this
        is the answer, and it costs a few kilobytes per mail to be able to give it.
        """
        directory = self.out_dir / "messages"
        directory.mkdir(parents=True, exist_ok=True)
        stem = f"{plan.row:04d}-{_slug(plan.key)}"

        header = (
            (f"From: {self.mailbox}\n" if self.mailbox else "")
            + f"To: {plan.address}\n"
            + (f"Cc: {', '.join(self.cc)}\n" if self.cc else "")
            + (f"Bcc: {', '.join(self.bcc)}\n" if self.bcc else "")
            + f"Subject: {plan.rendered.subject}\n"
            + (f"Attachments: {', '.join(plan.resolution.names)}\n"
               if plan.resolution.names else "")
        )

        (directory / f"{stem}.txt").write_text(
            header + "\n" + (plan.rendered.text or "(no plain-text body)\n"), encoding="utf-8"
        )
        if plan.rendered.html:
            (directory / f"{stem}.html").write_text(plan.rendered.html, encoding="utf-8")

    def cancel_sign_in(self) -> bool:
        """Call off a device flow this run is waiting on. Safe from another thread."""
        return self._mailer.cancel_sign_in() if self._mailer is not None else False

    def _sign_in(self, on_device_code) -> None:
        if self._mailer is not None:
            return

        if self.settings is None:
            raise RunProblem("No settings, so there is nothing to sign in with.")

        self.settings.require_credentials()
        self._mailer = GraphMailer(
            self.settings.tenant_id, self.settings.client_id, self.settings.token_cache,
            mailbox=self.mailbox,
        ).sign_in(on_device_code=on_device_code)

        account = self._mailer.cached_account()
        if account:
            logger.info("signed in as {}", account.username)
        if self.mailbox:
            logger.info("sending from {}, not from the signed-in mailbox", self.mailbox)


def interruptible_sleep(seconds: float, should_cancel=None, slice_seconds: float = 0.1) -> bool:
    """Wait, but notice a cancel while waiting. Returns False if it was cut short.

    A plain `time.sleep` of the pause cannot be interrupted, and the pause is where a run spends
    most of its time -- 2.5 seconds by default, and far more when somebody is being careful about
    throttling. Cancel then appears to do nothing until it happens to end, and closing the window
    waits exactly as long with the window frozen.
    """
    deadline = time.monotonic() + max(0.0, seconds)

    while True:
        if should_cancel is not None and should_cancel():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(slice_seconds, remaining))


def _merge(from_meta: list[str], from_flags: list[str] | None) -> list[str]:
    """Template defaults plus command-line additions, in order, without duplicates."""
    merged: list[str] = []
    for address in list(from_meta) + list(from_flags or []):
        cleaned = (address or "").strip()
        if cleaned and cleaned.lower() not in {a.lower() for a in merged}:
            merged.append(cleaned)
    return merged


def _slug(text: str) -> str:
    """A key as a filename. Keeps the row number in front of it, so collisions cannot overwrite."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-")
    return (cleaned or "row")[:60]
