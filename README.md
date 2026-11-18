# Solana Wallet Activity

A dependency-free Python CLI for inspecting recent finalized activity associated with a Solana public address. The JSON report includes transaction status, UTC timestamps, native SOL balance changes, wallet-paid fees and missing details.

## Try the demo

Requires Python 3.10 or newer. From the project folder:

```powershell
python wallet_activity.py --demo
python -m unittest -v
```

The offline demo uses three synthetic records. One succeeds and adds 0.25 SOL to the address. One fails and still charges the address a 5,000-lamport fee. The third has a known success status and unavailable transaction details. The net change of the two records with available balances is 249,995,000 lamports.

## Query mainnet

Replace the example public address with the one you want to inspect.

```powershell
python wallet_activity.py --address 3M2b3tLji7rvscqrLAHMukYxDK2nB96Q9hwfV6QkdzBN --limit 5
```

Set the optional `SOLANA_RPC_URL` environment variable to use another endpoint. The CLI sends public addresses and transaction signatures to that endpoint. It uses no private keys or wallet connection.

The tool calls [getSignaturesForAddress](https://solana.com/docs/rpc/http/getsignaturesforaddress) for up to 25 recent signatures and [getTransaction](https://solana.com/docs/rpc/http/gettransaction) for each signature's details. It retries timeouts, connection errors, rate limits and selected server errors twice. If the initial history request fails, it returns a nonzero exit code. If a detail request fails, the report keeps the signature and records the error.

Balance changes are native SOL deltas in lamports and can include fees or rent. The summary sums only the rows with available balance data. The report covers this address and the requested history window. SPL token balances and activity limited to related token accounts are outside its scope.
