"""Templates -- the ways a template renders and is still wrong.

A template that raises is a template somebody fixes. What is pinned here is the other kind: the
body that renders fine and carries the wrong thing into a mailbox.
"""

import datetime

import pytest

from kasseimail.templates import Template, TemplateProblem, TemplateSet


# -- escaping -------------------------------------------------------------------------------

def test_the_html_body_escapes_and_the_subject_and_plain_text_do_not(make_template):
    """`select_autoescape(["html"])` matches the *last* extension, and every file here ends in
    `.j2` -- so it reads `body.html.j2` as "not HTML" and renders it raw. The values come out of
    somebody's spreadsheet, and an unescaped `<` breaks the markup of every mail in the run. In the
    subject and the plain text `&amp;` would be the bug instead.
    """
    template = make_template(
        subject="Invoice for {{ company }}\n",
        html="<p>{{ company }}</p>\n",
        text="{{ company }}\n",
    )

    rendered = template.render({"company": "Peeters & <Zn>"})

    assert "&amp;" in rendered.html and "&lt;Zn&gt;" in rendered.html
    assert "<Zn>" not in rendered.html

    assert rendered.text.strip() == "Peeters & <Zn>"
    assert rendered.subject == "Invoice for Peeters & <Zn>"


# -- undefined names ------------------------------------------------------------------------

def test_a_misspelled_field_raises_instead_of_rendering_a_blank(make_template):
    """Without StrictUndefined `{{ frist_name }}` renders as nothing and seventy mails go out
    opening with "Dear ,". That cannot be taken back; a refusal can."""
    template = make_template(html="<p>Dear {{ frist_name }}</p>\n")

    with pytest.raises(TemplateProblem) as refusal:
        template.render({"first_name": "Jan"})

    assert "frist_name" in str(refusal.value)


def test_the_error_names_the_file_and_the_line(make_template):
    """A template set has four files; "undefined" without a filename sends somebody looking
    through all of them."""
    template = make_template(html="<p>one</p>\n<p>{{ nope }}</p>\n")

    with pytest.raises(TemplateProblem) as refusal:
        template.render({})

    assert "body.html.j2" in str(refusal.value)


# -- the subject ----------------------------------------------------------------------------

def test_only_the_first_line_of_the_subject_is_used(make_template):
    """A `.j2` file ends in a newline and an editor leaves stray lines behind. Graph rejects a
    subject containing one, and the failure names the whole message rather than the stray line."""
    template = make_template(subject="The real subject\nleft over from an edit\n")

    assert template.render({}).subject == "The real subject"


def test_a_comment_only_subject_does_not_become_a_blank_subject(make_template):
    """The starter template leads with a long Jinja comment listing the fields. If that were all
    that survived, every mail would go out with an empty subject and nobody would notice until
    they looked in Sent Items."""
    template = make_template(subject="{# just a comment #}\nHello\n")

    assert template.render({}).subject == "Hello"


# -- which body travels ---------------------------------------------------------------------

def test_a_template_without_html_still_loads_and_renders_text(make_template):
    """Graph carries one body. A text-only template is a complete template, not half of one."""
    template = make_template(html=None, text="Dear {{ first_name }},\n")

    rendered = template.render({"first_name": "Jan"})

    assert rendered.html is None
    assert rendered.text.startswith("Dear Jan")


def test_a_template_with_no_body_at_all_is_refused_when_it_is_loaded(make_template):
    """Not at the first render, which is after the recipients are read and the run has started."""
    with pytest.raises(TemplateProblem) as refusal:
        make_template(html=None, text=None)

    assert "no body" in str(refusal.value)


def test_a_template_without_a_subject_is_refused(make_template):
    with pytest.raises(TemplateProblem) as refusal:
        make_template(subject=None)

    assert "subject.j2" in str(refusal.value)


# -- the filters ----------------------------------------------------------------------------

def test_the_date_filter_formats_a_spreadsheet_datetime(make_template):
    """Rendering one raw gives `2026-03-01 00:00:00` in front of a customer."""
    import datetime

    template = make_template(html='<p>{{ due | date("%d %B %Y") }}</p>\n')

    rendered = template.render({"due": datetime.datetime(2026, 3, 1)})

    assert "01 March 2026" in rendered.html
    assert "00:00:00" not in rendered.html


@pytest.mark.parametrize("where", ["C", "nl_BE.UTF-8", "fr_FR.UTF-8"])
def test_a_date_renders_the_same_whatever_the_process_locale_is(make_template, where):
    """**The bug this is really about.** `strftime("%B")` reads the process locale, and that is
    not ours to rely on: constructing a QApplication calls `setlocale(LC_ALL, "")`, and so does
    anything else in a large GUI stack that feels like it -- at a moment nobody controls. The same
    template then renders "01 March 2026" from the command line and "01 maart 2026" from the
    window, on the same machine, from the same file.
    """
    import locale

    template = make_template(html='<p>{{ due | date("%d %B %Y") }}</p>\n')

    previous = locale.setlocale(locale.LC_ALL)
    try:
        try:
            locale.setlocale(locale.LC_ALL, where)
        except locale.Error:
            pytest.skip(f"{where} is not installed here")

        rendered = template.render({"due": datetime.datetime(2026, 3, 1)})
    finally:
        locale.setlocale(locale.LC_ALL, previous)

    assert "01 March 2026" in rendered.html


def test_the_language_of_a_date_is_chosen_in_the_template(make_template):
    """Taking it from the machine would mean a mail that reads differently depending on who ran
    it, which for a tool whose whole output is text is not a detail."""
    template = make_template(
        html='<p>{{ due | date("%d %B %Y", "nl") }} / {{ due | date("%A", "fr") }}</p>\n')

    rendered = template.render({"due": datetime.datetime(2026, 3, 1)})

    assert "01 maart 2026" in rendered.html
    assert "dimanche" in rendered.html


def test_an_unknown_date_language_falls_back_rather_than_failing(make_template):
    """A typo in the language should not stop a run of seventy mails."""
    template = make_template(html='<p>{{ due | date("%B", "kl") }}</p>\n')

    assert "March" in template.render({"due": datetime.datetime(2026, 3, 1)}).html


def test_the_numeric_parts_of_a_date_still_come_from_strftime(make_template):
    """Only the names are substituted; everything else is strftime's job and stays its job."""
    template = make_template(html='<p>{{ due | date("%Y-%m-%d %H:%M") }}</p>\n')

    rendered = template.render({"due": datetime.datetime(2026, 3, 1, 14, 30)})

    assert "2026-03-01 14:30" in rendered.html


def test_the_date_filter_passes_text_through_unchanged(make_template):
    """The same column is a date in one export and text in the next; the template should not have
    to care which file it was handed."""
    template = make_template(html="<p>{{ due | date }}</p>\n")

    assert "1 March" in template.render({"due": "1 March"}).html


def test_the_money_filter_groups_the_continental_way(make_template):
    template = make_template(html="<p>{{ amount | money }}</p>\n")

    assert "€ 1.234,50" in template.render({"amount": 1234.5}).html


def test_the_blank_filter_turns_an_empty_cell_into_nothing(make_template):
    """`{{ note }}` on an empty cell must never render the word None."""
    template = make_template(html="<p>{{ note | blank }}</p>\n")

    assert template.render({"note": None}).html.strip() == "<p></p>"


# -- meta.toml ------------------------------------------------------------------------------

def test_meta_carries_the_defaults_the_template_owns(make_template):
    template = make_template(meta="""
        description = "Invoices"
        required = ["email", "invoice_no"]
        attachments = ["invoices/{{ invoice_no }}.pdf"]
        cc = ["books@example.be"]
    """)

    assert template.meta.description == "Invoices"
    assert template.meta.required == ["email", "invoice_no"]
    assert template.meta.attachments == ["invoices/{{ invoice_no }}.pdf"]
    assert template.meta.cc == ["books@example.be"]


def test_a_missing_meta_is_not_an_error(make_template):
    """meta.toml is optional; a template that is only text is a whole template."""
    assert make_template().meta.description == ""


def test_a_broken_meta_names_the_file_rather_than_being_ignored(make_template):
    """Silently falling back to the defaults makes a typo in the TOML look like a missing column,
    and the message you would get is about the wrong thing."""
    with pytest.raises(TemplateProblem) as refusal:
        make_template(meta="required = [unquoted]\n")

    assert "meta.toml" in str(refusal.value)


def test_a_single_string_where_a_list_belongs_is_accepted(make_template):
    """`cc = "someone@example.be"` is what people write. Treating it as a list of characters is a
    failure mode nobody would guess from the error."""
    template = make_template(meta='cc = "books@example.be"\n')

    assert template.meta.cc == ["books@example.be"]


# -- the directory --------------------------------------------------------------------------

def test_a_new_template_comes_out_of_the_built_in_starter(template_dir):
    """An empty template directory is a dead end -- the window opens on nothing to click."""
    templates = TemplateSet(template_dir)

    created = templates.create("welcome")

    assert created.name == "welcome"
    assert (created.directory / "subject.j2").is_file()
    assert (created.directory / "body.html.j2").is_file()
    assert created.render({"first_name": "Jan", "attachments": []}).subject


def test_the_starter_template_renders_with_only_the_columns_it_requires(template_dir):
    """Its meta says it needs `email` and `first_name`. If it referenced anything else, a brand-new
    template would fail its own preflight and there would be nowhere to start."""
    template = TemplateSet(template_dir).create("welcome")
    context = {name: "x" for name in template.meta.required} | {"attachments": [], "row": 1}

    rendered = template.render(context)

    assert rendered.subject and rendered.html and rendered.text


def test_a_new_template_can_be_copied_from_an_existing_one(template_dir, make_template):
    make_template("invoice", subject="Invoice {{ invoice_no }}\n")

    copied = TemplateSet(template_dir).create("reminder", copy_from="invoice")

    assert copied.read_part("subject.j2") == "Invoice {{ invoice_no }}\n"


def test_creating_a_template_that_exists_refuses_rather_than_overwriting(template_dir,
                                                                        make_template):
    """The files being overwritten are somebody's text, and there is no undo."""
    make_template("welcome")

    with pytest.raises(TemplateProblem):
        TemplateSet(template_dir).create("welcome")


def test_a_name_with_a_slash_in_it_is_refused(template_dir):
    """The name becomes a directory and a filename. `../x` would write outside the template
    directory entirely."""
    with pytest.raises(TemplateProblem):
        TemplateSet(template_dir).create("../escape")


def test_an_unknown_template_lists_the_ones_that_exist(template_dir, make_template):
    """A typo in `-t` is the most common way this fails, and the fix is on the next line."""
    make_template("welcome")
    make_template("invoice")

    with pytest.raises(TemplateProblem) as refusal:
        TemplateSet(template_dir).get("wecome")

    assert "welcome" in str(refusal.value) and "invoice" in str(refusal.value)


def test_a_directory_without_a_subject_is_not_listed_as_a_template(template_dir):
    """`assets/`, `.git/` and a half-made folder all live next to the real ones."""
    (template_dir / "assets").mkdir()
    (template_dir / "assets" / "logo.png").write_bytes(b"x")

    assert TemplateSet(template_dir).names() == []


def test_saving_a_part_is_visible_to_the_very_next_render(make_template):
    """The window saves on every keystroke pause and re-renders the preview. Jinja caches compiled
    templates by name and mtime, and two saves inside one second would otherwise show the first."""
    template = make_template(html="<p>before</p>\n")

    template.write_part("body.html.j2", "<p>after</p>\n")

    assert "after" in template.render({}).html


def test_adding_a_text_body_to_a_template_that_had_none_takes_effect(make_template):
    """`has_text` is worked out when the template loads; the editor can create the file later."""
    template = make_template(text=None)
    assert template.render({}).text is None

    template.write_part("body.txt.j2", "plain\n")

    assert template.render({}).text == "plain\n"


def test_the_variables_a_template_uses_can_be_listed(make_template):
    """What the window checks a spreadsheet against before anybody presses send."""
    template = make_template(
        subject="Invoice {{ invoice_no }}\n",
        html="<p>Dear {{ first_name }}, {{ amount | money }}</p>\n",
    )

    assert {"invoice_no", "first_name", "amount"} <= template.variables()


def test_render_string_uses_the_same_rules_as_a_body(make_template):
    """Attachment patterns go through here. A pattern naming a column that does not exist has to
    fail the same way a body does, or a missing PDF becomes a file called `invoices/.pdf`."""
    template = make_template()

    assert template.render_string("out/{{ n }}.pdf", {"n": 7}) == "out/7.pdf"

    with pytest.raises(TemplateProblem):
        template.render_string("out/{{ missing }}.pdf", {})


def test_a_template_directory_that_does_not_exist_yet_is_created_and_seeded(tmp_path):
    """First run on a new machine. Without the seed, `templates list` is empty and there is no
    hint about what a template even looks like."""
    templates = TemplateSet(tmp_path / "nowhere" / "templates")

    templates.ensure()

    assert templates.names() == ["basic"]
    assert isinstance(templates.get("basic"), Template)
