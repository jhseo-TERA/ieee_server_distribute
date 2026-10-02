"""Run the reproducible full-corpus SerDes audit for the requested IEEE venues.

Each physical DB source is first screened in full from its local title and
metadata. Official IEEE abstracts are then fetched only for the Core/Adjacent/
review shortlist, followed by a second full screening pass and metric extraction.
Successful batches and terminal metadata misses are checkpointed in SQL, so the
command is safe to resume after interruption or an account rate-limit reset.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from serdes_data_pipeline import (
    db_connect,
    ensure_schema,
    extract_abstracts,
    fetch_abstracts,
    screen_venue,
)


@dataclass(frozen=True)
class VenueScope:
    venue: str
    year_from: int | None = None
    year_to: int | None = None
    label: str | None = None


# VLSI-Circuits is the circuit track through 2021. From 2022 onward the merged
# Technology and Circuits proceedings are stored under VLSI-Tech. MWCL is the
# 2006-2022 predecessor of the renamed MWTL series.
VENUE_SCOPES = (
    VenueScope("OJSSC"),
    VenueScope("ICTA"),
    VenueScope("VLSI-Tech", year_from=2022, label="VLSI 2022+"),
    VenueScope("VLSI-Circuits", label="VLSI Circuits"),
    VenueScope("MWTL"),
    VenueScope("ASSCC"),
    VenueScope("RFIC"),
    VenueScope("CICC"),
    VenueScope("MWCL", label="MWTL predecessor"),
    VenueScope("ISSCC"),
    VenueScope("JSSC"),
    VenueScope("TCAS-I"),
    VenueScope("TCAS-II"),
    VenueScope("ISCAS"),
)


def scope_total(conn, scope: VenueScope) -> int:
    where = ["source_system='ieee'", "source_name=%s"]
    params: list[object] = [scope.venue]
    if scope.year_from:
        where.append("CAST(year AS UNSIGNED)>=%s")
        params.append(scope.year_from)
    if scope.year_to:
        where.append("CAST(year AS UNSIGNED)<=%s")
        params.append(scope.year_to)
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) total FROM papers WHERE {' AND '.join(where)}", tuple(params))
        return int(cur.fetchone()["total"])


def parse_only(value: str | None) -> set[str] | None:
    if not value:
        return None
    return {item.strip().upper() for item in value.split(",") if item.strip()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="Comma-separated exact DB source names")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--delay", type=float, default=1.1)
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--skip-screen", action="store_true")
    parser.add_argument("--skip-extract", action="store_true")
    parser.add_argument("--retry-terminal", action="store_true")
    args = parser.parse_args()

    only = parse_only(args.only)
    scopes = [scope for scope in VENUE_SCOPES if only is None or scope.venue.upper() in only]
    unknown = (only or set()) - {scope.venue.upper() for scope in VENUE_SCOPES}
    if unknown:
        parser.error(f"Unknown venue(s): {', '.join(sorted(unknown))}")
    if not scopes:
        parser.error("No venue scopes selected")

    conn = db_connect()
    try:
        print(f"Schema: {ensure_schema(conn)} idempotent statements applied")
        for index, scope in enumerate(scopes, 1):
            total = scope_total(conn, scope)
            label = scope.label or scope.venue
            print(f"\n[{index}/{len(scopes)}] {label}: {total:,} papers")
            if not args.skip_screen:
                result = screen_venue(
                    conn,
                    scope.venue,
                    "all",
                    year_from=scope.year_from,
                    year_to=scope.year_to,
                )
                if result["targeted"] != total:
                    raise RuntimeError(
                        f"{scope.venue} pre-screening invariant failed: "
                        f"{result['targeted']} != {total}"
                    )
                print("Pre-screen:", result)
            if not args.skip_fetch:
                result = fetch_abstracts(
                    conn,
                    total,
                    args.batch_size,
                    args.delay,
                    venue=scope.venue,
                    scope="screened",
                    year_from=scope.year_from,
                    year_to=scope.year_to,
                    retry_terminal=args.retry_terminal,
                )
                print("Fetch:", result)
            if not args.skip_screen and not args.skip_fetch:
                result = screen_venue(
                    conn,
                    scope.venue,
                    "all",
                    year_from=scope.year_from,
                    year_to=scope.year_to,
                )
                if result["targeted"] != total:
                    raise RuntimeError(
                        f"{scope.venue} screening invariant failed: "
                        f"{result['targeted']} != {total}"
                    )
                print("Post-screen:", result)
            if not args.skip_extract:
                result = extract_abstracts(
                    conn,
                    venue=scope.venue,
                    screened_only=True,
                    year_from=scope.year_from,
                    year_to=scope.year_to,
                )
                print("Extract:", result)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
