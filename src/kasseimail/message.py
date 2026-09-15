"""One mail, in the shape Microsoft Graph takes it.

Graph's `message` carries a single `body` with one content type -- there is no multipart/alternative
here. So an HTML template wins and a plain-text one is used only when there is no HTML. The text
version is still rendered and written to the output directory, where it is what you read to check a
run; it just does not travel.

Attachments go one of two ways, and `plan_delivery` below decides which:

- **Inline**, base64 inside the request body, when the files together stay under ~3 MB. One request
  per mail, which is what almost every run does.
- **An upload session**, when they do not: create the message as a draft, push each file to a URL
  Graph hands back, then send the draft. Three or more requests, but no size ceiling worth worrying
  about.
"""

import base64
import mimetypes

from dataclasses import dataclass

from kasseimail.attachments import Attachment, Resolution
from kasseimail.config import MAX_INLINE_TOTAL_BYTES

FILE_ATTACHMENT = "#microsoft.graph.fileAttachment"


def _recipients(addresses) -> list[dict]:
    return [{"emailAddress": {"address": address}} for address in addresses if address]


def _content_type(attachment: Attachment) -> str:
    guessed, _ = mimetypes.guess_type(attachment.path.name)
    return guessed or "application/octet-stream"


def inline_attachment(attachment: Attachment) -> dict:
    """One file, base64 inside the message. Reads it into memory -- the caller has checked the size."""
    return {
        "@odata.type": FILE_ATTACHMENT,
        "name": attachment.name,
        "contentType": _content_type(attachment),
        "contentBytes": base64.b64encode(attachment.path.read_bytes()).decode("ascii"),
    }


def build(
    *,
    subject: str,
    text: str | None,
    html: str | None,
    to: list[str],
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    reply_to: list[str] | None = None,
    attachments: list[Attachment] | None = None,
) -> dict:
    """The message JSON. `attachments` empty means none *in this request* -- see `plan_delivery`."""
    if html is not None:
        body = {"contentType": "HTML", "content": html}
    else:
        body = {"contentType": "Text", "content": text or ""}

    message = {
        "subject": subject,
        "body": body,
        "toRecipients": _recipients(to),
    }

    if cc:
        message["ccRecipients"] = _recipients(cc)
    if bcc:
        message["bccRecipients"] = _recipients(bcc)
    if reply_to:
        message["replyTo"] = _recipients(reply_to)
    if attachments:
        message["attachments"] = [inline_attachment(item) for item in attachments]

    return message


@dataclass
class Delivery:
    """How this particular mail has to travel."""

    message: dict
    upload: list[Attachment]

    @property
    def needs_upload_session(self) -> bool:
        return bool(self.upload)


def plan_delivery(resolution: Resolution, **message_fields) -> Delivery:
    """Decide inline or upload session, and shape the message accordingly.

    All-or-nothing rather than splitting the files across both paths: a message that is partly
    inline and partly uploaded is two failure modes for one mail, and the saving is one request.
    """
    if resolution.total_size > MAX_INLINE_TOTAL_BYTES:
        return Delivery(
            message=build(attachments=None, **message_fields),
            upload=list(resolution.attachments),
        )

    return Delivery(
        message=build(attachments=resolution.attachments, **message_fields),
        upload=[],
    )
