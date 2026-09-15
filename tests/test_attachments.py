"""Which files end up on which mail.

The failure this guards against is not an exception -- it is a mail that goes out with the wrong
PDF, or with none, and looks perfectly normal on the way past.
"""

from kasseimail.attachments import AttachmentSpec, describe_size
from kasseimail.config import MAX_INLINE_TOTAL_BYTES
from kasseimail.recipients import Recipient


def recipient(**fields):
    fields.setdefault("email", "jan@example.be")
    fields.setdefault("invoice_no", 1001)
    return Recipient(row=2, email=fields["email"], key=fields["email"], fields=fields)


# -- the three sources -------------------------------------------------------------------------

def test_a_common_file_goes_on_every_message(tmp_path, make_files, make_template):
    make_files("handbook.pdf")
    spec = AttachmentSpec(common=["handbook.pdf"], root=tmp_path)

    found = spec.resolve(recipient(), make_template())

    assert found.names == ["handbook.pdf"]
    assert found.missing == []


def test_a_pattern_is_rendered_against_the_row(tmp_path, make_files, make_template):
    """This is what makes a per-person attachment possible at all: one rule, seventy files."""
    make_files("invoices/A-1001.pdf", "invoices/A-1002.pdf")
    spec = AttachmentSpec(patterns=["invoices/A-{{ invoice_no }}.pdf"], root=tmp_path)

    found = spec.resolve(recipient(invoice_no=1002), make_template())

    assert found.names == ["A-1002.pdf"]


def test_a_column_may_hold_several_paths(tmp_path, make_files, make_template):
    """One cell, two files, because that is how people fill a spreadsheet in."""
    make_files("a.pdf", "b.pdf")
    spec = AttachmentSpec(columns=["attachment"], root=tmp_path)

    found = spec.resolve(recipient(attachment="a.pdf; b.pdf"), make_template())

    assert sorted(found.names) == ["a.pdf", "b.pdf"]


def test_a_comma_does_not_split_a_cell(tmp_path, make_files, make_template):
    """Filenames contain commas far more often than people expect, and splitting on one turns a
    real file into two missing ones."""
    make_files("Invoice, final.pdf")
    spec = AttachmentSpec(columns=["attachment"], root=tmp_path)

    found = spec.resolve(recipient(attachment="Invoice, final.pdf"), make_template())

    assert found.names == ["Invoice, final.pdf"]


def test_the_three_sources_combine(tmp_path, make_files, make_template):
    make_files("handbook.pdf", "extra.pdf", "invoices/A-1001.pdf")
    spec = AttachmentSpec(
        common=["handbook.pdf"],
        columns=["attachment"],
        patterns=["invoices/A-{{ invoice_no }}.pdf"],
        root=tmp_path,
    )

    found = spec.resolve(recipient(attachment="extra.pdf"), make_template())

    assert sorted(found.names) == ["A-1001.pdf", "extra.pdf", "handbook.pdf"]


def test_the_same_file_from_two_sources_is_attached_once(tmp_path, make_files, make_template):
    """A handbook named both in meta.toml and on the command line is one handbook, and a mail with
    it twice looks like a mistake because it is one."""
    make_files("handbook.pdf")
    spec = AttachmentSpec(common=["handbook.pdf"], patterns=["handbook.pdf"], root=tmp_path)

    assert spec.resolve(recipient(), make_template()).names == ["handbook.pdf"]


# -- where relative paths point ----------------------------------------------------------------

def test_a_relative_path_is_relative_to_the_root_not_the_working_directory(tmp_path, make_files,
                                                                          make_template):
    """The root defaults to the directory the spreadsheet is in, which is the arrangement people
    actually have. Resolving against the cwd instead would work when run from one place and fail
    from another, which is the worst way for this to break."""
    make_files("invoices/A-1001.pdf")
    spec = AttachmentSpec(patterns=["invoices/A-{{ invoice_no }}.pdf"], root=tmp_path)

    found = spec.resolve(recipient(), make_template())

    assert found.attachments[0].path == (tmp_path / "invoices" / "A-1001.pdf").resolve()


def test_an_absolute_path_is_left_alone(tmp_path, make_files, make_template):
    (path,) = make_files("elsewhere.pdf", directory=tmp_path / "other")
    spec = AttachmentSpec(common=[str(path)], root=tmp_path)

    assert spec.resolve(recipient(), make_template()).names == ["elsewhere.pdf"]


# -- globs ---------------------------------------------------------------------------------------

def test_a_pattern_with_a_star_takes_everything_that_matches(tmp_path, make_files, make_template):
    """The filenames on disk carry a date nobody has in the spreadsheet; the invoice number is the
    only part both sides know."""
    make_files("invoices/A-1001-jan.pdf", "invoices/A-1001-feb.pdf", "invoices/A-1002-jan.pdf")
    spec = AttachmentSpec(patterns=["invoices/A-{{ invoice_no }}-*.pdf"], root=tmp_path)

    found = spec.resolve(recipient(), make_template())

    assert sorted(found.names) == ["A-1001-feb.pdf", "A-1001-jan.pdf"]


def test_a_glob_that_matches_nothing_is_missing_rather_than_silently_empty(tmp_path, make_template):
    """Otherwise a typo in the pattern sends seventy mails with no attachment at all, and every
    one of them looks fine."""
    spec = AttachmentSpec(patterns=["invoices/A-{{ invoice_no }}-*.pdf"], root=tmp_path)

    found = spec.resolve(recipient(), make_template())

    assert found.attachments == []
    assert found.missing


# -- what goes wrong -------------------------------------------------------------------------

def test_a_file_that_is_not_there_is_reported_with_its_path(tmp_path, make_template):
    """Named, so somebody can look at the path and see which half of it is wrong."""
    spec = AttachmentSpec(common=["handbook.pdf"], root=tmp_path)

    found = spec.resolve(recipient(), make_template())

    assert found.attachments == []
    assert "handbook.pdf" in found.missing[0]


def test_a_pattern_naming_a_column_that_does_not_exist_is_an_error_not_a_path(tmp_path,
                                                                             make_template):
    """Without StrictUndefined reaching in here, the pattern would render to `invoices/.pdf` and
    be reported as a missing file -- sending somebody to look for a file that was never named."""
    spec = AttachmentSpec(patterns=["invoices/{{ nope }}.pdf"], root=tmp_path)

    found = spec.resolve(recipient(), make_template())

    assert found.errors and "nope" in found.errors[0]


def test_an_empty_file_is_refused(tmp_path, make_template):
    """Graph accepts a zero-byte attachment and the recipient gets something they cannot open. It
    is nearly always a half-written export."""
    (tmp_path / "empty.pdf").write_bytes(b"")
    spec = AttachmentSpec(common=["empty.pdf"], root=tmp_path)

    found = spec.resolve(recipient(), make_template())

    assert found.attachments == []
    assert "empty.pdf" in found.errors[0]


def test_an_empty_cell_in_the_attachment_column_attaches_nothing_quietly(tmp_path, make_template):
    """Not every row has a file, and a blank cell is not a missing one."""
    spec = AttachmentSpec(columns=["attachment"], root=tmp_path)

    found = spec.resolve(recipient(attachment=""), make_template())

    assert found.attachments == [] and found.missing == []


# -- how it will travel -----------------------------------------------------------------------

def test_a_small_set_of_files_rides_inside_the_request(tmp_path, make_files, make_template):
    make_files("a.pdf", size=1024)
    spec = AttachmentSpec(common=["a.pdf"], root=tmp_path)

    assert not spec.resolve(recipient(), make_template()).needs_upload_session


def test_files_over_the_inline_budget_go_the_long_way(tmp_path, make_files, make_template):
    """Graph packs an inline attachment base64 into the request body, which caps near 4 MB. Past
    that the message has to be built as a draft and the files pushed up separately."""
    make_files("big.pdf", size=MAX_INLINE_TOTAL_BYTES + 1)
    spec = AttachmentSpec(common=["big.pdf"], root=tmp_path)

    assert spec.resolve(recipient(), make_template()).needs_upload_session


def test_the_budget_is_on_the_total_and_not_on_one_file(tmp_path, make_files, make_template):
    """Three files of 1.5 MB each pass a per-file check and still overflow the request."""
    two_thirds = (MAX_INLINE_TOTAL_BYTES // 3) * 2
    make_files("a.pdf", "b.pdf", size=two_thirds)
    spec = AttachmentSpec(common=["a.pdf", "b.pdf"], root=tmp_path)

    assert spec.resolve(recipient(), make_template()).needs_upload_session


def test_a_size_reads_the_way_a_person_would_say_it():
    assert describe_size(512) == "512 B"
    assert describe_size(2048) == "2 kB"
    assert describe_size(5 * 1024 * 1024) == "5.0 MB"
