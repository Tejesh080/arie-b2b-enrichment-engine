"""Mint an ARIE organization API key directly against the database.

Bootstrap tooling for machine callers (n8n, the demo script) that need a
`leads:*`/`reviews:*`-scoped credential before any owner/admin JWT session
exists to call `POST /api-keys` through the API itself — there is no
Supabase-authenticated human user for local dev or for the n8n/demo
integrations, so this talks to `arie.apikeys.create_api_key` directly instead.

The raw key is printed to stdout exactly once and nowhere else persisted or
logged, matching `create_api_key`'s own contract. Paste it straight into an
n8n credential or a local `.env` — never into a file this repo tracks.
"""

from __future__ import annotations

import argparse
import sys
from uuid import UUID

import psycopg

from arie.apikeys import SCOPES, create_api_key
from arie.config import DATABASE
from arie.tenancy import LEGACY_ORGANIZATION_ID

_BOOTSTRAP_CREATED_BY_USER_ID = UUID("00000000-0000-0000-0000-0000000000b0")
"""A fixed sentinel, not a real user — `organization_api_keys.created_by_user_id`
carries no foreign key to `auth.users` (see `migrations/0017`'s own docstring
for why), so there is no real session to attribute this to when minting a key
through this bootstrap path rather than the admin-JWT `POST /api-keys` route."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--label", required=True, help="human-readable label, e.g. 'n8n lead ingestion'"
    )
    parser.add_argument(
        "--scopes",
        required=True,
        help=f"comma-separated, from: {', '.join(SCOPES)}",
    )
    parser.add_argument(
        "--organization-id",
        default=str(LEGACY_ORGANIZATION_ID),
        help="defaults to the legacy/local-dev organization",
    )
    args = parser.parse_args(argv)

    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    unknown = sorted(set(scopes) - set(SCOPES))
    if unknown:
        print(f"unknown scope(s) {unknown} — valid scopes are {list(SCOPES)}", file=sys.stderr)
        return 1

    if not DATABASE.url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 1

    with psycopg.connect(DATABASE.url) as conn:
        record, raw_key = create_api_key(
            conn,
            organization_id=UUID(args.organization_id),
            created_by_user_id=_BOOTSTRAP_CREATED_BY_USER_ID,
            label=args.label,
            scopes=scopes,
        )

    print(f"key_id:  {record.key_id}")
    print(f"label:   {record.label}")
    print(f"scopes:  {list(record.scopes)}")
    print(f"raw_key: {raw_key}")
    print(
        "\nCopy raw_key into its n8n credential (or a local .env) now — it will "
        "never be shown again and is not stored anywhere in plaintext."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
