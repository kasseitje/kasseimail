"""Grouping several rows into one message, and how the other columns are combined.

The case this exists for: a flea market books stands one row at a time, so the same person turns up
three times with three stand numbers and should get one mail listing all three -- not three mails
each mentioning one.
"""

import pytest

from kasseimail.recipients import (
    DEFAULT_SEPARATOR, GroupSpec, RecipientProblem, aggregate_values,
)
from kasseimail.recipients import group as group_recipients
from kasseimail.recipients import load as load_recipients


@pytest.fixture
def market(make_xlsx):
    """Three stands for Jan, one for An. Two prices differ, so `sum` has something to do."""
    return make_xlsx([
        ["Email", "First Name", "Stand Number", "Size", "Price"],
        ["jan@example.be", "Jan", 12, "3m", 15.0],
        ["jan@example.be", "Jan", 14, "3m", 15.0],
        ["jan@example.be", "Jan", 19, "6m", 30.0],
        ["an@example.be", "An", 7, "3m", 15.0],
    ], name="market.xlsx")


def grouped(path, by="email", **aggregators):
    table = load_recipients(path)
    return group_recipients(table, GroupSpec(by=by, aggregators=aggregators))


# -- the aggregators themselves ---------------------------------------------------------------

def test_auto_keeps_one_value_when_every_row_agrees():
    """The name is the same on all three rows, and "Jan, Jan, Jan" is not a name."""
    assert aggregate_values(["Jan", "Jan", "Jan"], "auto") == "Jan"


def test_auto_lists_the_values_when_they_differ():
    """Which is the whole point: three stand numbers become the list you wanted to send."""
    assert aggregate_values([12, 14, 19], "auto") == "12, 14, 19"


def test_auto_needs_no_configuration_to_do_the_right_thing_to_a_whole_sheet():
    """Both halves at once, because that is what makes `auto` the default: the columns that are
    the same collapse and the ones that differ list, with nothing chosen by anybody."""
    assert aggregate_values(["3m", "3m"], "auto") == "3m"
    assert aggregate_values(["3m", "6m"], "auto") == "3m, 6m"


def test_auto_does_not_repeat_a_value_that_appears_twice():
    assert aggregate_values([12, 14, 12], "auto") == "12, 14"


def test_first_takes_the_first_row_s_value():
    assert aggregate_values([12, 14, 19], "first") == 12


def test_list_keeps_every_value_including_repeats():
    """`unique` is the one that dedupes; `list` says what was in the file."""
    assert aggregate_values([12, 14, 12], "list") == "12, 14, 12"


def test_unique_drops_the_repeats_but_keeps_the_order():
    assert aggregate_values([14, 12, 14], "unique") == "14, 12"


def test_sum_adds_the_numbers_up():
    assert aggregate_values([15.0, 15.0, 30.0], "sum") == 60


def test_a_whole_total_is_not_shown_with_a_decimal_point():
    """"3 stands" and "3.0 stands" are the same fact and only one of them reads as deliberate."""
    assert aggregate_values([1, 2], "sum") == 3
    assert isinstance(aggregate_values([1, 2], "sum"), int)


def test_sum_keeps_the_fraction_when_there_is_one():
    assert aggregate_values([15.5, 15.0], "sum") == 30.5


def test_sum_ignores_what_is_not_a_number():
    """A column with a stray "n/a" in it should still total the rest rather than refuse."""
    assert aggregate_values([10, "n/a", 5], "sum") == 15


def test_sum_of_nothing_numeric_is_blank_rather_than_zero():
    """Zero is a claim about the data; blank is the absence of one."""
    assert aggregate_values(["n/a", ""], "sum") == ""


def test_count_counts_the_rows_that_had_a_value():
    assert aggregate_values([12, "", 19], "count") == 2


def test_blank_cells_never_reach_the_list():
    """A row with nothing in the column would otherwise show as ", 14, 19"."""
    assert aggregate_values(["", 14, None, 19], "auto") == "14, 19"


def test_one_surviving_value_keeps_its_own_type():
    """`auto` hands back the value rather than its text when the rows agree, so a number stays a
    number and `| money` still works on it."""
    assert aggregate_values(["", 14, None], "auto") == 14


def test_an_unknown_aggregator_is_refused_by_name():
    with pytest.raises(RecipientProblem) as refusal:
        aggregate_values([1, 2], "average")

    assert "average" in str(refusal.value)


def test_the_separator_can_be_changed():
    assert aggregate_values([12, 14], "list", separator=" / ") == "12 / 14"


# -- grouping a real file --------------------------------------------------------------------

def test_rows_with_the_same_address_become_one_message(market):
    table = grouped(market)

    assert len(table) == 2
    assert table.grouped
    assert [row.email for row in table.rows] == ["jan@example.be", "an@example.be"]


def test_the_group_carries_the_rows_it_came_from(market):
    """So a template can lay them out as a table with each stand's size beside it, rather than
    only ever getting joined strings."""
    table = grouped(market)
    jan = table.rows[0]

    assert jan.count == 3
    assert jan.source_rows == [2, 3, 4]
    assert [member["stand_number"] for member in jan.members] == [12, 14, 19]


def test_the_template_context_gets_both_the_list_and_the_rows(market):
    context = grouped(market).rows[0].context()

    assert context["stand_number"] == "12, 14, 19"
    assert context["first_name"] == "Jan"
    assert context["count"] == 3
    assert [row["size"] for row in context["rows"]] == ["3m", "3m", "6m"]


def test_a_column_can_be_summed_instead_of_listed(market):
    assert grouped(market, price="sum").rows[0].fields["price"] == 60


def test_a_group_of_one_is_unchanged(market):
    table = grouped(market)
    an = table.rows[1]

    assert an.count == 1
    assert an.fields["stand_number"] == 7
    assert an.source_rows == [5]


def test_the_row_number_of_a_group_is_its_first(market):
    """It is what the report records and half of what --resume matches on, so it has to be one
    number and it has to be stable."""
    assert grouped(market).rows[0].row == 2


def test_grouping_is_off_unless_asked_for(market):
    table = load_recipients(market)

    assert group_recipients(table, GroupSpec()) is table
    assert not table.grouped


# -- the things that would quietly go wrong ------------------------------------------------------

def test_blank_group_values_are_never_collapsed_together(make_xlsx):
    """They would otherwise become one recipient keyed on the empty string -- a single message
    standing for everybody the file failed to identify, which is the worst way to lose them."""
    path = make_xlsx([
        ["email", "reference"],
        ["", "A"],
        ["", "B"],
        ["jan@example.be", "C"],
    ])

    table = group_recipients(load_recipients(path), GroupSpec(by="email"))

    assert len(table) == 3


def test_the_address_is_never_a_joined_list(make_xlsx):
    """Graph cannot send to "a@x.be, b@y.be". Grouping on something other than the address has to
    leave the address a single address."""
    path = make_xlsx([
        ["email", "company"],
        ["a@example.be", "Acme"],
        ["b@example.be", "Acme"],
    ])

    table = group_recipients(load_recipients(path), GroupSpec(by="company"))

    assert len(table) == 1
    assert table.rows[0].email == "a@example.be"
    assert table.rows[0].fields["email"] == "a@example.be"


def test_a_group_spanning_two_addresses_says_so(make_xlsx):
    """It almost always means the group column is not the one you meant, and only one of the two
    people is going to hear from you."""
    path = make_xlsx([
        ["email", "company"],
        ["a@example.be", "Acme"],
        ["b@example.be", "Acme"],
    ])

    table = group_recipients(load_recipients(path), GroupSpec(by="company"))

    assert any("2 addresses" in warning for warning in table.warnings)


def test_grouping_by_a_column_that_is_not_there_says_which_columns_are(make_xlsx):
    path = make_xlsx([["email", "company"], ["a@example.be", "Acme"]])

    with pytest.raises(RecipientProblem) as refusal:
        group_recipients(load_recipients(path), GroupSpec(by="bedrijf"))

    assert "company" in str(refusal.value)


def test_aggregating_a_column_that_is_not_there_is_refused_too(make_xlsx):
    path = make_xlsx([["email", "company"], ["a@example.be", "Acme"]])

    with pytest.raises(RecipientProblem):
        group_recipients(load_recipients(path), GroupSpec(by="email",
                                                         aggregators={"prijs": "sum"}))


def test_addresses_group_regardless_of_case(make_xlsx):
    """Jan@Example.be and jan@example.be are one mailbox, and two mails is the bug."""
    path = make_xlsx([
        ["email", "stand"],
        ["Jan@Example.be", 1],
        ["jan@example.be", 2],
    ])

    assert len(group_recipients(load_recipients(path), GroupSpec(by="email"))) == 1


def test_a_date_that_is_the_same_on_every_row_stays_a_date(make_xlsx):
    """`auto` returns the original value rather than its text when the rows agree, so a template's
    `| date(...)` still has a datetime to format."""
    import datetime

    path = make_xlsx([
        ["email", "due"],
        ["a@example.be", datetime.datetime(2026, 3, 1)],
        ["a@example.be", datetime.datetime(2026, 3, 1)],
    ])

    table = group_recipients(load_recipients(path), GroupSpec(by="email"))

    assert isinstance(table.rows[0].fields["due"], datetime.datetime)


# -- the command line form ---------------------------------------------------------------------

def test_the_flags_parse_into_a_spec():
    spec = GroupSpec.parse("Email", ["Stand Number=list", "price=sum"])

    assert spec.by == "email"
    assert spec.aggregators == {"stand_number": "list", "price": "sum"}
    assert spec.enabled


def test_a_malformed_aggregate_flag_says_what_it_wanted():
    with pytest.raises(RecipientProblem) as refusal:
        GroupSpec.parse("email", ["price"])

    assert "COLUMN=HOW" in str(refusal.value)


def test_an_unknown_aggregator_on_the_flag_lists_the_real_ones():
    with pytest.raises(RecipientProblem) as refusal:
        GroupSpec.parse("email", ["price=average"])

    assert "sum" in str(refusal.value)


def test_no_group_column_means_no_grouping():
    assert not GroupSpec.parse(None, []).enabled
    assert GroupSpec.parse(None, []).separator == DEFAULT_SEPARATOR
