"""Language code normalisation.

Everything in the app is keyed on a canonical ISO 639-2/B code ('eng', 'chi',
'jpn', ...) because that is what mkvmerge reports in `properties.language`.
Anything the user types, or that appears in a subtitle filename, is funnelled
through :func:`normalize` first.
"""

from __future__ import annotations

# canonical -> every spelling we accept for it
ALIASES: dict[str, list[str]] = {
    "eng": ["eng", "en", "english"],
    "chi": [
        "chi", "zho", "zh", "chs", "cht", "chinese", "mandarin", "cantonese",
        "big5", "gb", "gbk", "zhcn", "zhtw", "zhhk", "zhhans", "zhhant",
        "hans", "hant", "cmn", "yue", "cn", "tw",
        # common release-group spellings
        "simplified", "traditional", "zhs", "zht",
    ],
    "jpn": ["jpn", "ja", "jp", "japanese", "nihongo"],
    "kor": ["kor", "ko", "kr", "korean"],
    "fre": ["fre", "fra", "fr", "french", "francais"],
    "ger": ["ger", "deu", "de", "german", "deutsch"],
    "spa": ["spa", "es", "esp", "spanish", "castellano", "latino"],
    "rus": ["rus", "ru", "russian"],
    "ita": ["ita", "it", "italian"],
    "por": ["por", "pt", "ptbr", "pob", "portuguese", "brazilian"],
    "dut": ["dut", "nld", "nl", "dutch"],
    "pol": ["pol", "pl", "polish"],
    "tur": ["tur", "tr", "turkish"],
    "ara": ["ara", "ar", "arabic"],
    "hin": ["hin", "hi", "hindi"],
    "tha": ["tha", "th", "thai"],
    "vie": ["vie", "vi", "vietnamese"],
    "ind": ["ind", "id", "indonesian"],
    "swe": ["swe", "sv", "swedish"],
    "nor": ["nor", "no", "nob", "norwegian"],
    "dan": ["dan", "da", "danish"],
    "fin": ["fin", "fi", "finnish"],
    "cze": ["cze", "ces", "cs", "czech"],
    "gre": ["gre", "ell", "el", "greek"],
    "heb": ["heb", "he", "hebrew"],
    "ukr": ["ukr", "uk", "ukrainian"],
    "hun": ["hun", "hu", "hungarian"],
    "ron": ["ron", "rum", "ro", "romanian"],
    "per": ["per", "fas", "fa", "farsi", "persian", "parsi"],
    "ben": ["ben", "bn", "bengali", "bangla"],
    "tam": ["tam", "ta", "tamil"],
    "tel": ["tel", "te", "telugu"],
    "mar": ["mar", "mr", "marathi"],
    "guj": ["guj", "gu", "gujarati"],
    "kan": ["kan", "kn", "kannada"],
    "mal": ["mal", "ml", "malayalam"],
    "pan": ["pan", "pa", "punjabi", "panjabi"],
    "urd": ["urd", "ur", "urdu"],
    "nep": ["nep", "ne", "nepali"],
    "sin": ["sin", "si", "sinhala", "sinhalese"],
    "khm": ["khm", "km", "khmer", "cambodian"],
    "lao": ["lao", "lo", "laotian"],
    "bur": ["bur", "mya", "my", "burmese", "myanmar"],
    "may": ["may", "msa", "ms", "malay", "melayu"],
    "fil": ["fil", "tgl", "tl", "tagalog", "filipino"],
    "bul": ["bul", "bg", "bulgarian"],
    "hrv": ["hrv", "scr", "hr", "croatian"],
    "srp": ["srp", "scc", "sr", "serbian"],
    "bos": ["bos", "bs", "bosnian"],
    "slv": ["slv", "sl", "slovenian", "slovene"],
    "slo": ["slo", "slk", "sk", "slovak"],
    "mkd": ["mkd", "mac", "mk", "macedonian"],
    "alb": ["alb", "sqi", "sq", "albanian"],
    "lav": ["lav", "lv", "latvian"],
    "lit": ["lit", "lt", "lithuanian"],
    "est": ["est", "et", "estonian"],
    "ice": ["ice", "isl", "is", "icelandic"],
    "gle": ["gle", "ga", "irish", "gaeilge"],
    "wel": ["wel", "cym", "cy", "welsh"],
    "cat": ["cat", "ca", "catalan"],
    "eus": ["eus", "baq", "eu", "basque"],
    "glg": ["glg", "gl", "galician"],
    "afr": ["afr", "af", "afrikaans"],
    "swa": ["swa", "sw", "swahili", "kiswahili"],
    "amh": ["amh", "am", "amharic"],
    "hau": ["hau", "ha", "hausa"],
    "yor": ["yor", "yo", "yoruba"],
    "zul": ["zul", "zu", "zulu"],
    "aze": ["aze", "az", "azerbaijani"],
    "kaz": ["kaz", "kk", "kazakh"],
    "uzb": ["uzb", "uz", "uzbek"],
    "geo": ["geo", "kat", "ka", "georgian"],
    "arm": ["arm", "hye", "hy", "armenian"],
    "bel": ["bel", "be", "belarusian"],
    "mon": ["mon", "mn", "mongolian"],
    "lat": ["lat", "la", "latin"],
    "epo": ["epo", "eo", "esperanto"],
    "mri": ["mri", "mao", "mi", "maori"],
    "haw": ["haw", "hawaiian"],
    "yid": ["yid", "yi", "yiddish"],
    "mlt": ["mlt", "mt", "maltese"],
    "und": ["und", "unknown", "undetermined"],
    "mul": ["mul", "multiple"],
    "zxx": ["zxx"],
}

# Human-friendly names, used only for console output.
DISPLAY: dict[str, str] = {
    "eng": "English", "chi": "Chinese", "jpn": "Japanese", "kor": "Korean",
    "fre": "French", "ger": "German", "spa": "Spanish", "rus": "Russian",
    "ita": "Italian", "por": "Portuguese", "dut": "Dutch", "pol": "Polish",
    "tur": "Turkish", "ara": "Arabic", "hin": "Hindi", "tha": "Thai",
    "vie": "Vietnamese", "ind": "Indonesian", "swe": "Swedish",
    "nor": "Norwegian", "dan": "Danish", "fin": "Finnish", "cze": "Czech",
    "gre": "Greek", "heb": "Hebrew", "ukr": "Ukrainian", "hun": "Hungarian",
    "ron": "Romanian",
    "per": "Persian",
    "ben": "Bengali",
    "tam": "Tamil",
    "tel": "Telugu",
    "mar": "Marathi",
    "guj": "Gujarati",
    "kan": "Kannada",
    "mal": "Malayalam",
    "pan": "Punjabi",
    "urd": "Urdu",
    "nep": "Nepali",
    "sin": "Sinhala",
    "khm": "Khmer",
    "lao": "Lao",
    "bur": "Burmese",
    "may": "Malay",
    "fil": "Filipino",
    "bul": "Bulgarian",
    "hrv": "Croatian",
    "srp": "Serbian",
    "bos": "Bosnian",
    "slv": "Slovenian",
    "slo": "Slovak",
    "mkd": "Macedonian",
    "alb": "Albanian",
    "lav": "Latvian",
    "lit": "Lithuanian",
    "est": "Estonian",
    "ice": "Icelandic",
    "gle": "Irish",
    "wel": "Welsh",
    "cat": "Catalan",
    "eus": "Basque",
    "glg": "Galician",
    "afr": "Afrikaans",
    "swa": "Swahili",
    "amh": "Amharic",
    "hau": "Hausa",
    "yor": "Yoruba",
    "zul": "Zulu",
    "aze": "Azerbaijani",
    "kaz": "Kazakh",
    "uzb": "Uzbek",
    "geo": "Georgian",
    "arm": "Armenian",
    "bel": "Belarusian",
    "mon": "Mongolian",
    "lat": "Latin",
    "epo": "Esperanto",
    "mri": "Maori",
    "haw": "Hawaiian",
    "yid": "Yiddish",
    "mlt": "Maltese",
    "und": "Undetermined",
    "mul": "Multiple languages",
    "zxx": "No linguistic content",
}

_LOOKUP: dict[str, str] = {}
for _canon, _spellings in ALIASES.items():
    _LOOKUP[_canon] = _canon
    for _s in _spellings:
        _LOOKUP[_s] = _canon

#: Every spelling that resolves to a language. Safe for an explicit code the
#: user typed; NOT safe for scanning filenames.
ALL_TOKENS: frozenset[str] = frozenset(_LOOKUP)

#: Two-letter codes common enough in subtitle filenames to be worth matching.
_SAFE_SHORT = frozenset({"en", "zh", "ja", "jp", "ko", "fr", "de", "es",
                         "it", "pt", "ru", "cn", "tw", "hk"})

#: Tokens safe to treat as a language marker *inside a filename*. Bare
#: two-letter codes are excluded because they collide with ordinary words:
#: "ne" is Nepali, but "Ne" in "Ne.Zha.2019" is half the title, and matching
#: it truncates the title to nothing and silently breaks subtitle pairing.
FILENAME_TOKENS: frozenset[str] = frozenset(
    t for t in _LOOKUP if len(t) >= 3) | _SAFE_SHORT

#: Tokens that qualify a subtitle track but are not languages.
QUALIFIERS: frozenset[str] = frozenset(
    {"forced", "sdh", "full", "default", "hi", "cc", "signs", "songs"}
)


def normalize(code: str | None) -> str | None:
    """'zh-Hans', 'Chinese', 'chs' -> 'chi'. Unknown codes pass through lowered."""
    if not code:
        return None
    key = code.strip().lower().replace("-", "").replace("_", "")
    if key in _LOOKUP:
        return _LOOKUP[key]
    # 'zh-Hans-CN' style: try progressively shorter prefixes
    parts = [p for p in code.strip().lower().replace("_", "-").split("-") if p]
    for n in range(len(parts), 0, -1):
        key = "".join(parts[:n])
        if key in _LOOKUP:
            return _LOOKUP[key]
    if parts and parts[0] in _LOOKUP:
        return _LOOKUP[parts[0]]
    return code.strip().lower() or None


def display(code: str | None) -> str:
    if not code:
        return "?"
    canon = normalize(code) or code
    return DISPLAY.get(canon, canon)


def matches(track_lang: str | None, wanted: str | None) -> bool:
    """True if a track's language is the language the user asked for."""
    if not wanted:
        return False
    a, b = normalize(track_lang), normalize(wanted)
    return a is not None and a == b
