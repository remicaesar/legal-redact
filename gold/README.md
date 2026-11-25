# Mini-Gold Accuracy Dataset — the regression ratchet

> **This is not the accuracy measurement.** Its labels and the rules in
> `legal_analyzer/privacy.py` have been edited in the same commits, so its recall is 1.000
> in every category by construction and it cannot produce a false negative. That is what
> makes `false_negative_count == 0` a usable CI gate — it guarantees no capability is
> silently lost — and it is also why no figure here describes what the detector can do.
> For that, see `eval/README.md` and `docs/accuracy.md`, which are measured on a held-out
> corpus nothing is tuned against. Keep the two apart: a document added here to make an
> `eval/` number look better would destroy both.

Use this folder for manually labeled regression cases. Speed benchmarks only show that the scanner is fast; this dataset is for recall, precision, false negatives, and false-Low checks against behaviour that has already shipped.

Recommended production audit set:

- 5 pleadings or petitions, including UYAP/UDF where available
- 5 contracts
- 5 scanned/image PDFs or exhibits
- 5 public/template documents

For each document, label direct identifiers, indirect identifiers, sensitive attributes, contextual identifiers, and confidential legal/business information.

For Turkish legal practice, explicitly label:

- TCKN, VKN, MERSIS, IBAN, phone, email, and address data
- Bar registration numbers and lawyer/person names
- Court, enforcement office, prosecutor, UYAP, investigation, case, decision, and file numbers
- Party roles such as davacı, davalı, müşteki, şüpheli, sanık, borçlu, and alacaklı
- Privileged/confidential legal-business context

Use optional `ocr_text` fields for scanned exhibits where reviewer-accepted OCR should be compared against pre-OCR extraction.

Run:

```bash
python3 accuracy_audit.py --labels gold/gold_labels.example.json
```

The key goals are `false_low_count = 0` (no document reported Low when it has undetected
gold labels or an expected risk above Low) and `risk_shortfall_count = 0` (no document's
computed risk level is ranked below its gold-expected level, e.g. expected High computing
Medium — a case `false_low_count` alone cannot see, since it only fires when the computed
level is exactly Low).

## English documents (2026-10-08)

Five documents under `tests/fixtures/english/` were added with the English rules and
labelled in the same commits, so they are fitted exactly like the Turkish seventeen. Four
read as English and expect residual risk `Unknown`: the release gate does not measure an
English document, whatever the English rules find in it. The fifth is a Turkish annex
carrying English witness details as labelled fields; it reads as Turkish and expects
`Medium`.

One identifier in them is deliberately not labelled, because no rule detects it and the
ratchet can only hold what the rules do: the trading name "Hartington Joinery" in the letter
before claim. Its surname is still removed on export, because an approved "Hartington" is
replaced wherever it occurs.

## Court citations and contract party roles (2026-10-31)

Three regression cases, labelled in the same commit as the rules they hold:

- `tests/fixtures/case_law_opinion.txt` cites Yargıtay and Danıştay the way a lawyer
  does: `Yargıtay 3. Hukuk Dairesi'nin 2021/4521 E., 2022/1187 K. sayılı kararı`, and
  `E. 2020/2210`. The four case and decision numbers matched no rule, so they could not
  be approved and were delivered in cleartext; the court came out as `Yargıtay 3` plus a
  second finding `3. Hukuk Dairesi`. This audit cannot see the second defect, because
  `Yargıtay 3` is a substring of the label, so `tests/test_court_citations.py` pins it on
  offsets and on the delivered file.
- `tests/fixtures/service_agreement.txt` names its parties by role
  (`Hizmet Veren ile Hizmet Alan arasında`, and a signature block of the two roles), and
  the roles were reported as people. `Murat Alan` is labelled on purpose: a guard on
  the word "Alan" rather than on the whole candidate would lose him, and this set would
  then fail on a false negative.
- `tests/fixtures/english/gold_en_scc_execution.txt` ends on `Data Exporter` and
  `Data Importer` signature lines, which were reported as people.

Measured against the rules as they stood before (`main` on 2026-10-29), this set reported
4 false negatives (the case numbers) and 51 false positives, 5 of them the roles above.

## A title typed without the cedilla (2026-11-01)

`tests/fixtures/medical_report_unaccented.txt` is a Turkish expert-report annex typed without
diacritics, as Turkish routinely is: `Davaci hasta Doc. Dr. Selin Arman gozetiminde kronik
bobrek yetmezligi tanisi aldigini`. The context sweeps crossed the period of `Doç.` but not
of `Doc.`, so the health span stopped at `hasta Doc` and the diagnosis after the physician's
name was in no finding: it could not be approved, so it could not be redacted. Its label,
`kronik bobrek yetmezligi tanisi`, is reachable only by the sweep crossing that period.

Measured against the rules as they stood before (`main` on 2026-11-01, once the
bare-surname mention rule had landed), this case reported
1 false negative (the diagnosis) and every other document's result was identical. What the
fix costs, English `doc.` ending a sentence being crossed too, is not something this set can
show; it is priced in the comment on `_CONTEXT_SWEEP_ABBREVIATIONS` instead.

## Investigation numbers and genitive authority names (2026-11-01)

One regression case, labelled before the rules changed:
`tests/fixtures/investigation_reference.txt`, a file note that names three prosecutor's
offices, a criminal judgeship, a notary and a civil court with a genitive case ending
(`İstanbul Cumhuriyet Başsavcılığının`, `Kadıköy 3. Sulh Ceza Hakimliğinin`, `Beyoğlu 24.
Noterliğinin`, `Masalkent 5. Asliye Hukuk Mahkemesinin`), and cites investigation numbers
in the three forms that follow an office: `2024/12345 sayılı soruşturma`, `2023/9071
soruşturma numaralı` and `2025/4410 Soruşturma No'lu`.

Measured against the same 2026-11-01 baseline as the cedilla case above, the document reported 6 false negatives:
the three investigation numbers, `İstanbul` and `Ankara Cumhuriyet Başsavcılığı`, and
`Beyoğlu 24. Noterliği`. None of them was inside any finding, so the reviewed export
shipped every one in cleartext (`tests/test_court_full_span.py` verified this against
those rules). Two defects lay behind them. First, every court rule ended its institution
word on `\b`, which a word with a case suffix attached never offers. Second, the case-number
rule read a number only after its label (`Soruşturma No: …`). The audit cannot see two more
defects in this document, because a finding's text is a substring of the label: the
numbered court and the judgeship backtracked to `Masalkent 5. Asliye Hukuk` and
`Kadıköy 3. Sulh Ceza`, with the court word outside the span, and `İzmir Cumhuriyet
Başsavcılığı` ran on to the end of its sentence. Those are pinned on offsets and on the
delivered DOCX in `tests/test_court_full_span.py`.

After the change the document has 0 false negatives. It contributes 3 false positives, all
`criminal_allegation` at CRITICAL: the bare `soruşturma` clauses, which allege no offence.
That is the over-reach recorded under "Out-of-scope observations" below. It is left
unlabelled here because the gold set labels allegations, not dockets. On every other
document the false-positive set is unchanged except for one sample:
`Cumhuriyet Başsavcılığı'na sunulmak üzere hazırlanmıştır` (`synthetic_pleading`, still
unlabelled) is now `Cumhuriyet Başsavcılığı`, because the prosecutor's office no longer
carries the generic rule's 120-character sweep.

## Docket references are not allegations (2026-11-04)

One regression case, labelled and committed before the rule changed:
`tests/fixtures/docket_references.txt`, a file note whose first five lines cite
investigation dockets and allege nothing (`2024/12345 sayılı soruşturma dosyası incelendi`,
`Soruşturma No: 2024/12346 olan dosya incelendi`, `Soruşturma No: 2026/123 sayılı dosya
Soruşturma Bürosu tarafından yürütülmektedir`, `2024/12347 sayılı soruşturma kapsamında …`),
followed by two lines that do: `2024/12348 sayılı soruşturma dosyasında Burak Ertan'ın
dolandırıcılık suçunu işlediği iddia edilmektedir`, and `Şüpheli Kemal Arslan'ın hırsızlık
suçunu işlediği iddia edilen eylemleri …`. The gold set cannot label an absence, so the
docket lines carry only their case numbers and office; that they report no
`criminal_allegation` is pinned in `tests/test_docket_allegation.py`.

The first allegation line is why the rule was not simply narrowed. `suçunu` is not `suç` to
the `\b`-bounded trigger, so the docket word `soruşturma` is the only trigger in front of
that allegation: dropping docket forms from the vocabulary outright would ship it with no
finding. A docket-form `soruşturma` -- tied to a case number (`2024/12345 sayılı
soruşturma`, `Soruşturma No: 2024/12345`) or naming the office (`Soruşturma Bürosu`) -- now
stays silent only when the text from the start of its sentence to the end of its sweep
-- so an allegation or a person named in front of the number counts too, including on an
earlier line when no sentence-ending punctuation or blank line comes between, unless the
docket's own line is a `Label : value` field whose label starts with a capital
(`Soruşturma No : …`; a lowercase `soruşturma No:` is a wrapped continuation) -- carries
neither a person name nor an allegation marker
(`suç…`, `iddia`, `isnat`, `itham`, `işledi…`, `tespit`, a measure such as `gözaltı` or
`tutuk…`, a common offence name, a suspect or defendant). Everything else still reports,
including `soruşturma` with no number (`X'in soruşturma dosyası`, `2 soruşturma`) and bare
`soruşturma` ("… hakkında soruşturma başlatılmıştır"). The person check is what keeps the
bounded marker list from failing open; an offence the list does not name, in a numbered
docket clause that names nobody, is the residual.

Measured against the rules as they stood once the investigation-number change above had
landed, with this case added, the set reported
14 `criminal_allegation` false positives (9 on the other 34 documents, 5 on this one) and no
false negatives. After the change it reports 4, still with no false negatives: `Supheli`
in `ocr_scan_noisy`; `hakkında nitelikli dolandırıcılık iddiası ile soruşturma
yürütülmektedir` in `criminal_investigation`, an allegation segment its label does not
contain; `Soruşturma Bürosu` in that document's heading `… Dolandırıcılık Suçları
Soruşturma Bürosu`, whose own name puts an offence word in front of the docket word, which
the sentence check cannot tell from an allegation and so reports (the `Soruşturma No:
2026/815` field on the next line is its own statement and stays silent); and this
document's heading `Soruşturma dosyalarına ilişkin yazışma notu`, which cites no number and
is deliberately still reported. No other category's findings moved: the 10 findings that
went were 10 of those false positives.

Known limits of the look-back, each a direction in which a real allegation can lose its
only CRITICAL finding:

- **A capitalised field-shaped docket line reads nothing above it.** Any line starting with a
  capitalised word and up to three more words before a colon counts as a field, so a
  wrapped sentence whose next line happens to start `Soruşturma No: …` or `Konu: …` (a
  capitalised proper noun or a sentence-initial word followed by a colon) is cut off from
  an allegation on the line before. Field shape is judged by layout, not meaning.
- **The look-back stops at a blank line and at sentence-ending punctuation**, so an
  allegation stated in an earlier sentence and then referred to by docket number alone
  ("Şirket müdürünün rüşvet aldığı ileri sürüldü. Dosya 2024/12345 sayılı soruşturma
  kapsamında incelendi.") has no `criminal_allegation` finding: the earlier sentence holds
  no trigger, and the docket sentence no marker or person.
- **The marker list is bounded and the person check depends on the name detector**: an
  offence the list does not name, in a docket sentence naming no person the detector finds,
  ships with no `criminal_allegation` finding.
- **The cross-line look-back is bounded**: only the last 400 characters
  (`legal_semantics.SENTENCE_LOOK_BACK`) before the docket word are read from earlier
  lines -- a longer line above is clipped, not dropped -- so a long line-per-record
  extraction costs linear time, and an allegation further back than that across
  unpunctuated lines above the docket is not read.

## Company spans that start at the company name (2026-11-04)

One regression case, labelled before the rule changed:
`tests/fixtures/company_span_clauses.txt`, a commercial note that names five companies
mid-clause: after ordinary clause words (`Taraflar arasında Ankara'da imzalanan sözleşmeye
göre Mavi Deniz Lojistik Ltd. Şti.`), behind a party-role word (`Alacaklı Yıldız LTD.
ŞTİ.`), between both (`Bu nedenle Davacı ile Kuzey Yapı Anonim Şirketi`), in lowercase
(`Sözleşme gereği Davalı taraf olan yıldızlı örnek holding a.ş.`), and with a lowercase `ve`
inside the name (`Çınaraltı Gıda Sanayi ve Ticaret A.Ş.`).

The company rule is a lazy body behind a case-sensitive capital, so its match began at the
earliest capital within ninety characters of the legal form, and against the rules at
`main` before the docket-reference change above landed, four of the five
findings were the whole clause. That is the redaction
target, so the export replaced the clause along with the company, and the clause text
became the pseudonym key. **This audit cannot see that defect.** A label is matched to a
finding by substring in either direction, and the label is a substring of the clause, so
the document reported 0 false negatives before the change and 0 after.
`tests/test_company_span.py` pins the spans on offsets instead.

The document contributes 2 false positives, both `natural_person_name`: `Kuzey Yapı
Anonim` and `Yıldız LTD`. The person rule reads them inside two company names, and each
lies inside its company finding. That is the person rule, and it is not changed here. On
every other document the false-positive set is unchanged from that baseline.
The current figures, re-measured on this change rebased onto `main` once the
docket-reference change had landed, are in the [README](../README.md#the-regression-ratchet). Six company
findings got narrower, and each now starts at its name: `Devreden, ORNEK TEKNOLOJİ ANONİM ŞİRKETİ`,
`İşbu sözleşme DEMİR YAZILIM ANONİM ŞİRKETİ`, `Alacaklı Verdi Faktoring A.Ş.`, `Davacı
YILMAZ İNŞAAT ANONİM ŞİRKETİ`, `We act for Wrenfield Interiors Limited` and `September 14,
2026 between Thomas Whitacre and Halvorsen Example Logistics Inc.`, where the person
keeps a finding of its own.

Known limits, each over-redaction rather than a miss. A lowercase `for`, `with`, `to`,
`against` or `ile` behind a capitalised word is read as part of the name (`Back to Basics
Limited`, `Made with Love Ltd`, `Kaya Holding ile Mavi Deniz Ltd. Şti.`), unless that word ends a person's name or is a party-role word (`made
by John Smith to Acme Holdings Limited`, `from the Claimant to Acme Holdings Limited` both
start at `Acme`). Any other capitalised word, a place included, still pulls the clause in:
`The goods were sent by London to Acme Holdings Limited` gives `goods were sent by London
to Acme Holdings Limited`. An unknown lowercase word with no clause word before it is kept
too (`Our client is Made with Love Ltd`).

## Labelling gaps closed (2026-11-24)

Labels and one fixture line only; no detection rule changed. Before this pass most of the
set's false positives were findings nobody had labelled, so its precision measured the
labels rather than the rules. `tests/test_accuracy_audit.py::GoldLabelGapTests` pins each
gap so it cannot reopen: every `party_role` finding is labelled, every detected direct
identifier is labelled except the three rule errors named below, no allegation, health or
privilege label contains a labelled person's name, and the company labels carry a
title-case, a lowercase and a cedilla-free spelling.

- **`party_role`** — labelled wherever a role word stands as a role (`Davacı`, `Davalı`,
  `Müşteki`, `Şüpheli`, `Tanık`, `Alacaklı`, `Borçlu`, and the unaccented `Davaci`, `Davali`,
  `Supheli`, `Sikayetci`) in the twelve documents that use one, as the labelling
  instructions at the top of this file always asked. This closes the gap the 2026-09-05
  triage accepted below.
- **Dates, authorities, an address and a trade registry number that were simply
  unlabelled**: seven `dd.mm.yyyy` dates, `Cumhuriyet Başsavcılığı` in `synthetic_pleading`,
  `Kadıköy 2. Noterliği` in `power_of_attorney`, the street address `Yoğurtçu Parkı Caddesi
  No: 27 Daire: 6 Kadıköy` in `lease_agreement`, and `Ticaret Sicil No: 123456` in
  `corporate_resolution`. That last number is no longer read as a bar registration: the
  trade-registry rule now claims it, so the missing label was all that remained.
- **Allegation labels that embedded a person's name.** `Şüpheli Kemal Arslan hakkında
  nitelikli dolandırıcılık` (`criminal_investigation`) is now `nitelikli dolandırıcılık
  iddiası`, and the two `docket_references` labels that ran on through `Burak Ertan'ın` and
  `Şüpheli Kemal Arslan'ın` are dropped, because each already had a name-free label for the
  same allegation. Scoring matches a finding inside a label as well as a label inside a
  finding, so those labels credited a bare `Şüpheli` or `soruşturma dosyasında` finding as
  the allegation. Three false positives that were hidden that way are now counted.
- **A second label for `medical_case` and `ocr_scan_noisy`.** `medical_case` labels its
  diagnosis, `kalıcı sinir hasarı teşhisi`, beside the procedure. `ocr_scan_noisy` labels
  its suspect role and gains one line, `Sikayetci : Ege Deniz Lojistik Ltd. Sti.`, the
  set's first cedilla-free company name (its `ocr_text` carries the same line). The line was
  chosen to keep that fixture below the language gate's Turkish floor, which
  `tests/test_language_gate.py` pins.
- **`turkish_national_id`** needed nothing: thirteen national IDs were labelled before this
  pass. Title-case (`Verdi Faktoring A.Ş.`, `Mavi Deniz Lojistik Ltd. Şti.`) and lowercase
  (`yıldızlı örnek holding a.ş.`) company labels were also already present.

**A gap this set cannot hold: a cedilla-free `A.S.` suffix.** `Kaya Insaat Sanayi ve
Ticaret A.S.` gets no `company_name` finding at all; the person rule reads `Kaya Insaat
Sanayi` and the rest ships. The ratchet can only hold what the rules already do, so it is
recorded here for the rule change rather than labelled (as with "Hartington Joinery" above).

The false positives left after this pass are rule behaviour, each accepted for now with its
reason:

- `criminal_allegation` (6, CRITICAL): the office name `Dolandırıcılık Suçları Soruşturma
  Bürosu`, the docket note's heading (see "Docket references are not allegations"), and
  four role-word or docket-word fragments (`Şüpheli` twice, `Supheli`, `soruşturma
  dosyasında`) reported as an allegation of their own next to the finding that covers the
  allegation.
- `health_data` (5) and `privileged_or_confidential` (3), all CRITICAL: context words
  (`hasta`, `Hasta dosyası ve reçete`, `Müvekkil`) and fragments of a wider sweep (`),
  Özel Marmara Hastanesi'nde 22.09.2025`, `), Kadıköy 2`). The set labels the diagnosis and
  the procedure, not the words around them.
- `natural_person_name` (3): `Barış Görüşmesi`, and `Kuzey Yapı Anonim` and `Yıldız LTD`
  read inside a company name (see "Company spans that start at the company name").
- `court_or_authority` (1): `UYAP No: 2026/9981 kaydı ile dosyalanmıştır` (see the
  triage below).

## Names a document spells for you (2026-11-24)

One regression case, labelled before the rules changed:
`tests/fixtures/names/en_signature_and_email_names.txt`, an English custodian list whose six
people all have given names that neither the hand-written list nor the official ones carry,
so before the change no path found any of them (6 false negatives here). It names them in
the two places an English document spells a name out regardless of the gazetteer:

* right before an e-mail address whose local part spells the name, on the same line
  (`Ottoline Quarrendon, ottoline.quarrendon@…`, `Lysander Pellingham (l.pellingham@…)`,
  `Hepzibah Wrexbridge <hepzibah_wrexbridge@…>`), then once again in prose, which name
  propagation takes from the full name;
* on the signature line itself: `Signed: Cressida Dunstanmere`, and `Signed by Euphemia
  Ashworth-Lyle and Septimus Ravenhurst` with no colon. The signature window reads only the
  lines after such a line, and the label path wanted `Signed by:`.

Beside them, two team mailboxes spell their addresses exactly the way a person does
(`Records Management (records.management@…)`, `Data Protection Team
<data.protection@…>`); the document also writes "records management" and "data protection"
in lowercase, and that is what keeps them out. The document contributes no false positive.

The shapes came from reading which held-out English names were missed (`eval/`, where
every one of the nine had a given name outside the lists); the case here uses its own
invented names and none of `eval/`'s, and the rules were checked against it alone before
`eval/` was re-measured. The rules generalise by construction rather than by list: neither
consults a name list, and run over every `.txt` and `.docx` fixture, sample and `eval/`
document (Turkish included) they add only person names.

What they do not see: a name with an unlisted given name in plain prose ("I met Adelaide
Mortlake at the offices"), and an address that spells only part of the name or a nickname
(`sandy.p@…` for "Alexander Pellingham"). A team mailbox is set aside only when the
document writes every one of its words in lowercase; one it does not is proposed as a
person, and the reviewer rejects it.

## False-positive triage (2026-09-05)

> Historical record, measured at the go-public preparation merge of 2026-09-05. The figures below are as of that commit and
> are not re-run. The current figures are stated in one place only — the "regression
> ratchet" paragraph of the [README](../README.md#the-regression-ratchet) — and
> `python3 accuracy_audit.py --labels gold/gold_labels.example.json` prints them.

Triage of the 58 false positives the gold-set audit reported on 2026-09-05, so that
reviewers are not trained to click Approve through a queue of noise.

The tracker for this work said 65 false positives. That figure does not reproduce: the
audit on 2026-09-05 reports 58, against 117 gold labels and 194 findings. Everything below
uses the measured 58.

Reproduce any of the numbers below with:

```bash
python3 accuracy_audit.py --labels gold/gold_labels.example.json
```

### Before / after

**What the two columns are.** *Before* is a measurement: the audit run on
2026-09-05. *After* is the audit re-run once the two tightenings described below had
landed, at a commit this file does not name — so it is reproducible only in the sense
that the rules it measured are still the rules in the tree.

**Neither column is the current figure.** The current one is in the
[README](../README.md#the-regression-ratchet), which is the only place it is stated;
[docs/accuracy.md](../docs/accuracy.md) carries the held-out measurement, which this
fitted set is not. Where the current count differs from the 43 after, check the tightened
rules' post-triage counts (`court_or_authority` 4, `address` 1) before reading the
difference as a partial undoing of this triage: so far it has come from new false
positives in categories this pass ruled out of scope.

| Rule | FPs before | FPs after | Outcome |
|---|---|---|---|
| `party_role` | 20 | 20 | accepted, see below |
| `court_or_authority` | 11 | 4 | tightened |
| `address` | 9 | 1 | tightened |
| `date` | 7 | 7 | out of scope |
| `criminal_allegation` | 4 | 4 | out of scope |
| `privileged_or_confidential` | 3 | 3 | out of scope |
| `health_data` | 3 | 3 | out of scope |
| `bar_registration_number` | 1 | 1 | out of scope |
| **total** | **58** | **43** | |

| Metric | Before (2026-09-05) | After |
|---|---|---|
| recall | 1.0 | 1.0 |
| precision | 0.701 | 0.7557 |
| gold labels | 117 | 117 |
| findings | 194 | 176 |
| `false_negative_count` | 0 | 0 |
| `false_low_count` | 0 | 0 |
| `risk_shortfall_count` | 0 | 0 |
| post-OCR precision | 0.6429 | 0.6923 |

Recall, `false_low_count` and `risk_shortfall_count` were re-checked after each
individual change, not only at the end. Full suite: 318 tests, OK.

### Tightened: `address` — dropped the bare place-name triggers

`beşiktaş|besiktas|ümraniye|umraniye|[İi]stanbul` were triggers in their own right, so a
city or district named anywhere in running text matched and then swallowed the following
140 characters. Every one of the eight false positives it produced was a court, an
institution or a contract clause reported as an address:

- `İstanbul Anadolu 4. Asliye Ticaret Mahkemesi`
- `İstanbul 7. İş Mahkemesi kararının temyiz incelemesinde, davacı işçi Hasan Demirci'nin`
- `İstanbul Cumhuriyet Başsavcılığı Dolandırıcılık Suçları Soruşturma Bürosu`
- `İstanbul Barosu'na kayıtlı Av`
- `İstanbul Ticaret Sicili Müdürlüğü kaydı güncellenecektir`
- `tarihinde\nİstanbul'da akdedilmiştir`

**Why it is safe.** A place name on its own is not an address — it does not narrow anyone
down. All seven `address` gold labels are anchored on a structure word instead (`Caddesi
Merkez Apartmanı`, `Bağdat Caddesi No: 41`, `Birlik Mahallesi 45. Sokak No: 8`, `Etlik
Mahallesi Güven Sokak No: 3`, `Alsancak Mahallesi 1475 Sokak`, `Moda Caddesi Deniz
Apartmanı No: 15`, `Fenerbahçe Mahallesi`), and the structure word's 140-character
trailing span already sweeps up the district and city that follow it. The place-name
alternative matched no gold label before the change and none after: recall stayed 1.0 and
`false_negative_count` stayed 0.

**Accepted cost, and its gate consequence.** An address written as a bare district and
city (`Adres: Kadıköy İstanbul`), with no street or neighbourhood, is no longer flagged as
an address. That is under-detection, which is the unsafe direction in this codebase, so
the consequence is recorded here in full. `address` is in `DIRECT_IDENTIFIER_CATEGORIES`,
so such a document now produces **zero** findings and no longer trips the *"No direct
identifiers may remain in detected findings"* arm of the external-LLM gate —
`external_llm_gate.direct_identifiers_detected` goes `True` -> `False` and that entry
disappears from `failed_conditions` (verified before and after on that exact string). For
this particular document the gate still blocks, on the separate *"Residual risk must be
Low"* arm; but a document that is otherwise Low would lose a blocking condition it
previously had. Extend the gold set with a bare district-and-city document before relying
on the rule for that shape.

### Tightened: `court_or_authority` — the generic suffix rule yields to a named court

`GENERIC_COURT_SUFFIX_RULE` fires on a bare institution *suffix* (`Mahkemesi`,
`Hakimliği`, `Noterliği`, `Savcılığı`) and sweeps up to 120 characters, because the
institution's own name is not part of its trigger. Where one of the specific
numbered-court rules had already matched the full name, the generic rule reported a
second, nameless copy of the same institution starting mid-name:

| Already reported by a name-anchored rule | Reported again by the generic rule |
|---|---|
| `11. Aile Mahkemesi` | `Mahkemesi Sayın Hakimliğine` |
| `7. İş Mahkemesi` | `Mahkemesi kararının temyiz incelemesinde, davacı işçi Hasan Demirci'nin` |
| `3. İş Mahkemesi` | `Mahkemesi'ne` |
| `5. Asliye Hukuk Mahkemesi` | `Mahkemesi - Malpraktis Dosyası` |
| `3. Sulh Ceza Hakimliği` | `Hakimliği kararı ile dosyaya eklenmiştir` |
| `24. Noterliği` | `Noterliği'nde düzenlenmiştir` |
| `2. Noterliği` | `Noterliği onaylı örneği ile,` |

`_generic_court_matches()` now skips a match of the generic rule that *begins strictly
inside* a span one of the name-anchored rules already matched, **and resumes the scan at
the end of that enclosing span** rather than after the skipped match. There was no
existing overlap-suppression mechanism to reuse: `_dedupe_findings()` only drops exact
`(category, start, end, fingerprint)` repeats, and `build_redacted_preview()` resolves
overlap for the preview only, not for the finding list a reviewer works through.

**The resume is the load-bearing half.** Simply dropping the match and letting
`re.finditer()` continue is *not* equivalent, and the first version of this change made
exactly that mistake: `finditer` is non-overlapping, so the scan restarted after the
discarded 120-character match and any second authority inside it was never offered again.
In `"İstanbul Anadolu 4. Asliye Ceza Mahkemesi kararı Kadıköy Cumhuriyet Başsavcılığı'na
gönderilmiştir"` the prosecutor's office got no finding of any kind and survived verbatim
into `redacted_preview` — and a span with no finding cannot be approved by a reviewer, so
it is unredactable on export by construction. The gold set contains no document of that
shape, which is why the audit could not see it; the metrics are identical either way.

**What is guaranteed.** Precisely two things, no more:

1. A name-anchored court is never suppressed.
2. The **start** of every authority mention lies inside at least one finding span — its
   own, or the name-anchored finding that encloses it — so the place name and the
   institution stem can always be approved and redacted. Not the whole mention: the
   Yargıtay/Danıştay rule's fixed 60-character sweep can end mid-word, the resume then
   lands where `\b` cannot match, and the tail of that word falls outside every finding.
   `"Yargıtay bozma ilamı doğrultusunda dosya Kadıköy Cumhuriyet Başsavcılığına
   gönderilmiştir"` previews as `[COURT_AUTHORITY_1]lığına gönderilmiştir`. Measured on
   15,000 adversarial sentences during review: 0 whole misses, 664 such tail fragments,
   none carrying an identifier. Two candidate code fixes were tried and neither closed it
   cleanly, so this is recorded rather than patched.

Point 2 is weaker than "every authority gets its own finding", and deliberately so: the
Yargıtay/Danıştay rule sweeps up to 60 characters past the institution name, so an
authority named inside that sweep (`Ümraniye Kaymakamlığı` in `"Danıştay içtihadına göre
Ümraniye Kaymakamlığı işlemi iptal edilmiş ve Kadıköy Valiliği yeni bir karar almıştır"`)
is covered by the Danıştay finding rather than by one of its own. It is still redacted.
`CourtAuthoritySuffixSuppressionTests` asserts coverage by finding *span*, not by `sample`
substring, so it cannot pass on a truncated fragment that merely contains the word.

Three *true* positives were also removed (`Mahkemesi`, `İcra Müdürlüğü`, `MAHKEMESİ` —
bare-suffix fragments of `4. Asliye Ticaret Mahkemesi`, `12. İcra Müdürlüğü` and
`ANKARA 7. ASLİYE HUKUK MAHKEMESİ`); each of those gold labels is still matched by the
enclosing name-anchored finding, which is why recall stayed 1.0.

That gap is closed: every `court_or_authority` rule — the generic institution-suffix rule
included — now carries the list-gated place prefix, so `Kadıköy` in `Kadıköy Cumhuriyet
Başsavcılığı` is inside the finding and the preview renders `[COURT_AUTHORITY_1]` with no
city beside it. What remains is a bounded-list gap rather than a rule gap: a real Turkish
place that `legal_analyzer/turkish_places.py` does not list still falls outside the span,
and the repair is to add the place. See that module's docstring for the residual.

**The 4 remaining `court_or_authority` false positives are gold-set gaps, not rule
defects** — three of them are real authorities the gold set simply does not label in that
document. *(Since labelled where they still occur; `11. Hukuk Dairesi` no longer reports
separately. Only the `UYAP` over-reach remains; see "Labelling gaps closed" above.)*

- `Cumhuriyet Başsavcılığı'na sunulmak üzere hazırlanmıştır` (`synthetic_pleading`, which has no `court_or_authority` label at all)
- `11. Hukuk Dairesi` (`synthetic_commercial_case` labels only `Yargıtay`)
- `Kadıköy 2. Noterliği` (`power_of_attorney` labels only `24. Noterliği`)
- `UYAP No: 2026/9981 kaydı ile dosyalanmıştır` — the one genuine over-reach left. `UYAP` is a trigger in the generic rule, so a docket reference is reported as an authority. Not fixed here: removing `UYAP` from the trigger list is a recall change on documents outside the gold set, and the docket number itself is already caught at HIGH by `case_or_investigation_number`.

### Accepted with reason: `party_role` (20 FPs, unchanged)

`party_role` was **not** tightened. Three reasons, in order of weight:

1. **The gold set contains zero `party_role` labels.** Every `party_role` finding is
   therefore a false positive *by construction of the scoring* — `label_matches_finding()`
   requires the categories to be equal, so no `party_role` finding can ever match
   anything. No amount of regex tightening moves this rule's FP count to zero; only
   deleting the category would, and that is a safety change, not a precision change. The
   absence also contradicts this README's own labelling instructions above, which ask for
   "Party roles such as davacı, davalı, müşteki, şüpheli, sanık, borçlu, and alacaklı" to
   be labelled. **The fix belongs in the gold set, not in `privacy.py`.**
2. **The obvious tightening is blocked by an existing test.** Requiring an identifying
   continuation after the role word (at most one intervening lowercase word, then a
   capitalised token or a digit — note this must be written case-sensitively with
   `(?-i:...)`, since the rule runs under `re.IGNORECASE`, where `[A-ZÇĞİÖŞÜ]` also
   matches lowercase and the whole check is vacuous) yields **zero** matches on
   `"Tanık beyanı alınmış, mağdur ifadesi dosyaya eklenmiştir."`, which
   `tests/test_turkish_legal_detection.py::TurkishLegalDetectionTests::test_detects_witness_and_victim_party_roles`
   asserts *does* produce `party_role`. That test encodes the documented intent of the
   rule: a party role is a re-identification *signal* for a reviewer to weigh, not an
   identifier.
3. **The measured gain is small and the span is already minimal.** That tightening drops
   4 of the 20 gold-set matches (`Şüpheli hakkında suç duyurusu…`, `Davalı tanık
   beyanlarına göre…`, `tanık beyanlarına göre…`, `Tanık listesi ekte sunulmuştur.`) and
   keeps 16, all still scored as false positives — precision 0.7557 → 0.7733. The rule
   already matches the role word *only*, so its samples carry no name and no national id.

Reviewer noise is still the real concern here: 20 MEDIUM `party_role` flags is the single
largest group in the queue. Address it by labelling party roles in the gold set and by
grouping same-category findings in the review UI, not by weakening detection. *(Since
labelled: see "Labelling gaps closed" above.)*

### Out-of-scope observations (not changed)

- **`criminal_allegation` fires on a case number, not an allegation.**
  `Soruşturma No: 2026/123` (`synthetic_pleading`), `Soruşturma No: 2026/815`
  (`criminal_investigation`) and `Sorusturma No : 2026 / 3377` (`ocr_scan_noisy`) are
  docket references already reported at HIGH by `case_or_investigation_number`. The
  `soruşturma|sorusturma` trigger cannot tell "an investigation exists with this number"
  from "this person is accused", and it escalates to CRITICAL. `Soruşturma Bürosu` (part
  of a prosecutor's office name) is the same defect. Worth a rule that requires the
  trigger *not* to be followed by a `no/numarası` + `YYYY/NNN` shape. *(Since fixed: see
  "Docket references are not allegations" above.)*
- **A real address the gold set does not label.**
  `Yoğurtçu Parkı Caddesi No: 27 Daire: 6 Kadıköy` in `lease_agreement` is a genuine
  street address, and is scored as a false positive only because `lease_agreement`'s
  single `address` label is `Fenerbahçe Mahallesi`. This is the last remaining `address`
  FP and it is **not** something to tighten — the gold set should gain a second `address`
  label for that document. *(Since labelled: see "Labelling gaps closed" above.)*
- **`date` (7).** Bare `dd.mm.yyyy` dates in documents whose gold labels include no `date`
  entry. Two documents do label dates, so the category is live; the other five simply do
  not label theirs. *(Since labelled: see "Labelling gaps closed" above.)*
- **`health_data` (3) and `privileged_or_confidential` (3).** Both rules sweep up to
  120/160 characters of trailing clause, so their samples store an unrelated person's name
  and TCKN in cleartext (`hasta Leyla Kaya (TCKN: 66666666660), Özel Marmara
  Hastanesi'nde 22`, `Müvekkil Cemile Doğan (TCKN: 11111111110), Kadıköy 2`). This is the
  same span over-reach that was already fixed for `party_role` and it should be fixed the
  same way, but changing a CRITICAL rule's span is out of scope for a precision pass.
- **`bar_registration_number` (1).** `Sicil No: 123456` in `corporate_resolution` is a
  *company* registry number matched by the widened `sicil no` alternative, not a bar
  registration. *(Since fixed: the trade-registry rule claims it, and it is now labelled
  as one; see "Labelling gaps closed" above.)*
