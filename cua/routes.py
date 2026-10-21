"""Route templates: turning concrete URLs into parameterised patterns and back.

    http://host/acme/mbr/100234/share/new   (base path /acme, member_id=100234)
        -> canonicalize ->  /mbr/{{member_id}}/share/new
    /mbr/result?q1={{member_id}}  -> render(member_id=100456) -> /mbr/result?q1=100456

Routes are stored relative to the tenant base path, so the same artifact runs
on /acme and /blueriver. Concrete ids that are *not* parameters become ``:id``
wildcards so a postcondition never depends on recorded data.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

PLACEHOLDER = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
IDLIKE = re.compile(r"^(?=.*\d)[A-Za-z0-9-]{4,}$")


def base_path(base_url: str) -> str:
    return urlsplit(base_url).path.rstrip("/")


def relative(url: str, base_url: str) -> tuple[str, str]:
    """(path relative to tenant base, query string)."""
    parts = urlsplit(url)
    bp = base_path(base_url)
    path = parts.path
    if bp and path.startswith(bp):
        path = path[len(bp):] or "/"
    return path, parts.query


def canonicalize(url: str, base_url: str, param_values: dict[str, str]) -> str:
    path, query = relative(url, base_url)
    by_value = {v: k for k, v in param_values.items() if v}

    def seg(s: str) -> str:
        if s in by_value:
            return "{{" + by_value[s] + "}}"
        return ":id" if IDLIKE.match(s) else s

    tpath = "/".join(seg(s) for s in path.split("/"))
    if query:
        q = [(k, "{{" + by_value[v] + "}}" if v in by_value else ("*" if IDLIKE.match(v) else v))
             for k, v in parse_qsl(query, keep_blank_values=True)]
        tpath += "?" + "&".join(f"{k}={v}" for k, v in q)
    return tpath


def render(template: str, params: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        if m.group(1) not in params:
            raise KeyError(f"route needs param {m.group(1)!r}")
        return quote(str(params[m.group(1)]), safe="")
    return PLACEHOLDER.sub(sub, template)


def matches(template: str, url: str, base_url: str, params: dict[str, str]) -> bool:
    """Does a concrete URL satisfy a route template (path exact, query subset)?"""
    tpath, _, tquery = template.partition("?")
    path, query = relative(url, base_url)
    tsegs, segs = tpath.strip("/").split("/"), path.strip("/").split("/")
    if len(tsegs) != len(segs):
        return False
    for t, s in zip(tsegs, segs):
        if t in (":id", "*"):
            continue
        if PLACEHOLDER.fullmatch(t):
            if params.get(PLACEHOLDER.fullmatch(t).group(1)) != s:
                return False
        elif t != s:
            return False
    if tquery:
        actual = dict(parse_qsl(query, keep_blank_values=True))
        for k, v in parse_qsl(tquery, keep_blank_values=True):
            if k not in actual:
                return False
            if v == "*":
                continue
            want = PLACEHOLDER.sub(lambda m: str(params.get(m.group(1), "")), v)
            if actual[k] != want:
                return False
    return True


def join(base_url: str, route: str) -> str:
    return base_url.rstrip("/") + route


__all__ = ["canonicalize", "render", "matches", "relative", "base_path", "join", "urlencode"]
