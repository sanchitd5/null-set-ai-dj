"""Parses and indexes the ./DJ/05 - Transition Cookbook/ markdown notes into
structured, executable TransitionRecipe objects.

Each recipe file follows a standardized 16-section template (see any file in
DJ/05 - Transition Cookbook/ for reference):
    Technique Name, What it is, What problem it solves, Musical principle,
    Setup, Step-by-step, When to use it, When NOT to use it, Best genres,
    Beginner difficulty, Risk of sounding gimmicky, Common mistakes,
    Advanced variation, Example scenario, Practice drill, Related techniques.

This module is intentionally dependency-light (stdlib + PyYAML) so it can run
before any heavy audio dependencies (Demucs, pedalboard, librosa) are needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from app.music_brain.config import TRANSITION_COOKBOOK_DIR

# Canonical section order used by every recipe file. Keys are the slug used
# on TransitionRecipe; values are the exact "### " heading text in the note.
SECTION_HEADINGS: Dict[str, str] = {
    "technique_name": "Technique Name",
    "what_it_is": "What it is",
    "problem_it_solves": "What problem it solves",
    "musical_principle": "Musical principle",
    "setup": "Setup",
    "step_by_step": "Step-by-step",
    "when_to_use": "When to use it",
    "when_not_to_use": "When NOT to use it",
    "best_genres": "Best genres",
    "beginner_difficulty": "Beginner difficulty",
    "risk_of_gimmicky": "Risk of sounding gimmicky",
    "common_mistakes": "Common mistakes",
    "advanced_variation": "Advanced variation",
    "example_scenario": "Example scenario",
    "practice_drill": "Practice drill",
    "related_techniques": "Related techniques",
}

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")

# Recipes that explicitly bypass Camelot key-compatibility gating because they
# route around harmonic clash entirely (per DJ/02 - Music Theory/Harmonic
# Mixing & Camelot System.md: "Clashing keys disallowed unless using Echo Out
# or Breakdown transition logic").
_CAMELOT_BYPASS_RECIPES = {
    "Echo Out", "Breakdown Transition", "Quick Cut", "Hard Cut",
    "Backspin (Spinback)", "Genre Bridge", "Tempo Bridge", "Fake Drop",
    "Loop Roll", "Stutter Transition",
}

# Recipes that require live/rendered stem separation to execute as described.
# Bass Swap / Drop Swap are deliberately excluded: they're executed with
# plain EQ knobs on the full mix, not isolated Demucs stems.
_STEMS_REQUIRED_RECIPES = {
    "Stems Transition", "Acapella Overlay", "Instrumental Overlay",
    "Vocal Transition", "Drum Bridge", "Live Mashup",
}

# Recipes designed specifically to bridge large BPM gaps (no ceiling on delta).
_UNLIMITED_BPM_DELTA_RECIPES = {
    "Echo Out", "Breakdown Transition", "Quick Cut", "Hard Cut",
    "Backspin (Spinback)", "Genre Bridge", "Tempo Bridge", "Fake Drop",
}


@dataclass
class TransitionRecipe:
    """A single structured, executable DJ transition recipe."""

    name: str
    slug: str
    source_path: Path
    difficulty: str = ""
    tags: List[str] = field(default_factory=list)

    technique_name: str = ""
    what_it_is: str = ""
    problem_it_solves: str = ""
    musical_principle: str = ""
    setup: str = ""
    step_by_step: str = ""
    when_to_use: str = ""
    when_not_to_use: str = ""
    best_genres: str = ""
    beginner_difficulty: str = ""
    risk_of_gimmicky: str = ""
    common_mistakes: str = ""
    advanced_variation: str = ""
    example_scenario: str = ""
    practice_drill: str = ""
    related_techniques: str = ""

    # Automatically derived acoustic prerequisites (spec 3.1).
    requires_stems: bool = False
    max_bpm_delta: Optional[float] = None
    camelot_compatible_only: bool = True

    @property
    def steps(self) -> List[str]:
        """Step-by-step section as an ordered list of individual steps."""
        items = re.findall(r"^\d+\.\s*(.+)$", self.step_by_step, re.MULTILINE)
        return [_strip_markdown(item) for item in items]

    @property
    def related(self) -> List[str]:
        """Related technique names, extracted from [[wiki-links]]."""
        return WIKILINK_RE.findall(self.related_techniques)

    @property
    def genres(self) -> List[str]:
        """Best genres as a flat list of genre names."""
        line = self.best_genres.strip().lstrip("*").strip()
        return [g.strip() for g in line.split(",") if g.strip()]

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "source_path"}
        d["source_path"] = str(self.source_path)
        d["steps"] = self.steps
        d["related"] = self.related
        d["genres"] = self.genres
        return d


def _strip_markdown(text: str) -> str:
    """Strip bold/italic markers and wiki-links down to plain text."""
    text = WIKILINK_RE.sub(r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    return text.strip()


def _parse_frontmatter(raw_text: str) -> tuple[dict, str]:
    match = FRONTMATTER_RE.match(raw_text)
    if not match:
        return {}, raw_text
    frontmatter_yaml, body = match.groups()
    metadata = yaml.safe_load(frontmatter_yaml) or {}
    return metadata, body


def _split_sections(body: str) -> Dict[str, str]:
    """Split a note body into {canonical_heading: content} on '### ' headings.

    A couple of recipes (Genre Bridge, Tempo Bridge) suffix the "Step-by-step"
    heading with a parenthetical, e.g. "### Step-by-step (The Pop -> Drum &
    Bass Pivot)". Headings are matched against SECTION_HEADINGS by prefix so
    those still map onto the canonical "Step-by-step" slot.
    """
    parts = re.split(r"^### (.+)$", body, flags=re.MULTILINE)
    known_headings = list(SECTION_HEADINGS.values())
    sections: Dict[str, str] = {}
    # parts[0] is preamble (title); afterwards it alternates heading, content.
    for i in range(1, len(parts), 2):
        raw_heading = parts[i].strip()
        content = parts[i + 1].strip() if i + 1 < len(parts) else ""
        heading = next(
            (h for h in known_headings if raw_heading.startswith(h)),
            raw_heading,
        )
        sections[heading] = content
    return sections


def _derive_prerequisites(name: str, tags: List[str]) -> tuple[bool, Optional[float], bool]:
    """Derive requires_stems / max_bpm_delta / camelot_compatible_only.

    Heuristics per spec 3.1, keyed off explicit recipe-name membership tests
    (see the module-level _*_RECIPES sets) plus the 'dj/stems' tag.
    """
    requires_stems = name in _STEMS_REQUIRED_RECIPES or "dj/stems" in tags
    camelot_compatible_only = name not in _CAMELOT_BYPASS_RECIPES
    if name in _UNLIMITED_BPM_DELTA_RECIPES:
        max_bpm_delta = None
    else:
        max_bpm_delta = 0.06
    return requires_stems, max_bpm_delta, camelot_compatible_only


def parse_recipe_file(path: Path) -> TransitionRecipe:
    """Parse a single transition recipe markdown file into a TransitionRecipe."""
    raw_text = path.read_text(encoding="utf-8")
    metadata, body = _parse_frontmatter(raw_text)
    sections = _split_sections(body)

    name = path.stem
    tags = list(metadata.get("tags") or [])
    difficulty = str(metadata.get("difficulty") or "")

    kwargs = {
        slug: sections.get(heading, "")
        for slug, heading in SECTION_HEADINGS.items()
    }

    requires_stems, max_bpm_delta, camelot_compatible_only = _derive_prerequisites(name, tags)

    return TransitionRecipe(
        name=name,
        slug=_slugify(name),
        source_path=path,
        difficulty=difficulty,
        tags=tags,
        requires_stems=requires_stems,
        max_bpm_delta=max_bpm_delta,
        camelot_compatible_only=camelot_compatible_only,
        **kwargs,
    )


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug


class KnowledgeParser:
    """Scans DJ/05 - Transition Cookbook/ and indexes every recipe in memory."""

    def __init__(self, cookbook_dir: Path = TRANSITION_COOKBOOK_DIR):
        self.cookbook_dir = Path(cookbook_dir)
        self._recipes: Dict[str, TransitionRecipe] = {}
        self.load()

    def load(self) -> "KnowledgeParser":
        self._recipes = {}
        for path in sorted(self.cookbook_dir.glob("*.md")):
            recipe = parse_recipe_file(path)
            self._recipes[recipe.name] = recipe
        return self

    def get_all(self) -> List[TransitionRecipe]:
        return list(self._recipes.values())

    def get(self, name: str) -> Optional[TransitionRecipe]:
        if name in self._recipes:
            return self._recipes[name]
        target_slug = _slugify(name)
        for recipe in self._recipes.values():
            if recipe.slug == target_slug:
                return recipe
        return None

    def find_by_tag(self, tag: str) -> List[TransitionRecipe]:
        return [r for r in self._recipes.values() if tag in r.tags]

    def find_by_genre(self, genre: str) -> List[TransitionRecipe]:
        genre_lower = genre.lower()
        return [
            r for r in self._recipes.values()
            if any(genre_lower in g.lower() for g in r.genres)
        ]

    def find_by_difficulty(self, difficulty: str) -> List[TransitionRecipe]:
        return [
            r for r in self._recipes.values()
            if r.difficulty.lower() == difficulty.lower()
        ]

    def stem_only_recipes(self) -> List[TransitionRecipe]:
        return [r for r in self._recipes.values() if r.requires_stems]

    def __len__(self) -> int:
        return len(self._recipes)

    def __iter__(self):
        return iter(self._recipes.values())


if __name__ == "__main__":
    parser = KnowledgeParser()
    print(f"Parsed {len(parser)} transition recipes from {parser.cookbook_dir}")
    for recipe in parser.get_all():
        print(f"  - {recipe.name} ({recipe.difficulty}, tags={recipe.tags})")
