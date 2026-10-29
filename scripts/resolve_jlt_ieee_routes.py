"""Resolve missing favorite JLT PDFs through DOI metadata, without fetching PDFs."""
import re
import time
from urllib.parse import urlsplit, parse_qs, quote
import requests


def ieee_url(location):
    parsed = urlsplit(str(location or ""))
    if parsed.scheme != "https" or parsed.hostname not in {"ieeexplore.ieee.org", "www.ieeexplore.ieee.org"}:
        return None
    match = re.search(r"/(?:abstract/)?document/(\d+)(?:/|$)", parsed.path)
    number = match.group(1) if match else parse_qs(parsed.query).get("arnumber", [""])[0]
    return f"https://ieeexplore.ieee.org/document/{number}" if number.isdigit() else None


def resolve_routes(db, *, limit=30):
    import pymysql
    connection = pymysql.connect(**db)
    updated = 0
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT article_number, doi FROM papers WHERE source_system='optica' "
                           "AND source_name='JLT' AND is_favorite=1 AND pdf_available=0 "
                           "AND doi LIKE '10.1109/JLT.%%' "
                           "AND COALESCE(url,'') NOT LIKE 'https://ieeexplore.ieee.org/document/%%' LIMIT %s", (limit,))
            rows = cursor.fetchall()
        with requests.Session() as session:
            for index, (key, doi) in enumerate(rows):
                if index:
                    time.sleep(2)
                try:
                    response = session.head("https://doi.org/" + quote(doi, safe="/"),
                                            allow_redirects=False, timeout=20)
                    route = ieee_url(response.headers.get("Location")) if response.is_redirect else None
                    response.close()
                    if not route:
                        continue
                    with connection.cursor() as cursor:
                        cursor.execute("UPDATE papers SET url=%s WHERE article_number=%s "
                                       "AND source_system='optica' AND source_name='JLT' AND pdf_available=0",
                                       (route, key))
                    connection.commit()
                    updated += 1
                except requests.RequestException:
                    continue
        return updated
    finally:
        connection.close()
