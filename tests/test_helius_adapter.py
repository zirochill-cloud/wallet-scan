"""Backward signature paging: ``before`` is the cursor, ``until`` is not.

Solana's ``getSignaturesForAddress`` returns signatures newest-first and pages
backwards only when the request carries ``before``. ``until`` means "stop when
you reach this signature", so a walk that passes ``until`` re-reads the same
newest page forever. These tests pin the parameter mapping, not the network.
"""

import json
import sys

from solscope.adapters.helius import HeliusRpc


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _recorder(pages):
    """A fake opener that records each request's param object and replays pages."""
    seen = []

    def opener(req, timeout=20):
        payload = json.loads(req.data)
        seen.append(payload["params"][1])
        index = min(len(seen) - 1, len(pages) - 1)
        body = {"jsonrpc": "2.0", "id": payload["id"], "result": pages[index]}
        return _Response(json.dumps(body).encode())

    return seen, opener


def page(suffix: str, count: int = 2):
    return [{"signature": f"{suffix}{i}", "slot": i, "blockTime": 1_700_000_000 + i}
            for i in range(count)]


def main() -> int:
    checks = []

    def check(name, ok, detail=""):
        checks.append(ok)
        print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))

    seen, opener = _recorder([page("a"), page("b"), page("c")])
    rpc = HeliusRpc(api_key="test-key", opener=opener)

    first, _ = rpc.signatures_for_address("MINT", limit=2)
    check("page 1 carries no cursor", "before" not in seen[0] and "until" not in seen[0], f"{seen[0]}")

    oldest = first["result"][-1]["signature"]
    second, _ = rpc.signatures_for_address("MINT", limit=2, before=oldest)
    check("page 2 passes page 1's oldest signature as before",
          seen[1].get("before") == oldest, f"{seen[1]}")
    check("page 2 does not repurpose until", "until" not in seen[1], f"{seen[1]}")

    rpc.signatures_for_address("MINT", before="X", until="Y")
    check("before and until coexist independently",
          seen[2].get("before") == "X" and seen[2].get("until") == "Y", f"{seen[2]}")

    check("pages differ", second["result"][0]["signature"] != first["result"][0]["signature"])

    failed = checks.count(False)
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
