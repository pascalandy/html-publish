from __future__ import annotations

import html
import json
import posixpath
import re
import shutil
import unicodedata
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from markdown_it import MarkdownIt
from markdown_it import __version__ as MARKDOWN_IT_VERSION
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict

from html_publish.artifact import capture, capture_record, capture_source
from html_publish.model import (
    Deadline,
    Limits,
    PreparedPublication,
    PublishError,
    Revision,
    SourceKind,
    WarningDetail,
)

TEMPLATE_VERSION = "reading-column-v1"
RENDER_PROFILE_ID = (
    f"markdown-it-py/{MARKDOWN_IT_VERSION}:commonmark:html-off:table-on:linkify-off:"
    f"template-{TEMPLATE_VERSION}"
)

_WIKILINK = re.compile(r"\[\[([^\[\]]+)\]\]")
_PAGE_STYLE = """<style>
:root {
  color-scheme: dark;
  --bg: #000;
  --fg: #fff;
  --muted: #c5c5c5;
  --rule: #383838;
  --surface: #0d0d0d;
}
* { box-sizing: border-box; }
html { background: var(--bg); color: var(--fg); }
body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font: 18px/1.7 system-ui, sans-serif;
}
a {
  color: var(--fg);
  text-decoration-thickness: 1px;
  text-underline-offset: .22em;
}
a:focus-visible, [tabindex="0"]:focus-visible {
  outline: 2px solid var(--fg);
  outline-offset: 4px;
}
main { max-width: 70ch; margin: 0 auto; padding: 56px 24px 80px; }
h1, h2, h3 { line-height: 1.18; text-wrap: balance; }
h1 { margin: 0 0 32px; font-size: clamp(2.25rem, 5vw, 3.6rem); letter-spacing: -.04em; }
h2 { margin: 54px 0 16px; font-size: 1.55rem; letter-spacing: -.015em; }
h3 { margin: 38px 0 12px; font-size: 1.2rem; }
p, ul, ol, blockquote { margin: 0 0 18px; }
ul, ol { padding-left: 1.4rem; }
li + li { margin-top: 8px; }
blockquote { padding-left: 20px; border-left: 1px solid var(--rule); }
code, pre { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
code { font-size: .92em; }
pre {
  max-width: 100%;
  margin: 20px 0 24px;
  padding: 18px 20px;
  overflow-x: auto;
  background: var(--surface);
  font-size: .92rem;
  line-height: 1.65;
}
pre code { font-size: inherit; }
.table-scroll { max-width: 100%; margin: 22px 0 26px; overflow-x: auto; }
table {
  width: 100%;
  min-width: 36rem;
  border-collapse: collapse;
  text-align: left;
  line-height: 1.5;
}
th, td {
  padding: 12px 14px 12px 0;
  border-bottom: 1px solid var(--rule);
  vertical-align: top;
}
th { font-size: .9rem; }
img { display: block; max-width: 100%; height: auto; margin: 22px 0; }
hr { height: 1px; margin: 42px 0; border: 0; background: var(--rule); }
@media (max-width: 700px) {
  body { font-size: 16px; }
  main { padding: 34px 20px 56px; }
  h1 { font-size: clamp(2rem, 10vw, 3rem); }
  h2 { margin-top: 42px; }
  pre { padding: 15px; }
}
</style>"""


def _table_open(
    renderer: RendererHTML,
    tokens: Sequence[Token],
    index: int,
    options: OptionsDict,
    env: EnvType,
) -> str:
    return (
        '<div class="table-scroll" role="region" aria-label="Markdown table" tabindex="0"><table>\n'
    )


def _table_close(
    renderer: RendererHTML,
    tokens: Sequence[Token],
    index: int,
    options: OptionsDict,
    env: EnvType,
) -> str:
    return "</table></div>\n"


def _parser() -> MarkdownIt:
    parser = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
    parser.add_render_rule("table_open", _table_open)
    parser.add_render_rule("table_close", _table_close)
    return parser


def prepare(
    source: Path,
    workspace: Path,
    object_format: str,
    limits: Limits,
    deadline: Deadline,
    *,
    entry: str | None,
) -> PreparedPublication:
    captured_source = capture_source(source, workspace, limits, deadline)
    source_paths = tuple(str(item.path) for item in captured_source.entries)
    entry_path = _select_entry(captured_source.kind, source_paths, entry)
    source_to_output = _output_map(source_paths, entry_path)
    output_root = workspace / "rendered"
    output_root.mkdir()
    parser = _parser()
    unresolved: list[WarningDetail] = []
    warnings: list[str] = []

    for source_path in source_paths:
        input_file = captured_source.root.joinpath(*PurePosixPath(source_path).parts)
        output_path = PurePosixPath(source_to_output[source_path])
        destination = output_root.joinpath(*output_path.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source_path.endswith(".md"):
            rendered, page_warnings = _render_document(
                input_file,
                source_path,
                str(output_path),
                source_to_output,
                source_paths,
                parser,
            )
            destination.write_bytes(rendered)
            for warning in page_warnings:
                if warning.code not in warnings:
                    warnings.append(warning.code)
                unresolved.append(warning)
        else:
            shutil.copyfile(input_file, destination)

    site = capture(output_root, workspace, object_format, limits, deadline)
    provenance = _provenance(entry_path, source_to_output, site.revision)
    record = capture_record(captured_source, provenance, workspace, object_format, deadline)
    site = replace(
        site,
        warnings=tuple(dict.fromkeys((*site.warnings, *warnings))),
        warning_details=(*site.warning_details, *unresolved),
    )
    return PreparedPublication(site, record, RENDER_PROFILE_ID)


def _select_entry(source_kind: SourceKind, source_paths: tuple[str, ...], entry: str | None) -> str:
    markdown_paths = {path for path in source_paths if path.endswith(".md")}
    if source_kind == "file":
        if entry is not None:
            raise PublishError(
                "invalid_entry",
                "render",
                "--entry is only valid for a Markdown document directory",
                "remove_entry",
            )
        path = source_paths[0]
        if not path.endswith(".md"):
            raise PublishError(
                "invalid_input",
                "render",
                "A Markdown file input must end in .md",
                "fix_input",
            )
        return path

    if entry is not None:
        candidate = PurePosixPath(entry)
        if (
            candidate.is_absolute()
            or "\\" in entry
            or any(part in {"", ".", ".."} for part in candidate.parts)
        ):
            raise PublishError(
                "invalid_entry",
                "render",
                "--entry must be a relative .md path inside the document directory",
                "fix_entry",
            )
        path = candidate.as_posix()
        if not path.endswith(".md") or path not in markdown_paths:
            raise PublishError(
                "invalid_entry",
                "render",
                f"The entry is not a captured Markdown file: {entry}",
                "fix_entry",
            )
        return path

    candidates = [path for path in ("index.md", "README.md") if path in markdown_paths]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise PublishError(
            "missing_entry",
            "render",
            "The document directory needs index.md, README.md, or an explicit --entry",
            "specify_entry",
            ("entry",),
        )
    raise PublishError(
        "ambiguous_entry",
        "render",
        "Both index.md and README.md are present; choose an entry with --entry",
        "specify_entry",
        ("entry",),
    )


def _output_map(source_paths: tuple[str, ...], entry_path: str) -> dict[str, str]:
    source_keys: dict[str, str] = {}
    output_keys: dict[str, str] = {}
    result: dict[str, str] = {}
    for source_path in source_paths:
        normalized_source = unicodedata.normalize("NFC", source_path).casefold()
        previous_source = source_keys.get(normalized_source)
        if previous_source is not None:
            raise PublishError(
                "duplicate_input",
                "render",
                "Input paths collide after Unicode normalization and case folding: "
                f"{previous_source} and {source_path}",
                "fix_input",
            )
        source_keys[normalized_source] = source_path

        source = PurePosixPath(source_path)
        output = (
            PurePosixPath("index.html")
            if source_path == entry_path
            else source.with_suffix(".html")
            if source_path.endswith(".md")
            else source
        )
        normalized_output = unicodedata.normalize("NFC", output.as_posix())
        output_path = PurePosixPath(normalized_output)
        if output_path.is_absolute() or any(part in {"", ".", ".."} for part in output_path.parts):
            raise PublishError(
                "invalid_output_path",
                "render",
                f"The source maps outside the publication: {source_path}",
                "fix_input",
            )
        output_key = normalized_output.casefold()
        previous_output = output_keys.get(output_key)
        if previous_output is not None:
            raise PublishError(
                "output_collision",
                "render",
                f"Source paths {previous_output} and {source_path} map to the same output path",
                "fix_input",
            )
        output_keys[output_key] = source_path
        result[source_path] = normalized_output

    for output_path, source_path in result.items():
        parts = PurePosixPath(output_path).parts
        for index in range(1, len(parts)):
            parent = PurePosixPath(*parts[:index]).as_posix().casefold()
            conflict = output_keys.get(parent)
            if conflict is not None:
                raise PublishError(
                    "output_collision",
                    "render",
                    f"Source paths {conflict} and {source_path} map to a file/directory collision",
                    "fix_input",
                )
    return result


def _provenance(
    entry: str,
    source_to_output: dict[str, str],
    site_revision: Revision,
) -> bytes:
    value = {
        "schema_version": 1,
        "format": "markdown",
        "entry": entry,
        "render_profile_id": RENDER_PROFILE_ID,
        "renderer": {
            "name": "markdown-it-py",
            "version": MARKDOWN_IT_VERSION,
            "preset": "commonmark",
            "html": False,
            "table": True,
            "linkify": False,
            "template": TEMPLATE_VERSION,
        },
        "source_to_output": dict(sorted(source_to_output.items())),
        "site_revision": str(site_revision),
    }
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{serialized}\n".encode()


def _strip_frontmatter(source: str, source_path: str) -> str:
    lines = source.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return source
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r\n") == "---":
            return "".join(lines[index + 1 :])
    raise PublishError(
        "frontmatter_unterminated",
        "render",
        f"Frontmatter in {source_path} has no closing --- line",
        "fix_input",
    )


def _render_document(
    path: Path,
    source_path: str,
    output_path: str,
    source_to_output: dict[str, str],
    source_paths: tuple[str, ...],
    parser: MarkdownIt,
) -> tuple[bytes, tuple[WarningDetail, ...]]:
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise PublishError(
            "markdown_encoding",
            "render",
            f"Markdown source is not valid UTF-8: {source_path}",
            "fix_input",
        ) from error
    source = _strip_frontmatter(source, source_path)
    warnings: list[WarningDetail] = []
    try:
        env: EnvType = {}
        tokens = parser.parse(source, env)
        _add_heading_ids(tokens)
        for token in tokens:
            if token.type == "inline" and token.children is not None:
                token.children = _rewrite_inline(
                    token.children,
                    source_path,
                    output_path,
                    source_to_output,
                    source_paths,
                    warnings,
                )
        body = parser.renderer.render(tokens, parser.options, env)
    except Exception as error:
        raise PublishError(
            "markdown_render_failure",
            "render",
            f"Markdown could not be rendered: {source_path}: {error}",
            "fix_input",
        ) from error
    title = _document_title(tokens)
    document = (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '  <meta name="color-scheme" content="dark">\n'
        f"  <title>{html.escape(title)}</title>\n"
        f"  {_PAGE_STYLE}\n"
        "</head>\n<body>\n"
        f'<main><article class="document">\n{body}</article></main>\n'
        "</body>\n</html>\n"
    )
    return document.encode("utf-8"), tuple(warnings)


def _document_title(tokens: Sequence[Token]) -> str:
    for index, token in enumerate(tokens[:-1]):
        if token.type == "heading_open" and token.tag == "h1":
            inline = tokens[index + 1]
            if inline.type == "inline" and inline.content.strip():
                return inline.content.strip()
    return "Documentation"


def _add_heading_ids(tokens: Sequence[Token]) -> None:
    used: set[str] = set()
    for index, token in enumerate(tokens[:-1]):
        if token.type != "heading_open":
            continue
        inline = tokens[index + 1]
        if inline.type != "inline":
            continue
        base = _heading_slug(inline.content)
        slug = base
        suffix = 1
        while slug in used:
            slug = f"{base}-{suffix}"
            suffix += 1
        used.add(slug)
        token.attrSet("id", slug)


def _heading_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).casefold()
    words = re.findall(r"[\w-]+", normalized, flags=re.UNICODE)
    return "-".join(words) or "section"


def _rewrite_inline(
    tokens: list[Token],
    source_path: str,
    output_path: str,
    source_to_output: dict[str, str],
    source_paths: tuple[str, ...],
    warnings: list[WarningDetail],
) -> list[Token]:
    rewritten: list[Token] = []
    link_stack: list[bool] = []
    for token in tokens:
        if token.type == "link_open":
            href_value = token.attrGet("href")
            href = href_value if isinstance(href_value, str) else ""
            if link_stack and link_stack[-1]:
                token.hidden = True
                link_stack.append(True)
            else:
                result = _resolve_url(
                    href,
                    source_path,
                    output_path,
                    source_to_output,
                )
                if result is None:
                    token.hidden = True
                    link_stack.append(True)
                    _warning(
                        warnings,
                        "unresolved_markdown_link",
                        source_path,
                        href,
                        _expected_relative_path(href, source_path),
                    )
                else:
                    token.attrSet("href", result)
                    link_stack.append(False)
            rewritten.append(token)
        elif token.type == "link_close":
            if link_stack and link_stack.pop():
                token.hidden = True
            rewritten.append(token)
        elif token.type == "image":
            src_value = token.attrGet("src")
            src = src_value if isinstance(src_value, str) else ""
            result = _resolve_url(src, source_path, output_path, source_to_output)
            if result is None:
                alt_value = token.attrGet("alt")
                alt = alt_value if isinstance(alt_value, str) else ""
                rewritten.append(Token("text", "", 0, content=alt))
                _warning(
                    warnings,
                    "unresolved_markdown_image",
                    source_path,
                    src,
                    _expected_relative_path(src, source_path),
                )
            else:
                token.attrSet("src", result)
                rewritten.append(token)
        elif token.type == "text" and not any(link_stack):
            rewritten.extend(
                _rewrite_wikilinks(
                    token.content,
                    source_path,
                    output_path,
                    source_to_output,
                    source_paths,
                    warnings,
                )
            )
        else:
            rewritten.append(token)
    return rewritten


def _resolve_url(
    reference: str,
    source_path: str,
    output_path: str,
    source_to_output: dict[str, str],
) -> str | None:
    try:
        parsed = urlsplit(reference)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or not parsed.path:
        return reference
    if parsed.path.startswith("/"):
        return None
    target = posixpath.normpath(
        posixpath.join(posixpath.dirname(source_path), unquote(parsed.path))
    )
    if target == ".." or target.startswith("../") or target.startswith("/"):
        return None
    mapped = source_to_output.get(target)
    if mapped is None:
        return None
    return _relative_output_url(mapped, output_path, parsed.query, parsed.fragment)


def _expected_relative_path(reference: str, source_path: str) -> str | None:
    try:
        parsed = urlsplit(reference)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    if parsed.path.startswith("/"):
        return parsed.path
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_path), unquote(parsed.path)))


def _relative_output_url(
    mapped: str,
    output_path: str,
    query: str,
    fragment: str,
) -> str:
    output_parent = posixpath.dirname(output_path) or "."
    relative_output = posixpath.relpath(mapped, output_parent)
    encoded = quote(relative_output, safe="/-._~")
    return urlunsplit(("", "", encoded, query, fragment))


def _rewrite_wikilinks(
    value: str,
    source_path: str,
    output_path: str,
    source_to_output: dict[str, str],
    source_paths: tuple[str, ...],
    warnings: list[WarningDetail],
) -> list[Token]:
    result: list[Token] = []
    cursor = 0
    parent = posixpath.dirname(source_path)
    markdown_paths = tuple(path for path in source_paths if path.endswith(".md"))
    for match in _WIKILINK.finditer(value):
        if match.start() > cursor:
            result.append(Token("text", "", 0, content=value[cursor : match.start()]))
        raw = match.group(1)
        target_text, separator, label = raw.partition("|")
        target_text = target_text.strip()
        display = label.strip() if separator else PurePosixPath(target_text).stem
        try:
            parsed = urlsplit(target_text)
        except ValueError:
            parsed = None
        target_path: str | None = None
        expected_path: str | None = None
        query = fragment = ""
        if (
            parsed is not None
            and not parsed.scheme
            and not parsed.netloc
            and not parsed.path.startswith("/")
        ):
            query, fragment = parsed.query, parsed.fragment
            decoded = unquote(parsed.path)
            if not PurePosixPath(decoded).suffix:
                decoded += ".md"
            candidate = posixpath.normpath(posixpath.join(parent, decoded))
            expected_path = candidate
            within_root = candidate != ".." and not candidate.startswith("../")
            if within_root:
                if candidate in markdown_paths:
                    target_path = candidate
                else:
                    basename = PurePosixPath(candidate).name.casefold()
                    matches = [
                        path
                        for path in markdown_paths
                        if PurePosixPath(path).name.casefold() == basename
                    ]
                    if len(matches) == 1:
                        target_path = matches[0]
        if target_path is None:
            literal = match.group(0)
            result.append(Token("text", "", 0, content=literal))
            _warning(warnings, "unresolved_wikilink", source_path, literal, expected_path)
        else:
            mapped = source_to_output[target_path]
            url = _relative_output_url(mapped, output_path, query, fragment)
            result.extend(
                (
                    Token("link_open", "a", 1, attrs={"href": url}),
                    Token("text", "", 0, content=display),
                    Token("link_close", "a", -1),
                )
            )
        cursor = match.end()
    if cursor < len(value):
        result.append(Token("text", "", 0, content=value[cursor:]))
    return result or [Token("text", "", 0, content=value)]


def _warning(
    warnings: list[WarningDetail],
    code: str,
    source_path: str,
    reference: str,
    expected_path: str | None,
) -> None:
    detail = WarningDetail(code, source_path, reference, expected_path)
    if detail not in warnings:
        warnings.append(detail)
