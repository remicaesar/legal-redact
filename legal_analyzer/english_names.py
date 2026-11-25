"""Standalone English person-name detection.

The English twin of `turkish_names.py`, and built the same way: no model, a
given-name gazetteer as the precision anchor, and structural cues that find a
name by its POSITION when its given name is not in the list. Recall bias is the
same, for the same reason: a missed name leaks a real person, an over-flagged
one costs a reviewer one click.

Each path is a shape English legal documents put names in:

* a title -- "Mr Smith", "Mrs Jane Doe", "Dr. Okafor", "His Honour Judge
  Patel". One surname is enough here, because "Mr Smith" is how an English
  document refers to a party for most of its length; the Turkish title path in
  privacy.PERSON_CONTEXT_RE needs two words and so never saw it;
* a given name from GIVEN_NAMES followed by a surname -- parties in running
  text, attendee lists, the "between" clause;
* a labelled line -- "Name: …", "Claimant: …", "Second witness: …", "Signed
  by: …";
* a witness-statement opener -- "I, Jane Doe, of …", "WITNESS STATEMENT OF
  JANE DOE", "My name is …";
* a party line in a claim heading -- "JANE DOE<spaces>Claimant", "(1) JANE
  DOE of …" -- and the "between X and Y" clause;
* the signature line itself -- "Signed: Jane Doe", "Signed by Jane Doe and
  John Roe", "witnessed by Jane Doe";
* the e-mail address written right after the name on its line, when the
  address's local part spells it -- "Jane Doe <jane.doe@…>", "J. Doe,
  jdoe@…" -- unless the document also writes every one of those words in
  lowercase, which is what a team mailbox ("Records Management
  <records.management@…>") does;
* a list or signature window -- the lines after "Present:", "Witnesses:",
  "Yours sincerely".

What it does not see, stated so nobody reads silence as absence: a name with an
unlisted given name and no title, label or structure around it ("the report by
Quillfeather was late"), and a name alone on a line outside a list or
signature window. Where the same document names that person in full somewhere
this module (or the Turkish one) does see, privacy._propagated_surname_mentions
finds the bare surname and the prose repeat from that full name.
"""

from __future__ import annotations

import re
import unicodedata

from legal_analyzer import given_names_en, turkish_names


def _fold(value: str) -> str:
    """Case-fold for lookups, with the Turkish i-family collapsed to "i".

    Not turkish_names.tr_fold: that maps an ASCII capital I to the dotless
    "ı", which is right for "IŞIL" and wrong for every English word in
    capitals -- "KEITH" would fold to "keıth" and miss the gazetteer, and
    "LIMITED" to "lımıted" and miss the guard, so a company heading was read
    as a person. Collapsing the family the other way serves both languages.
    """
    return value.casefold().replace("ı", "i").replace("\u0307", "")


# --- Given-name gazetteer -------------------------------------------------
# Common given names in English-language legal documents: British, Irish,
# American and the names of the UK's larger communities. A candidate on the
# gazetteer path is accepted only when its FIRST word is here.
#
# Left OUT on purpose, because each is an ordinary capitalised word as often as
# a name, and a capitalised word followed by another capitalised word is exactly
# what a heading or a defined term looks like: Will (a will is a probate
# document), Grant ("Grant of Probate"), Bill ("Bill of Costs"), Mark, Art,
# Pat, Rich, Earl, Major, King, Prince, Duke, Baron, Max, Ray, Dale, Glen, Gene,
# Lance, Miles, Price, Sterling, Chase, Morgan, Taylor, Jordan, Austin,
# Christian, Regina ("Regina v Smith" is the Crown), the virtues (Grace, Hope,
# Faith, Joy), the months (April, May, June, August), the plants and stones
# (Rose, Holly, Ivy, Amber, Ruby, Pearl, Crystal), and places (Victoria,
# Florence, Georgia, Virginia, Sydney, Paris).
#
# Also left out: every word that is an ordinary TURKISH word, because this path
# runs on Turkish documents too -- Ben ("I"), Ana ("main": "Ana Sözleşme" is a
# company's articles), Ada ("island", and a company name in the fixtures),
# Adam ("man"), Kim ("who"), Can, Don. And everything already in
# turkish_names.GIVEN_NAMES is subtracted below, so a name both gazetteers
# know is found by the Turkish path alone and the two never race to different
# spans over one person.
#
# Both exclusions are EXCLUDED_HOMOGRAPHS below, which GIVEN_NAMES subtracts,
# so the list built from official statistics (given_names_en.NAMES, see
# build_given_names.py) cannot bring one back.
EXCLUDED_HOMOGRAPHS: frozenset[str] = frozenset(
    """
    Will Grant Bill Mark Art Pat Rich Earl Major King Prince Duke Baron Max Ray
    Dale Glen Gene Lance Miles Price Sterling Chase Morgan Taylor Jordan Austin
    Christian Regina Grace Hope Faith Joy April May June August Rose Holly Ivy
    Amber Ruby Pearl Crystal Victoria Florence Georgia Virginia Sydney Paris
    Ben Ana Ada Adam Kim Can Don
    """.split()
)

# The hand-written list, then the official one (given_names_en.NAMES: US
# Social Security Administration and ONS birth registrations, filtered for
# homographs by build_given_names.py), less the exclusions above.
_HAND_WRITTEN_GIVEN_NAMES = frozenset(
    _fold(name)
    for name in """
    James John Robert Michael William David Richard Joseph Thomas Charles
    Christopher Daniel Matthew Anthony Donald Steven Stephen Paul Andrew Joshua
    Kenneth Kevin Brian George Timothy Edward Ronald Jason Jeffrey Ryan Jacob
    Gary Nicholas Eric Jonathan Larry Justin Scott Brandon Benjamin Samuel
    Gregory Alexander Patrick Jack Dennis Jerry Tyler Aaron Henry Douglas Peter
    Nathan Zachary Kyle Walter Harold Jeremy Ethan Carl Keith Roger Gerald
    Terry Sean Arthur Noah Lawrence Jesse Joe Bryan Albert Dylan Bruce Gabriel
    Alan Logan Wayne Ralph Roy Eugene Randy Vincent Russell Louis Philip
    Phillip Bobby Johnny Bradley Oliver Harry Charlie Leo Oscar Archie Alfie
    Freddie Theo Theodore Isaac Lucas Toby Rory Callum Connor Liam Owen Rhys
    Gareth Dafydd Huw Ewan Euan Hamish Angus Alistair Alasdair Fergus Niall
    Ciaran Declan Seamus Padraig Eoin Cormac Darragh Desmond Malcolm Nigel
    Graham Graeme Colin Neil Ian Iain Barry Derek Trevor Clive Geoffrey Rupert
    Hugo Giles Julian Simon Martin Dominic Marcus Adrian Howard Roland Rodney
    Stuart Stewart Duncan Gordon Leslie Lewis Frank Guy Dean Mohammed Muhammad
    Mohammad Ahmed Omar Imran Tariq Bilal Raj Rajesh Sanjay Vikram Arjun Rohan
    Amit Ravi Kwame Kofi Chidi Emeka Tunde Femi Wei Hiroshi Kenji Pierre Jean
    Luca Marco Francesco Giovanni Alessandro Carlos Miguel Diego Jose Javier
    Pablo Sergei Ivan Dmitri Andrei Pieter Lars Erik Sven Hans Klaus Stefan
    Matthias Tobias Felix Jakob Edmund Edwin Alfred Ernest Herbert Hubert
    Reginald Cecil Bernard Francis Frederick Gilbert Harvey Jasper Jonah Jude
    Nathaniel Rufus Sebastian Tristan Xavier Elliot Elliott Jake Josh Luke
    Matt Mike Nick Sam Tom Tony Andy Dave Steve Pete Rob Bob Jim Jimmy Danny
    Mary Patricia Jennifer Linda Elizabeth Barbara Susan Jessica Sarah Karen
    Lisa Nancy Betty Margaret Sandra Ashley Kimberly Emily Donna Michelle Carol
    Amanda Dorothy Melissa Deborah Stephanie Rebecca Sharon Laura Cynthia
    Kathleen Amy Angela Shirley Anna Brenda Pamela Emma Nicole Helen Samantha
    Katherine Catherine Christine Debra Rachel Carolyn Janet Maria Heather Diane
    Julie Joyce Kelly Christina Lauren Joan Evelyn Olivia Judith Megan Cheryl
    Martha Andrea Frances Hannah Jacqueline Ann Anne Gloria Jean Kathryn Alice
    Teresa Sara Janice Doris Madison Julia Judy Abigail Marie Denise Beverly
    Theresa Marilyn Danielle Diana Natalie Sophia Sophie Isabella Alexis
    Charlotte Amelia Isla Ava Mia Ella Freya Evie Lily Daisy Harriet Imogen
    Georgina Eleanor Lucy Phoebe Chloe Zoe Jasmine Ellie Millie Erin Siobhan
    Aoife Niamh Ciara Roisin Sinead Orla Caitlin Fiona Morag Mhairi Eilidh
    Kirsty Gwen Gwyneth Bronwen Cerys Rhiannon Sian Ffion Bethan Nia Yasmin
    Aisha Fatima Amina Priya Anjali Sunita Meera Mei Yuki Sofia Elena
    Francesca Giulia Lucia Carmen Ines Ingrid Astrid Greta Johanna Annika
    Gillian Jill Wendy Valerie Beverley Maureen Pauline Sheila Rosemary Hilary
    Penelope Philippa Rosalind Miranda Juliet Bridget Agnes Edith Ethel Mabel
    Vera Hazel Jane Joanne Kate Katie Kathy Claire Clare Louise Lorraine Tracey
    Tracy Nicola Natasha Tanya Vanessa Yvonne Zara Leah Abbie Molly Elsie Esme
    Matilda Edna Eileen Irene Jennie Jenny Josephine Lydia Mildred Nora Norah
    Olive Patience Ruth Sylvia Una Winifred Alexandra Caroline Cassandra
    Deirdre Emilia Gemma Hayley Jodie Kerry Lindsay Lindsey Lorna Maxine Paula
    Rosie Sally Stella Susanna Suzanne Tamsin Thea Ursula Alex Chris Jamie
    Casey Riley Robin Jo Rowan Aiden Caleb Elijah Ezra Finley Harrison Hudson
    Leon Louie Reuben Ronan Arlo Albie Ollie Frankie Teddy Tommy Ronnie Reggie
    Kian Aria Aurora Bella Clara Lola Luna Maya Mila Rosa Sienna Willow Elsa
    Esther Eve Faye Iris Jessie Lara Layla Maisie Mira Naomi Nina Paige Pippa
    Rebekah Robyn Tara Tess Verity Wren
    """.split()
)
GIVEN_NAMES: frozenset[str] = (
    _HAND_WRITTEN_GIVEN_NAMES
    | {_fold(name) for name in given_names_en.NAMES}
) - {_fold(name) for name in turkish_names.GIVEN_NAMES} - {_fold(name) for name in EXCLUDED_HOMOGRAPHS}

# --- Non-person guards ----------------------------------------------------
# A capitalised word in one of these marks the run as an institution, a place,
# an address, a document, a role or a heading rather than a person. As in the
# Turkish module, a guard SPLITS a run rather than ending the search, so a name
# after a heading word is still found.
NON_PERSON_TOKENS: frozenset[str] = frozenset(
    _fold(token)
    for token in """
    Court Courts Tribunal Tribunals Division Chamber Chambers Bench Registry
    Judge Justice Justices Magistrates Magistrate Master Registrar Coroner
    Crown County District Circuit Supreme High Appeal Appeals Appellate
    Street St Road Rd Lane Ln Avenue Ave Drive Close Place Square Sq Gardens
    Terrace Crescent Way Row Mews Walk Grove Parade Boulevard Wharf Quay Yard
    Hill Park Green Gate Circus Embankment Broadway Highway House Building
    Buildings Tower Centre Center Floor Suite Unit Flat Apartment Estate Lodge
    Limited Ltd LLP PLC Plc Inc LLC Corp Corporation Company Co Group Holdings
    Partners Partnership Associates Solicitors Barristers Bank Trust Fund
    Council Department Ministry Office Agency Authority Commission Committee
    Board Service Services Police Hospital Clinic Surgery School College
    University Institute Society Foundation Association Union Federation
    Kingdom United States England Wales Scotland Ireland Britain British
    Agreement Contract Deed Schedule Clause Section Article Act Acts Regulation
    Regulations Rules Rule Order Directive Statute Code Part Paragraph Annex
    Appendix Exhibit Page Note Notes Form Report Letter Notice Statement Claim
    Claims Case Matter Particulars Defence Defense Reply Judgment Judgement
    Hearing Trial Application Evidence Disclosure Costs Damages Interest
    Payment Invoice Pounds Sterling Dollars Euros Policy Terms Conditions
    Claimant Claimants Defendant Defendants Respondent Respondents Applicant
    Applicants Appellant Appellants Petitioner Plaintiff Party Parties
    Witness Witnesses Employee Employer Tenant Landlord Buyer Seller Purchaser
    Vendor Guarantor Borrower Lender Client Customer Supplier Contractor
    Director Directors Secretary Chairman Chair Chairperson President Officer
    Manager Counsel Solicitor Barrister Attorney Advocate Esq KC QC CBE OBE
    MBE JP MP PhD LLB LLM MA BA BSc MSc Partner Senior Junior Associate Trainee
    Head Chief General Executive Legal Managing Deputy Assistant Chancellor
    Mayor Commissioner Lieutenant Inspector Sergeant Constable
    Word Excel Teams Outlook PowerPoint Windows SharePoint
    Dear Sir Madam Yours Sincerely Faithfully Regards Re Subject Date Dated
    Name Address Tel Telephone Phone Mobile Email Fax Ref Reference No Number
    Signed Signature Signatory Print Printed Witnessed Present Attendance
    Privileged Confidential Private Strictly Without Prejudice Draft Final
    Monday Tuesday Wednesday Thursday Friday Saturday Sunday
    The This That These Those Such Said Each Every All Any Some Our Your Their
    His Her Its We He She They It And Or Of For To In On At By With From As If
    Upon Under Between Where When Whereas Now Therefore Hereby Further
    I II III IV V VI VII VIII IX X
    Mr Mrs Ms Miss Mx Dr Prof Professor Dame Lord Lady Rev Revd HHJ
    Av Öğr Üyesi Uzm Op Doç Arş Gör Yrd
    """.split()
)

_TURKISH_NON_PERSON_TOKENS = frozenset(_fold(token) for token in turkish_names.NON_PERSON_TOKENS)

# Month names guard a run only where they read as a date ("12 March 2025",
# "March 2025"); none of them is in GIVEN_NAMES, so a month never STARTS a
# gazetteer run, and as a later word it is checked in context.
MONTH_TOKENS: frozenset[str] = frozenset(
    _fold(month)
    for month in """
    January February March April May June July August September October
    November December Jan Feb Mar Apr Jun Jul Aug Sep Sept Oct Nov Dec
    """.split()
)
_MONTH_YEAR_AFTER_RE = re.compile(r"[ \t]+\d{1,2}(?:st|nd|rd|th)?,?[ \t]+\d{4}\b|[ \t]+\d{4}\b")
_MONTH_DAY_BEFORE_RE = re.compile(r"\d{1,2}(?:st|nd|rd|th)?(?:[ \t]+day[ \t]+of)?[ \t]+$", re.IGNORECASE)


def _is_month_as_date(text: str, start: int, end: int) -> bool:
    if _MONTH_YEAR_AFTER_RE.match(text, end):
        return True
    return bool(_MONTH_DAY_BEFORE_RE.search(text[max(0, start - 16):start]))


# A name word: a capital, then letters, apostrophes and hyphens -- "O'Brien",
# "Smith-Jones", "McDonald", "D'Souza", all-caps "SMITH". Latin-1 accented
# letters and the Turkish letters are in the class, because an English
# document names Turkish, French and Polish parties too ("Mr Yılmaz").
_UPPER = "A-ZÀ-ÖØ-ÞĞİŞ"
_LETTER = "A-Za-zÀ-ÖØ-öø-ÿĞğİıŞş"
_TOKEN = rf"[{_UPPER}][{_LETTER}'’\-]*[{_LETTER}]"
# An initial inside a name: "Mr J Smith", "John A. Smith", "Name: J. Smith".
# It counts as a name word, but a run may not END on one -- "Smith J" in a
# judgment is a judge's style, not a person with an initial.
_INITIAL = rf"[{_UPPER}]\.?(?=[ \t])"
_TOKEN_RE = re.compile(rf"{_TOKEN}|{_INITIAL}")
_INITIAL_WORD_RE = re.compile(rf"[{_UPPER}]\.?")

# Lowercase name particles: "Ludwig van Beethoven", "Maria de la Cruz". They
# join a run but never end it, and a run must still carry a capitalised word.
_PARTICLE_RE = re.compile(r"(?:van|von|der|den|de|del|della|du|da|di|la|le|ter|ten|bin|ibn|al|el)(?![\w'’\-])")

# Inside a name: spaces and tabs, never a line break -- a finding sample that
# contains a newline cannot be removed from a DOCX (see privacy._NAME_SEP).
_SEP_RE = re.compile(r"[ \t]+")

# A company legal form right after the run makes it a company, not a person:
# "John Lewis Limited", "Grant Thornton LLP". The Turkish suffix list is the
# shared one; LLP is English-only.
_COMPANY_AFTER_RE = re.compile(
    turkish_names.COMPANY_SUFFIX_RE.pattern + r"|\s*(?:LLP|L\.L\.P\.|Corp\.?|Corporation)\b",
    re.IGNORECASE,
)

_MAX_NAME_WORDS = 4
_MAX_PARTICLES = 2
_POSSESSIVE_RE = re.compile(r"['’]s$")


def _walk(text: str, pos: int) -> list[tuple[str, int, int, str, bool]]:
    """Name-shaped words from `pos`: (word, start, end, gap before, is particle)."""
    words: list[tuple[str, int, int, str, bool]] = []
    cursor = pos
    gap = ""
    while len(words) < _MAX_NAME_WORDS + _MAX_PARTICLES:
        token = _TOKEN_RE.match(text, cursor)
        particle = None if token else _PARTICLE_RE.match(text, cursor)
        found = token or particle
        if not found:
            break
        words.append((found.group(0), found.start(), found.end(), gap, particle is not None))
        separator = _SEP_RE.match(text, found.end())
        if not separator:
            break
        gap = separator.group(0)
        cursor = separator.end()
    return words


def _name_run(
    text: str, words: list[tuple[str, int, int, str, bool]], min_words: int
) -> tuple[str, int, int] | None:
    """The first run of name words, split at guards and column gaps.

    The same reading as turkish_names._best_name_run: a guard word splits the
    run, a tab or a double space after a complete name is a column boundary,
    and a month is a guard only where it reads as a date. Particles may sit
    inside a run, never at its end.
    """
    run: list[tuple[str, int, int, bool]] = []
    for value, start, end, gap, is_particle in words:
        wide_gap = "\t" in gap or len(gap) > 1
        capitalised = sum(1 for word in run if not word[3])
        if wide_gap and capitalised >= min_words:
            break
        folded = _fold(value)
        guard = not is_particle and (
            folded in NON_PERSON_TOKENS
            or folded in _TURKISH_NON_PERSON_TOKENS
            or (folded in MONTH_TOKENS and _is_month_as_date(text, start, end))
        )
        if guard:
            if capitalised >= min_words:
                break
            run = []
            continue
        if is_particle and not run and min_words > 1:
            continue
        run.append((value, start, end, is_particle))
        if sum(1 for word in run if not word[3]) == _MAX_NAME_WORDS:
            break
    while run and (run[-1][3] or _INITIAL_WORD_RE.fullmatch(run[-1][0])):
        run.pop()
    if sum(1 for word in run if not word[3]) < min_words:
        return None
    # A possessive is grammar, not the name: "Mr Smith's letter" is Mr Smith,
    # and keeping the "'s" would key him to a second pseudonym.
    last_value, last_start, last_end, _ = run[-1]
    possessive = _POSSESSIVE_RE.search(last_value)
    if possessive:
        run[-1] = (last_value[:possessive.start()], last_start, last_end - len(possessive.group()), False)
    return " ".join(word[0] for word in run), run[0][1], run[-1][2]


def _accept(run: tuple[str, int, int] | None, text: str, hits: dict[tuple[int, int], str]) -> None:
    if run is None:
        return
    value, start, end = run
    if _COMPANY_AFTER_RE.match(text, end):
        return
    if turkish_names.made_of_terms([_fold(word) for word in value.split()], PARTY_ROLE_TERMS):
        return
    hits[(start, end)] = value


# Defined party roles, guarded as a WHOLE candidate the way
# turkish_names._PARTY_ROLE_TERMS is: an execution page's "Data Exporter" /
# "Data Importer" lines and an attendance list's "Data Controller" sit exactly
# where a list or signature window reads a name. Only a candidate made entirely
# of these is rejected, so a surname that is also a role word stays a name.
PARTY_ROLE_TERMS: frozenset[tuple[str, ...]] = frozenset(
    tuple(_fold(word) for word in term.split())
    for term in (
        "Buyer", "Seller", "Purchaser", "Vendor", "Licensor", "Licensee", "Lessor", "Lessee",
        "Landlord", "Tenant", "Employer", "Employee", "Contractor", "Subcontractor", "Client",
        "Customer", "Supplier", "Recipient", "Discloser", "Guarantor", "Borrower", "Lender",
        "Assignor", "Assignee", "Controller", "Processor", "Sub-processor", "Subprocessor",
        "Exporter", "Importer", "Company", "Party", "Parties",
        "Data Controller", "Data Processor", "Data Exporter", "Data Importer", "Data Subject",
        "Joint Controller", "Sub Processor", "Service Provider", "Disclosing Party",
        "Receiving Party", "First Party", "Second Party",
    )
)


# --- Structural cues ------------------------------------------------------

# A title, optionally chained ("Mr Justice", "His Honour Judge", "Prof. Dr."),
# then the name. One surname is enough: "Mr Smith".
_TITLE = (
    r"(?:Mr|Mrs|Ms|Miss|Mx|Dr|Prof|Professor|Sir|Dame|Lord|Lady|Rev|Revd|Reverend|HHJ"
    r"|His[ \t]+Honour[ \t]+Judge|Her[ \t]+Honour[ \t]+Judge|District[ \t]+Judge"
    r"|Employment[ \t]+Judge|Deputy[ \t]+District[ \t]+Judge|Judge)"
)
TITLE_RE = re.compile(
    r"(?<![\w'’])(?i:" + _TITLE + r")\.?(?:[ \t]+(?i:Justice|" + _TITLE + r")\.?)*[ \t]+(?=[" + _UPPER + r"]|(?:van|von|de|du|da|di|le|la)[ \t])"
)

# A labelled line: "Name: …", "Claimant: …", "Second witness: …". The label
# must start the line (after an optional list marker), and a colon or a dash
# must follow it.
LABEL_RE = re.compile(
    r"(?:^|\n)[ \t]*(?:[-*•][ \t]*|\(?\d{1,2}[).][ \t]*)?"
    r"(?i:(?:full[ \t]+(?:legal[ \t]+)?|print(?:ed)?[ \t]+|witness[ \t]+|employee[ \t]+|client[ \t]+|contact[ \t]+)?name"
    r"|(?:(?:first|second|third|1st|2nd|3rd)[ \t]+)?witness|claimant|defendant|respondent|applicant|appellant"
    r"|petitioner|plaintiff|employee|tenant|landlord|guarantor|signatory|signed(?:[ \t]+by)?|signature"
    r"|witnessed[ \t]+by"
    r"|client|provider|supplier|customer|contractor|consultant|licensor|licensee|lessor|lessee|buyer|seller"
    r"|purchaser|vendor|borrower|lender|agent|principal"
    r"|prepared[ \t]+by|approved[ \t]+by|reviewed[ \t]+by|attention|attn|fao|for[ \t]+the[ \t]+attention[ \t]+of"
    r"|contact(?:[ \t]+person)?|next[ \t]+of[ \t]+kin|emergency[ \t]+contact|patient|data[ \t]+subject"
    r"|solicitor|counsel|before|judge|chair(?:man|person)?|secretary|director|deceased|beneficiary|executor"
    r"|from|to|cc)"
    r"[ \t]*[:\-–][ \t]*"
)

# "I, Jane Doe, of …" -- the opening of every English witness statement --
# and "My name is Jane Doe".
OPENER_RE = re.compile(r"(?:(?<![\w'’])I,[ \t]+|\b(?i:my[ \t]+(?:full[ \t]+)?name[ \t]+is)[ \t]+)")

# "WITNESS STATEMENT OF JANE DOE", "Affidavit of …", "evidence of …".
STATEMENT_OF_RE = re.compile(
    r"\b(?i:statement|affidavit|declaration|deposition|evidence|testimony)[ \t]+(?i:of)[ \t]+"
)

# A party line in a claim heading: the name, then the party's role at the end
# of the same line -- "MARIGOLD QUILLFEATHER<spaces>Claimant", "Jane Doe
# (Respondent)", "(2) JOHN SMITH, Second Defendant" -- or a numbered party
# described by its address: "(1) JOHN SMITH of 12 Rose Lane".
_LINE_START_RE = re.compile(r"(?:^|\n)[ \t]*(?P<marker>\(?\d{1,2}[).][ \t]*)?")
_PARTY_ROLE_AFTER_RE = re.compile(
    r"(?:[ \t]{2,}|\t|[ \t]*[,(][ \t]*|[ \t]+[-–][ \t]+)"
    r"(?:(?i:first|second|third|fourth|\d{1,2}(?:st|nd|rd|th))[ \t]+)?"
    r"(?i:claimant|defendant|respondent|applicant|appellant|petitioner|plaintiff|party|interested[ \t]+party)s?"
    r"\)?[ \t]*(?:\n|$)"
)
_PARTY_OF_AFTER_RE = re.compile(r"[ \t]*,?[ \t]+(?i:of)[ \t]+")

# "between John Smith and Mary Jones".
BETWEEN_RE = re.compile(r"\b(?i:between)[ \t]+")
_AND_RE = re.compile(r"[ \t]*,?[ \t]+(?i:and)[ \t]+")

# "Signed by Jane Doe and John Roe", "witnessed by Jane Doe": the signatories
# on the signature line itself, with no colon (LABEL_RE takes "Signed by:").
# Not "executed by": works and services are executed by firms, and a firm's
# name without a legal form reads exactly like a person's.
SIGNED_BY_RE = re.compile(r"\b(?i:signed|countersigned|witnessed)[ \t]+(?i:by)[ \t]+")

# --- A name beside the e-mail address that spells it ---------------------
# "Jane Doe <jane.doe@…>", "Jane Doe, j.doe@…", "JANE DOE (jdoe@…)": the
# address's local part spells the run written right before it on the same
# line, which is evidence of a person that no gazetteer is needed for. The
# run must END at the separator before the address and the local part must
# spell its first and last words (in full or as an initial), so "Write to
# Jane Doe at …" with "jdoe@…" on the next line, or "Jane Doe, info@…",
# proves nothing.
_EMAIL_RE = re.compile(r"(?<![\w.+\-])(?P<local>[A-Za-z0-9][A-Za-z0-9._%+\-]*)@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_BEFORE_EMAIL_RE = re.compile(r"(?:[ \t]*[,(<:\-–—][ \t]*|[ \t]+)$")
_ORDINARY_WORD_RE = re.compile(r"[^\W\d_][\w'’\-]*")


def _letters(value: str) -> str:
    """Folded ASCII letters only: "Ashworth-Lyle" -> "ashworthlyle", "Öztürk" -> "ozturk"."""
    decomposed = unicodedata.normalize("NFKD", _fold(value))
    return "".join(char for char in decomposed if "a" <= char <= "z")


def _local_part_spells(local: str, words: list[str]) -> bool:
    keys = [key for key in (_letters(word) for word in words) if key]
    if len(keys) < 2 or len(keys[-1]) < 2:
        return False
    first, last = keys[0], keys[-1]
    spelled = _letters(local)
    if len(spelled) < 4:
        return False
    initials = "".join(key[0] for key in keys[:-1])
    return spelled in {first + last, first[0] + last, "".join(keys), initials + last, last + first}


def _ordinary_lowercase_words(text: str) -> frozenset[str]:
    """Words the document itself writes in lowercase, outside e-mail addresses.

    A team mailbox spells its address the way a person does ("Records
    Management <records.management@…>"); what says its words are ordinary is
    the document using them in lowercase ("questions on records management").
    """
    blanked = _EMAIL_RE.sub(lambda match: " " * len(match.group()), text)
    return frozenset(_fold(word) for word in _ORDINARY_WORD_RE.findall(blanked) if word[0].islower())


# How far before the address a run may start. A run is at most four name words
# and two particles, so this only bounds the cost on a long single-line
# extraction, where scanning from the line start for every address would be
# quadratic.
_EMAIL_LOOKBACK = 160


def _names_spelled_by_emails(text: str, hits: dict[tuple[int, int], str]) -> None:
    ordinary: frozenset[str] | None = None
    for email in _EMAIL_RE.finditer(text):
        window_start = max(0, email.start() - _EMAIL_LOOKBACK)
        line_start = max(text.rfind("\n", window_start, email.start()) + 1, window_start)
        separator = _BEFORE_EMAIL_RE.search(text, line_start, email.start())
        if not separator or separator.start() == line_start:
            continue
        name_end = separator.start()
        for token in _TOKEN_RE.finditer(text, line_start, name_end):
            if token.start() and text[token.start() - 1].isalnum():
                continue  # the window cut a word: "TTOLINE" is not a name word
            run = _name_run(text, _walk(text, token.start()), 2)
            if not run or run[2] != name_end:
                continue
            words = run[0].split()
            if not _local_part_spells(email.group("local"), words):
                continue
            if ordinary is None:
                ordinary = _ordinary_lowercase_words(text)
            if all(_fold(word) in ordinary for word in words if not word[0].islower()):
                break
            _accept(run, text, hits)
            break

# Headers that introduce a list of people, and signature cues; names sit on
# the following lines.
LIST_HEADER_RE = re.compile(
    r"(?i:present|in[ \t]+attendance|attendees|apologies|witnesses|directors[ \t]+present"
    r"|distribution|copied[ \t]+to|signatories)[ \t]*:"
)
SIGNATURE_CUE_RE = re.compile(
    r"(?:^|\n)[ \t]*(?i:yours[ \t]+(?:sincerely|faithfully|truly)|kind[ \t]+regards|best[ \t]+regards"
    r"|signed|signatures?|executed[ \t]+as[ \t]+a[ \t]+deed)\b[^\n]*"
)
_LIST_SEPARATOR_RE = re.compile(r"[\n,;]")
_LIST_MARKER_RE = re.compile(r"[ \t]*(?:[-*•]|\(?\d{1,2}[).])?[ \t]*")


def detect_english_person_names(text: str) -> list[tuple[str, int, int]]:
    """Return (value, start, end) for English-shaped person names in `text`."""
    if not text:
        return []
    hits: dict[tuple[int, int], str] = {}

    # Path 1 - title: one surname is enough. An all-caps title takes only an
    # all-caps name: "MS Word" and "DR Congo" are not people, while a heading
    # written "MR JOHN SMITH" still is.
    for match in TITLE_RE.finditer(text):
        run = _name_run(text, _walk(text, match.end()), 1)
        title = match.group(0).strip()
        if run and len(title) > 1 and title.replace(".", "").isupper() and not run[0].isupper():
            continue
        _accept(run, text, hits)

    # Path 2 - gazetteer: anchor on a known given name and read forward.
    for token in _TOKEN_RE.finditer(text):
        if _fold(token.group(0)) in GIVEN_NAMES:
            _accept(_name_run(text, _walk(text, token.start()), 2), text, hits)

    # Path 3 - structure: a label, an opener, "statement of", "between",
    # "signed by".
    anchors = [match.end() for regex in (LABEL_RE, OPENER_RE, STATEMENT_OF_RE) for match in regex.finditer(text)]
    for position in anchors:
        _accept(_name_run(text, _walk(text, position), 2), text, hits)
    for match in _LINE_START_RE.finditer(text):
        run = _name_run(text, _walk(text, match.end()), 2)
        if run and (
            _PARTY_ROLE_AFTER_RE.match(text, run[2])
            or (match.group("marker") and _PARTY_OF_AFTER_RE.match(text, run[2]))
        ):
            _accept(run, text, hits)
    for match in [*BETWEEN_RE.finditer(text), *SIGNED_BY_RE.finditer(text)]:
        first = _name_run(text, _walk(text, match.end()), 2)
        _accept(first, text, hits)
        if first:
            joiner = _AND_RE.match(text, first[2])
            if joiner:
                _accept(_name_run(text, _walk(text, joiner.end()), 2), text, hits)

    # Path 4 - the e-mail address written beside the name spells it.
    _names_spelled_by_emails(text, hits)

    # Path 5 - list and signature windows, scoped so the scan cannot run
    # into unrelated prose.
    windows = [(match.end(), match.end() + 240) for match in LIST_HEADER_RE.finditer(text)]
    windows += [(match.end(), match.end() + 160) for match in SIGNATURE_CUE_RE.finditer(text)]
    for window_start, window_end in windows:
        positions = [window_start] + [sep.end() for sep in _LIST_SEPARATOR_RE.finditer(text, window_start, window_end)]
        for position in positions:
            marker = _LIST_MARKER_RE.match(text, position)
            _accept(_name_run(text, _walk(text, marker.end()), 2), text, hits)

    return _resolve_overlaps(hits)


def _resolve_overlaps(hits: dict[tuple[int, int], str]) -> list[tuple[str, int, int]]:
    """Keep the longest span where several paths found one name.

    A span inside one already kept is the same name read twice and would key a
    second pseudonym, so it goes. One that reaches PAST the kept span stays:
    the characters only it covers would otherwise be in no finding at all.
    """
    ordered = sorted(hits.items(), key=lambda item: (item[0][0], -(item[0][1] - item[0][0])))
    resolved: list[tuple[str, int, int]] = []
    last_end = -1
    for (start, end), value in ordered:
        if end <= last_end:
            continue
        resolved.append((value, start, end))
        last_end = end
    return resolved
