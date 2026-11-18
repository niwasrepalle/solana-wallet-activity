import io
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from wallet_activity import DemoRpc, RpcError, SolanaRpc, collect_activity, public_address

FIXTURE = json.loads(Path(__file__).with_name("demo_fixture.json").read_text(encoding="utf-8"))
ADDRESS = FIXTURE["address"]


class ReportTests(unittest.TestCase):
    def test_demo_preserves_failures_and_missing_details(self):
        report = collect_activity(DemoRpc(FIXTURE), ADDRESS, 3, "synthetic demo")
        self.assertEqual(report["summary"], {
            "transactions": 3, "successful": 2, "failed": 1, "unknown_status": 0,
            "missing_balance_details": 1, "known_balance_change_lamports": 249995000,
        })
        success, failure, missing = report["transactions"]
        self.assertEqual(success["fee_paid_lamports"], 0)
        self.assertEqual(failure["fee_paid_lamports"], 5000)
        self.assertIn("InstructionError", failure["transaction_error"])
        self.assertIsNone(missing["time_utc"])
        self.assertIsNone(missing["balance_change_lamports"])

    def test_detail_timeout_keeps_signature(self):
        rpc = Mock()
        rpc.call.side_effect = [[{"signature": "one", "err": None}], RpcError("timed out")]
        report = collect_activity(rpc, ADDRESS, 1, "test")
        self.assertEqual(report["summary"]["missing_balance_details"], 1)
        self.assertIsNone(report["summary"]["known_balance_change_lamports"])
        self.assertEqual(report["transactions"][0]["detail_error"], "timed out")

    def test_empty_history(self):
        rpc = Mock()
        rpc.call.return_value = []
        report = collect_activity(rpc, ADDRESS, 1, "test")
        self.assertEqual(report["transactions"], [])
        self.assertIsNone(report["summary"]["known_balance_change_lamports"])

    def test_valid_public_address(self):
        self.assertEqual(public_address(ADDRESS), ADDRESS)
        self.assertEqual(public_address("1" * 32), "1" * 32)


class RpcTests(unittest.TestCase):
    def setUp(self):
        self.rpc = SolanaRpc("https://example.test", 2)

    @patch("wallet_activity.time.sleep")
    @patch("wallet_activity.urlopen")
    def test_timeout_retries_then_succeeds(self, urlopen, sleep):
        body = io.BytesIO(b'{"jsonrpc":"2.0","id":1,"result":[]}')
        urlopen.side_effect = [URLError(TimeoutError()), body]
        self.assertEqual(self.rpc.call("getSignaturesForAddress", []), [])
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.5)
        request = urlopen.call_args.args[0]
        self.assertEqual(json.loads(request.data)["method"], "getSignaturesForAddress")

    @patch("wallet_activity.time.sleep")
    @patch("wallet_activity.urlopen")
    def test_exhausted_timeouts_fail_cleanly(self, urlopen, sleep):
        urlopen.side_effect = TimeoutError()
        with self.assertRaisesRegex(RpcError, "timed out after three attempts"):
            self.rpc.call("getSignaturesForAddress", [])
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch("wallet_activity.time.sleep")
    @patch("wallet_activity.urlopen")
    def test_rate_limit_is_retried(self, urlopen, sleep):
        rate_limit = HTTPError("https://example.test", 429, "rate limited", {}, None)
        urlopen.side_effect = [rate_limit, io.BytesIO(b'{"id":1,"result":null}')]
        self.assertIsNone(self.rpc.call("getTransaction", []))
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.5)

    @patch("wallet_activity.urlopen")
    def test_missing_result_is_not_treated_as_empty_history(self, urlopen):
        urlopen.return_value = io.BytesIO(b'{"id":1}')
        with self.assertRaisesRegex(RpcError, "missing its result"):
            self.rpc.call("getSignaturesForAddress", [])
