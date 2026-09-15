"""Reading the recipient list -- mostly the ways a spreadsheet lies about its own values.

A file that cannot be opened is a failure somebody sees immediately. What is pinned here is the
file that opens fine and renders `1001.0` into a customer's mail.
"""

import datetime

import pytest

from kasseimail.recipients import RecipientProblem, clean_value, normalise_header
from kasseimail.recipients import load as load_recipients


# -- the values ------------------------------------------------------------------------------

def test_an_excel_whole_number_does_not_render_with_a_decimal_point(make_xlsx):
    """Excel stores every number as a float, so an invoice number comes back as `1001.0` and
    `{{ invoice_no }}` puts *1001.0* in front of a customer. Every other check passes while this
    goes wrong, which is what makes it worth a test of its own.
    """
    path = make_xlsx([
        ["email", "invoice_no"],
        ["jan@example.be", 1001],
    ])

    table = load_recipients(path)

    assert table.rows[0].fields["invoice_no"] == 1001
    assert str(table.rows[0].fields["invoice_no"]) == "1001"


def test_a_real_decimal_keeps_its_decimals(make_xlsx):
    """The fix above must not round a price to the nearest euro."""
    path = make_xlsx([["email", "amount"], ["jan@example.be", 1234.5]])

    assert load_recipients(path).rows[0].fields["amount"] == 1234.5


def test_a_date_arrives_as_a_datetime_for_the_date_filter(make_xlsx):
    """If it were flattened to a string here, `| date("%d %B %Y")` would have nothing to format."""
    path = make_xlsx([["email", "due"], ["jan@example.be", datetime.datetime(2026, 3, 1)]])

    assert isinstance(load_recipients(path).rows[0].fields["due"], datetime.datetime)


def test_an_empty_cell_becomes_an_empty_string_and_never_none(make_xlsx):
    """`{{ note }}` on a `None` renders the word None into the mail."""
    path = make_xlsx([["email", "note"], ["jan@example.be", None]])

    assert load_recipients(path).rows[0].fields["note"] == ""


def test_whitespace_around_a_value_is_dropped():
    """A trailing space on an address is invisible in the spreadsheet and fatal at Graph."""
    assert clean_value("  jan@example.be \n") == "jan@example.be"


# -- the headers -----------------------------------------------------------------------------

def test_a_header_becomes_a_name_a_template_can_type():
    """`{{ First Name }}` is not valid Jinja, and nobody should have to know that
    `{{ row['First Name'] }}` is the way round it."""
    assert normalise_header("First Name ", 0) == "first_name"
    assert normalise_header("E-mail", 0) == "e_mail"
    assert normalise_header("Bedrag (€)", 0) == "bedrag"


def test_an_unnamed_column_still_gets_a_name():
    """A blank header would otherwise collapse every unnamed column onto the empty string."""
    assert normalise_header("", 2) == "column_3"
    assert normalise_header(None, 0) == "column_1"


def test_a_header_starting_with_a_digit_is_made_usable():
    """`{{ 2026 }}` is a number literal, not a variable."""
    assert normalise_header("2026 total", 0) == "c_2026_total"


def test_two_headers_that_collapse_to_one_name_are_reported(make_xlsx):
    """"First Name" and "first name" both become `first_name` and the second wins silently, so
    half the mails would greet people by whatever the other column held."""
    path = make_xlsx([["email", "First Name", "first name"], ["jan@example.be", "Jan", "J."]])

    table = load_recipients(path)

    assert any("first_name" in warning for warning in table.warnings)


def test_a_column_the_run_would_overwrite_is_reported(make_xlsx):
    """The run puts its own `attachments` into the context. A column of that name is not lost
    quietly."""
    path = make_xlsx([["email", "attachments"], ["jan@example.be", "a.pdf"]])

    assert any("attachments" in warning for warning in load_recipients(path).warnings)


# -- CSV dialects ----------------------------------------------------------------------------

def test_a_semicolon_csv_is_read_without_being_told(make_csv):
    """A Belgian Excel writes semicolons. Asking which delimiter a file uses is asking somebody to
    open it in a text editor first."""
    path = make_csv([["email", "first_name"], ["jan@example.be", "Jan"]], delimiter=";")

    table = load_recipients(path)

    assert table.headers == ["email", "first_name"]
    assert table.rows[0].email == "jan@example.be"


def test_a_comma_csv_is_read_too(make_csv):
    path = make_csv([["email", "first_name"], ["jan@example.be", "Jan"]], delimiter=",")

    assert load_recipients(path).headers == ["email", "first_name"]


def test_a_byte_order_mark_does_not_end_up_in_the_first_header(make_csv):
    """Excel writes a BOM. Read as plain utf-8 it folds into the first header, leaving a column
    called `_email` that no template and no --email-column matches."""
    path = make_csv([["email", "first_name"], ["jan@example.be", "Jan"]], encoding="utf-8-sig")

    assert load_recipients(path).headers[0] == "email"


def test_a_single_column_file_is_not_an_error(make_csv):
    """There is nothing for the sniffer to find, and that is a normal file."""
    path = make_csv([["email"], ["jan@example.be"]])

    assert len(load_recipients(path)) == 1


def test_blank_rows_below_the_data_are_ignored(make_csv):
    """Excel leaves them behind constantly, and each one would otherwise be a row with no address."""
    path = make_csv([["email"], ["jan@example.be"], [""], [""]])

    assert len(load_recipients(path)) == 1


# -- addresses -------------------------------------------------------------------------------

def test_a_row_without_an_address_is_kept_and_listed(people):
    """Dropping it would make somebody disappear between the spreadsheet and the report. It is
    carried through and named, and the run skips it."""
    table = load_recipients(people)

    assert table.without_email == [4]
    assert table.rows[2].fields["first_name"] == "Nobody"


def test_an_address_that_cannot_work_is_found_before_the_run(make_csv):
    """A missing @ or a stray comma is a typo, and Graph reports it one message at a time after
    the run has started."""
    path = make_csv([
        ["email"],
        ["jan at example.be"],
        ["an@example,be"],
        ["good@example.be"],
    ])

    table = load_recipients(path)

    assert [row for row, _ in table.invalid_email] == [2, 3]


def test_an_address_with_a_plus_or_a_dash_is_accepted(make_csv):
    """The check is for typos, not for RFC conformance; refusing a real address is the worse error."""
    path = make_csv([["email"], ["jan+invoices@sub.example.co.uk"], ["a-b@example.be"]])

    assert load_recipients(path).invalid_email == []


# -- keys ------------------------------------------------------------------------------------

def test_the_key_is_the_address_unless_another_column_is_named(people):
    table = load_recipients(people)
    assert table.rows[0].key == "jan@example.be"

    keyed = load_recipients(people, key_column="Invoice No")
    assert keyed.rows[0].key == "1001"


def test_the_same_address_on_two_rows_is_reported_and_not_merged(people):
    """`persons.email` is not unique anywhere real: households share a mailbox. Merging them into
    one mail would show three people a mail that is mostly about somebody else."""
    table = load_recipients(people)

    assert table.duplicate_keys() == [("jan@example.be", [2, 5])]


def test_a_row_with_neither_a_key_nor_an_address_still_gets_one(people):
    """Otherwise every such row shares the empty key, and one `--resume` would skip them all."""
    table = load_recipients(people)

    assert table.rows[2].key == "row-4"


# -- what the row number means ----------------------------------------------------------------

def test_the_row_number_is_the_one_the_spreadsheet_shows(people):
    """Off by one here and every error message sends somebody to the wrong line of a 400-row file.
    Row 1 is the header, so the first recipient is row 2."""
    table = load_recipients(people)

    assert [row.row for row in table.rows] == [2, 3, 4, 5]


# -- sheets ----------------------------------------------------------------------------------

def test_a_named_sheet_can_be_chosen(make_xlsx):
    path = make_xlsx(
        [["email"], ["first@example.be"]],
        extra_sheets={"Members": [["email"], ["member@example.be"]]},
    )

    assert load_recipients(path, sheet="Members").rows[0].email == "member@example.be"


def test_asking_for_a_sheet_that_is_not_there_lists_the_ones_that_are(make_xlsx):
    path = make_xlsx([["email"], ["a@example.be"]], extra_sheets={"Members": [["email"]]})

    with pytest.raises(RecipientProblem) as refusal:
        load_recipients(path, sheet="Leden")

    assert "Members" in str(refusal.value)


# -- refusals --------------------------------------------------------------------------------

def test_a_file_without_the_address_column_says_which_columns_it_has(make_csv):
    """The most common mistake is a column called something else, and the fix is on the next line."""
    path = make_csv([["e-mail", "name"], ["jan@example.be", "Jan"]])

    with pytest.raises(RecipientProblem) as refusal:
        load_recipients(path)

    assert "e_mail" in str(refusal.value)
    assert "--email-column" in str(refusal.value)


def test_naming_the_address_column_is_forgiving_about_its_spelling(make_csv):
    """`--email-column "E-Mail"` should match the column headed "E-Mail", not a normalised form
    the user never saw."""
    path = make_csv([["E-Mail"], ["jan@example.be"]])

    assert load_recipients(path, email_column="E-Mail").rows[0].email == "jan@example.be"


def test_an_unreadable_format_says_what_to_do_about_it(tmp_path):
    """An .xls from an old Excel is the case that turns up, and it needs saving as .xlsx."""
    path = tmp_path / "people.xls"
    path.write_bytes(b"\xd0\xcf\x11\xe0")

    with pytest.raises(RecipientProblem) as refusal:
        load_recipients(path)

    assert ".xlsx" in str(refusal.value)


def test_a_missing_file_is_refused_by_name(tmp_path):
    with pytest.raises(RecipientProblem) as refusal:
        load_recipients(tmp_path / "nope.csv")

    assert "nope.csv" in str(refusal.value)


# -- the context a template sees ---------------------------------------------------------------

def test_the_context_carries_the_row_the_address_and_the_attachments(people):
    table = load_recipients(people)

    context = table.rows[0].context(attachments=["a.pdf"])

    assert context["row"] == 2
    assert context["email"] == "jan@example.be"
    assert context["attachments"] == ["a.pdf"]
    assert context["first_name"] == "Jan"
