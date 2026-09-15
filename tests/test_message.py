"""The JSON handed to Graph.

Graph answers a wrongly-shaped message with a 400 whose text names a field, not a cause, and it does
so once per message with the run already under way. These are cheap to pin and expensive to find.
"""

from kasseimail.attachments import AttachmentSpec
from kasseimail.config import MAX_INLINE_TOTAL_BYTES
from kasseimail.message import FILE_ATTACHMENT, build, plan_delivery
from kasseimail.recipients import Recipient


def recipient():
    return Recipient(row=2, email="jan@example.be", key="jan@example.be", fields={})


def resolve(tmp_path, template, **kwargs):
    return AttachmentSpec(root=tmp_path, **kwargs).resolve(recipient(), template)


# -- the body ---------------------------------------------------------------------------------

def test_html_wins_when_there_is_both(tmp_path):
    """Graph's message carries one body, not a multipart alternative. The text version is written
    to the output directory and does not travel -- worth pinning, because the obvious reading of
    "both bodies rendered" is that both are sent."""
    message = build(subject="s", text="plain", html="<p>rich</p>", to=["jan@example.be"])

    assert message["body"] == {"contentType": "HTML", "content": "<p>rich</p>"}


def test_without_html_the_mail_goes_out_as_text(tmp_path):
    message = build(subject="s", text="plain", html=None, to=["jan@example.be"])

    assert message["body"] == {"contentType": "Text", "content": "plain"}


# -- the recipients -----------------------------------------------------------------------------

def test_the_recipients_are_shaped_the_way_graph_takes_them():
    message = build(subject="s", text="t", html=None, to=["jan@example.be"],
                    cc=["books@example.be"], bcc=["archive@example.be"],
                    reply_to=["info@example.be"])

    assert message["toRecipients"] == [{"emailAddress": {"address": "jan@example.be"}}]
    assert message["ccRecipients"][0]["emailAddress"]["address"] == "books@example.be"
    assert message["bccRecipients"][0]["emailAddress"]["address"] == "archive@example.be"
    assert message["replyTo"][0]["emailAddress"]["address"] == "info@example.be"


def test_empty_cc_and_bcc_are_left_out_rather_than_sent_as_empty_lists():
    """Graph takes an empty list, but the message is also what a person reads in the dry run, and
    an empty `ccRecipients` there reads as "somebody was meant to be in cc"."""
    message = build(subject="s", text="t", html=None, to=["jan@example.be"], cc=[], bcc=[])

    assert "ccRecipients" not in message and "bccRecipients" not in message


# -- attachments --------------------------------------------------------------------------------

def test_an_attachment_is_declared_as_a_file_attachment(tmp_path, make_files, make_template):
    """The `@odata.type` is what tells Graph this is a file and not an item or a reference; without
    it the message is refused."""
    make_files("handbook.pdf")
    found = resolve(tmp_path, make_template(), common=["handbook.pdf"])

    message = build(subject="s", text="t", html=None, to=["jan@example.be"],
                    attachments=found.attachments)

    attachment = message["attachments"][0]
    assert attachment["@odata.type"] == FILE_ATTACHMENT
    assert attachment["name"] == "handbook.pdf"
    assert attachment["contentBytes"]


def test_the_content_type_is_guessed_from_the_extension(tmp_path, make_files, make_template):
    """Sent as application/octet-stream, a PDF arrives as a download rather than a preview."""
    make_files("handbook.pdf", "notes.txt")
    found = resolve(tmp_path, make_template(), common=["handbook.pdf", "notes.txt"])

    message = build(subject="s", text="t", html=None, to=["a@b.be"],
                    attachments=found.attachments)

    by_name = {item["name"]: item["contentType"] for item in message["attachments"]}
    assert by_name["handbook.pdf"] == "application/pdf"
    assert by_name["notes.txt"].startswith("text/plain")


def test_the_bytes_are_base64_of_what_is_on_disk(tmp_path, make_files, make_template):
    """A wrong encoding here produces a file that arrives and will not open, which no status code
    reports."""
    import base64

    (path,) = make_files("handbook.pdf")
    found = resolve(tmp_path, make_template(), common=["handbook.pdf"])

    message = build(subject="s", text="t", html=None, to=["a@b.be"],
                    attachments=found.attachments)

    encoded = message["attachments"][0]["contentBytes"]
    assert base64.b64decode(encoded) == path.read_bytes()


# -- inline or upload session -------------------------------------------------------------------

def test_a_small_message_carries_its_attachments_and_needs_no_upload(tmp_path, make_files,
                                                                    make_template):
    make_files("small.pdf", size=1024)
    found = resolve(tmp_path, make_template(), common=["small.pdf"])

    delivery = plan_delivery(found, subject="s", text="t", html=None, to=["a@b.be"])

    assert not delivery.needs_upload_session
    assert len(delivery.message["attachments"]) == 1


def test_a_large_message_leaves_the_attachments_out_of_the_request(tmp_path, make_files,
                                                                  make_template):
    """They go up separately afterwards. Leaving them in as well would send the bytes twice and
    run straight into the size limit the upload session exists to avoid."""
    make_files("big.pdf", size=MAX_INLINE_TOTAL_BYTES + 1)
    found = resolve(tmp_path, make_template(), common=["big.pdf"])

    delivery = plan_delivery(found, subject="s", text="t", html=None, to=["a@b.be"])

    assert delivery.needs_upload_session
    assert "attachments" not in delivery.message
    assert [item.name for item in delivery.upload] == ["big.pdf"]


def test_a_message_with_no_attachments_at_all_has_no_attachments_key(tmp_path, make_template):
    delivery = plan_delivery(resolve(tmp_path, make_template()), subject="s", text="t",
                             html=None, to=["a@b.be"])

    assert "attachments" not in delivery.message
    assert delivery.upload == []
