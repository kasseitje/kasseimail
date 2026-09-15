"""The desktop window.

Nothing under here is imported by the engine, and nothing under `kasseimail/` outside this package
imports Qt. That separation is what lets `kasseimail send` run on a machine with no display: the
CLI and the window drive the same `SendRun`, and only this side knows what a widget is.
"""
