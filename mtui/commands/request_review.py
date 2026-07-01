"""The ``request_review`` command — Slack-driven review and auto-approve.

Ensures the loaded update's testreport is committed, posts its
``reports_url/<RRID>/log`` URL to a configured Slack channel asking for a
review, then (unless ``--no-watch``) blocks streaming thread replies to the
user until a 👍 reaction acks the request. On an ack, unless ``--no-approve``
is given, it drives the existing ``approve`` path — recording the best-effort
reactor as the reviewer — bound to *this* fanout template.

The Slack message reference (channel + ts) is persisted durably in the
testreport (``set_slack_review``) so the ack survives a reload and is
re-checkable by the ``approve``/``reject`` review gate.
"""

import subprocess
from argparse import Namespace
from logging import getLogger

from ..cli.argparse import ArgumentParser
from ..cli.completion import complete_choices, template_completion
from ..data_sources import SlackClient
from ..support.cancellation import current_cancel_event
from ..support.exceptions import SlackError
from ..support.misc import requires_update
from ..support.spinner import spinner
from ..test_reports.svn_io import TemplateFormatError, svn_commit_testreport
from . import Command
from .approve import Approve

logger = getLogger("mtui.command.request_review")


class RequestReview(Command):
    """Requests a Slack review of the loaded update and auto-approves on ack.

    Commits the testreport (auto-committing first if needed), posts its log
    URL to the configured Slack channel, then watches the thread: replies are
    streamed as they arrive and a 👍 reaction is treated as review approval.
    On ack the update is approved with the reactor recorded as reviewer.

    Use ``--no-watch`` to post and return without watching, or ``--no-approve``
    to watch and report the ack without approving.
    """

    command = "request_review"
    scope = "fanout"

    @classmethod
    def _add_arguments(cls, parser: ArgumentParser) -> None:
        """Adds arguments to the command's argument parser."""
        parser.add_argument(
            "--no-watch",
            dest="no_watch",
            action="store_true",
            help="post the review request and return without watching the thread",
        )
        parser.add_argument(
            "--no-approve",
            dest="no_approve",
            action="store_true",
            help="watch and report the ack, but do not approve the update",
        )
        parser.add_argument(
            "-g",
            "--group",
            nargs="?",
            action="append",
            help="Group wanted to approve\n Not valid for Gitea Workflow",
        )
        parser.add_argument(
            "-u",
            "--user",
            action="store",
            default="",
            help="User override for gitea workflow (Gitea only)",
        )
        cls._add_template_arg(parser)

    @requires_update
    def __call__(self) -> None:
        """Commit, post to Slack, watch for an ack, and approve on 👍."""
        # 1) Auto-commit so the qam.suse.de /log mirror reflects the report we
        #    are asking people to review; abort before posting if it fails.
        try:
            svn_commit_testreport(
                self.metadata.report_wd(),
                self.config.install_logs,
                ["-m", "commit before review request"],
            )
        except subprocess.CalledProcessError as e:
            self.println(f"Failed to commit testreport, not requesting review: {e}")
            return

        # 1b) A template that cannot record the review marker would leave a
        #     dangling Slack message whose ack the approve gate can never see;
        #     refuse up front, before anything is posted.
        if not self.metadata.has_slack_review_anchor():
            self.println(
                f"Testreport for {self.metadata.rrid} has no "
                "'Test Plan Reviewer:' line to anchor the Slack review marker; "
                "fix the template before requesting review."
            )
            return

        url = self.metadata._testreport_url()  # noqa: SLF001 -- shared report URL helper
        channel = self.config.slack_channel

        # 2) Post the review request to Slack.
        try:
            client = SlackClient(self.config)
            ts = client.chat_postMessage(
                channel,
                f"Please review {self.metadata.rrid}: {url}",
            )
        except SlackError as e:
            self.println(f"Failed to post Slack review request: {e}")
            return

        # 3) Persist the marker so the ack survives a reload, then commit it.
        #    The anchor was checked before posting, but the file can still turn
        #    on us (concurrent edit, read-only checkout) — report it instead of
        #    dying with the request already live in the channel.
        try:
            self.metadata.set_slack_review(channel, ts)
        except (TemplateFormatError, OSError) as e:
            self.println(
                f"Posted review request {channel}/{ts} but could not record "
                f"the marker: {e}. The approve gate will not see this review; "
                "fix the template and re-run request_review."
            )
            return
        try:
            svn_commit_testreport(
                self.metadata.report_wd(),
                self.config.install_logs,
                ["-m", f"Add Slack Review: {channel}/{ts}"],
            )
        except subprocess.CalledProcessError as e:
            logger.error("Failed to commit Slack review marker: %s", e)

        if self.args.no_watch:
            self.println(
                f"Posted review request for {self.metadata.rrid}; not watching."
            )
            return

        # 4) Block on the thread until acked, timed out, or interrupted. A review
        # can take hours; the wait runs to slack_watch_timeout (a full working day
        # by default). In the REPL press Ctrl-C to stop watching; over MCP call
        # this with background=true and poll the job instead of blocking.
        self.println(
            f"Watching Slack for a 👍 on {self.metadata.rrid} "
            "(this can take a while; Ctrl-C to stop)."
        )
        with spinner(f"Waiting for review of {self.metadata.rrid}") as is_stopped:
            outcome = client.wait_for_ack(
                channel,
                ts,
                on_reply=self.println,
                should_stop=is_stopped,
                interval=self.config.slack_poll_interval,
                timeout=self.config.slack_watch_timeout,
                # Set by the MCP session when the job is cancelled or the
                # client disconnects, so the watch (and its worker thread)
                # exits promptly instead of polling on unobserved — and
                # possibly auto-approving — for hours. None in the REPL.
                cancel_event=current_cancel_event.get(),
            )

        if not outcome.acked:
            if outcome.unreachable:
                self.println(
                    f"Review watch for {self.metadata.rrid} failed (Slack unreachable)"
                )
            else:
                self.println(
                    f"No review ack for {self.metadata.rrid} (timed out or stopped)"
                )
            return

        self.println(f"Review acked by {outcome.reviewer} (best-effort reactor)")
        if self.args.no_approve:
            return

        # 5) Approve THIS fanout template (not prompt's active one) by binding
        #    the already-resolved metadata/targets onto a fresh Approve. Its
        #    __call__ runs the Slack review gate, which passes because the ack
        #    was just persisted and is still live.
        appr = Approve(
            Namespace(
                reviewer=outcome.reviewer,
                group=self.args.group,
                user=self.args.user,
                template=None,
                all_templates=False,
                force=False,
            ),
            self.config,
            self.sys,
            self.prompt,
        )
        appr.metadata = self.metadata
        appr.targets = self.targets
        appr.__call__()

    @staticmethod
    def complete(state, text, line, begidx, endidx) -> list[str]:
        """Provides tab completion for the command."""
        return complete_choices(
            [
                ("--no-watch",),
                ("--no-approve",),
                ("-g", "--group"),
                ("-u", "--user"),
                *template_completion(state),
            ],
            line,
            text,
        )
