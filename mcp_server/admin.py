"""Server-side administration of MCP credentials.

    python -m mcp_server.admin create --label "Masroor Grok Bot" \
        --tools get_sales_guest_counts,get_labor_metrics --stores 6,7,14
    python -m mcp_server.admin list | revoke KEY_ID | rotate KEY_ID | usage [KEY_ID]

There is deliberately no HTTP route for any of this. Run it as the service
user on the server; it needs filesystem access to MCP_DATA_DIR and nothing else.

A new secret is printed ONCE, to stdout only when stdout is a terminal (or
--write-secret-file PATH creates a 0600 file). It is never logged and cannot be
retrieved again -- only revoked/rotated.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import keystore


def _parse_stores(v: str):
    if v.strip() == "*":
        return "*"
    return [int(x) for x in v.split(",") if x.strip()]


def _deliver(token: str, rec, args) -> int:
    meta = {"key_id": rec.key_id, "label": rec.label, "status": rec.status,
            "tools": rec.tools, "stores": rec.stores, "expires_at": rec.expires_at}
    if args.write_secret_file:
        try:
            fd = os.open(args.write_secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token + "\n")
        except OSError as e:
            keystore.revoke_key(rec.key_id, "never delivered (secret file error)")
            print(f"error: could not write the secret file ({e.strerror}); the new key "
                  "was revoked.", file=sys.stderr)
            return 2
        print(json.dumps(meta, indent=2))
        print(f"\nSecret written once to {args.write_secret_file} (mode 0600). "
              "Hand it over securely, then shred the file.", file=sys.stderr)
        return 0
    if not sys.stdout.isatty():
        # Refuse to emit a secret into a pipe/log/CI capture by accident.
        keystore.revoke_key(rec.key_id, "never delivered (stdout not a terminal)")
        print("refusing to print a secret to a non-terminal; the key was revoked. "
              "Re-run in a terminal or use --write-secret-file.", file=sys.stderr)
        return 2
    print(json.dumps(meta, indent=2))
    print("\nTOKEN (shown once, store it in the Grok client secret store):\n" + token)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mcp_server.admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--label", required=True)
    c.add_argument("--tools", required=True, help="comma list, or 'all'")
    c.add_argument("--stores", required=True,
                   help="comma list of establishment ids, or '*' for every store")
    c.add_argument("--expires", help="ISO-8601 UTC expiry, e.g. 2027-01-01T00:00:00+00:00")
    c.add_argument("--rate-per-min", type=int)
    c.add_argument("--daily-asks", type=int)
    c.add_argument("--write-secret-file")
    sub.add_parser("list")
    r = sub.add_parser("revoke"); r.add_argument("key_id"); r.add_argument("--reason", default="")
    t = sub.add_parser("rotate"); t.add_argument("key_id")
    t.add_argument("--grace-hours", type=float, default=0.0)
    t.add_argument("--write-secret-file")
    u = sub.add_parser("usage"); u.add_argument("key_id", nargs="?")
    a = ap.parse_args(argv)

    sf = getattr(a, "write_secret_file", None)
    if sf and os.path.exists(sf):
        # Checked BEFORE anything changes, so a rotation can never revoke the old
        # key and then fail to hand over the new one.
        print("error: the secret file already exists; refusing to overwrite it.",
              file=sys.stderr)
        return 2
    try:
        if a.cmd == "create":
            tools = keystore.TOOLS if a.tools == "all" else [x.strip() for x in a.tools.split(",")]
            token, rec = keystore.create_key(
                a.label, tools, _parse_stores(a.stores), expires_at=a.expires,
                rate_per_min=a.rate_per_min, daily_asks=a.daily_asks)
            return _deliver(token, rec, a)
        if a.cmd == "list":
            for k in keystore.list_keys():
                print(json.dumps({"key_id": k.key_id, "label": k.label, "status": k.status,
                                  "created_at": k.created_at, "expires_at": k.expires_at,
                                  "revoked_at": k.revoked_at, "tools": k.tools,
                                  "stores": k.stores, "rate_per_min": k.rate_per_min,
                                  "daily_asks": k.daily_asks,
                                  "rotated_from": k.rotated_from}))
            return 0
        if a.cmd == "revoke":
            ok = keystore.revoke_key(a.key_id, a.reason)
            print("revoked" if ok else "no active key with that id")
            return 0 if ok else 1
        if a.cmd == "rotate":
            token, rec = keystore.rotate_key(a.key_id, grace_hours=a.grace_hours)
            return _deliver(token, rec, a)
        if a.cmd == "usage":
            for row in keystore.usage_summary(a.key_id, 50):
                print(json.dumps(row))
            return 0
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
