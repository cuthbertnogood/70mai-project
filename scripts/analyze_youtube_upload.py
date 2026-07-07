#!/usr/bin/env python3
"""Analyze YouTube upload diagnostic log and print remediation hints."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from youtube_upload_diagnostics import DEFAULT_DIAG_LOG, analyze_log, format_report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Analyze youtube_upload.diag.jsonl and suggest fixes"
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_DIAG_LOG,
        help=f"Diagnostic JSONL (default: {DEFAULT_DIAG_LOG})",
    )
    parser.add_argument("--upload-id", help="Analyze only this upload_id")
    parser.add_argument(
        "--last",
        type=int,
        metavar="N",
        default=5,
        help="Analyze last N upload runs (default: 5)",
    )
    parser.add_argument(
        "--write-report",
        type=Path,
        metavar="PATH",
        help="Write markdown report to PATH",
    )
    parser.add_argument("--json", action="store_true", help="Output raw analysis JSON")
    args = parser.parse_args(argv)

    if not args.log.is_file():
        print(f"No diagnostic log at {args.log}", file=sys.stderr)
        print("Run an upload first; events append to this file automatically.", file=sys.stderr)
        raise SystemExit(1)

    analysis = analyze_log(
        args.log,
        upload_id=args.upload_id,
        last_n_uploads=None if args.upload_id else args.last,
    )

    if args.json:
        import json

        print(json.dumps(analysis, indent=2, ensure_ascii=False))
    else:
        print(format_report(analysis))

    if args.write_report:
        args.write_report.parent.mkdir(parents=True, exist_ok=True)
        args.write_report.write_text(format_report(analysis), encoding="utf-8")
        print(f"\nReport written: {args.write_report}", file=sys.stderr)


if __name__ == "__main__":
    from project_env import ensure_venv_python

    ensure_venv_python()
    main()
