# Held-out evaluation corpus

This is the **measurement** set. `gold/` is the **regression ratchet**. They are not
interchangeable and the difference is the reason this directory exists.

| | `gold/` + `accuracy_audit.py` | `eval/` + `accuracy_eval.py` |
|---|---|---|
| question | "did the detector lose something it could do?" | "what can the detector do?" |
| corpus | 37 hand-labelled documents: 34 `.txt` fixtures (23 Turkish, 6 English, 5 name documents) and 3 Turkish `.docx` practice samples | 40 generated documents, Turkish and English, as `.txt`, `.docx` and `.pdf` |
| labels | written alongside the rules, in the same commits | emitted by the generator as it places each entity |
| recall on it | 1.000 in every category, by construction | whatever it is |
| CI | **gated** — `false_negative_count` must be 0 | **reported** — `docs/accuracy.md` must match the run |
| may a rule be tuned against it? | yes, that is its job | **no** |

## Why the gold set cannot answer the second question

Its labels and the rules in `legal_analyzer/privacy.py` have been edited in the same
commits, and its recall is 1.000 in every single category. Anything the
detector misses in those documents was, by and large, never labelled — `gold/README.md`
records three such gaps itself. So the set cannot produce a false negative, and CI
asserting `false_negative_count == 0` is a ratchet: it guarantees no capability is
silently lost, and says nothing about capabilities that were never there.

That is a real guarantee and it stays exactly as it is. This corpus is the other half.

## The one rule for this directory

**Never tune a detection rule until a number here goes up.**

The moment that happens, `eval/` becomes fitted too, this stops being a measurement, and
the repo is back to having two ratchets and no ruler. If a rule change is motivated by
something seen here, the evidence belongs in `gold/` as a new labelled regression case —
and then the number here moves on its own, which is what makes it worth reading.

Related: nothing in `accuracy_eval.py` asserts a recall or precision floor, for the same
reason. A floor is a ratchet with extra steps.

## What is in here

- `generate_corpus.py` — writes `corpus/` and `labels.json`. Deterministic: same seed,
  byte-identical output. `--check` verifies the committed corpus still matches, and CI
  runs it, so the ruler cannot drift from its own description.
- `corpus/*.txt` — 40 documents. Twenty Turkish templates (petition, supply contract,
  hearing minutes, enforcement payment order, termination letter, notarial power of
  attorney, expert report, KVKK subject request, lease, board resolution) and twenty
  English ones (engagement letter, share purchase agreement, witness statement,
  settlement agreement, letter before claim, board minutes, employment contract, data
  processing agreement, litigation hold notice, mutual NDA), two instances each.
- `labels.json` — 492 entity spans with exact `start`/`end` offsets into the `.txt`.

`.docx` and `.pdf` renderings are built at measurement time into a temp directory rather
than committed, so no binaries live in git; `accuracy_eval.py` owns both renderers.

## Synthetic, and it has to stay that way

Every person, company, matter number, address, account and amount here is invented. This
is a hard project constraint (`AGENTS.md`: "Use only synthetic documents and synthetic
personal data in tests"), and it is also why the corpus is generated rather than
collected.

National id (TCKN) and tax (VKN) numbers carry valid check digits, because
`legal_analyzer/privacy.py` rejects invalid ones and a corpus of rejects would measure the
generator instead of the product. They are computed from a seeded PRNG and never looked
up. A valid check digit does not mean a number is unused — that caveat is the same one
`samples/README.md` carries — which is another reason these documents stay inside the
repo's test tooling and are not presented as examples of anything.

## Reserved contact ranges

This corpus and the fixtures in `tests/fixtures/` are published with the repository, so
no phone number or email domain in them may be one that a real subscriber can hold or a
stranger can register. Detection is exercised on the realistic *shape*; the *value* comes
only from a range that is reserved or unallocated:

| kind | range | why it cannot reach anyone |
|---|---|---|
| UK landline | `+44 20 7946 0000`–`0999` (`020 7946 0xxx`) | Ofcom drama range, never assigned |
| UK mobile | `07700 900000`–`900999` | Ofcom drama range, never assigned |
| North American | `NXX-555-0100`–`0199`, any area code | NANPA reserves the block for fictional use |
| Turkish mobile | `052x xxx xx xx` | no operator holds any `52x` block (see below) |
| Turkish landline | an area code BTK has not allocated, e.g. `0299 xxx xx xx` | no exchange behind it |
| email | `example.com` / `example.org` / `example.net` and their subdomains, or the `.test` / `.example` / `.invalid` TLDs | RFC 2606 / RFC 6761 reserved, never registrable |

Turkey has no official fictional numbering range, so the Turkish mobile block was chosen
as *unallocated*. The check: BTK's public numbering report ("Numara Yönetim Sistemi
Numaralandırma Raporu", report date 23/09/2026, linked from
btk.gov.tr/isletmecilere-tahsisli-numaralar) lists the mobile and M2M blocks held by
every operator. The 3-digit `5xx` prefixes it shows as allocated are 501, 505–507, 510,
516, 530–549, 551–555, 559, 561, 572, 574, 575, 582 and 592; nothing in `520`–`529`
is allocated. Its geographic list (212 … 488) is where the landline area codes come from.
Numbering changes, so re-check the report before relying on the block for anything
beyond fixtures.

Turkish-flavoured domain labels are kept as subdomains (`ornekhukuk.example.org`) so the
text still reads like a Turkish filing. A real Turkish law firm's `.av.tr` address is
therefore absent from the corpus; note that `av.` is a `privileged_or_confidential`
marker in `legal_analyzer/privacy.py`, so on real documents such an address also raises
that flag when its sentence carries an advice or communication word, which the corpus no
longer measures.

`tests/test_reserved_contacts.py` fails if the generator, a fixture or
`gold/gold_labels.example.json` carries a phone or email outside these ranges. Its
allowed ranges are written in the test, not imported from the generator, so the two
cannot drift together.

Turkish given names are drawn half from inside the hand-written gazetteer
(`legal_analyzer/turkish_names._HAND_WRITTEN_GIVEN_NAMES`, which was all of
`GIVEN_NAMES` when the corpus was generated) and half from outside it, deliberately, so the
gazetteer's edge is measurable rather than averaged away. That split is a property of the
corpus, not of any real population: to read the name-recall figure against a real
caseload, re-weight the two buckets. `GIVEN_NAMES` now also carries the official name
lists (`given_names_tr.py`, `given_names_en.py`, built by `build_given_names.py` from
public statistics without reference to this corpus), and they cover part of the outside
half; that is where part of the name-recall gain since then comes from (most of the English
gain; the Turkish gain came mainly from name propagation), and a caseload with rarer names
than the birth-register tables will not see all of it. Their homograph filter also takes
names out (every Turkish name Zemberek lists as a word, and Adelaide with the cities), and
this corpus measured that cost rather than set it: nothing was put back because a name
here was missed.

## What this corpus is not

A production accuracy baseline. It measures the rules against the document shapes the
generator knows how to write, which is a ceiling as well as a floor — Turkish matter
numbers score well here partly because the generator writes them in the labelled
`Esas No: 2025/417` form, while the `2025/417 E.` form real decisions use is not matched
by any rule. Extend it with hand-labelled real documents before treating any figure as
what a lawyer would see on their own files.

## Running it

```bash
python3 eval/generate_corpus.py           # rewrite the corpus (only when the generator changes)
python3 eval/generate_corpus.py --check   # verify the committed corpus matches the generator
python3 accuracy_eval.py                  # measure; writes eval_report.json
python3 accuracy_eval.py --formats txt    # skip the rendered formats
python3 accuracy_eval.py --write-docs     # refresh docs/accuracy.md
python3 -m unittest tests.test_accuracy_eval
```
