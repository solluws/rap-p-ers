#!/usr/bin/env python3
"""
Scrape member profiles from a Root community into a CSV.

Speed notes -- the original version cost ~420 requests for 200 rows:


  * It resolved avatar AND banner URLs for every member, then threw most of
    them away. Whether someone has a banner is visible on the profile itself,
    so filtering first removes ~90% of the asset requests.
  * It re-drew random members each round, so later rounds mostly re-fetched
    people it had already seen.
  * Every request was sequential.

This version walks the member list once, fetches profiles in batches of 100,
filters, and only then resolves assets for the survivors -- concurrently.
"""

import asyncio
import csv
import os
import random
import time
from pathlib import Path

# Run from anywhere, installed or not: put the project root on sys.path.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from rootpy import RootClient

OUTPUT_FILE = "scraped_profiles.csv"
COMMUNITY_ID = os.environ.get(
    "ROOT_COMMUNITY_ID", "002b3f7c-e3e2-8c02-9cbe-0185e8ddd654"
)
COUNT = int(os.environ.get("SCRAPE_COUNT", "200"))

# Image size to store: 32, 128, 512, or None for the largest available.
IMAGE_SIZE = None

PROFILE_BATCH = 100      # profiles per request
TOKEN_FILE = Path(__file__).with_name("tokens.txt")


def load_token() -> str:
    token = os.environ.get("ROOT_TOKEN", "").strip()
    if token:
        return token
    if TOKEN_FILE.exists():
        for line in TOKEN_FILE.read_text(encoding="utf-8").splitlines():
            key, _, value = line.strip().partition("=")
            if key.strip().lower() == "token" and value.strip():
                return value.strip()
    raise SystemExit(
        "No token. Set ROOT_TOKEN or create tokens.txt:\n    token=<your token>"
    )


async def scrape_profiles(community_id: str, count: int) -> list[dict]:
    client = RootClient(token=load_token())
    started = time.perf_counter()

    rows: list[dict] = []
    try:
        await client.login_token()

        # One GetExtended, then cached -- this costs nothing per batch.
        members = list(await client.get_members(community_id))
        if not members:
            print("No members visible in that community.")
            return rows

        random.shuffle(members)          # sample without repeating anyone
        print(f"{len(members)} members; looking for {count} with a banner")

        for start in range(0, len(members), PROFILE_BATCH):
            if len(rows) >= count:
                break

            batch = members[start:start + PROFILE_BATCH]

            # One request for up to 100 profiles.
            profiles = await client.get_profiles([m.user_id for m in batch])

            # Filter BEFORE touching the asset service: banner_uri is already
            # on the profile, so rejects cost nothing.
            keepers = [
                profile for profile in profiles.values()
                if profile.banner_uri
            ][:count - len(rows)]
            if not keepers:
                continue

            # Resolve only the survivors' images, concurrently.
            uris = [
                uri for profile in keepers
                for uri in (profile.avatar_url, profile.banner_uri) if uri
            ]
            urls = await client.asset_urls(uris, size=IMAGE_SIZE)

            for profile in keepers:
                banner = urls.get(profile.banner_uri)
                if not banner:
                    continue
                rows.append(
                    {
                        "user_id": profile.user_id,
                        "username": profile.username or "",
                        "avatar_url": urls.get(profile.avatar_url, "") or "",
                        "banner_url": banner,
                        "about_me": profile.description or "",
                    }
                )

            print(
                f"  scanned {min(start + PROFILE_BATCH, len(members))}"
                f"/{len(members)} -> {len(rows)} rows"
            )

    finally:
        elapsed = time.perf_counter() - started
        stats = client.timings()["totals"]
        print(
            f"\n{len(rows)} rows in {elapsed:.1f}s "
            f"({stats['calls']} requests, {stats['roundtrip_ms']:.0f}ms "
            f"round trip, {stats['waiting_ms']:.0f}ms waiting)"
        )
        await client.close()

    return rows[:count]


async def main() -> None:
    rows = await scrape_profiles(COMMUNITY_ID, COUNT)
    if not rows:
        print("No members scraped.")
        return

    fieldnames = ["user_id", "username", "avatar_url", "banner_url", "about_me"]
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    note = "" if len(rows) >= COUNT else f" (wanted {COUNT}; that's all with banners)"
    print(f"Wrote {len(rows)} profiles to {OUTPUT_FILE}{note}")
    print(
        "\nNote: these image URLs are signed and expire in a few weeks.\n"
        "For a lasting copy use client.save_asset(uri, path) to store the bytes."
    )


if __name__ == "__main__":
    asyncio.run(main())
