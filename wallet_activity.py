"""Read recent finalized Solana account activity into a portable report."""
import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

MAINNET_RPC = "https://api.mainnet-beta.solana.com"
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class RpcError(Exception):
    """An RPC request could not produce a usable result."""


def public_address(value):
    value = value.strip()
    if not 32 <= len(value) <= 44:
        raise argparse.ArgumentTypeError("address must encode a 32-byte public key")
    number = 0
    for character in value:
        digit = BASE58.find(character)
        if digit < 0:
            raise argparse.ArgumentTypeError("address contains an invalid Base58 character")
        number = number * 58 + digit
    leading_zeros = len(value) - len(value.lstrip("1"))
    if leading_zeros + (number.bit_length() + 7) // 8 != 32:
        raise argparse.ArgumentTypeError("address must encode a 32-byte public key")
    return value


class SolanaRpc:
    def __init__(self, endpoint, timeout):
        self.endpoint = endpoint
        self.timeout = timeout

    def call(self, method, params):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        request = Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(3):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    message = json.load(response)
                break
            except HTTPError as error:
                code = error.code
                error.close()
                if code not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise RpcError(f"{method}: HTTP {code}") from None
            except (TimeoutError, URLError, OSError) as error:
                if attempt == 2:
                    reason = getattr(error, "reason", error)
                    label = "timed out" if isinstance(reason, TimeoutError) else "connection failed"
                    raise RpcError(f"{method}: {label} after three attempts") from None
            except (ValueError, UnicodeError):
                raise RpcError(f"{method}: invalid JSON response") from None
            time.sleep(0.5 * 2**attempt)

        if not isinstance(message, dict) or message.get("id") != 1:
            raise RpcError(f"{method}: invalid RPC response")
        if message.get("error") is not None:
            error = message["error"]
            code = error.get("code") if isinstance(error, dict) else None
            label = f" ({code})" if type(code) is int else ""
            raise RpcError(f"{method}: RPC returned an error{label}")
        if "result" not in message:
            raise RpcError(f"{method}: RPC response is missing its result")
        return message["result"]


def transaction_amounts(transaction, address):
    """Return the address balance change and any fee paid by that address."""
    if transaction is None:
        return None, None
    try:
        metadata = transaction["meta"]
        keys = transaction["transaction"]["message"]["accountKeys"]
        index = next(i for i, account in enumerate(keys) if account["pubkey"] == address)
        before = metadata["preBalances"][index]
        after = metadata["postBalances"][index]
    except (KeyError, TypeError, IndexError, StopIteration):
        return None, None
    delta = after - before if type(before) is int and type(after) is int else None
    fee = metadata.get("fee") if index == 0 else 0
    return delta, fee if type(fee) is int else None


def collect_activity(rpc, address, limit, source):
    signatures = rpc.call("getSignaturesForAddress", [address, {"limit": limit, "commitment": "finalized"}])
    if not isinstance(signatures, list):
        raise RpcError("getSignaturesForAddress: expected a list")
    rows = []
    for item in signatures:
        if not isinstance(item, dict) or not isinstance(item.get("signature"), str):
            raise RpcError("getSignaturesForAddress: invalid signature entry")
        error = item.get("err")
        status = "unknown" if "err" not in item else "success" if error is None else "failed"
        detail_error = None
        try:
            transaction = rpc.call("getTransaction", [item["signature"], {
                "encoding": "jsonParsed", "commitment": "finalized",
                "maxSupportedTransactionVersion": 0,
            }])
        except RpcError as exc:
            transaction = None
            detail_error = str(exc)
        delta, fee = transaction_amounts(transaction, address)
        timestamp = item.get("blockTime")
        try:
            utc_time = datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if type(timestamp) is int else None
        except (OverflowError, OSError, ValueError):
            utc_time = None
        rows.append({
            "signature": item["signature"], "slot": item.get("slot"), "time_utc": utc_time,
            "status": status, "transaction_error": error, "balance_change_lamports": delta,
            "fee_paid_lamports": fee, "detail_error": detail_error,
        })

    known_deltas = [row["balance_change_lamports"] for row in rows if row["balance_change_lamports"] is not None]
    return {
        "source": source, "address": address, "commitment": "finalized",
        "summary": {
            "transactions": len(rows),
            "successful": sum(row["status"] == "success" for row in rows),
            "failed": sum(row["status"] == "failed" for row in rows),
            "unknown_status": sum(row["status"] == "unknown" for row in rows),
            "missing_balance_details": len(rows) - len(known_deltas),
            "known_balance_change_lamports": sum(known_deltas) if known_deltas else None,
        },
        "transactions": rows,
    }


class DemoRpc:
    """Replay synthetic RPC results without contacting a network."""
    def __init__(self, fixture):
        self.fixture = fixture

    def call(self, method, params):
        if method == "getSignaturesForAddress":
            return self.fixture["signatures"][:params[1]["limit"]]
        return self.fixture["transactions"][params[0]]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--address", type=public_address, help="Public Solana account address")
    mode.add_argument("--demo", action="store_true", help="Run against synthetic data offline")
    parser.add_argument("--limit", type=int, default=10, help="Number of recent transactions (1-25)")
    parser.add_argument("--timeout", type=float, default=5.0, help="Timeout in seconds per RPC attempt")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 25:
        parser.error("--limit must be between 1 and 25")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a finite positive number")

    if args.demo:
        fixture_path = Path(__file__).with_name("demo_fixture.json")
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        rpc, address, source = DemoRpc(fixture), fixture["address"], "synthetic demo"
    else:
        endpoint = os.getenv("SOLANA_RPC_URL", MAINNET_RPC)
        try:
            parsed_url = urlsplit(endpoint)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
                raise ValueError
        except ValueError:
            parser.error("SOLANA_RPC_URL must be a valid HTTP(S) endpoint")
        rpc, address, source = SolanaRpc(endpoint, args.timeout), args.address, "live RPC"
    try:
        report = collect_activity(rpc, address, args.limit, source)
    except RpcError as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
