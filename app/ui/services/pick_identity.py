"""Pick identity: does a YouTube / YouTube Music candidate hold the recording the AI asked for?

A pick is "Artist - Title". The old matcher counted loose word hits (half of the artist's words, 80 % of
the title's words, found anywhere in title + channel), so a same-titled song by an unrelated artist, an
interview, a sample clip or a fan upload could win. This module scores ONE candidate against ONE pick with
whole-name rules and returns a verdict with reasons; callers keep the best candidate that clears the bar,
and when none does the pick is skipped (never a low-confidence play).

Rules (all text is folded first: case, diacritics, & -> and, punctuation, apostrophes):
  artist   the pick's artist must be a whole credited name of the candidate (never a word of it): YouTube
           Music artist metadata, a "- Topic" channel, the "Artist - Title" prefix of the title, the artist's own
           / VEVO / official channel, or a "(feat. ...)" credit. Metadata or a Topic channel that names someone
           else rejects the candidate even when the title matches. "Ali" is not "Ali Sethi".
  title    the title left after dropping noise ("Official Video", "Lyrics", "Explicit", years, "(feat. ...)")
           must equal the wanted title; extra words ("Home" vs "Home Alone") reject.
  version  a remix / edit / VIP / acoustic is a different song: wanted and found must agree on it.
  shape    < min_secs or > max_secs, interviews, podcasts, reactions, samples, snippets, previews, teasers,
           shorts, type beats, full albums, covers, slowed / sped-up, fan edits, "no vocals" ... reject.
  script   a title in another writing system than the pick and the set, with no confirmed artist, rejects.
  score    0.50 * artist + 0.35 * title + 0.15 * source quality. OK_SCORE clears, HIGH_SCORE is the
           "official" tier the first pass wants.

Pure functions, no I/O. The Dart twin is dj_core lib/source/pick_identity.dart; both run the shared
fixtures (app/tests/fixtures/pick_identity_cases.json == dj_core test/fixtures/pick_identity_cases.json).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

OK_SCORE = 0.8
HIGH_SCORE = 0.9
MIN_SECS = 90
MAX_SECS = 12 * 60
EXTENDED_MAX_SECS = 15 * 60

# ---- folding ------------------------------------------------------------------------------------
# (source characters, replacement): diacritics and ligatures to plain Latin. Kept as a table (not
# unicodedata) so the Dart twin folds exactly the same way.
_FOLD_PAIRS = [
    ("àáâãäåāăąǎ", "a"), ("çćĉċč", "c"), ("ďđð", "d"), ("èéêëēĕėęě", "e"), ("ĝğġģ", "g"), ("ĥħ", "h"),
    ("ìíîïĩīĭįıǐ", "i"), ("ĵ", "j"), ("ķ", "k"), ("ĺļľŀł", "l"), ("ñńņňŉ", "n"), ("òóôõöōŏőøǒ", "o"),
    ("ŕŗř", "r"), ("śŝşšș", "s"), ("ţťŧț", "t"), ("ùúûüũūŭůűųǔ", "u"), ("ŵ", "w"), ("ýÿŷ", "y"),
    ("źżž", "z"), ("æ", "ae"), ("œ", "oe"), ("ß", "ss"), ("þ", "th"),
]
_FOLD = {ord(c): rep for chars, rep in _FOLD_PAIRS for c in chars}
_DROP = {ord(c): None for c in "'’‘`´"}          # don't -> dont
_JOIN = {ord("&"): " and "}


def fold(text: str) -> str:
    s = str(text or "").lower()
    # decomposed accents (e + U+0301) from tags: drop the combining marks 0300-036F
    s = re.sub("[̀-ͯ]", "", s)
    return s.translate(_FOLD).translate(_DROP)


def norm(text: str) -> str:
    """Words only: folded, & = and, punctuation = space."""
    s = fold(text).translate(_JOIN)
    return " ".join(re.sub(r"[\W_]+", " ", s).split())


def squash(text: str) -> str:
    return norm(text).replace(" ", "")


# ---- scripts ------------------------------------------------------------------------------------
_SCRIPTS = [
    ("devanagari", 0x0900, 0x097F), ("bengali", 0x0980, 0x09FF), ("gurmukhi", 0x0A00, 0x0A7F),
    ("gujarati", 0x0A80, 0x0AFF), ("tamil", 0x0B80, 0x0BFF), ("telugu", 0x0C00, 0x0C7F),
    ("arabic", 0x0600, 0x06FF), ("hebrew", 0x0590, 0x05FF), ("cyrillic", 0x0400, 0x04FF),
    ("greek", 0x0370, 0x03FF), ("thai", 0x0E00, 0x0E7F), ("kana", 0x3040, 0x30FF),
    ("cjk", 0x4E00, 0x9FFF), ("hangul", 0xAC00, 0xD7AF),
]


def scripts_of(text: str) -> set[str]:
    """Writing systems present in the letters of text; 'latin' for A-Z and Latin extensions."""
    out: set[str] = set()
    for ch in str(text or ""):
        c = ord(ch)
        if (0x41 <= c <= 0x5A) or (0x61 <= c <= 0x7A) or (0xC0 <= c <= 0x24F) or (0x1E00 <= c <= 0x1EFF):
            out.add("latin")
            continue
        for name, lo, hi in _SCRIPTS:
            if lo <= c <= hi:
                out.add(name)
                break
    return out


# ---- credits ------------------------------------------------------------------------------------
_CREDIT_SPLIT = re.compile(
    r"\s*(?:,|;|/|\+|&|\bx\b|\band\b|\bwith\b|\bvs\.?|\bfeat\.?|\bft\.?|\bfeaturing\b)\s*")


def _key(text: str) -> str:
    k = squash(text)
    return k[3:] if len(k) > 6 and k.startswith("the") else k


def credit_parts(text: str) -> list[str]:
    """Normalised credited names: the whole string and each part ('A feat. B' -> ['a feat b', 'a', 'b'])."""
    s = fold(text)
    out: list[str] = []
    for k in [_key(s)] + [_key(p) for p in _CREDIT_SPLIT.split(s)]:
        if k and k not in out:
            out.append(k)
    return out


def primary_credit(text: str) -> str:
    """The first credited name of a pick's artist field ('A & B' -> 'a'), squashed."""
    parts = [p for p in (_key(x) for x in _CREDIT_SPLIT.split(fold(text))) if p]
    return parts[0] if parts else _key(text)


def credits_in_order(text: str) -> list[str]:
    """Squashed credited names of a pick's artist field in credit order ('A & B feat. C' -> ['a', 'b', 'c'])."""
    return [k for k in (_key(x) for x in _CREDIT_SPLIT.split(fold(text))) if k]


def _credit_set(texts: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for t in texts:
        out.update(credit_parts(t))
    return out


# ---- title parsing ------------------------------------------------------------------------------
VERSION_RE = re.compile(
    r"\b(remix|re-?edit|edit|rework|bootleg|vip|flip|extended|mashup|refix|dub|sped|slowed|acoustic|instrumental)\b")
_NOISE = {
    "official", "audio", "video", "music", "lyric", "lyrics", "lyrical", "visualizer", "visualiser", "hd", "hq",
    "4k", "1080p", "explicit", "remastered", "remaster", "clean", "full", "song", "mv", "clip", "single",
    "original", "radio", "version", "new", "out", "now", "topic", "audios",
}
_NOISE_TRAIL = {"official", "audio", "video", "lyrics", "lyric", "visualizer", "visualiser", "hd", "hq", "4k",
                "explicit", "remastered", "mv"}
_NOISE_PHRASES = ["radio edit", "original mix", "album version", "single version", "radio version", "original version"]
_FEAT_GROUP = re.compile(r"^(?:feat|ft|featuring|with|prod|prod by)\b(.*)$")
_BRACKETS = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")
_TRAIL_FEAT = re.compile(r"\s(?:feat\.?|ft\.?|featuring)\s+(.+)$")
_DASH = re.compile(r"\s+[-–—]\s+")
_YEAR = re.compile(r"^(?:19|20)\d\d$")


def _classify(inner: str) -> tuple[str, str]:
    """('feat'|'noise'|'version'|'keep', normalised text) of one bracketed / dash-suffix group."""
    n = norm(inner)
    m = _FEAT_GROUP.match(n)
    if m:
        return "feat", m.group(1).strip()
    rest = n
    for ph in _NOISE_PHRASES:
        rest = rest.replace(ph, " ")
    toks = rest.split()
    if all(t in _NOISE or _YEAR.match(t) for t in toks):
        return "noise", n
    if VERSION_RE.search(n):
        return "version", n
    return "keep", n


@dataclass
class Parsed:
    core: list[str]                      # words of the song title, noise / feat / version groups removed
    feats: list[str] = field(default_factory=list)      # credited names from "(feat. X)"
    version: list[str] = field(default_factory=list)    # words of a version group ("askjell remix")
    versioned: bool = False              # the title says remix / edit / ... anywhere


def parse_song(text: str) -> Parsed:
    s = fold(text)
    feats: list[str] = []
    versions: list[str] = []

    def group(m):
        kind, n = _classify(m.group(1))
        if kind == "feat":
            feats.append(n)
            return " "
        if kind == "noise":
            return " "
        if kind == "version":
            versions.append(n)
            return " "
        return " " + m.group(1) + " "

    s = _BRACKETS.sub(group, s)
    s = s.split(" | ")[0]
    m = _TRAIL_FEAT.search(s)
    if m:
        feats.append(norm(m.group(1)))
        s = s[:m.start()]
    d = _DASH.search(s)
    if d:
        head, tail = s[:d.start()], s[d.end():]
        kind, n = _classify(tail)
        if kind == "feat":
            feats.append(n)
            s = head
        elif kind == "noise":
            s = head
        elif kind == "version":
            versions.append(n)
            s = head
    toks = norm(s).split()
    while len(toks) > 1 and toks[-1] in _NOISE_TRAIL:
        toks.pop()
    versioned = bool(versions) or bool(VERSION_RE.search(" ".join(toks)))
    return Parsed(toks, feats, [w for v in versions for w in v.split()], versioned)


def split_title(title: str, pick_credits: set[str]) -> tuple[str, str, str]:
    """(artist part, song part, link) of an upload title. link: 'prefix' (Artist - Title), 'suffix'
    (Title - Artist), 'other' (a different artist's prefix), '' (no artist in the title)."""
    s = fold(title)
    d = _DASH.search(s)
    if not d:
        return "", title, ""
    left, right = s[:d.start()], s[d.end():]
    if pick_credits & _credit_set([_strip_feat(left)]):
        return left, right, "prefix"
    if pick_credits & _credit_set([_strip_feat(right)]):
        return right, left, "suffix"
    kind, _ = _classify(right)
    if kind in ("noise", "version", "feat"):
        return "", title, ""                    # "Title - Official Video" / "Title - X Remix": no artist prefix
    return left, right, "other"


def _strip_feat(text: str) -> str:
    return _BRACKETS.sub(" ", text)


# ---- junk ---------------------------------------------------------------------------------------
_JUNK_RE = re.compile(
    r"\b(samples?|snippets?|previews?|teasers?|shorts?|type\s+beat|a\s+cappella|acapella|isolated\s+vocals?|"
    r"no\s+vocals?|full\s+album|album\s+stream|interviews?|podcasts?|reacts?|reaction|pronounce|pronunciation|"
    r"tutorial|behind\s+the\s+scenes|making\s+of|trailer|promo|commentary|explained|story\s+behind|"
    r"bass\s+boosted|reverb|nightcore|sped\s+up|8d|fan\s*made|fan\s+edit|unofficial|whatsapp\s+status|"
    r"status\s+video|ringtone|karaoke|cover|tribute|asmr|vlogs?|documentary|unreleased|leaked|"
    r"in\s+the\s+style\s+of|originally\s+performed)\b|#shorts")
_LYRIC_RE = re.compile(r"lyric|lyrical|letra|visuali[sz]er|lyrics? booklet|visual backdrop")
_CHANNEL_SUFFIX = re.compile(r"\s*(?:vevo|official|music|records|recordings|topic)\s*$")


# ---- verdict ------------------------------------------------------------------------------------
@dataclass
class Verdict:
    ok: bool
    score: float
    tier: str                            # high | ok | reject
    reasons: list[str]                   # why rejected, or the evidence that cleared it

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def _reject(reason: str, score: float = 0.0) -> Verdict:
    return Verdict(False, score, "reject", [reason])


def _tier(score: float) -> str:
    return "high" if score >= HIGH_SCORE else "ok" if score >= OK_SCORE else "reject"


def judge(
    pick_artist: str, pick_title: str, title: str, *, channel: str = "", uploader: str = "",
    artists: Optional[Iterable[str]] = None, duration: Optional[float] = None,
    min_secs: float = MIN_SECS, max_secs: float = MAX_SECS, expected_duration: Optional[float] = None,
    scene_scripts: Optional[Iterable[str]] = None, min_score: float = OK_SCORE,
) -> Verdict:
    """Score one candidate against the pick. ok = every hard rule passed and score >= min_score."""
    title = str(title or "")
    if not title.strip():
        return _reject("empty title")
    pick_ents = {primary_credit(pick_artist), _key(pick_artist)}
    pick_ents.discard("")
    if not pick_ents:
        return _reject("pick has no artist")
    want = parse_song(pick_title)
    if not want.core:
        return _reject("pick has no title")

    # shape: length and spoken / clip / fan uploads
    if duration is not None and duration > 0:
        cap = max(max_secs, EXTENDED_MAX_SECS) if "extended" in want.version + want.core else max_secs
        if duration >= cap:
            return _reject(f"too long ({int(duration)}s), likely a mix/set")
        if duration < min_secs:
            return _reject(f"too short ({int(duration)}s), likely a clip/snippet")
        if expected_duration and expected_duration > 0 and abs(duration - expected_duration) > max(45.0, 0.35 * expected_duration):
            return _reject(f"length {int(duration)}s far from the known {int(expected_duration)}s")
    hay = norm(f"{title} {channel}")
    wanted_words = set(norm(pick_title).split())
    for m in _JUNK_RE.finditer(" " + hay + " "):
        w = m.group(0).strip()
        if w and not set(w.split()) & wanted_words and w not in norm(pick_artist):
            return _reject(f"not the studio song (\"{w}\")")

    # split "Artist - Title" and read the song part
    left, right, link = split_title(title, pick_ents)
    got = parse_song(right)
    cand_title_feats = list(got.feats)
    if link in ("prefix", "suffix"):
        cand_title_feats += parse_song(left).feats

    # artist evidence
    meta = [str(a) for a in (artists or []) if a]
    meta_set = _credit_set(meta)
    ch, up = str(channel or ""), str(uploader or "")
    evidence = 0.0
    how = ""
    contradicted = ""
    if meta_set:
        if pick_ents & meta_set:
            evidence, how = 1.0, "metadata"
        else:
            contradicted = "artist metadata names someone else"
    if ch.lower().rstrip().endswith("- topic"):
        topic = _credit_set([re.sub(r"\s*-\s*topic\s*$", "", ch, flags=re.I)])
        if pick_ents & topic:
            evidence, how, contradicted = 1.0, how or "topic channel", ""
        elif not evidence:
            contradicted = "topic channel names someone else"
    if contradicted:
        return _reject(contradicted)
    official_channel = False
    if evidence < 1.0:
        if link in ("prefix", "suffix") and evidence < 0.9:
            evidence, how = 0.9, "title credit"
        for who in (ch, up):
            base = _CHANNEL_SUFFIX.sub("", fold(who))
            if base and pick_ents & _credit_set([base]):
                official_channel = True
                if evidence < 0.85:
                    evidence, how = 0.85, "artist channel"
        if evidence < 0.8 and pick_ents & _credit_set(cand_title_feats):
            evidence, how = 0.8, "featured credit"
    if link == "other" and evidence < 0.8:
        return _reject("title credits another artist")
    if evidence == 0.0:
        return _reject("artist not confirmed")

    # title
    if _sequel(got.core, want.core):
        return _reject("a sequel (Two / Five / Pt. 2) of the requested song, not the song")
    if got.core == want.core:
        t_score = 1.0
    elif "".join(got.core) == "".join(want.core):
        t_score = 0.95
    else:
        t_score = 0.0
        if len(want.core) >= 4 and len(got.core) >= 4:
            a, b = set(want.core), set(got.core)
            if len(a & b) / len(a | b) >= 0.85:
                t_score = 0.8
        if not t_score:
            return _reject(f"song title does not match (\"{' '.join(got.core)}\")")

    # version
    if got.versioned != want.versioned:
        return _reject("alternate version (remix/edit) not requested" if got.versioned else "wanted version not found")
    if want.version:
        need = {w for w in want.version if not VERSION_RE.fullmatch(w)}
        if need and not need <= set(got.version) | set(got.core):
            return _reject("a different remix / version")

    # writing system
    scripts_title = scripts_of(title)
    allowed = scripts_of(pick_artist + pick_title) | set(scene_scripts or ())
    if scene_scripts is not None and scripts_title - {"latin"} and not scripts_title & allowed and evidence < 0.9:
        return _reject("writing system differs from the pick and the set")

    quality = 0.6
    topic_ch = ch.lower().rstrip().endswith("- topic")
    if topic_ch:
        quality = 1.0
    elif official_channel or "official audio" in norm(title):
        quality = 0.9
    if _LYRIC_RE.search(title.lower()) and not _LYRIC_RE.search(pick_title.lower()):
        quality = 0.4
    score = round(0.5 * evidence + 0.35 * t_score + 0.15 * quality, 4)
    tier = _tier(score)
    if score < min_score or tier == "reject":
        return _reject(f"low confidence {score:.2f} ({how})", score)
    return Verdict(True, score, tier, [f"artist: {how}", f"title: {t_score:.2f}", f"source: {quality:.1f}"])


_SEQUEL = {"two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
           "ii", "iii", "iv", "v", "2", "3", "4", "5", "6", "7", "8", "9", "10"}


def _sequel(found: list[str], wanted: list[str]) -> bool:
    """found == wanted + a sequel marker the request lacks ("Victory Lap Five", "Song Pt. 2")."""
    n = len(wanted)
    if len(found) <= n or found[:n] != wanted:
        return False
    rest = found[n:n + 2]
    return rest[0] in _SEQUEL or (rest[0] in ("pt", "part") and len(rest) > 1)


def pick_of(query: str) -> tuple[str, str]:
    """'Artist - Title' -> (artist, title); no ' - ' -> ('', whole)."""
    artist, sep, title = str(query or "").partition(" - ")
    return (artist.strip(), title.strip()) if sep else ("", str(query or "").strip())


def side_artists(artist: str, title: str = "") -> set[str]:
    """Credited names of the playing song that are NOT its primary artist (feat. / & / x / , / with):
    the 'side artists' a pick may hop to."""
    primary = primary_credit(artist)
    names = set()
    for text in (artist, *[m for m in _feat_names(title)]):
        for p in _CREDIT_SPLIT.split(fold(text)):
            k = _key(p)
            if k:
                names.add(k)
    names.discard(primary)
    return names


def _feat_names(title: str) -> list[str]:
    out = []
    for m in _BRACKETS.finditer(fold(title)):
        kind, n = _classify(m.group(1))
        if kind == "feat":
            out.append(n)
    m = _TRAIL_FEAT.search(fold(title))
    if m:
        out.append(m.group(1))
    return out
