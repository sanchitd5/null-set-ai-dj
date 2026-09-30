"""Clean "who / what" for a track before it reaches the LLM.

Download names look like "Fred again.., The Blessed Madonna - Marea (we've lost
dancing)" or "BICEP ｜ GLUE (Official Video)". Handing the model every credited
artist (a less-known featured artist next to the main one) plus upload noise
confused it: it anchored on the unfamiliar name and suggested the wrong world.

clean_identity() returns the PRIMARY artist and the bare song title:
  * artist: first credit before ",", "&", " x ", " and ", "feat.", "ft.", "with"
  * title: drops "(feat. ...)", "(Official Video)", "[Audio]", "(HQ)", "(Lyrics)",
    "(Visualiser)" etc., and trailing credits. Remix / edit / VIP tags are KEPT
    (they are a different song for the DJ).
"""

from __future__ import annotations

import re
from typing import List, Tuple

_SEPARATORS = re.compile(r"\s+[-–—|｜•·]\s+|\s[｜|]\s?|\s*●+\s*|\s+[：:]\s+|\s*：\s*")
_ARTIST_SPLIT = re.compile(
    r"\s*(?:,|&|\+|\bx\b|\band\b|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b|\bvs\.?)\s*",
    re.IGNORECASE,
)
_NOISE = re.compile(
    r"\s*[\(\[]\s*(?:official\b[^)\]]*|"
    r"(?:lyric|lyrics|music)\s+video|audio(?:\s+hq)?|hq|hd|4k|lyrics?|visuali[sz]er|"
    r"(?:feat|ft|featuring|with)\b[^)\]]*|explicit|clean|out now|premiere)\s*[\)\]]",
    re.IGNORECASE,
)
_TRAILING_CREDIT = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)


def primary_artist(artists: str) -> str:
    first = _ARTIST_SPLIT.split(artists.strip(), maxsplit=1)[0].strip()
    return first or artists.strip()


def clean_title(title: str) -> str:
    t = title
    for _ in range(3):  # nested / repeated noise tags
        t = _NOISE.sub("", t)
    t = _TRAILING_CREDIT.sub("", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -–—|｜")
    return t or title.strip()


def credited_artists(display: str) -> List[str]:
    """Every credited artist, in credit order ("LATIN MAFIA, Fred again.. - X" ->
    ["LATIN MAFIA", "Fred again.."]); includes "(feat. ...)" credits from the title."""
    parts = _SEPARATORS.split(display.strip(), maxsplit=1)
    names: List[str] = []
    if len(parts) == 2 and parts[0] and parts[1]:
        names += [n.strip() for n in _ARTIST_SPLIT.split(parts[0]) if n.strip()]
        m = re.search(r"[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+([^)\]]+)[\)\]]", parts[1], re.IGNORECASE)
        if m:
            names += [n.strip() for n in _ARTIST_SPLIT.split(m.group(1)) if n.strip()]
    seen, out = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def clean_identity(display: str) -> Tuple[str, str]:
    """("Primary Artist", "Song Title") from a download display name."""
    parts = _SEPARATORS.split(display.strip(), maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1]:
        artist, title = parts
    else:
        artist, title = "Unknown", display.strip()
    artist = primary_artist(artist)
    # smart-case shouting uploads ("BICEP | GLUE") without touching "Fred again.."
    if artist.isupper() and len(artist) > 3:
        artist = artist.title()
    title = clean_title(title)
    if title.isupper() and len(title) > 3:
        title = title.title()
    return artist, title
