"""Finding the font that looks most like the one a document already uses.

Two jobs live here:

* :func:`family_key` reduces the many ways a font gets named in a PDF
  (``"HSYXQO+Georgia Regular"``, ``"Arial-BoldMT"``, ``"Calibri"``) to a
  comparable family name.
* :class:`SystemFontIndex` indexes the fonts installed on this machine, so a
  replacement can be drawn in the closest available face when the document's own
  font cannot be reused.

Scanning costs roughly 1.5s on a typical machine, so it happens lazily and is
cached for the life of the process.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from dataclasses import dataclass
from typing import Iterable, Sequence

import pymupdf

__all__ = ["FontFile", "SystemFontIndex", "css_family_stack", "display_family",
           "family_key", "name_key", "system_fonts"]

# "ABCDEF+Georgia" -> "Georgia". Six capitals and a plus is the subset convention.
_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Stripped only from the *end* of a normalised name, and deliberately WITHOUT
# "roman": it is a style word in "Times-Roman" but part of the family in "Times
# New Roman", and stripping it shatters the latter into "timesnew".
_TRAILING_STYLE = re.compile(
    r"(?:(?:bold|italic|oblique|light|medium|semibold|demibold|black|thin|heavy|"
    r"condensed|narrow|extended|regular|book|text|display|caption|subhead|"
    r"mt|ps|ms|std|pro|it|bd|rg|lt))+$"
)

# Icon, symbol and collection faces. These are never a sane substitute for body
# text, and a glyph-coverage check will NOT catch them: dingbat fonts map the
# Latin range to pictures, so they happily "cover" a normal sentence and render
# it as little pictures.
_DENY_HINTS = (
    "icon", "mdl2", "fluent", "dingbat", "webding", "wingding", "emoji", "symbol",
    "assets", "collection", "braille", "signwriting", "marlett", "holiday", "math",
    "music", "chess", "arrows", "ornament", "decotype", "seguisym",
)

# Widely-available faces, used only to break ties when the family is unknown.
_COMMON_FAMILIES = frozenset((
    "arial", "helvetica", "liberationsans", "dejavusans", "arimo",
    "times", "timesnewroman", "liberationserif", "tinos",
    "courier", "couriernew", "liberationmono", "cousine",
    "calibri", "carlito", "cambria", "caladea", "georgia",
    "verdana", "tahoma", "segoeui", "notosans", "notoserif",
))

# Families that are metric- or shape-compatible with one another, best first.
# Keeps a document set in Calibri looking like Calibri even on a machine that has
# never had Calibri installed.
ALIASES: dict[str, tuple[str, ...]] = {
    "helvetica": ("arial", "liberationsans", "arimo", "helveticaneue", "nimbussans"),
    "helveticaneue": ("helvetica", "arial", "liberationsans"),
    "arial": ("helvetica", "liberationsans", "arimo"),
    "arialnarrow": ("liberationsansnarrow", "arial", "helvetica"),
    "times": ("timesnewroman", "liberationserif", "tinos"),
    "timesroman": ("times", "timesnewroman", "liberationserif", "tinos"),
    "timesnewroman": ("times", "liberationserif", "tinos"),
    "courier": ("couriernew", "liberationmono", "cousine"),
    "couriernew": ("courier", "liberationmono", "cousine"),
    "calibri": ("carlito", "segoeui", "verdanat"),
    "cambria": ("caladea", "georgia"),
    "segoeui": ("selawik", "carlito", "tahoma"),
    "georgia": ("gelasio", "cambria"),
    "verdana": ("dejavusans", "tahoma"),
    "tahoma": ("verdana", "dejavusans"),
    "garamond": ("ebgaramond", "georgia"),
    "consolas": ("inconsolata", "couriernew"),
    "roboto": ("opensans", "arial", "helvetica"),
    "opensans": ("roboto", "arial", "helvetica"),
    "lato": ("opensans", "arial", "helvetica"),
    "futura": ("josefinsans", "montserrat", "arial"),
    "palatino": ("texgyrepagella", "georgia"),
    "bookantiqua": ("texgyrepagella", "georgia"),
    "candara": ("carlito", "calibri"),
    "constantia": ("caladea", "cambria"),
    "corbel": ("carlito", "calibri"),
}

_MONO_HINTS = ("courier", "mono", "consol", "menlo", "inconsolata", "cousine", "code", "terminal")
_SERIF_HINTS = ("serif", "times", "georgia", "garamond", "cambria", "palatino", "book", "roman", "constantia", "kelvin")
_SANS_HINTS = ("sans", "arial", "helvet", "calibri", "segoe", "verdana", "tahoma", "roboto", "lato", "futura", "corbel", "candara")


def family_key(name: str | None) -> str:
    """Comparable family name: ``"HSYXQO+Georgia Regular"`` -> ``"georgia"``."""
    text = _SUBSET_PREFIX.sub("", (name or "").strip())
    key = _NON_ALNUM.sub("", text.lower())
    if not key:
        return ""
    stripped = _TRAILING_STYLE.sub("", key)
    return stripped or key


def name_key(name: str | None) -> str:
    """Full name with styles intact: ``"HSYXQO+Georgia Regular"`` -> ``"georgiaregular"``.

    Used to pair a text run with the matching entry in the page's font list, so
    that a bold run finds the bold face rather than the regular one.
    """
    text = _SUBSET_PREFIX.sub("", (name or "").strip())
    return _NON_ALNUM.sub("", text.lower())


def style_of(name: str | None) -> tuple[bool, bool]:
    """(bold, italic) guessed from a font name, for when no metrics are available."""
    low = (name or "").lower()
    bold = any(h in low for h in ("bold", "black", "heavy", "semibold", "demibold", "-bd", "medi"))
    italic = any(h in low for h in ("italic", "oblique", "-it", "slanted"))
    return bold, italic


def classify(name: str | None) -> str:
    """'mono' | 'serif' | 'sans', from the name alone."""
    low = (name or "").lower()
    if any(h in low for h in _MONO_HINTS):
        return "mono"
    if any(h in low for h in _SERIF_HINTS):
        return "serif"
    return "sans"


def default_font_dirs() -> list[str]:
    """Where fonts usually live, honouring an override for unusual setups."""
    override = os.environ.get("PDF_EDITOR_FONT_DIRS")
    if override:
        return [p for p in override.split(os.pathsep) if p]
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        windir = os.environ.get("WINDIR", r"C:\Windows")
        return [os.path.join(windir, "Fonts"),
                os.path.join(os.environ.get("LOCALAPPDATA", home), "Microsoft", "Windows", "Fonts")]
    if sys.platform == "darwin":
        return ["/System/Library/Fonts", "/Library/Fonts", os.path.join(home, "Library", "Fonts")]
    return ["/usr/share/fonts", "/usr/local/share/fonts",
            os.path.join(home, ".fonts"), os.path.join(home, ".local/share/fonts")]


@dataclass(frozen=True)
class FontFile:
    """One installed font, with just enough metadata to rank it."""

    path: str
    name: str        # "Georgia Regular"
    family: str      # normalised: "georgia"
    bold: bool
    italic: bool
    kind: str        # mono | serif | sans

    def label(self) -> str:
        return self.name or os.path.basename(self.path)


class SystemFontIndex:
    """The installed fonts, indexed for close-enough matching."""

    def __init__(self, dirs: Sequence[str] | None = None, max_files: int = 600) -> None:
        self._dirs = list(dirs) if dirs is not None else default_font_dirs()
        self._max_files = max_files
        self._fonts: list[FontFile] | None = None
        self._by_family: dict[str, list[FontFile]] = {}
        self._coverage: dict[tuple[str, int], bool] = {}
        self._lock = threading.Lock()

    # -- discovery ---------------------------------------------------------

    def _iter_files(self) -> Iterable[str]:
        seen = 0
        for root in self._dirs:
            if not os.path.isdir(root):
                continue
            for dirpath, _dirnames, filenames in os.walk(root):
                for filename in sorted(filenames):
                    if not filename.lower().endswith((".ttf", ".otf", ".ttc", ".otc")):
                        continue
                    yield os.path.join(dirpath, filename)
                    seen += 1
                    if seen >= self._max_files:
                        return

    def scan(self) -> list[FontFile]:
        """Read every font's metadata. Called once, then cached."""
        with self._lock:
            if self._fonts is not None:
                return self._fonts
            fonts: list[FontFile] = []
            for path in self._iter_files():
                try:
                    f = pymupdf.Font(fontfile=path)
                except Exception:
                    continue  # collections, broken files, unsupported formats
                name = getattr(f, "name", "") or os.path.basename(path)
                if any(hint in name.lower() for hint in _DENY_HINTS):
                    continue
                family = family_key(name) or family_key(path)
                if not family:
                    continue
                bold, italic = style_of(name)
                bold = bool(getattr(f, "is_bold", 0)) or bold
                italic = bool(getattr(f, "is_italic", 0)) or italic
                fonts.append(FontFile(path=path, name=name, family=family,
                                      bold=bold, italic=italic, kind=classify(name)))
            self._fonts = fonts
            index: dict[str, list[FontFile]] = {}
            for font in fonts:
                index.setdefault(font.family, []).append(font)
            self._by_family = index
            return fonts

    def __len__(self) -> int:
        return len(self.scan())

    # -- matching ----------------------------------------------------------

    def _score(self, font: FontFile, family: str, bold: bool, italic: bool, kind: str) -> int:
        score = 0
        if family and font.family == family:
            score += 100
        elif family:
            aliases = ALIASES.get(family, ())
            if font.family in aliases:
                # earlier aliases are better
                score += 80 - aliases.index(font.family)
            elif font.family.startswith(family) or family.startswith(font.family):
                score += 50
            elif family in ALIASES and font.family in ALIASES:
                score += 5  # both known, different families: weak signal
        score += 12 if font.bold == bold else -12
        score += 12 if font.italic == italic else -12
        score += 8 if font.kind == kind else -4
        if font.family in _COMMON_FAMILIES:
            score += 4  # predictable face for a family we could not identify
        if font.path.lower().endswith((".ttc", ".otc")):
            # Collections frequently cannot be embedded as a single face, and
            # MuPDF then substitutes a built-in instead of failing loudly.
            score -= 3
        return score

    def covers(self, font: FontFile, codepoints: set[int]) -> bool:
        """Can this file actually draw every one of these characters?"""
        missing = frozenset(codepoints)
        key = (font.path, hash(missing))
        cached = self._coverage.get(key)
        if cached is not None:
            return cached
        try:
            parsed = pymupdf.Font(fontfile=font.path)
            ok = all(parsed.has_glyph(cp) for cp in missing)
        except Exception:
            ok = False
        self._coverage[key] = ok
        return ok

    # How many candidates to test for glyph coverage. Latin text is settled well
    # inside this; anything else (CJK and friends) is better served by the
    # built-in fallback than by parsing every font on the machine.
    SEARCH_WINDOW = 24

    def best_match(self, name: str | None, text: str, *,
                   bold: bool | None = None, italic: bool | None = None) -> FontFile | None:
        """The installed font that looks most like `name` and can draw `text`."""
        fonts = self.scan()
        if not fonts:
            return None
        family = family_key(name)
        guess_bold, guess_italic = style_of(name)
        want_bold = guess_bold if bold is None else bold
        want_italic = guess_italic if italic is None else italic
        kind = classify(name)
        want = {ord(ch) for ch in text if not ch.isspace()}

        ranked = sorted(fonts, key=lambda f: self._score(f, family, want_bold, want_italic, kind),
                        reverse=True)
        # Walk the ranking and take the first face that can draw every character.
        for candidate in ranked[:self.SEARCH_WINDOW]:
            if self.covers(candidate, want):
                return candidate
        return None


_GENERIC_FAMILY = {"mono": "monospace", "serif": "serif", "sans": "sans-serif"}

# Style words that are safe to drop from the end of a display name. "Roman" is
# deliberately absent: it belongs to "Times New Roman".
_TRAILING_DISPLAY_STYLE = re.compile(
    r"(?i)\s*\b(?:bold|italic|oblique|regular|light|medium|semibold|demibold|black|"
    r"thin|book|display|mt|ps|ms|std|pro|bd|it|rg|lt)\b\s*$"
)


def display_family(name: str | None) -> str:
    """A family name a browser has a chance of knowing.

    ``"Cambria-Bold"`` -> ``"Cambria"``, ``"Times New Roman"`` unchanged.
    """
    text = _SUBSET_PREFIX.sub("", (name or "").strip())
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for _ in range(3):
        trimmed = _TRAILING_DISPLAY_STYLE.sub("", text).strip()
        if not trimmed or trimmed == text:
            break
        text = trimmed
    return text or (name or "").strip()


def css_family_stack(name: str | None, family: str) -> str:
    """A CSS font stack for previewing `name`, ending in a generic family.

    The editor's preview and the server run on the same machine, so the
    document's own family name and the closest installed face are both worth
    naming before falling back to a generic.
    """
    parts: list[str] = []
    own = display_family(name)
    if own:
        parts.append(f'"{own}"')
    try:
        match = system_fonts.best_match(name, "")
        if match is not None:
            installed = display_family(match.name)
            if installed and f'"{installed}"' not in parts:
                parts.append(f'"{installed}"')
    except Exception:
        pass
    parts.append(_GENERIC_FAMILY.get(family, "sans-serif"))
    return ", ".join(parts)


# One index for the whole process.
system_fonts = SystemFontIndex()


def warm_cache() -> None:
    """Scan in the background so the first export isn't the one that pays for it."""
    threading.Thread(target=system_fonts.scan, name="font-index", daemon=True).start()
