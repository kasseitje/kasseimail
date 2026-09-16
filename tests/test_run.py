"""The run: what stops it, what it skips, and what a resumed one does not send again.

Everything here is about a decision taken before or between messages. The Graph call itself is
faked -- what it does is pinned in `test_message.py`, and what matters here is the loop around it.
"""

import pytest

from kasseimail.attachments import AttachmentSpec
from kasseimail.ledger import MODE_DRAFTS, MODE_DRY_RUN, MODE_SEND, already_done, defused
from kasseimail.recipients import load as load_recipients
from kasseimail.run import RunProblem, SendRun


def build(template, table, tmp_path, mailer=None, **kwargs):
    kwargs.setdefault("spec", AttachmentSpec(root=tmp_path))
    kwargs.setdefault("out_dir", tmp_path / "out")
    kwargs.setdefault("pause", 0)
    return SendRun(template=template, table=table, mailer=mailer, **kwargs)


@pytest.fixture
def simple(make_template):
    return make_template(
        subject="Hello {{ first_name }}\n",
        html="<p>Hello {{ first_name }}</p>\n",
        text="Hello {{ first_name }}\n",
    )


# -- preflight stops what should not start --------------------------------------------------

def test_a_template_typo_is_found_once_rather_than_seventy_times(make_template, table, tmp_path):
    """The point of rendering every row before anything is sent. A misspelled column is not one
    broken mail, it is all of them."""
    template = make_template(html="<p>Dear {{ frist_name }}</p>\n")
    run = build(template, table, tmp_path, mode=MODE_SEND)

    found = run.preflight()

    assert not found.ok
    assert len(found.failing) == 3
    assert "frist_name" in found.failing[0].errors[0]


def test_sending_refuses_when_preflight_found_errors(make_template, table, tmp_path, fake_mailer):
    template = make_template(html="<p>{{ nope }}</p>\n")
    mailer = fake_mailer()
    run = build(template, table, tmp_path, mailer=mailer, mode=MODE_SEND)

    with pytest.raises(RunProblem) as refusal:
        run.execute()

    assert "Refusing to send" in str(refusal.value)
    assert mailer.sent == []


def test_a_dry_run_goes_ahead_anyway_so_the_errors_can_be_looked_at(make_template, table,
                                                                   tmp_path):
    """A refusal that also refuses to show you what is wrong leaves nothing to work from."""
    template = make_template(html="<p>{{ nope }}</p>\n")
    run = build(template, table, tmp_path, mode=MODE_DRY_RUN)

    summary = run.execute()

    assert summary.delivered == 0


def test_a_missing_required_column_names_it_and_the_columns_there_are(make_template, table,
                                                                     tmp_path):
    template = make_template(meta='required = ["email", "membership_no"]\n')
    run = build(template, table, tmp_path, mode=MODE_SEND)

    found = run.preflight()

    assert not found.ok
    assert "membership_no" in found.errors[0]


def test_a_missing_attachment_stops_the_run_before_it_starts(simple, table, tmp_path, fake_mailer):
    """Finding out at message 40 that the PDFs stop at 39 leaves 39 mails that cannot be recalled
    and a decision to make in a hurry."""
    mailer = fake_mailer()
    spec = AttachmentSpec(patterns=["invoices/A-{{ invoice_no }}.pdf"], root=tmp_path)
    run = build(simple, table, tmp_path, mailer=mailer, spec=spec, mode=MODE_SEND)

    with pytest.raises(RunProblem):
        run.execute()

    assert mailer.sent == []


def test_allowing_missing_attachments_sends_the_rest(simple, table, tmp_path, fake_mailer,
                                                     make_files):
    """Sometimes a blank really is allowed, and then the other sixty-nine should still go."""
    make_files("invoices/A-1001.pdf")
    mailer = fake_mailer()
    spec = AttachmentSpec(patterns=["invoices/A-{{ invoice_no }}.pdf"], root=tmp_path,
                          allow_missing=True)
    run = build(simple, table, tmp_path, mailer=mailer, spec=spec, mode=MODE_SEND)

    summary = run.execute()

    assert summary.delivered == 3
    assert len(mailer.sent[0].message["attachments"]) == 1
    assert "attachments" not in mailer.sent[1].message


def test_a_mailbox_that_is_not_an_address_is_refused_before_anything_is_sent(simple, table,
                                                                            tmp_path):
    """A typo in the mailbox to send from is the repeating failure preflight exists for: Graph
    answers the same 403 to every row, so a run of seventy reads as seventy problems."""
    run = build(simple, table, tmp_path, mode=MODE_SEND, mailbox="info@")

    found = run.preflight()

    assert not found.ok
    assert "info@" in found.errors[0]


def test_an_empty_mailbox_beats_a_configured_one(simple, table, tmp_path):
    """`None` means the run did not say and the settings decide; `""` is a front end saying 'my
    own mailbox'. The window's Send-from box is emptied to mean exactly that, and falling back to
    the configured mailbox there would send from the shared one anyway."""
    from kasseimail.config import Settings

    settings = Settings(mailbox="info@example.be")

    assert build(simple, table, tmp_path, settings=settings).mailbox == "info@example.be"
    assert build(simple, table, tmp_path, settings=settings, mailbox="").mailbox == ""


def test_the_written_body_says_which_mailbox_it_came_from(simple, table, tmp_path):
    """That file is the answer to 'what exactly went to row 3', and who it came from is part of
    the answer -- particularly for a mailbox several people send from."""
    build(simple, table, tmp_path, mailbox="info@example.be").execute()

    written = sorted((tmp_path / "out" / "messages").glob("*.txt"))[0].read_text()

    assert written.startswith("From: info@example.be\nTo: ")


# -- rows that are skipped --------------------------------------------------------------------

def test_a_row_without_an_address_is_skipped_and_still_named_in_the_report(simple, table,
                                                                          tmp_path, fake_mailer):
    """Nothing disappears quietly: the person is in the report with the reason, so somebody can
    chase the address."""
    run = build(simple, table, tmp_path, mailer=fake_mailer(), mode=MODE_SEND)

    summary = run.execute()

    assert summary.no_address == 1
    assert "no address" in (tmp_path / "out" / "report.csv").read_text(encoding="utf-8-sig")


def test_an_unusable_address_is_an_error_and_not_a_silent_skip(make_csv, simple, tmp_path,
                                                               fake_mailer):
    """Skipping it would mean somebody never gets their mail and nobody is told why."""
    path = make_csv([["email", "first_name"], ["jan at example.be", "Jan"]])
    run = build(simple, load_recipients(path), tmp_path, mailer=fake_mailer(), mode=MODE_SEND)

    with pytest.raises(RunProblem):
        run.execute()


def test_limit_counts_the_rows_that_would_actually_go_out(simple, table, tmp_path, fake_mailer):
    """`--limit 3` on a file whose first rows have no address should still give three mails --
    which is what somebody testing with it means by three."""
    mailer = fake_mailer()
    run = build(simple, table, tmp_path, mailer=mailer, mode=MODE_SEND, limit=2)

    summary = run.execute()

    assert summary.delivered == 2
    assert len(mailer.sent) == 2


# -- rehearsing ----------------------------------------------------------------------------------

def test_test_to_redirects_every_message_to_one_mailbox(simple, table, tmp_path, fake_mailer):
    mailer = fake_mailer()
    run = build(simple, table, tmp_path, mailer=mailer, mode=MODE_SEND, test_to="me@ours.be")

    run.execute()

    addresses = {d.message["toRecipients"][0]["emailAddress"]["address"] for d in mailer.sent}
    assert addresses == {"me@ours.be"}


def test_test_to_leaves_the_real_name_in_the_subject(simple, table, tmp_path, fake_mailer):
    """Three identical mails tell you nothing. The subject is what makes a rehearsal readable."""
    mailer = fake_mailer()
    run = build(simple, table, tmp_path, mailer=mailer, mode=MODE_SEND, test_to="me@ours.be")

    run.execute()

    assert {d.message["subject"] for d in mailer.sent} == {
        "Hello Jan", "Hello An", "Hello Jan junior"
    }


def test_test_to_still_skips_a_row_without_an_address(simple, table, tmp_path, fake_mailer):
    """A rehearsal that quietly fixes the empty cells is not a rehearsal of the real run: it sends
    four messages where the real one sends three, and hides the row somebody has to chase."""
    mailer = fake_mailer()
    run = build(simple, table, tmp_path, mailer=mailer, mode=MODE_SEND, test_to="me@ours.be")

    summary = run.execute()

    assert len(mailer.sent) == 3
    assert summary.no_address == 1


# -- drafts vs sending ------------------------------------------------------------------------

def test_drafts_mode_creates_drafts_and_sends_nothing(simple, table, tmp_path, fake_mailer):
    mailer = fake_mailer()
    run = build(simple, table, tmp_path, mailer=mailer, mode=MODE_DRAFTS)

    run.execute()

    assert len(mailer.drafts) == 3 and mailer.sent == []


def test_a_dry_run_does_not_sign_in_at_all(simple, table, tmp_path):
    """No mailer is passed and no settings either. If a dry run reached for a token it would fail
    here, which is exactly the property worth having: the safe mode needs no credentials."""
    run = build(simple, table, tmp_path, mode=MODE_DRY_RUN)

    summary = run.execute()

    assert summary.delivered == 3


def test_every_mode_writes_the_rendered_bodies_to_disk(simple, table, tmp_path, fake_mailer):
    """When somebody asks a week later what exactly went to row 3, this is the answer."""
    run = build(simple, table, tmp_path, mailer=fake_mailer(), mode=MODE_SEND)

    run.execute()

    written = sorted(p.name for p in (tmp_path / "out" / "messages").glob("*.html"))
    assert written[0].startswith("0002-")
    assert "Hello Jan" in (tmp_path / "out" / "messages" / written[0]).read_text()


# -- failures do not stop the run ----------------------------------------------------------------

def test_one_refused_message_does_not_stop_the_others(simple, table, tmp_path, fake_mailer):
    """A full mailbox at row two must not cost the other sixty-eight people their mail."""
    mailer = fake_mailer(fail_on={"an@example.be"})
    run = build(simple, table, tmp_path, mailer=mailer, mode=MODE_SEND)

    summary = run.execute()

    assert summary.delivered == 2
    assert summary.failed == 1
    assert summary.exit_code == 1


def test_a_failure_is_recorded_with_its_reason(simple, table, tmp_path, fake_mailer):
    run = build(simple, table, tmp_path, mailer=fake_mailer(fail_on={"an@example.be"}),
                mode=MODE_SEND)

    run.execute()

    report = (tmp_path / "out" / "report.csv").read_text(encoding="utf-8-sig")
    assert "failed" in report and "mailbox is full" in report


# -- resuming --------------------------------------------------------------------------------

def test_resume_skips_what_an_earlier_run_got_out(simple, table, tmp_path, fake_mailer):
    """Sending ninety people a second copy is worse than losing twenty minutes."""
    first = build(simple, table, tmp_path, mailer=fake_mailer(), mode=MODE_SEND, limit=1)
    first.execute()

    mailer = fake_mailer()
    second = build(simple, table, tmp_path, mailer=mailer, mode=MODE_SEND, resume=True)
    summary = second.execute()

    assert summary.delivered == 2
    assert [d.message["subject"] for d in mailer.sent] == ["Hello An", "Hello Jan junior"]


def test_resume_does_not_skip_the_second_person_at_a_shared_mailbox(simple, table, tmp_path,
                                                                    fake_mailer):
    """Rows 2 and 5 are two people at one address, which is the normal case this tool supports.
    Keyed on the address alone, a resumed run would see the first as done and Jan junior would
    never get his mail -- silently, which is the exact failure --resume exists to prevent. So the
    ledger identity is the row *and* the key.
    """
    build(simple, table, tmp_path, mailer=fake_mailer(), mode=MODE_SEND, limit=1).execute()

    mailer = fake_mailer()
    build(simple, table, tmp_path, mailer=mailer, mode=MODE_SEND, resume=True).execute()

    assert "Hello Jan junior" in [d.message["subject"] for d in mailer.sent]


def test_resume_retries_a_row_that_failed(simple, table, tmp_path, fake_mailer):
    """The failed row is exactly the one a resumed run is for."""
    build(simple, table, tmp_path, mailer=fake_mailer(fail_on={"an@example.be"}),
          mode=MODE_SEND).execute()

    mailer = fake_mailer()
    summary = build(simple, table, tmp_path, mailer=mailer, mode=MODE_SEND,
                    resume=True).execute()

    assert summary.delivered == 1
    assert mailer.sent[0].message["toRecipients"][0]["emailAddress"]["address"] == "an@example.be"


def test_a_dry_run_does_not_count_as_done(simple, table, tmp_path, fake_mailer):
    """Nothing reached a mailbox, so a resumed run has everything still to do. Counting it would
    mean a rehearsal silently cancels the real run."""
    build(simple, table, tmp_path, mode=MODE_DRY_RUN).execute()

    summary = build(simple, table, tmp_path, mailer=fake_mailer(), mode=MODE_SEND,
                    resume=True).execute()

    assert summary.delivered == 3


def test_the_report_is_appended_so_the_first_attempt_survives(simple, table, tmp_path,
                                                              fake_mailer):
    build(simple, table, tmp_path, mailer=fake_mailer(), mode=MODE_DRAFTS, limit=1).execute()
    build(simple, table, tmp_path, mailer=fake_mailer(), mode=MODE_DRAFTS, resume=True).execute()

    body = (tmp_path / "out" / "report.csv").read_text(encoding="utf-8-sig")

    assert body.count("timestamp;row") == 1
    assert len(already_done(tmp_path / "out")) == 3


# -- cancelling ------------------------------------------------------------------------------

def test_cancelling_stops_between_messages_and_keeps_what_was_done(simple, table, tmp_path,
                                                                   fake_mailer):
    """The window's Cancel button. Stopping mid-message would leave a half-uploaded draft and no
    record of it; stopping between them leaves a report that --resume can read."""
    mailer = fake_mailer()
    run = build(simple, table, tmp_path, mailer=mailer, mode=MODE_SEND)

    summary = run.execute(should_cancel=lambda: len(mailer.sent) >= 2)

    assert summary.cancelled
    assert len(mailer.sent) == 2
    assert len(already_done(tmp_path / "out")) == 2


# -- progress ---------------------------------------------------------------------------------

def test_progress_is_reported_once_per_row_with_the_running_count(simple, table, tmp_path,
                                                                  fake_mailer):
    """What drives the progress bar. A total that does not match the number of calls makes the bar
    stop short or run past the end."""
    seen = []
    run = build(simple, table, tmp_path, mailer=fake_mailer(), mode=MODE_SEND)

    run.execute(progress=seen.append)

    assert [event.index for event in seen] == [1, 2, 3]
    assert {event.total for event in seen} == {3}
    assert [event.status for event in seen] == ["ok", "ok", "ok"]


# -- cc, bcc and the template's own defaults ------------------------------------------------------

def test_the_templates_cc_and_the_flags_cc_are_both_used(make_template, table, tmp_path,
                                                         fake_mailer):
    """A standing cc in meta.toml is a policy; a --cc on the command line is one more person on
    this run. Replacing one with the other would quietly drop the bookkeeper."""
    template = make_template(subject="s\n", html="<p>b</p>\n", meta='cc = ["books@example.be"]\n')
    mailer = fake_mailer()
    run = build(template, table, tmp_path, mailer=mailer, mode=MODE_SEND, cc=["boss@example.be"])

    run.execute()

    addressed = [entry["emailAddress"]["address"] for entry in mailer.sent[0]["ccRecipients"]] \
        if isinstance(mailer.sent[0], dict) else \
        [entry["emailAddress"]["address"] for entry in mailer.sent[0].message["ccRecipients"]]
    assert addressed == ["books@example.be", "boss@example.be"]


def test_the_same_address_in_meta_and_on_the_flag_appears_once(make_template, table, tmp_path,
                                                               fake_mailer):
    template = make_template(subject="s\n", html="<p>b</p>\n", meta='cc = ["books@example.be"]\n')
    mailer = fake_mailer()
    run = build(template, table, tmp_path, mailer=mailer, mode=MODE_SEND,
                cc=["Books@example.be"])

    run.execute()

    assert len(mailer.sent[0].message["ccRecipients"]) == 1


# -- the report -------------------------------------------------------------------------------

def test_a_cell_that_excel_would_execute_is_defused():
    """A name like `=cmd|...` in a spreadsheet is a real technique, and every free-text column in
    this report came out of somebody else's file or out of an error message."""
    assert defused("=cmd|' /c calc'!A1").startswith("'=")
    assert defused("Peeters & Zn") == "Peeters & Zn"


def test_an_unknown_mode_is_refused_when_the_run_is_built(simple, table, tmp_path):
    """Before the spreadsheet is read, not after."""
    with pytest.raises(RunProblem):
        build(simple, table, tmp_path, mode="maybe")
