"""Build the given-name lists in legal_analyzer/given_names_en.py and given_names_tr.py.

The name gazetteers in `english_names.py` and `turkish_names.py` are the
precision anchor of the bare-name path: a run of capitalised words is a person
when its first word is a known given name. The hand-written lists there are
small (556 English, 278 Turkish names), and every name the held-out corpus
misses starts with a given name that is on neither. This script widens them
from official name statistics, and subtracts the words that would turn a
bigger list into false positives -- a given name that is also an ordinary
word of either language, a place, a brand or a legal term ("Kira" is a Social
Security name and the Turkish for rent, so "Kira Bedeli" read as a person;
"London" is a name and a city, so "London EC" did; "Kuzey" is a TÜİK name and
the Turkish for north, so "Kuzey Cephesi" did).

It is run by hand, offline, from downloaded source files; the app and the test
suite only ever import the two generated modules, which carry their own
provenance. Nothing here is fitted to `eval/`: the selection rule for each
source is the publisher's own headline cut, fixed before any measurement.

Sources (all public; licences in the generated modules and README.md):

* English: US Social Security Administration, "Baby Names from Social Security
  Card Applications - National Data" (names.zip, yob1880..yob2025) -- every
  name in the top 1,000 for its sex in any year 1930-2025; and the Office for
  National Statistics, "Baby names in England and Wales: 1996 to 2025" -- every
  name in the top 100 for its sex in any year.
* Turkish: TÜİK, Adrese Dayalı Nüfus Kayıt Sistemi tables DF_ADNKS_T32 / T33,
  the most-used male and female given names by birth year (each year's top
  100), every name in them; a compound name ("ALİ OSMAN") contributes each of
  its words.

Subtracted from what a source adds (the hand-written lists are kept whole):

1. names shorter than three letters, or with anything but letters, an
   apostrophe or a hyphen;
2. ordinary English words, from both lists: SCOWL (Kevin Atkinson's Spell
   Checker Oriented Word Lists, 2020.12.07), the english/american/british
   "words" lists up to size 50, lowercase entries only. For the English list
   this is the curation rule the hand-written list already follows (no Will,
   Mark, Grant, Rose);
3. ordinary Turkish words, from both lists: the lowercase lemmas of
   Zemberek-NLP's master dictionary (Apache-2.0). For the Turkish list this is
   its own language's dictionary -- the "Kira Bedeli" class -- and it is
   broad: Zemberek carries many names as common nouns too (Kuzey, Hava,
   Savaş, but also Alperen, Nevzat, Yunus), and every one goes. A name the
   hand-written list carries (Barış, Deniz, Umut) is kept regardless;
3a. Turkish verb forms, from the Turkish list: Zemberek stores a verb as its
   infinitive ("durmak"), so its stem and the participles a heading or a
   clause is built from passed the lemma filter as names -- "Duran
   Varlıklar" (fixed assets) read as a person. Every lemma ending in -mak or
   -mek gives its stem ("yüksel"), its -an/-en participle ("duran") and its
   -mış/-miş/-muş/-müş participle ("durmuş"); the names this removes are
   listed in the generated module;
4. places, as whole words: GeoNames cities of 15,000 or more people in the
   United Kingdom and Ireland and of 100,000 or more anywhere else, every
   country name and capital in GeoNames' countryInfo (CC BY 4.0), the
   single-word US states (US_STATES), and every place `turkish_places`
   recognises (see is_place for why the Turkish districts go too);
5. legal and structural terms: both modules' NON_PERSON_TOKENS and month lists,
   the party and role labels of both label paths, and any word one of
   `legal_semantics`' operative-term rules matches on its own ("Bedel",
   "Garanti", "Lisans");
6. for English, brands that are also given names (BRANDS: "Mercedes Benz",
   "Harley Davidson");
7. for English, the homographs `english_names.EXCLUDED_HOMOGRAPHS` names
   (Morgan, Taylor, Victoria, ...), which the hand-written list leaves out on
   purpose.

Usage:

    python3 build_given_names.py \\
        --ssa names.zip --ons babynames1996to2025.xlsx \\
        --tuik DF_ADNKS_T32.json --tuik DF_ADNKS_T33.json \\
        --scowl scowl-2020.12.07/final --zemberek master-dictionary.dict \\
        --geonames-cities cities15000.zip --geonames-countries countryInfo.txt

`--check` compares instead of writing, and exits 1 when a generated module
differs from what the sources produce.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import zipfile
from pathlib import Path

from legal_analyzer import english_names, legal_semantics, turkish_names, turkish_places

ROOT = Path(__file__).resolve().parent
ENGLISH_OUT = ROOT / "legal_analyzer" / "given_names_en.py"
TURKISH_OUT = ROOT / "legal_analyzer" / "given_names_tr.py"

SSA_FIRST_YEAR = 1930
SSA_RANK = 1000
ONS_RANK = 100
SCOWL_MAX_SIZE = 50
# GeoNames cities (cities15000 already stops at 15,000 people) by country, with
# the population a city must reach. The United Kingdom and Ireland are held to
# the cut's own floor, every other country to the larger figure: US towns are
# so often named after people that at 15,000 the filter took Logan, Harrison
# and Kennedy out of the given names, and elsewhere only a major city is a
# place an English legal document names bare ("Geneva Convention").
GEONAMES_MIN_POPULATION = {"GB": 15_000, "IE": 15_000}
GEONAMES_DEFAULT_MIN_POPULATION = 100_000
# The single-word US states, which GeoNames' cities file does not carry;
# several are Social Security names ("Montana Ridge" read as a person).
US_STATES = frozenset(
    """
    Alabama Alaska Arizona Arkansas California Colorado Connecticut Delaware
    Florida Georgia Hawaii Idaho Illinois Indiana Iowa Kansas Kentucky Louisiana
    Maine Maryland Massachusetts Michigan Minnesota Mississippi Missouri Montana
    Nebraska Nevada Ohio Oklahoma Oregon Pennsylvania Tennessee Texas Utah
    Vermont Virginia Washington Wisconsin Wyoming
    """.split()
)
# Brands and makers an English document names before another capitalised word
# ("Mercedes Benz", "Harley Davidson"), each also a Social Security or ONS
# given name.
BRANDS = frozenset(
    """
    Alexa Armani Barbie Cartier Celine Chanel Chevy Dior Harley Kia Lexus
    Mercedes Porsche Royce Tesla Tiffany Valentino
    """.split()
)

_NAME_RE = re.compile(r"[^\W\d_](?:[^\W\d_]|['’\-])*[^\W\d_]")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tr_title(word: str) -> str:
    """'İBRAHİM' -> 'İbrahim', 'IŞIL' -> 'Işıl': Turkish casing, first letter kept."""
    rest = word[1:].replace("I", "ı").replace("İ", "i").lower()
    return word[0] + rest


# --- sources --------------------------------------------------------------


def ssa_names(path: Path) -> set[str]:
    names: set[str] = set()
    with zipfile.ZipFile(path) as archive:
        years = sorted(
            int(match.group(1))
            for match in (re.fullmatch(r"yob(\d{4})\.txt", item) for item in archive.namelist())
            if match
        )
        for year in years:
            if year < SSA_FIRST_YEAR:
                continue
            rows = [line.split(",") for line in archive.read(f"yob{year}.txt").decode("utf-8").splitlines() if line]
            for sex in ("F", "M"):
                ranked = sorted(((int(count), name) for name, row_sex, count in rows if row_sex == sex), reverse=True)
                names.update(name for _count, name in ranked[:SSA_RANK])
    return names


def ons_names(path: Path) -> set[str]:
    import openpyxl  # local: only this source needs it

    names: set[str] = set()
    workbook = openpyxl.load_workbook(path, read_only=True)
    for title in ("Table_1", "Table_2"):
        rows = list(workbook[title].iter_rows(values_only=True))
        header_index = next(index for index, row in enumerate(rows) if row and row[0] == "Name")
        header = rows[header_index]
        rank_columns = [index for index, cell in enumerate(header) if isinstance(cell, str) and cell.endswith("Rank")]
        for row in rows[header_index + 1:]:
            if row and row[0] and any(isinstance(row[i], int) and row[i] <= ONS_RANK for i in rank_columns):
                names.add(str(row[0]).strip())
    return names


def tuik_names(paths: list[Path]) -> set[str]:
    names: set[str] = set()
    for path in paths:
        dataset = json.loads(path.read_text(encoding="utf-8"))
        dimension = next(key for key in dataset["dimension"] if key.endswith("_ISIM"))
        for label in dataset["dimension"][dimension]["category"]["label"].values():
            names.update(tr_title(word) for word in label.split())
    return names


def scowl_files(directory: Path) -> list[Path]:
    return [
        path
        for path in sorted(directory.iterdir())
        if (match := re.fullmatch(r"(english|american|british)-words\.(\d+)", path.name))
        and int(match.group(2)) <= SCOWL_MAX_SIZE
    ]


def scowl_words(directory: Path) -> set[str]:
    words: set[str] = set()
    for path in scowl_files(directory):
        for word in path.read_text(encoding="latin-1").split():
            if word.islower():
                words.add(word)
    return words


def zemberek_words(path: Path) -> set[str]:
    words: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        lemma = line.split(" [", 1)[0].strip()
        if lemma and " " not in lemma and lemma.islower():
            words.add(lemma)
    return words


def turkish_verb_forms(lemmas: set[str]) -> set[str]:
    """The stem, -an/-en participle and -miş participle of every infinitive lemma."""
    forms: set[str] = set()
    for lemma in lemmas:
        if lemma.endswith(("mak", "mek")) and len(lemma) > 4:
            stem = lemma[:-3]
            forms.update({stem, stem + "an", stem + "en"})
            forms.update(stem + suffix for suffix in ("mış", "miş", "muş", "müş"))
    return forms


def geonames_places(cities: Path, countries: Path) -> set[str]:
    places: set[str] = set()
    with zipfile.ZipFile(cities) as archive:
        member = next(name for name in archive.namelist() if name.endswith(".txt"))
        for line in io.TextIOWrapper(archive.open(member), encoding="utf-8"):
            fields = line.rstrip("\n").split("\t")
            if len(fields) <= 14:
                continue
            minimum = GEONAMES_MIN_POPULATION.get(fields[8], GEONAMES_DEFAULT_MIN_POPULATION)
            if int(fields[14] or 0) >= minimum:
                places.update((fields[1], fields[2]))
    for line in countries.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        fields = line.split("\t")
        places.update((fields[4], fields[5]))  # country name, capital
    places |= US_STATES
    # A place is a homograph of a name only as a whole word: "San Diego" says
    # nothing about Diego.
    return {place for place in places if place[:1].isupper() and " " not in place}


# --- filters --------------------------------------------------------------


_CIRCUMFLEX = str.maketrans("âîûÂÎÛ", "aiuAIU")


def _folds(word: str) -> set[str]:
    """Every way the detectors fold a word, with Turkish circumflexes dropped
    ("rüzgâr" is how Zemberek spells the "Rüzgar" TÜİK lists)."""
    word = word.translate(_CIRCUMFLEX)
    return {english_names._fold(word), turkish_names.tr_fold(word), word.casefold()}


def legal_terms() -> set[str]:
    """Structural and legal words of both detectors, folded every way they are read."""
    terms = set(english_names.NON_PERSON_TOKENS) | set(english_names.MONTH_TOKENS)
    terms |= set(turkish_names.NON_PERSON_TOKENS) | set(turkish_names.MONTH_TOKENS)
    for regex in (turkish_names.PERSON_LABEL_RE, english_names.LABEL_RE):
        terms |= {english_names._fold(word) for word in re.findall(r"[^\W\d_]{3,}", regex.pattern)}
    return terms


def is_legal_term(word: str, terms: set[str]) -> bool:
    if _folds(word) & terms:
        return True
    return any(regex.fullmatch(word) for regex in legal_semantics._TERM_RES.values())


def select(candidates: set[str], drops: list[tuple[str, callable]]) -> tuple[list[str], list[tuple[str, int]]]:
    """Apply each named filter in turn; return the kept names and the count each one removed."""
    kept = {name for name in candidates if _NAME_RE.fullmatch(name) and len(name) >= 3}
    counts = [("shorter than three letters or not a plain word", len(candidates) - len(kept))]
    for label, drop in drops:
        removed = {name for name in kept if drop(name)}
        counts.append((label, len(removed)))
        kept -= removed
    return sorted(kept, key=lambda name: (english_names._fold(name), name)), counts


def render(module_doc: str, names: list[str]) -> str:
    lines, line = [], ""
    for name in names:
        if len(line) + len(name) + 1 > 84:
            lines.append(line)
            line = ""
        line = f"{line} {name}" if line else name
    if line:
        lines.append(line)
    body = "\n".join(f"    {item}" for item in lines)
    return (
        f'"""{module_doc}"""\n\n'
        "# GENERATED by build_given_names.py -- do not edit by hand; rerun it from the sources.\n"
        "NAMES: frozenset[str] = frozenset(\n"
        '    """\n'
        f"{body}\n"
        '    """.split()\n'
        ")\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ssa", type=Path, required=True)
    parser.add_argument("--ons", type=Path, required=True)
    parser.add_argument("--tuik", type=Path, action="append", required=True)
    parser.add_argument("--scowl", type=Path, required=True)
    parser.add_argument("--zemberek", type=Path, required=True)
    parser.add_argument("--geonames-cities", type=Path, required=True)
    parser.add_argument("--geonames-countries", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    english_words = scowl_words(args.scowl)
    turkish_words = zemberek_words(args.zemberek)
    places = geonames_places(args.geonames_cities, args.geonames_countries)
    place_folds = {fold for place in places for fold in _folds(place)}
    terms = legal_terms()
    excluded = {english_names._fold(name) for name in english_names.EXCLUDED_HOMOGRAPHS}
    brands = {english_names._fold(name) for name in BRANDS}

    def is_place(name: str) -> bool:
        """Any place either source names, the Turkish districts included.

        Many districts are common Turkish given names too (Çınar, Defne,
        Nilüfer), and in front of a venue that is exactly the shape the
        detectors cannot untangle: with Çınar on the list, "Çınar Ankara 6.
        Asliye Hukuk Mahkemesine" read as the person "Çınar Ankara" and took the
        city out of the court (tests/test_court_full_span.py,
        SurnameBeforeAVenueTests). The hand-written list keeps the few it
        already carries (Selim, Fatih, Selçuk).
        """
        return turkish_places.is_venue_place_token(name) or bool(_folds(name) & place_folds)

    def is_word(words: set[str]):
        folded = {fold for word in words for fold in _folds(word)}
        return lambda name: bool(_folds(name) & folded)

    ssa, ons = ssa_names(args.ssa), ons_names(args.ons)
    english, english_counts = select(
        ssa | ons,
        [
            ("an ordinary English word (SCOWL <= 50)", is_word(english_words)),
            ("an ordinary Turkish word (Zemberek lemmas)", is_word(turkish_words)),
            ("a place", is_place),
            ("a legal or structural term", lambda name: is_legal_term(name, terms)),
            ("a brand", lambda name: english_names._fold(name) in brands),
            ("a homograph english_names leaves out on purpose", lambda name: english_names._fold(name) in excluded),
        ],
    )
    tuik = tuik_names(args.tuik)
    is_verb_form = is_word(turkish_verb_forms(turkish_words))
    turkish_word_filters = [
        ("an ordinary English word (SCOWL <= 50)", is_word(english_words)),
        ("an ordinary Turkish word (Zemberek lemmas)", is_word(turkish_words)),
    ]
    turkish, turkish_counts = select(
        tuik,
        [
            *turkish_word_filters,
            ("a Turkish verb stem or participle (Zemberek -mak/-mek lemmas)", is_verb_form),
            ("a place", is_place),
            ("a legal or structural term", lambda name: is_legal_term(name, terms)),
        ],
    )
    verb_forms_dropped = [name for name in select(tuik, turkish_word_filters)[0] if is_verb_form(name)]

    def provenance(counts: list[tuple[str, int]], sources: list[Path]) -> str:
        rows = "\n".join(f"  - {label}: {count}" for label, count in counts)
        files = "\n".join(f"  - {path.name}  sha256 {_sha256(path)}" for path in sources)
        return f"Removed by each filter, in order:\n\n{rows}\n\nInput files:\n\n{files}"

    filter_inputs = [*scowl_files(args.scowl), args.zemberek, args.geonames_cities, args.geonames_countries]
    english_doc = ENGLISH_DOC.format(
        ssa=len(ssa), ons=len(ons), candidates=len(ssa | ons), kept=len(english),
        provenance=provenance(english_counts, [args.ssa, args.ons, *filter_inputs]),
    )
    turkish_doc = TURKISH_DOC.format(
        candidates=len(tuik), kept=len(turkish), verb_forms=", ".join(verb_forms_dropped),
        provenance=provenance(turkish_counts, [*args.tuik, *filter_inputs]),
    )
    outputs = {ENGLISH_OUT: render(english_doc, english), TURKISH_OUT: render(turkish_doc, turkish)}
    stale = [path for path, content in outputs.items() if not path.exists() or path.read_text(encoding="utf-8") != content]
    if args.check:
        for path in stale:
            print(f"stale: {path.relative_to(ROOT)}", file=sys.stderr)
        return 1 if stale else 0
    for path, content in outputs.items():
        path.write_text(content, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    print(f"english: {len(ssa | ons)} candidates -> {len(english)} kept; {english_counts}")
    print(f"turkish: {len(tuik)} candidates -> {len(turkish)} kept; {turkish_counts}")
    return 0


ENGLISH_DOC = """English given names from official birth-registration statistics.

GENERATED by build_given_names.py; read that script's docstring for the rule.
`english_names.GIVEN_NAMES` is the hand-written list there plus this one, minus
every Turkish given name (found by the Turkish path instead) and minus
`english_names.EXCLUDED_HOMOGRAPHS`.

Sources, retrieved 2026-10-31:

* US Social Security Administration, "Baby Names from Social Security Card
  Applications - National Data", https://www.ssa.gov/oact/babynames/names.zip
  -- every name in the top 1,000 for its sex in any year 1930-2025
  ({ssa} names). A US government work, in the public domain; the data.gov
  catalogue entry licenses it CC0 1.0.
* Office for National Statistics, "Baby names in England and Wales: 1996 to
  2025", https://www.ons.gov.uk/peoplepopulationandcommunity/birthsdeathsandmarriages/livebirths/datasets/babynamesinenglandandwales
  -- every name in the top 100 for its sex in any year ({ons} names).
  Contains public sector information licensed under the Open Government
  Licence v3.0.

Homograph filter inputs: SCOWL 2020.12.07 (Copyright 2000-2018 Kevin Atkinson,
permissive licence reproduced in its Copyright file); Zemberek-NLP master
dictionary (Apache-2.0); GeoNames cities15000 and countryInfo (CC BY 4.0,
https://www.geonames.org/). None of them is shipped; only the names that
survive the filter are.

{candidates} candidate names, {kept} kept.

{provenance}
"""

TURKISH_DOC = """Turkish given names from official population-register statistics.

GENERATED by build_given_names.py; read that script's docstring for the rule.
`turkish_names.GIVEN_NAMES` is the hand-written list there plus this one.

Source, retrieved 2026-10-31: Türkiye İstatistik Kurumu (TÜİK), Adrese Dayalı
Nüfus Kayıt Sistemi, "Doğum Tarihine Göre En Çok Kullanımda Olan Erkek
İsimleri ve İlk 100 İsim İçindeki Sıralaması" (DF_ADNKS_T32) and the female
table (DF_ADNKS_T33), birth years 1950, 1960, 1970 and 1980-2025, from
https://databrowser2.tuik.gov.tr/#/tr/tuik/TR,DF_ADNKS_T32,1.1 (and T33) --
every name in either table; a compound name contributes each word. TÜİK's
"Yasal Uyarı" (https://www.tuik.gov.tr/Kurumsal/Yasal_Uyari) permits reuse
with the source cited: "verilerin, kaynak gösterilmek suretiyle herhangi bir
izine gerek duymaksızın yeniden kullanımı mümkündür" (Kaynak: TÜİK).

NVİ's most-used-name PDFs were not used: the NVİ site's terms forbid copying
and republishing its material, and they add one name beyond TÜİK.

Homograph filter inputs: SCOWL 2020.12.07 (Copyright 2000-2018 Kevin Atkinson,
permissive licence reproduced in its Copyright file); Zemberek-NLP master
dictionary (Apache-2.0); GeoNames cities15000 and countryInfo (CC BY 4.0,
https://www.geonames.org/). None of them is shipped.

{candidates} candidate names, {kept} kept. The verb-form filter took out these
TÜİK names, each also a Turkish verb stem or participle: {verb_forms}.

{provenance}
"""


if __name__ == "__main__":
    raise SystemExit(main())
