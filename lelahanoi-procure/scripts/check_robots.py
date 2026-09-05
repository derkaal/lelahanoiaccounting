#!/usr/bin/env python3
"""Check robots.txt of every shop we might read prices from, and record the result.

Why: hard constraint 2 says automated access is limited to public, logged-out pages
where robots.txt and the shop's terms allow it. The development sandbox could not
reach any shop domain, so this check has to run from the operator's machine.
Paste the output table into docs/suppliers.md.

Only robots.txt is fetched (one small request per host, honest User-Agent).
The terms of service are a legal text and are reviewed by a human, not parsed here.

Usage:
    python scripts/check_robots.py            # all hosts below
    python scripts/check_robots.py koro.com   # one host
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
import urllib.robotparser
from datetime import date

USER_AGENT = "lelahanoi-procure/0.1 (+internal price check for Le La Hanoi, Landshut; contact via shop account)"

# host -> representative public paths we would read (product page, search/listing).
HOSTS: dict[str, list[str]] = {
    "www.koro.com": ["/de/products/example", "/de/search?q=zucker"],
    "www.amazon.de": ["/dp/B000000000", "/s?k=rohrzucker"],
    "www.bremer-gewuerzhandel.de": ["/vanillezucker-gemahlen-mit-natur-vanille", "/search?search=vanille"],
    "www.pati-versand.de": ["/torten-kuchen/aromen/", "/search?search=vanille"],
    "www.asiafoodland.de": ["/manufacturer/vietnam.html", "/advanced_search_result.php?keywords=pandan"],
    "asia4friends.de": ["/zutaten", "/search?search=pandan"],
    "villagefoods.de": ["/collections/vietnamesische-lebensmittel-online-store", "/search?q=pandan"],
}


def check(host: str, paths: list[str]) -> tuple[str, str, list[tuple[str, bool]]]:
    url = f"https://{host}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "replace")
            status = f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        # 404 → no robots.txt → everything allowed by convention; other errors → treat as disallow.
        status = f"HTTP {e.code}"
        body = "" if e.code == 404 else "User-agent: *\nDisallow: /\n"
    except Exception as e:  # noqa: BLE001
        return host, f"ERROR {type(e).__name__}: {e}", [(p, False) for p in paths]
    rp.parse(body.splitlines())
    delay = rp.crawl_delay(USER_AGENT) or rp.crawl_delay("*")
    status += f", crawl-delay={delay}" if delay else ""
    return host, status, [(p, rp.can_fetch(USER_AGENT, f"https://{host}{p}") and rp.can_fetch("*", f"https://{host}{p}")) for p in paths]


def main() -> None:
    wanted = sys.argv[1:] or list(HOSTS)
    print(f"| host | robots.txt | path | allowed | checked |")
    print("|---|---|---|---|---|")
    today = date.today().isoformat()
    for host in wanted:
        h = host if host in HOSTS else next((k for k in HOSTS if host in k), host)
        _, status, results = check(h, HOSTS.get(h, ["/"]))
        for path, ok in results:
            print(f"| {h} | {status} | `{path}` | {'yes' if ok else 'NO'} | {today} |")


if __name__ == "__main__":
    main()
