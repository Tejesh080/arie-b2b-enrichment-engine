"""Generate the evaluation dataset to disk."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from arie.evalgen.generator import (
    DEFAULT_CALIBRATION_LEADS,
    DEFAULT_TEST_LEADS,
    generate_dataset,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the ARIE evaluation dataset")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("data/eval"))
    parser.add_argument("--calibration-leads", type=int, default=DEFAULT_CALIBRATION_LEADS)
    parser.add_argument("--test-leads", type=int, default=DEFAULT_TEST_LEADS)
    args = parser.parse_args()

    leads, manifest = generate_dataset(
        seed=args.seed,
        calibration_leads=args.calibration_leads,
        test_leads=args.test_leads,
    )

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    leads_path = out / "leads.jsonl"
    with leads_path.open("w", encoding="utf-8", newline="\n") as fh:
        for lead in leads:
            fh.write(json.dumps(lead.to_json(), sort_keys=True, separators=(",", ":")))
            fh.write("\n")

    manifest_path = out / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    print(f"wrote {manifest.n_leads} leads across {manifest.n_companies} companies")
    print(f"  {leads_path}")
    print(f"  {manifest_path}")
    print(f"  sha256={manifest.content_sha256[:16]}…")
    print(f"  bands={manifest.band_counts}")
    print(f"  decisions={manifest.decision_counts}")
    print(f"  cheap_misleads={manifest.cheap_misleads_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
