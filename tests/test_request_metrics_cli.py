from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from kwikledgers_langgraph.lifecycle_cli import record_cli_metrics


class RequestMetricsCliTests(unittest.TestCase):
    def test_successful_operation_is_not_relabelled_when_metrics_fail(self):
        output = {"outcome": "completed", "results": {"work_item_id": 42}}
        with patch("kwikledgers_langgraph.lifecycle_cli.append_request_metrics", side_effect=OSError("disk full")):
            with redirect_stderr(io.StringIO()) as errors:
                record_cli_metrics({}, output)
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(output["results"]["work_item_id"], 42)
        self.assertIn("metrics_error", output)
        self.assertIn("não repita", errors.getvalue())

    def test_successful_record_preserves_original_output(self):
        output = {"outcome": "blocked"}
        with patch("kwikledgers_langgraph.lifecycle_cli.append_request_metrics") as writer:
            record_cli_metrics({"observability": {}}, output)
        writer.assert_called_once_with({"observability": {}})
        self.assertEqual(output, {"outcome": "blocked"})


if __name__ == "__main__":
    unittest.main()
