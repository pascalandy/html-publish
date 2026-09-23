from __future__ import annotations

from dataclasses import dataclass
from importlib import resources

from html_publish import __version__


@dataclass(frozen=True)
class Guide:
    name: str
    title: str
    summary: str
    filename: str


GUIDES: tuple[Guide, ...] = (
    Guide(
        name="core",
        title="Core guide",
        summary="Publish and update artifacts through the durable artifact workflow.",
        filename="core.md",
    ),
    Guide(
        name="recovery",
        title="Recovery guide",
        summary="Interpret failure codes, interruption states, and guarded retries.",
        filename="recovery.md",
    ),
)


def names() -> tuple[str, ...]:
    return tuple(guide.name for guide in GUIDES)


def read(name: str) -> str:
    guide = next((guide for guide in GUIDES if guide.name == name), None)
    if guide is None:
        raise ValueError(f"unknown guide: {name}")
    return (
        resources.files("html_publish")
        .joinpath("guides")
        .joinpath(guide.filename)
        .read_text(encoding="utf-8")
    )


def inventory() -> dict[str, object]:
    return {
        "schema_version": 1,
        "executable": "html-publish",
        "version": __version__,
        "guides": [
            {
                "name": guide.name,
                "title": guide.title,
                "summary": guide.summary,
                "bytes": len(read(guide.name).encode("utf-8")),
            }
            for guide in GUIDES
        ],
    }
