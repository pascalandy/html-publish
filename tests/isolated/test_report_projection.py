"""Summary and detail projections of one report, computed in process.

Failure modes:
F1: cli: summary text exceeds its 4 KiB cap or reports wrong UTF-8 omission counts
"""

from __future__ import annotations

import unittest
from typing import Any, cast

from html_publish import cli
from html_publish.model import Failure, Report


class ReportProjectionTest(unittest.TestCase):
    def test_summary_reports_exact_utf8_omissions_without_changing_detail(self) -> None:
        """Proves F1."""

        message = "€" * 1500 + '\n"\\'
        report = Report(
            "publish",
            "error",
            "https://publisher.test/pages/",
            None,
            None,
            error=Failure("delivery_failure", "verify", message, "inspect"),
        )

        summary = cast(dict[str, Any], cli.report_dict(report, "summary"))
        detail = cast(dict[str, Any], cli.report_dict(report, "detail"))

        self.assertEqual(summary["error"]["code"], "delivery_failure")
        self.assertEqual(summary["error"]["next_action"]["kind"], "inspect")
        self.assertEqual(summary["error"]["message"], "€" * 1365)
        self.assertEqual(
            summary["report"]["text"]["/error/message"],
            {"total_bytes": 4503, "included_bytes": 4095, "omitted_bytes": 408},
        )
        self.assertEqual(detail["error"]["message"], message)
        self.assertEqual(detail["report"]["text"]["/error/message"]["omitted_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
