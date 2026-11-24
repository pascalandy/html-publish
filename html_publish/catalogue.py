"""Pure metadata parsing and rendering for the private publication catalogue."""

from __future__ import annotations

import html
import ipaddress
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import cast
from urllib.parse import urlsplit

_MAX_METADATA_BYTES = 8 * 1024
_MAX_FIELD_LENGTH = 160
_MAX_ITEMS = 12
_MAX_ITEM_LENGTH = 64
_KINDS = frozenset({"dashboard", "prototype", "report", "unclassified"})
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_KIND_LABELS = {
    "dashboard": "Tableau de bord",
    "prototype": "Prototype",
    "report": "Rapport",
    "unclassified": "À classer",
}


@dataclass(frozen=True)
class CatalogueMetadata:
    title: str
    kind: str
    topics: tuple[str, ...]
    projects: tuple[str, ...]
    source_url: str | None


@dataclass(frozen=True)
class CatalogueEntry:
    name: str
    metadata: CatalogueMetadata
    publication_url: str
    state: str
    updated_at: str


class _HeadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_head = False
        self.in_title = False
        self.explicit_head = False
        self.body_started = False
        self.title_parts: list[str] = []
        self.metadata_json: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "head":
            self.explicit_head = True
            self.in_head = True
            return
        if tag == "body":
            self.body_started = True
            self.in_head = False
            self.in_title = False
            return
        if tag == "title" and (self.in_head or (not self.explicit_head and not self.body_started)):
            self.in_title = True
        if self.in_head and tag == "meta":
            values = {key.lower(): value for key, value in attrs}
            name = values.get("name")
            if name is not None and name.casefold() == "html-publish-catalogue":
                self.metadata_json = values.get("content")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "title":
            self.in_title = False
        if tag == "head":
            self.in_head = False
        if tag == "body":
            self.body_started = True
            self.in_title = False

    def handle_data(self, data: str) -> None:
        implicit_head = not self.explicit_head and not self.body_started
        if (
            (self.in_head or implicit_head)
            and self.in_title
            and sum(map(len, self.title_parts)) < 512
        ):
            self.title_parts.append(data)


def _clean_string(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > limit:
        return None
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
        return None
    return value


def _valid_source_url(value: object) -> str | None:
    if not isinstance(value, str) or any(character.isspace() for character in value):
        return None
    cleaned = _clean_string(value, 2048)
    if cleaned is None or any(character.isspace() or character in "\\?#" for character in cleaned):
        return None
    try:
        parsed = urlsplit(cleaned)
        port = parsed.port
        hostname = parsed.hostname
        path = parsed.path
        segments = path[1:].split("/") if path.startswith("/") else []
        valid_host = _valid_https_host(hostname)
        github_path = (
            len(segments) >= 5
            and all(segments[:2])
            and segments[2] in {"blob", "tree"}
            and bool(segments[3])
            and all(segments[4:])
            and all(segment not in {".", ".."} for segment in segments)
        )
        gitlab_file = any(
            index >= 2
            and segments[index] == "-"
            and segments[index + 1] in {"blob", "tree"}
            and index + 3 < len(segments)
            and all(segments[:index])
            and bool(segments[index + 2])
            and all(segments[index + 3 :])
            and all(segment not in {".", ".."} for segment in segments)
            for index in range(max(0, len(segments) - 1))
        )
        if (
            parsed.scheme.casefold() != "https"
            or not parsed.netloc
            or not valid_host
            or parsed.username is not None
            or parsed.password is not None
            or (port is not None and not 1 <= port <= 65535)
            or path.endswith("/")
            or "//" in path
            or not (github_path or gitlab_file)
        ):
            return None
    except ValueError:
        return None
    return cleaned


def _valid_https_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        if ":" in hostname:
            return False
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if not ascii_hostname or len(ascii_hostname) > 253:
        return False
    return all(_HOST_LABEL.fullmatch(label) is not None for label in ascii_hostname.split("."))


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    items = cast(list[object], value)
    result: list[str] = []
    seen: set[str] = set()
    for item in items[:_MAX_ITEMS]:
        cleaned = _clean_string(item, _MAX_ITEM_LENGTH)
        if cleaned is None or cleaned.casefold() in seen:
            continue
        result.append(cleaned)
        seen.add(cleaned.casefold())
    return tuple(result)


def _metadata(payload: object, fallback_title: str) -> CatalogueMetadata:
    values = cast(dict[str, object], payload) if isinstance(payload, dict) else {}
    title = _clean_string(values.get("title"), _MAX_FIELD_LENGTH) or fallback_title
    kind = _clean_string(values.get("type"), 24)
    if kind not in _KINDS:
        kind = "unclassified"
    return CatalogueMetadata(
        title,
        kind,
        _string_list(values.get("topics")),
        _string_list(values.get("projects")),
        _valid_source_url(values.get("source_url")),
    )


def _json_payload(value: str | None) -> object:
    if value is None:
        return None
    try:
        if len(value.encode("utf-8", "strict")) > _MAX_METADATA_BYTES:
            return None
        return json.loads(value)
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        return None


def html_metadata(prefix: bytes, slug: str) -> CatalogueMetadata:
    parser = _HeadParser()
    try:
        parser.feed(prefix[:65536].decode("utf-8", "ignore"))
        parser.close()
    except ValueError:
        parser = _HeadParser()
    fallback = _clean_string("".join(parser.title_parts), _MAX_FIELD_LENGTH) or slug
    return _metadata(_json_payload(parser.metadata_json), fallback)


def markdown_metadata(
    prefix: bytes, slug: str, fallback_title: str | None = None
) -> CatalogueMetadata:
    try:
        source = prefix[:65536].decode("utf-8", "ignore")
    except (UnicodeError, ValueError):
        return _metadata(None, fallback_title or slug)
    lines = source.splitlines()
    if not lines or lines[0] != "---":
        return _metadata(None, fallback_title or slug)
    for line in lines[1:]:
        if line == "---":
            break
        key, separator, value = line.partition(":")
        if separator and key.strip() == "html-publish-catalogue":
            return _metadata(_json_payload(value.strip()), fallback_title or slug)
    return _metadata(None, fallback_title or slug)


def render_catalogue(entries: tuple[CatalogueEntry, ...], query: dict[str, str]) -> bytes:
    terms = [part.casefold() for part in query.get("q", "").split() if part]
    selected_kind = query.get("type", "")
    selected_topic = query.get("topic", "")
    selected_project = query.get("project", "")
    selected_state = query.get("state", "")

    def matches(entry: CatalogueEntry) -> bool:
        metadata = entry.metadata
        search_text = " ".join(
            (metadata.title, entry.name, *metadata.topics, *metadata.projects)
        ).casefold()
        return (
            all(term in search_text for term in terms)
            and (not selected_kind or metadata.kind == selected_kind)
            and (
                not selected_topic
                or selected_topic.casefold() in {x.casefold() for x in metadata.topics}
            )
            and (
                not selected_project
                or selected_project.casefold() in {x.casefold() for x in metadata.projects}
            )
            and (not selected_state or entry.state == selected_state)
        )

    visible = tuple(entry for entry in entries if matches(entry))
    topics = sorted(
        {topic for entry in entries for topic in entry.metadata.topics}, key=str.casefold
    )
    projects = sorted(
        {project for entry in entries for project in entry.metadata.projects}, key=str.casefold
    )

    def option(value: str, label: str, selected: str) -> str:
        mark = " selected" if value == selected else ""
        return (
            f'<option value="{html.escape(value, quote=True)}"{mark}>{html.escape(label)}</option>'
        )

    type_options = option("", "Tous les types", selected_kind) + "".join(
        option(kind, _KIND_LABELS[kind], selected_kind)
        for kind in sorted(_KINDS)
        if kind != "unclassified" or any(item.metadata.kind == kind for item in entries)
    )
    topic_options = option("", "Tous les sujets", selected_topic) + "".join(
        option(value, value, selected_topic) for value in topics
    )
    project_options = option("", "Tous les projets", selected_project) + "".join(
        option(value, value, selected_project) for value in projects
    )
    state_labels = {"selected": "Publiée", "archived": "Archivée", "unavailable": "Indisponible"}
    state_options = option("", "Tous les états", selected_state) + "".join(
        option(state, label, selected_state)
        for state, label in state_labels.items()
        if any(item.state == state for item in entries)
    )
    cards: list[str] = []
    for entry in visible:
        metadata = entry.metadata
        attributes = (
            f'data-publication="{html.escape(entry.name, quote=True)}" '
            f'data-type="{html.escape(metadata.kind, quote=True)}" '
            f'data-state="{html.escape(entry.state, quote=True)}"'
        )

        def facet_line(label: str, values: tuple[str, ...]) -> str:
            if not values:
                return ""
            items = " ".join(f'<span class="tag">{html.escape(value)}</span>' for value in values)
            return f'<p class="facets"><strong>{label} :</strong> {items}</p>'

        publication_action = (
            f'<a href="{html.escape(entry.publication_url, quote=True)}">Ouvrir la page</a>'
            if entry.state == "selected"
            else ""
        )
        source_action = (
            f'<a href="{html.escape(metadata.source_url, quote=True)}">Reprendre la source</a>'
            if metadata.source_url
            else ""
        )
        actions = " ".join(action for action in (publication_action, source_action) if action)
        navigation = f"<nav>{actions}</nav>" if actions else ""
        stamp = datetime.fromisoformat(entry.updated_at).astimezone(UTC)
        iso_stamp = stamp.isoformat(timespec="seconds").replace("+00:00", "Z")
        display_stamp = stamp.strftime("%Y-%m-%d %H:%M UTC")
        cards.append(
            f"<article {attributes}><h2>{html.escape(metadata.title)}</h2>"
            f"<p>{html.escape(_KIND_LABELS[metadata.kind])} · "
            f"{html.escape(state_labels[entry.state])}</p>"
            f"<p>Publication : {html.escape(entry.name)}</p>"
            f"{facet_line('Sujets', metadata.topics)}"
            f"{facet_line('Projets', metadata.projects)}"
            f'<p class="publication-url">URL de publication : '
            f'<span class="url">{html.escape(entry.publication_url)}</span></p>'
            f'<p>Dernière mise à jour : <time datetime="{html.escape(iso_stamp, quote=True)}">'
            f"{html.escape(display_stamp)}</time></p>{navigation}</article>"
        )
    content = "\n".join(cards) or "<p>Aucune publication trouvée.</p>"
    page = f"""<!doctype html>
<html lang="fr-CA"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mes publications</title><style>
body{{font:1rem/1.5 system-ui,sans-serif;margin:0 auto;max-width:72rem;padding:1rem;
color:#171717;background:#fff}}
main,form,label,article{{min-width:0}}
form{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,12rem),1fr));gap:.75rem;
margin:1rem 0 2rem}}
label{{display:grid;gap:.25rem}}input,select,button{{font:inherit;padding:.55rem;min-width:0;max-width:100%}}
input,select{{width:100%;box-sizing:border-box}}article{{border-top:1px solid #bbb;padding:1rem 0}}
nav{{display:flex;flex-wrap:wrap;gap:1rem}}h2,.publication-url,.url,.tag{{overflow-wrap:anywhere;word-break:break-word}}
.facets{{display:flex;flex-wrap:wrap;gap:.4rem;align-items:baseline}}
.tag{{border:1px solid #aaa;padding:.1rem .4rem;max-width:100%}}
</style></head><body><main><h1>Mes publications</h1>
<form method="get" action="">
<label>Rechercher<input name="q" value="{html.escape(query.get("q", ""), quote=True)}"></label>
<label>Type<select name="type">{type_options}</select></label>
<label>Sujet<select name="topic">{topic_options}</select></label>
<label>Projet<select name="project">{project_options}</select></label>
<label>État<select name="state">{state_options}</select></label>
<button type="submit">Filtrer</button></form>{content}</main></body></html>"""
    return page.encode("utf-8")
