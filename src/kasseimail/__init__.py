"""kasseimail -- bulk mail over the Microsoft Graph API.

A template, a spreadsheet and a list of attachments go in; a mail per row comes out. The CLI is
`kasseimail`, the desktop window is `kasseimail gui`, and both drive the same engine in `run.py`.
"""

from kasseimail.version import __version__

__all__ = ["__version__"]
