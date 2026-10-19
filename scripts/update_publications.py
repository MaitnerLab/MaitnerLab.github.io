"""Regenerate publications.qmd from Brian Maitner's ORCID record.

Usage (from the repository root):
    python scripts/update_publications.py

Uses only the Python standard library. Work titles and types come from ORCID;
full citation details (authors, journal, volume, pages) come from doi.org.
Preprints that were later published as journal articles are left out.
"""

import concurrent.futures
import datetime
import difflib
import html
import json
import re
import os
import time
import urllib.error
import urllib.parse
import urllib.request

ORCID = "0000-0002-2118-9880"
OUTPUT = "publications.qmd"
CACHE = "scripts/publications_cache.json"  # saved citation details, so re-runs only fetch new DOIs
CONTACT = "bmaitner@usf.edu"  # sent to Crossref, per their API etiquette
EXCLUDE_DOIS = set()  # DOIs to leave off the page (e.g., repository copies of a paper)
# Current lab members (family name, first initial), shown in bold. Update as people join or leave.
LAB_MEMBERS = [("Maitner", "B"), ("Pillet", "M"), ("Grosscup", "J"), ("Diodato", "A")]
MAX_AUTHORS = 8  # longer author lists are shortened with "et al."
PREPRINT_SERVERS = {  # DOI prefix -> name (Crossref lists the publisher, not the server)
    "10.1101": "bioRxiv", "10.21203": "Research Square", "10.22541": "Authorea",
    "10.2139": "SSRN", "10.32942": "EcoEvoRxiv",
}
KEEP_FIELDS = ("title", "author", "container-title", "publisher", "volume", "issue", "page")
PREPRINT_SOURCES = ("biorxiv", "research square", "ssrn", "authorea", "ecoevorxiv", "osf")


def get_json(url, accept="application/json"):
    req = urllib.request.Request(
        url, headers={"Accept": accept, "User-Agent": f"MaitnerLab-site (mailto:{CONTACT})"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", html.unescape(re.sub(r"<[^>]+>", "", t)).lower()).strip()


def orcid_works():
    works = []
    for group in get_json(f"https://pub.orcid.org/v3.0/{ORCID}/works")["group"]:
        s = group["work-summary"][0]
        dois = [e["external-id-value"].lower() for e in group["external-ids"]["external-id"]
                if e["external-id-type"] == "doi"]
        if not dois:
            continue
        year = (s.get("publication-date") or {}).get("year")
        source = ((s.get("journal-title") or {}).get("value") or "").lower()
        kind = s["type"]
        if kind in ("other", "report"):
            kind = "preprint" if any(p in source for p in PREPRINT_SOURCES) else "report"
        if kind == "data-set" and "cran" in source:
            kind = "software"
        title = s["title"]["title"]["value"]
        if dois[0] in EXCLUDE_DOIS or re.match(r"(author |publisher )?(correction|erratum|corrigendum)", title, re.I):
            continue
        works.append({"doi": dois[0], "kind": kind, "title": title,
                      "year": int(year["value"]) if year else None})
    return works


def csl(doi):
    """Citation metadata for a DOI: Crossref first, then doi.org (covers DataCite DOIs)."""
    urls = [(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}?mailto={CONTACT}", None),
            (f"https://doi.org/{doi}", "application/vnd.citationstyles.csl+json")]
    for url, accept in urls:
        for attempt in range(5):
            try:
                data = get_json(url, accept or "application/json")
                data = data.get("message", data)
                data = {k: data[k] for k in KEEP_FIELDS if k in data}
                data["author"] = [{k: v for k, v in au.items() if k in ("family", "given", "literal", "name")}
                                  for au in data.get("author", [])]
                return data
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(2 ** attempt)
                    continue
                break  # e.g. 404: try the next source
            except Exception:  # noqa: BLE001 - network hiccup; retry
                time.sleep(2 ** attempt)
    print(f"  could not fetch citation details for {doi}; using ORCID title only")
    return {}


def first(value):
    """Crossref returns some CSL fields as lists."""
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def md_escape(text):
    text = html.unescape(re.sub(r"<[^>]+>", "", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"([*_\[\]`<>])", r"\\\1", text)


def is_lab_member(a):
    if "family" in a:
        return any(a["family"] == fam and a.get("given", "").startswith(ini) for fam, ini in LAB_MEMBERS)
    full = a.get("literal") or a.get("name") or ""
    return any(re.search(rf"\b{ini}\w*\.? .*\b{fam}\b", full) for fam, ini in LAB_MEMBERS)


def fmt_author(a):
    if "family" not in a:
        name = md_escape(a.get("literal") or a.get("name") or "")
        return f"**{name}**" if is_lab_member(a) else name
    initials = "".join(p[0] for p in re.split(r"[\s.\-]+", a.get("given", "")) if p)
    name = md_escape(f"{a['family']} {initials}".strip())
    return f"**{name}**" if is_lab_member(a) else name


def fmt_authors(authors):
    names = [fmt_author(a) for a in authors]
    if len(names) <= MAX_AUTHORS:
        return ", ".join(names)
    shown = names[:MAX_AUTHORS - 2]
    for i, a in enumerate(authors[len(shown):], start=len(shown)):
        if is_lab_member(a):  # keep lab members visible in shortened author lists
            shown += ["…", names[i]]
    return ", ".join(shown) + ", et al."


def fmt_entry(w, c):
    title = md_escape(first(c.get("title")) or w["title"]).rstrip(".")
    authors = fmt_authors(c.get("author", []))
    server = PREPRINT_SERVERS.get(w["doi"].split("/")[0]) if w["kind"] == "preprint" else None
    container = md_escape(server or first(c.get("container-title")) or c.get("publisher", ""))
    details = ""
    if c.get("volume"):
        details += f" {c['volume']}"
        if c.get("issue"):
            details += f"({c['issue']})"
        if c.get("page"):
            details += f": {c['page'].replace('-', '–')}"
    year = w["year"] or ""
    parts = [f"{authors} ({year}). {title}." if authors else f"{title} ({year})."]
    if container:
        parts.append(f"*{container}*{details}.")
    parts.append(f"[doi:{w['doi']}](https://doi.org/{w['doi']})")
    return " ".join(parts)


def main():
    works = orcid_works()
    print(f"{len(works)} works with DOIs on ORCID")
    cache = {}
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    todo = [w["doi"] for w in works if not cache.get(w["doi"])]
    print(f"{len(works) - len(todo)} cached; fetching citation details for {len(todo)}...")
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        cache.update(zip(todo, pool.map(csl, todo)))
    with open(CACHE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cache, f, indent=1, sort_keys=True)
    details = cache

    articles = [w for w in works if w["kind"] == "journal-article"]
    published = [norm_title(w["title"]) for w in articles]

    def unpublished(w):
        t = norm_title(w["title"])
        return not any(difflib.SequenceMatcher(None, t, p).ratio() > 0.85 for p in published)

    seen = set()

    def dedupe(ws):
        out = []
        for w in sorted(ws, key=lambda w: (-(w["year"] or 0), norm_title(w["title"]))):
            key = norm_title(w["title"])
            if key not in seen:
                seen.add(key)
                out.append(w)
        return out

    articles = dedupe(articles)
    preprints = dedupe([w for w in works if w["kind"] in ("preprint", "report") and unpublished(w)])
    software = dedupe([w for w in works if w["kind"] == "software"])
    datasets = dedupe([w for w in works if w["kind"] == "data-set"])

    lines = [
        "---",
        'title: "Publications"',
        'description: "Publications from the Maitner Lab at the University of South Florida – '
        'St. Petersburg: peer-reviewed papers, preprints, R packages, and datasets on '
        'biodiversity, plant traits, macroecology, conservation, and Open Science."',
        "---",
        "",
        f"<!-- Generated by scripts/update_publications.py on {datetime.date.today()}. "
        "Edit the script, not this file, then re-run it. -->",
        "",
        "Lab members' publications, drawn from "
        f"[Brian Maitner's ORCID record](https://orcid.org/{ORCID}). Current lab members are "
        "shown in **bold**. See also "
        "[Google Scholar](https://scholar.google.com/citations?user=GVb9deIAAAAJ).",
        "",
        "## Journal articles",
        "",
    ]
    year = None
    for w in articles:
        if w["year"] != year:
            year = w["year"]
            lines += [f"### {year or 'Undated'}", ""]
        lines += [f"- {fmt_entry(w, details[w['doi']])}", ""]
    for heading, ws in (("Preprints and reports", preprints),
                        ("Software (R packages)", software),
                        ("Datasets", datasets)):
        if ws:
            lines += [f"## {heading}", ""]
            lines += [line for w in ws for line in (f"- {fmt_entry(w, details[w['doi']])}", "")]

    with open(OUTPUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    print(f"Wrote {OUTPUT}: {len(articles)} articles, {len(preprints)} preprints/reports, "
          f"{len(software)} software, {len(datasets)} datasets")


if __name__ == "__main__":
    main()
