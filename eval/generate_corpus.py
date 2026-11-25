# -*- coding: utf-8 -*-
"""Generate the held-out evaluation corpus under ``eval/corpus``.

Everything this writes is invented. No real person, company, matter, address or
account appears anywhere in it, and none is derived from one. National id and
tax numbers carry valid check digits because the detector rejects invalid ones
and a corpus of rejects would measure the generator instead of the product --
they are computed from a seeded PRNG, never looked up.

Ground truth is emitted BY THE GENERATOR. It records the offsets at which it
placed each entity, rather than asking the detector what it found or relying on
a human re-reading the finished document. That is the whole point: a corpus
labelled from detector output cannot produce a false negative, which is the
defect ``gold/`` has and this corpus must not inherit. See ``eval/README.md``.

Deterministic: same seed, byte-identical corpus. ``tests/test_accuracy_eval.py``
asserts the committed corpus still matches what this script produces, so a drift
in either direction is a test failure rather than a silent change of ruler.

    python3 eval/generate_corpus.py            # rewrite eval/corpus + eval/labels.json
    python3 eval/generate_corpus.py --check    # verify the committed corpus matches
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
CORPUS_DIR = EVAL_DIR / "corpus"
LABELS_PATH = EVAL_DIR / "labels.json"

# Fixed so the corpus is reproducible. Changing it rewrites every document and
# every number measured against them -- treat it as a corpus version, not a knob.
SEED = 20260918

# ---------------------------------------------------------------- entity pools
# Turkish given names: half deliberately drawn from OUTSIDE the product's 278-name
# gazetteer (Turkish has thousands of given names), half from common ones.
TR_GIVEN_COMMON = ["Mehmet","Ayşe","Emre","Zeynep","Hakan","Elif","Serkan","Burcu",
                   "Kemal","Sibel","Onur","Pınar","Tolga","Derya","Cem","Melis"]
TR_GIVEN_RARE   = ["Şevketcan","Hüdaverdi","Bengisu","Şirin","Alptuğ","Nazende",
                   "Kutluhan","Sıla","Tunahan","Ilgaz","Peri","Devrim",
                   "Yağızhan","Zerda","Ömür","Şafak"]
TR_SURNAME = ["Yıldırımhan","Özkeskin","Bayraktaroğlu","Gündoğmuş","Akbulutlu",
              "Sarıkayalı","Demirtepe","Eryılmazer","Kocatürkmen","Ünsalcan",
              "Türkoğluer","Çetinkayalı","Aydınlıoğlu","Balcıoğulları","Şahinkaya",
              "Doğrulmaz"]
EN_GIVEN = ["Margaret","Theodore","Priscilla","Nathaniel","Rosalind","Cornelius",
            "Beatrix","Desmond","Wilhelmina","Barnaby","Gwendolyn","Fitzgerald",
            "Adelaide","Montgomery","Henrietta","Percival"]
EN_SURNAME = ["Ashcombe","Pemberton","Harrowgate","Blackwood","Wrenfield",
              "Thistlewood","Corrigan","Fairweather","Ravenscroft","Mortlake",
              "Sandringham","Ellersley","Wynterbourne","Fothergill","Stanbridge",
              "Crowhurst"]

TR_STREET = ["Menekşe Sokak No: {n}","Zafer Caddesi No: {n}","Çınaraltı Mahallesi {n}. Sokak No: {m}",
             "Gülbahar Mahallesi Papatya Sokak No: {n}","Barbaros Bulvarı No: {n} Daire: {m}",
             "Yeşilvadi Caddesi Akasya Apartmanı No: {n}"]
TR_DISTRICT = ["Kadıköy / İstanbul","Çankaya / Ankara","Karşıyaka / İzmir","Nilüfer / Bursa",
               "Muratpaşa / Antalya","Selçuklu / Konya"]
EN_STREET = ["{n} Cobblewell Street","{n} Marchmont Avenue, Suite {m}","Unit {m}, {n} Harrow Road",
             "{n} Quillon Lane","Floor {m}, {n} Belvedere Court","{n} Anselm Road"]
EN_CITY = ["London EC2A 4NE, United Kingdom","Manchester M1 3BN, United Kingdom",
           "Bristol BS1 6QA, United Kingdom","Leeds LS1 4AP, United Kingdom",
           "Edinburgh EH2 2ER, United Kingdom","Cardiff CF10 1EP, United Kingdom"]

TR_COMPANY_CORE = ["Mavideniz Lojistik","Akarsu Enerji","Bereketli Tarım","Çelikhan Makine",
                   "Pusula Yazılım","Karaçam Tekstil","Gökkuşağı Gıda","Yalıboyu İnşaat"]
TR_COMPANY_SUFFIX = ["Anonim Şirketi","Limited Şirketi","Ltd. Şti.","A.Ş."]
EN_COMPANY_CORE = ["Harrowgate Maritime","Quillon Analytics","Belvedere Pharmaceuticals",
                   "Stanbridge Logistics","Mortlake Renewables","Crowhurst Foods",
                   "Ravenscroft Media","Fairweather Robotics"]
EN_COMPANY_SUFFIX = ["Limited","Ltd.","LLC","Inc.","PLC"]

TR_COURTS = ["İstanbul Anadolu 3. Asliye Ticaret Mahkemesi","Ankara 12. İş Mahkemesi",
             "İzmir 5. Aile Mahkemesi","Bursa 2. Sulh Hukuk Mahkemesi",
             "Antalya 9. Asliye Hukuk Mahkemesi","Yargıtay 11. Hukuk Dairesi"]
EN_COURTS = ["the High Court of Justice, Business and Property Courts of England and Wales",
             "the London Circuit Commercial Court",
             "the Employment Tribunal sitting at Reading",
             "the Court of Appeal (Civil Division)"]

def tckn(rng) -> str:
    """A syntactically valid but wholly invented national id (check digits computed)."""
    while True:
        d = [rng.randint(1, 9)] + [rng.randint(0, 9) for _ in range(8)]
        tenth = ((sum(d[0:9:2]) * 7) - sum(d[1:8:2])) % 10
        eleventh = (sum(d) + tenth) % 10
        s = "".join(map(str, d + [tenth, eleventh]))
        if len(s) == 11:
            return s

def iban(rng) -> str:
    return "TR" + f"{rng.randint(10,99)}" + " " + " ".join(
        f"{rng.randint(1000,9999)}" for _ in range(5)) + f" {rng.randint(10,99)}"

def vkn(rng) -> str:
    """Check-digit-valid but wholly invented VKN (published algorithm)."""
    while True:
        d = [rng.randint(0, 9) for _ in range(9)]
        total = 0
        for i in range(9):
            tmp = (d[i] + 9 - i) % 10
            if tmp == 0:
                continue
            part = (tmp * (2 ** (9 - i))) % 9
            total += 9 if part == 0 else part
        d.append((10 - (total % 10)) % 10)
        if d[0] != 0:
            return "".join(map(str, d))
def mersis(rng): return "0" + "".join(str(rng.randint(0,9)) for _ in range(15))
# Contact details come only from ranges that can never reach a real subscriber
# or a registrable domain -- see "Reserved contact ranges" in eval/README.md.
# Each function still makes the draws it made before the ranges were narrowed,
# so every other entity in the corpus keeps the value it had.
TR_MOBILE_PREFIXES = tuple(f"052{d}" for d in range(10))  # 52x: unallocated
UK_DRAMA_PREFIX = "+44 20 7946 0"                        # Ofcom 020 7946 0000-0999
TR_EMAIL_DOMAIN = "example.org"                          # RFC 2606
EN_EMAIL_DOMAIN = "example.com"                          # RFC 2606
def tr_phone(rng):
    prefix = TR_MOBILE_PREFIXES[rng.randint(10,59) % 10]
    return f"{prefix} {rng.randint(100,999)} {rng.randint(10,99)} {rng.randint(10,99)}"
def uk_phone(rng):
    line = rng.randint(100,999); rng.randint(1000,9999)
    return f"{UK_DRAMA_PREFIX}{line:03d}"
def case_no(rng): return f"{rng.randint(2023,2026)}/{rng.randint(100,9999)}"
def tr_date(rng): return f"{rng.randint(1,28):02d}.{rng.randint(1,12):02d}.{rng.randint(2023,2026)}"
def en_date(rng): return f"{rng.randint(1,28)} {rng.choice(['January','March','April','June','September','November'])} {rng.randint(2023,2026)}"
def money_tr(rng): return f"{rng.randint(50,990)}.{rng.randint(100,999)} TL"
def money_en(rng): return f"£{rng.randint(50,990)},{rng.randint(100,999)}"
def passport(rng): return f"U{rng.randint(10000000,99999999)}"

def tr_person(rng, rare=False):
    pool = TR_GIVEN_RARE if rare else TR_GIVEN_COMMON
    return f"{rng.choice(pool)} {rng.choice(TR_SURNAME)}"
def en_person(rng):
    return f"{rng.choice(EN_GIVEN)} {rng.choice(EN_SURNAME)}"
def tr_company(rng):
    return f"{rng.choice(TR_COMPANY_CORE)} {rng.choice(TR_COMPANY_SUFFIX)}"
def en_company(rng):
    return f"{rng.choice(EN_COMPANY_CORE)} {rng.choice(EN_COMPANY_SUFFIX)}"
def tr_address(rng):
    return f"{rng.choice(TR_STREET).format(n=rng.randint(1,180), m=rng.randint(1,20))}, {rng.choice(TR_DISTRICT)}"
def en_address(rng):
    return f"{rng.choice(EN_STREET).format(n=rng.randint(1,180), m=rng.randint(1,20))}, {rng.choice(EN_CITY)}"
def tr_email(rng, person):
    g = person.split()[0].lower().translate(str.maketrans("çğıöşüİ","cgiosui"))
    s = person.split()[-1].lower().translate(str.maketrans("çğıöşüİ","cgiosui"))
    return f"{g}.{s}@{rng.choice(['ornekhukuk','burohukuk','sentetikofis'])}.{TR_EMAIL_DOMAIN}"
def en_email(rng, person):
    g, s = person.split()[0].lower(), person.split()[-1].lower()
    return f"{g}.{s}@{rng.choice(['exampleco','sampletrust','fictionlaw'])}.{EN_EMAIL_DOMAIN}"

# ------------------------------------------------------------------- assembly
class Doc:
    """Builds text from segments; every labelled segment records its exact offsets."""
    def __init__(self, doc_id, lang, kind):
        self.id, self.lang, self.kind = doc_id, lang, kind
        self.parts, self.labels, self.pos = [], [], 0
    def t(self, s):                      # plain text, unlabelled
        self.parts.append(s); self.pos += len(s); return self
    def e(self, category, s):            # labelled entity
        self.labels.append({"category": category, "text": s,
                            "start": self.pos, "end": self.pos + len(s)})
        self.parts.append(s); self.pos += len(s); return self
    def text(self): return "".join(self.parts)
    def as_dict(self):
        return {"id": self.id, "lang": self.lang, "kind": self.kind,
                "path": f"{self.id}.txt", "labels": self.labels}

# ------------------------------------------------------- Turkish templates (10)
def tr_petition(i, rng):
    d = Doc(f"tr_petition_{i}", "tr", "pleading")
    court = rng.choice(TR_COURTS); plaintiff = tr_person(rng, i % 2 == 0)
    defendant = tr_company(rng); lawyer = tr_person(rng, i % 2 == 1)
    d.t(""); d.e("court_or_authority", court.upper()); d.t("'NE\n\n")
    d.t("DOSYA NO   : "); d.e("case_or_investigation_number", f"Esas No: {case_no(rng)}"); d.t("\n")
    d.t("DAVACI     : "); d.e("natural_person_name", plaintiff)
    d.t(" (T.C. Kimlik No: "); d.e("turkish_national_id", tckn(rng)); d.t(")\n")
    d.t("ADRES      : "); d.e("address", tr_address(rng)); d.t("\n")
    d.t("VEKİLİ     : Av. "); d.e("natural_person_name", lawyer); d.t(" (")
    d.e("bar_registration_number", f"Baro Sicil No: {rng.randint(10000,99999)}"); d.t(")\n")
    d.t("E-POSTA    : "); d.e("email_address", tr_email(rng, lawyer)); d.t("\n")
    d.t("TELEFON    : "); d.e("phone_number", tr_phone(rng)); d.t("\n")
    d.t("DAVALI     : "); d.e("company_name", defendant); d.t("\n")
    d.t("             Vergi Kimlik No: "); d.e("tax_number", vkn(rng)); d.t("\n\n")
    d.t("KONU: Alacak davası hakkındadır.\n\nAÇIKLAMALAR\n\n")
    d.t("1. Müvekkil ile davalı şirket arasında "); d.e("date", tr_date(rng))
    d.t(" tarihinde bir tedarik sözleşmesi akdedilmiştir.\n")
    d.t("2. Sözleşme uyarınca müvekkile ödenmesi gereken "); d.e("money_amount", money_tr(rng))
    d.t(" tutarındaki bedel bugüne kadar ödenmemiştir.\n")
    d.t("3. Ödemenin yapılması için "); d.e("date", tr_date(rng))
    d.t(" tarihinde ihtarname keşide edilmiş, sonuç alınamamıştır.\n")
    d.t("4. Ödemenin yapılacağı hesap: "); d.e("iban", iban(rng)); d.t("\n\n")
    d.t("SONUÇ VE İSTEM: Yukarıda açıklanan nedenlerle davanın kabulüne karar verilmesini arz ederim.\n\n")
    d.t("Davacı Vekili\nAv. "); d.e("natural_person_name", lawyer); d.t("\n")
    return d

def tr_contract(i, rng):
    d = Doc(f"tr_contract_{i}", "tr", "contract")
    a, b = tr_company(rng), tr_company(rng)
    sig_a, sig_b = tr_person(rng, True), tr_person(rng, False)
    d.t("TEDARİK SÖZLEŞMESİ\n\nMADDE 1 - TARAFLAR\n\n")
    d.t("İşbu sözleşme, "); d.e("company_name", a); d.t(" (bundan sonra \"Tedarikçi\") ile ")
    d.e("company_name", b); d.t(" (bundan sonra \"Alıcı\") arasında ")
    d.e("date", tr_date(rng)); d.t(" tarihinde akdedilmiştir.\n\n")
    d.t("Tedarikçi adresi: "); d.e("address", tr_address(rng)); d.t("\n")
    d.t("Tedarikçi MERSİS No: "); d.e("mersis_number", mersis(rng)); d.t("\n")
    d.t("Alıcı adresi    : "); d.e("address", tr_address(rng)); d.t("\n")
    d.t("Alıcı Vergi No  : "); d.e("tax_number", vkn(rng)); d.t("\n\n")
    d.t("MADDE 2 - BEDEL\n\nSözleşme bedeli "); d.e("money_amount", money_tr(rng))
    d.t(" olup, ödeme aşağıdaki hesaba yapılacaktır:\n")
    d.t("IBAN: "); d.e("iban", iban(rng)); d.t("\n\n")
    d.t("MADDE 3 - İLETİŞİM\n\nTedarikçi yetkilisi: "); d.e("natural_person_name", sig_a)
    d.t(", "); d.e("phone_number", tr_phone(rng)); d.t(", "); d.e("email_address", tr_email(rng, sig_a)); d.t("\n")
    d.t("Alıcı yetkilisi   : "); d.e("natural_person_name", sig_b)
    d.t(", "); d.e("phone_number", tr_phone(rng)); d.t(", "); d.e("email_address", tr_email(rng, sig_b)); d.t("\n\n")
    d.t("MADDE 4 - UYUŞMAZLIK\n\nUyuşmazlık halinde "); d.e("court_or_authority", rng.choice(TR_COURTS))
    d.t(" yetkilidir.\n\nİMZALAR\n\n")
    d.e("natural_person_name", sig_a); d.t("\t\t\t"); d.e("natural_person_name", sig_b); d.t("\n")
    return d

def tr_hearing(i, rng):
    d = Doc(f"tr_hearing_{i}", "tr", "hearing_minutes")
    judge, clerk = tr_person(rng, True), tr_person(rng, False)
    w1, w2, w3 = tr_person(rng, False), tr_person(rng, True), tr_person(rng, True)
    d.e("court_or_authority", rng.choice(TR_COURTS)); d.t("\nDURUŞMA TUTANAĞI\n\n")
    d.t("ESAS NO: "); d.e("case_or_investigation_number", f"Esas No: {case_no(rng)}"); d.t("\n")
    d.t("Hakim: "); d.e("natural_person_name", judge); d.t("\n")
    d.t("Katip: "); d.e("natural_person_name", clerk); d.t("\n")
    d.t("Duruşma Tarihi: "); d.e("date", tr_date(rng)); d.t("\n\n")
    d.t("Tanıklar:\n")
    d.t("1) "); d.e("natural_person_name", w1); d.t("\n")
    d.t("2) "); d.e("natural_person_name", w2); d.t("\n")
    d.t("3) "); d.e("natural_person_name", w3); d.t("\n\n")
    d.t("Celse açıldı. Tanık "); d.e("natural_person_name", w1)
    d.t(" dinlendi; beyanında olay tarihinde "); d.e("address", tr_address(rng))
    d.t(" adresinde bulunduğunu belirtti.\n")
    d.t("Tanığın telefon numarası "); d.e("phone_number", tr_phone(rng)); d.t(" olarak zapta geçirildi.\n")
    d.t("Bir sonraki celsenin "); d.e("date", tr_date(rng)); d.t(" tarihine bırakılmasına karar verildi.\n\n")
    d.t("İmza\n"); d.e("natural_person_name", judge); d.t("\n")
    return d

def tr_enforcement(i, rng):
    d = Doc(f"tr_enforcement_{i}", "tr", "enforcement")
    debtor, creditor = tr_person(rng, True), tr_company(rng)
    d.t("T.C.\n"); d.e("court_or_authority", f"{rng.choice(['KADIKÖY','ÇANKAYA','KONAK','NİLÜFER'])} {rng.randint(1,20)}. İcra Müdürlüğü")
    d.t("\n\nÖDEME EMRİ\n\n")
    d.t("Takip No : "); d.e("case_or_investigation_number", f"Takip No: {case_no(rng)}"); d.t("\n")
    d.t("Alacaklı : "); d.e("company_name", creditor); d.t("\n")
    d.t("Borçlu   : "); d.e("natural_person_name", debtor)
    d.t("  T.C. Kimlik No "); d.e("turkish_national_id", tckn(rng)); d.t("\n")
    d.t("Adres    : "); d.e("address", tr_address(rng)); d.t("\n")
    d.t("Borç     : "); d.e("money_amount", money_tr(rng)); d.t("\n")
    d.t("Vade     : "); d.e("date", tr_date(rng)); d.t("\n\n")
    d.t("Yukarıda yazılı borcu ödemeniz, aksi halde cebri icraya devam olunacağı ihtar olunur.\n")
    d.t("Ödeme için hesap numarası: "); d.e("iban", iban(rng)); d.t("\n")
    d.t("Bilgi için: "); d.e("phone_number", tr_phone(rng)); d.t(" / ")
    d.e("email_address", f"icra.kalemi@sentetikadalet.{TR_EMAIL_DOMAIN}"); d.t("\n")
    return d

def tr_employment(i, rng):
    d = Doc(f"tr_employment_{i}", "tr", "employment")
    emp, hr = tr_person(rng, False), tr_person(rng, True)
    co = tr_company(rng)
    d.e("company_name", co); d.t("\nİNSAN KAYNAKLARI MÜDÜRLÜĞÜ\n\nFESİH BİLDİRİMİ\n\n")
    d.t("Sayın "); d.e("natural_person_name", emp); d.t(",\n\n")
    d.t("Şirketimizde "); d.e("date", tr_date(rng)); d.t(" tarihinden bu yana ")
    d.t("çalışmakta olduğunuz görevinize, iş sözleşmenizin ")
    d.e("date", tr_date(rng)); d.t(" tarihi itibarıyla son verilmiştir.\n\n")
    d.t("Sicil numaranız: "); d.e("bar_registration_number", f"Sicil No: {rng.randint(100000,999999)}"); d.t("\n")
    d.t("Kimlik numaranız: "); d.e("turkish_national_id", tckn(rng)); d.t("\n")
    d.t("Kayıtlı adresiniz: "); d.e("address", tr_address(rng)); d.t("\n\n")
    d.t("Kıdem ve ihbar tazminatı olarak "); d.e("money_amount", money_tr(rng))
    d.t(" tutarındaki ödeme, bildirdiğiniz "); d.e("iban", iban(rng)); d.t(" numaralı hesaba yatırılacaktır.\n\n")
    d.t("Sorularınız için İnsan Kaynakları yetkilisi "); d.e("natural_person_name", hr)
    d.t(" ile "); d.e("phone_number", tr_phone(rng)); d.t(" numarasından ya da ")
    d.e("email_address", tr_email(rng, hr)); d.t(" adresinden iletişime geçebilirsiniz.\n\nSaygılarımızla\n")
    d.e("natural_person_name", hr); d.t("\n")
    return d

def tr_poa(i, rng):
    d = Doc(f"tr_poa_{i}", "tr", "notary")
    principal, agent = tr_person(rng, True), tr_person(rng, False)
    d.t("T.C.\n"); d.e("court_or_authority", f"{rng.choice(['BEŞİKTAŞ','ÇANKAYA','ALSANCAK'])} {rng.randint(1,40)}. Noterliği")
    d.t("\n\nVEKALETNAME\n\n")
    d.t("Yevmiye No: "); d.e("case_or_investigation_number", f"Yevmiye No: {case_no(rng)}"); d.t("\n")
    d.t("Düzenleme Tarihi: "); d.e("date", tr_date(rng)); d.t("\n\n")
    d.t("Vekalet Veren: "); d.e("natural_person_name", principal); d.t("\n")
    d.t("T.C. Kimlik No: "); d.e("turkish_national_id", tckn(rng)); d.t("\n")
    d.t("Adres: "); d.e("address", tr_address(rng)); d.t("\n")
    d.t("Pasaport No: "); d.e("passport_number", passport(rng)); d.t("\n\n")
    d.t("Vekil: Av. "); d.e("natural_person_name", agent); d.t("\n")
    d.t("Baro Sicil No: "); d.e("bar_registration_number", f"Baro Sicil No: {rng.randint(10000,99999)}"); d.t("\n")
    d.t("Büro Adresi: "); d.e("address", tr_address(rng)); d.t("\n\n")
    d.t("Beni temsil etmeye, "); d.e("court_or_authority", rng.choice(TR_COURTS))
    d.t(" nezdinde dava açmaya ve takip etmeye yetkili kılınmıştır.\n\nİmza\n")
    d.e("natural_person_name", principal); d.t("\n")
    return d

def tr_expert(i, rng):
    d = Doc(f"tr_expert_{i}", "tr", "expert_report")
    expert = tr_person(rng, True)
    party = tr_person(rng, False)
    d.t("BİLİRKİŞİ RAPORU\n\n")
    d.t("Bilirkişi: "); d.e("natural_person_name", expert); d.t("\n")
    d.t("Rapor Tarihi: "); d.e("date", tr_date(rng)); d.t("\n")
    d.t("Dosya: "); d.e("case_or_investigation_number", f"Esas No: {case_no(rng)}"); d.t("\n")
    d.t("Mahkeme: "); d.e("court_or_authority", rng.choice(TR_COURTS)); d.t("\n\n")
    d.t("İNCELEME\n\nDosya kapsamında davacı "); d.e("natural_person_name", party)
    d.t(" tarafından sunulan belgeler incelenmiştir. Taşınmazın bulunduğu ")
    d.e("address", tr_address(rng)); d.t(" adresinde keşif yapılmıştır.\n")
    d.t("Taşınmazın rayiç değeri "); d.e("money_amount", money_tr(rng)); d.t(" olarak tespit edilmiştir.\n\n")
    d.t("Bilirkişi ücreti "); d.e("money_amount", money_tr(rng)); d.t(" olup, ")
    d.e("iban", iban(rng)); d.t(" numaralı hesaba yatırılmasını talep ederim.\n\n")
    d.t("Saygılarımla\n"); d.e("natural_person_name", expert); d.t("\n")
    d.t("İletişim: "); d.e("email_address", tr_email(rng, expert)); d.t("\n")
    return d

def tr_kvkk(i, rng):
    d = Doc(f"tr_kvkk_{i}", "tr", "kvkk_request")
    subject = tr_person(rng, True)
    co = tr_company(rng)
    d.e("company_name", co); d.t(" Veri Sorumlusuna\n\nKVKK BAŞVURU FORMU\n\n")
    d.t("Ad Soyad      : "); d.e("natural_person_name", subject); d.t("\n")
    d.t("T.C. Kimlik No: "); d.e("turkish_national_id", tckn(rng)); d.t("\n")
    d.t("Adres         : "); d.e("address", tr_address(rng)); d.t("\n")
    d.t("E-posta       : "); d.e("email_address", tr_email(rng, subject)); d.t("\n")
    d.t("Telefon       : "); d.e("phone_number", tr_phone(rng)); d.t("\n")
    d.t("Başvuru Tarihi: "); d.e("date", tr_date(rng)); d.t("\n\n")
    d.t("6698 sayılı Kanun'un 11. maddesi uyarınca, şahsıma ait kişisel verilerin ")
    d.t("hangi amaçla işlendiğinin tarafıma bildirilmesini talep ederim.\n\n")
    d.t("Talebimin reddi halinde "); d.e("court_or_authority", "Kişisel Verileri Koruma Kurulu")
    d.t("'na şikayette bulunacağımı beyan ederim.\n\nSaygılarımla\n")
    d.e("natural_person_name", subject); d.t("\n")
    return d

def tr_lease(i, rng):
    d = Doc(f"tr_lease_{i}", "tr", "lease")
    landlord, tenant = tr_person(rng, False), tr_person(rng, True)
    d.t("KİRA SÖZLEŞMESİ\n\n")
    d.t("Kiraya Veren: "); d.e("natural_person_name", landlord)
    d.t(", T.C. Kimlik No "); d.e("turkish_national_id", tckn(rng)); d.t("\n")
    d.t("Kiracı: "); d.e("natural_person_name", tenant)
    d.t(", T.C. Kimlik No "); d.e("turkish_national_id", tckn(rng)); d.t("\n\n")
    d.t("Kiralanan Taşınmaz: "); d.e("address", tr_address(rng)); d.t("\n")
    d.t("Kira Başlangıç Tarihi: "); d.e("date", tr_date(rng)); d.t("\n")
    d.t("Aylık Kira Bedeli: "); d.e("money_amount", money_tr(rng)); d.t("\n")
    d.t("Depozito: "); d.e("money_amount", money_tr(rng)); d.t("\n")
    d.t("Ödeme Hesabı: "); d.e("iban", iban(rng)); d.t("\n\n")
    d.t("Kiracı iletişim: "); d.e("phone_number", tr_phone(rng)); d.t(" / ")
    d.e("email_address", tr_email(rng, tenant)); d.t("\n\n")
    d.t("İşbu sözleşme "); d.e("natural_person_name", landlord); d.t(" ile ")
    d.e("natural_person_name", tenant); d.t(" arasında imzalanmıştır.\n")
    return d

def tr_board(i, rng):
    d = Doc(f"tr_board_{i}", "tr", "corporate")
    co = tr_company(rng)
    m1, m2, m3 = tr_person(rng, True), tr_person(rng, False), tr_person(rng, True)
    d.e("company_name", co); d.t("\nYÖNETİM KURULU KARARI\n\n")
    d.t("Karar Tarihi: "); d.e("date", tr_date(rng)); d.t("\n")
    d.t("Karar No: "); d.e("case_or_investigation_number", f"Karar No: {case_no(rng)}"); d.t("\n")
    d.t("MERSİS No: "); d.e("mersis_number", mersis(rng)); d.t("\n")
    d.t("Vergi Kimlik No: "); d.e("tax_number", vkn(rng)); d.t("\n")
    d.t("Şirket Merkezi: "); d.e("address", tr_address(rng)); d.t("\n\n")
    d.t("Toplantıda hazır bulunanlar:\n")
    d.t("- "); d.e("natural_person_name", m1); d.t(" (Başkan)\n")
    d.t("- "); d.e("natural_person_name", m2); d.t(" (Üye)\n")
    d.t("- "); d.e("natural_person_name", m3); d.t(" (Üye)\n\n")
    d.t("Şirket sermayesinin "); d.e("money_amount", money_tr(rng))
    d.t(" tutarında artırılmasına oybirliği ile karar verilmiştir.\n\n")
    d.t("İmzalar\n"); d.e("natural_person_name", m1); d.t("\t\t"); d.e("natural_person_name", m2); d.t("\n")
    return d

# ------------------------------------------------------- English templates (10)
def en_engagement(i, rng):
    d = Doc(f"en_engagement_{i}", "en", "engagement_letter")
    client, partner = en_person(rng), en_person(rng)
    co = en_company(rng)
    d.t("ENGAGEMENT LETTER\n\n")
    d.t("Date: "); d.e("date", en_date(rng)); d.t("\n\n")
    d.t("To: "); d.e("natural_person_name", client); d.t("\n")
    d.t("Of: "); d.e("company_name", co); d.t("\n")
    d.t("Address: "); d.e("address", en_address(rng)); d.t("\n")
    d.t("Email: "); d.e("email_address", en_email(rng, client)); d.t("\n")
    d.t("Telephone: "); d.e("phone_number", uk_phone(rng)); d.t("\n\n")
    d.t("Dear "); d.e("natural_person_name", client); d.t(",\n\n")
    d.t("We are pleased to confirm that this firm will act for you in connection with ")
    d.t("the proceedings before "); d.e("court_or_authority", rng.choice(EN_COURTS))
    d.t(" under claim number "); d.e("case_or_investigation_number", f"Claim No: HC-{case_no(rng)}"); d.t(".\n\n")
    d.t("Our estimated fees for this matter are "); d.e("money_amount", money_en(rng))
    d.t(" plus VAT. An initial payment on account of "); d.e("money_amount", money_en(rng))
    d.t(" is requested.\n\n")
    d.t("The partner responsible for your matter is "); d.e("natural_person_name", partner)
    d.t(", who may be reached at "); d.e("email_address", en_email(rng, partner)); d.t(".\n\n")
    d.t("Yours sincerely,\n\n"); d.e("natural_person_name", partner); d.t("\n")
    return d

def en_spa(i, rng):
    d = Doc(f"en_spa_{i}", "en", "contract")
    seller, buyer = en_company(rng), en_company(rng)
    s1, s2 = en_person(rng), en_person(rng)
    d.t("SHARE PURCHASE AGREEMENT\n\nTHIS AGREEMENT is made on ")
    d.e("date", en_date(rng)); d.t("\n\nBETWEEN:\n\n")
    d.t("(1) "); d.e("company_name", seller); d.t(", a company registered in England and Wales, ")
    d.t("whose registered office is at "); d.e("address", en_address(rng)); d.t(" (the \"Seller\"); and\n\n")
    d.t("(2) "); d.e("company_name", buyer); d.t(", whose registered office is at ")
    d.e("address", en_address(rng)); d.t(" (the \"Buyer\").\n\n")
    d.t("1. CONSIDERATION\n\nThe consideration for the Shares shall be ")
    d.e("money_amount", money_en(rng)); d.t(", payable in cash on Completion.\n\n")
    d.t("2. NOTICES\n\nNotices to the Seller shall be marked for the attention of ")
    d.e("natural_person_name", s1); d.t(" at "); d.e("email_address", en_email(rng, s1)); d.t(".\n")
    d.t("Notices to the Buyer shall be marked for the attention of ")
    d.e("natural_person_name", s2); d.t(" at "); d.e("email_address", en_email(rng, s2)); d.t(".\n\n")
    d.t("3. GOVERNING LAW\n\nAny dispute shall be referred to ")
    d.e("court_or_authority", rng.choice(EN_COURTS)); d.t(".\n\n")
    d.t("SIGNED for and on behalf of the Seller\n\n"); d.e("natural_person_name", s1); d.t("\n\n")
    d.t("SIGNED for and on behalf of the Buyer\n\n"); d.e("natural_person_name", s2); d.t("\n")
    return d

def en_witness(i, rng):
    d = Doc(f"en_witness_{i}", "en", "witness_statement")
    w = en_person(rng); other = en_person(rng)
    d.t("WITNESS STATEMENT OF "); d.e("natural_person_name", w.upper()); d.t("\n\n")
    d.t("Claim No: "); d.e("case_or_investigation_number", f"Claim No: QB-{case_no(rng)}"); d.t("\n")
    d.t("In "); d.e("court_or_authority", rng.choice(EN_COURTS)); d.t("\n\n")
    d.t("I, "); d.e("natural_person_name", w); d.t(", of "); d.e("address", en_address(rng))
    d.t(", WILL SAY AS FOLLOWS:\n\n")
    d.t("1. I am the claimant in these proceedings. I make this statement from my own knowledge.\n\n")
    d.t("2. On "); d.e("date", en_date(rng)); d.t(" I met "); d.e("natural_person_name", other)
    d.t(" at the offices of "); d.e("company_name", en_company(rng)); d.t(".\n\n")
    d.t("3. I was told that an invoice for "); d.e("money_amount", money_en(rng))
    d.t(" had been settled. I later discovered that it had not.\n\n")
    d.t("4. My contact details are "); d.e("phone_number", uk_phone(rng)); d.t(" and ")
    d.e("email_address", en_email(rng, w)); d.t(".\n\n")
    d.t("I believe that the facts stated in this witness statement are true.\n\n")
    d.t("Signed: "); d.e("natural_person_name", w); d.t("\nDated: "); d.e("date", en_date(rng)); d.t("\n")
    return d

def en_settlement(i, rng):
    d = Doc(f"en_settlement_{i}", "en", "settlement")
    a, b = en_person(rng), en_person(rng)
    d.t("SETTLEMENT AGREEMENT\n\nDATED "); d.e("date", en_date(rng)); d.t("\n\nPARTIES\n\n")
    d.t("(1) "); d.e("natural_person_name", a); d.t(" of "); d.e("address", en_address(rng)); d.t(" (\"the Claimant\")\n")
    d.t("(2) "); d.e("company_name", en_company(rng)); d.t(" of "); d.e("address", en_address(rng)); d.t(" (\"the Respondent\")\n\n")
    d.t("1. The Respondent shall pay the Claimant the sum of "); d.e("money_amount", money_en(rng))
    d.t(" within 28 days.\n")
    d.t("2. Payment shall be made to the Claimant's account, sort code 20-00-00, ")
    d.e("account_number", f"account number {rng.randint(10000000,99999999)}"); d.t(".\n")
    d.t("3. The Claimant's solicitor is "); d.e("natural_person_name", b)
    d.t(", reachable on "); d.e("phone_number", uk_phone(rng)); d.t(" or ")
    d.e("email_address", en_email(rng, b)); d.t(".\n")
    d.t("4. This agreement is confidential and shall not be disclosed to any third party.\n\n")
    d.t("SIGNED\n"); d.e("natural_person_name", a); d.t("\t\t"); d.e("natural_person_name", b); d.t("\n")
    return d

def en_demand(i, rng):
    d = Doc(f"en_demand_{i}", "en", "demand_letter")
    debtor, author = en_person(rng), en_person(rng)
    d.t("LETTER BEFORE CLAIM\n\n"); d.e("date", en_date(rng)); d.t("\n\n")
    d.t("To: "); d.e("natural_person_name", debtor); d.t("\n")
    d.t("    "); d.e("address", en_address(rng)); d.t("\n\n")
    d.t("Dear Mr. "); d.e("natural_person_name", debtor.split()[-1]); d.t(",\n\n")
    d.t("We act for "); d.e("company_name", en_company(rng)); d.t(" in relation to unpaid invoices ")
    d.t("totalling "); d.e("money_amount", money_en(rng)); d.t(", due on ")
    d.e("date", en_date(rng)); d.t(".\n\n")
    d.t("Unless payment is received within 14 days we are instructed to issue proceedings in ")
    d.e("court_or_authority", rng.choice(EN_COURTS)); d.t(".\n\n")
    d.t("Please direct any correspondence to "); d.e("natural_person_name", author)
    d.t(" at "); d.e("email_address", en_email(rng, author)); d.t(" or ")
    d.e("phone_number", uk_phone(rng)); d.t(".\n\nYours faithfully,\n\n")
    d.e("natural_person_name", author); d.t("\n")
    return d

def en_board(i, rng):
    d = Doc(f"en_board_{i}", "en", "corporate")
    co = en_company(rng)
    m1, m2, m3 = en_person(rng), en_person(rng), en_person(rng)
    d.e("company_name", co); d.t("\nMINUTES OF A MEETING OF THE BOARD OF DIRECTORS\n\n")
    d.t("Held on "); d.e("date", en_date(rng)); d.t(" at "); d.e("address", en_address(rng)); d.t("\n\n")
    d.t("Present:\n")
    d.t("  "); d.e("natural_person_name", m1); d.t(" (Chair)\n")
    d.t("  "); d.e("natural_person_name", m2); d.t(" (Director)\n")
    d.t("  "); d.e("natural_person_name", m3); d.t(" (Company Secretary)\n\n")
    d.t("1. The Chair reported that the company had received an offer of ")
    d.e("money_amount", money_en(rng)); d.t(" for its interest in ")
    d.e("company_name", en_company(rng)); d.t(".\n")
    d.t("2. It was RESOLVED that "); d.e("natural_person_name", m2)
    d.t(" be authorised to execute the transfer.\n\n")
    d.t("Signed: "); d.e("natural_person_name", m1); d.t("\n")
    return d

def en_employment(i, rng):
    d = Doc(f"en_employment_{i}", "en", "employment")
    emp, hr = en_person(rng), en_person(rng)
    d.t("CONTRACT OF EMPLOYMENT\n\nBETWEEN\n\n")
    d.e("company_name", en_company(rng)); d.t(" (the \"Employer\")\n\nAND\n\n")
    d.e("natural_person_name", emp); d.t(" of "); d.e("address", en_address(rng)); d.t(" (the \"Employee\")\n\n")
    d.t("Start date: "); d.e("date", en_date(rng)); d.t("\n")
    d.t("Salary: "); d.e("money_amount", money_en(rng)); d.t(" per annum\n")
    d.t("National Insurance number: "); d.t(f"QQ {rng.randint(10,99)} {rng.randint(10,99)} {rng.randint(10,99)} C\n")
    d.t("Passport No: "); d.e("passport_number", passport(rng)); d.t("\n")
    d.t("Personal email: "); d.e("email_address", en_email(rng, emp)); d.t("\n")
    d.t("Mobile: "); d.e("phone_number", uk_phone(rng)); d.t("\n\n")
    d.t("Any grievance should be raised with "); d.e("natural_person_name", hr)
    d.t(" in Human Resources.\n\nSigned: "); d.e("natural_person_name", emp); d.t("\n")
    return d

def en_dpa(i, rng):
    d = Doc(f"en_dpa_{i}", "en", "data_processing")
    dpo1, dpo2 = en_person(rng), en_person(rng)
    d.t("DATA PROCESSING AGREEMENT\n\nThis Agreement is entered into on ")
    d.e("date", en_date(rng)); d.t(" between:\n\n")
    d.t("Controller: "); d.e("company_name", en_company(rng)); d.t(", ")
    d.e("address", en_address(rng)); d.t("\n")
    d.t("Processor: "); d.e("company_name", en_company(rng)); d.t(", ")
    d.e("address", en_address(rng)); d.t("\n\n")
    d.t("Controller's Data Protection Officer: "); d.e("natural_person_name", dpo1)
    d.t(", "); d.e("email_address", en_email(rng, dpo1)); d.t("\n")
    d.t("Processor's Data Protection Officer: "); d.e("natural_person_name", dpo2)
    d.t(", "); d.e("email_address", en_email(rng, dpo2)); d.t("\n\n")
    d.t("The Processor shall notify the Controller of any personal data breach without undue delay.\n")
    d.t("Liability under this Agreement is capped at "); d.e("money_amount", money_en(rng)); d.t(".\n")
    return d

def en_hold(i, rng):
    d = Doc(f"en_hold_{i}", "en", "litigation_hold")
    author = en_person(rng)
    c1, c2 = en_person(rng), en_person(rng)
    d.t("LITIGATION HOLD NOTICE - PRIVILEGED AND CONFIDENTIAL\n\n")
    d.t("From: "); d.e("natural_person_name", author); d.t(", General Counsel\n")
    d.t("Date: "); d.e("date", en_date(rng)); d.t("\n")
    d.t("Matter: "); d.e("case_or_investigation_number", f"Claim No: CL-{case_no(rng)}"); d.t("\n\n")
    d.t("Custodians:\n")
    d.t("  1. "); d.e("natural_person_name", c1); d.t(", "); d.e("email_address", en_email(rng, c1)); d.t("\n")
    d.t("  2. "); d.e("natural_person_name", c2); d.t(", "); d.e("email_address", en_email(rng, c2)); d.t("\n\n")
    d.t("You must preserve all documents relating to the dispute with ")
    d.e("company_name", en_company(rng)); d.t(", including those concerning the payment of ")
    d.e("money_amount", money_en(rng)); d.t(" made on "); d.e("date", en_date(rng)); d.t(".\n\n")
    d.t("Questions should be directed to "); d.e("natural_person_name", author)
    d.t(" on "); d.e("phone_number", uk_phone(rng)); d.t(".\n")
    return d

def en_nda(i, rng):
    d = Doc(f"en_nda_{i}", "en", "nda")
    a, b = en_person(rng), en_person(rng)
    d.t("MUTUAL NON-DISCLOSURE AGREEMENT\n\nDated "); d.e("date", en_date(rng)); d.t("\n\n")
    d.t("Party A: "); d.e("company_name", en_company(rng)); d.t(", registered office ")
    d.e("address", en_address(rng)); d.t("\n")
    d.t("Party B: "); d.e("company_name", en_company(rng)); d.t(", registered office ")
    d.e("address", en_address(rng)); d.t("\n\n")
    d.t("Authorised recipients:\n")
    d.t("  - "); d.e("natural_person_name", a); d.t(" ("); d.e("email_address", en_email(rng, a)); d.t(")\n")
    d.t("  - "); d.e("natural_person_name", b); d.t(" ("); d.e("email_address", en_email(rng, b)); d.t(")\n\n")
    d.t("Each party undertakes to keep the Confidential Information secret. ")
    d.t("Breach shall entitle the disclosing party to liquidated damages of ")
    d.e("money_amount", money_en(rng)); d.t(".\n\n")
    d.t("Signed by "); d.e("natural_person_name", a); d.t(" and "); d.e("natural_person_name", b); d.t("\n")
    return d

TR_TEMPLATES = [tr_petition, tr_contract, tr_hearing, tr_enforcement, tr_employment,
                tr_poa, tr_expert, tr_kvkk, tr_lease, tr_board]
EN_TEMPLATES = [en_engagement, en_spa, en_witness, en_settlement, en_demand,
                en_board, en_employment, en_dpa, en_hold, en_nda]

def build() -> tuple[dict, dict[str, str]]:
    """Return (manifest, {filename: text}) for the whole corpus.

    The PRNG is created here rather than at import time so that build() is
    deterministic however many times it is called in one process -- a test that
    builds twice and compares must not get two different corpora.
    """
    rng = random.Random(SEED)
    docs = []
    for rep in range(2):
        for template in TR_TEMPLATES:
            docs.append(template(rep + 1, rng))
        for template in EN_TEMPLATES:
            docs.append(template(rep + 1, rng))

    manifest = {
        "note": (
            "Held-out synthetic evaluation corpus. Offsets are emitted by "
            "eval/generate_corpus.py at the moment each entity is placed. Every name, "
            "company, identifier, address, matter number and amount is invented. This "
            "corpus is a MEASUREMENT set: never tune a detection rule against it."
        ),
        "seed": SEED,
        "documents": [],
    }
    texts: dict[str, str] = {}
    for doc in docs:
        text = doc.text()
        for label in doc.labels:
            # The offsets must slice back out of the text they describe, and a
            # label may not span a line break: privacy.py bounds every rule's
            # trailing context with [^\n] because a span crossing a newline can
            # never be matched against a DOCX part, so a gold label that crossed
            # one would be asking for something the deliverable cannot deliver.
            assert text[label["start"]:label["end"]] == label["text"], (doc.id, label)
            assert "\n" not in label["text"], (doc.id, label)
        texts[f"{doc.id}.txt"] = text
        manifest["documents"].append(doc.as_dict())
    return manifest, texts


def write(manifest: dict, texts: dict[str, str]) -> None:
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in texts.items():
        (CORPUS_DIR / name).write_text(text, encoding="utf-8")
    LABELS_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check(manifest: dict, texts: dict[str, str]) -> list[str]:
    """Differences between what build() produces and what is committed."""
    problems: list[str] = []
    if not LABELS_PATH.exists():
        return [f"{LABELS_PATH} is missing"]
    committed = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    if committed != manifest:
        problems.append("eval/labels.json differs from generate_corpus.py output")
    for name, text in texts.items():
        path = CORPUS_DIR / name
        if not path.exists():
            problems.append(f"missing: {path}")
        elif path.read_text(encoding="utf-8") != text:
            problems.append(f"differs: {path}")
    for path in sorted(CORPUS_DIR.glob("*.txt")):
        if path.name not in texts:
            problems.append(f"orphan: {path}")
    return problems


def summarise(manifest: dict) -> str:
    counts = collections.Counter(
        (doc["lang"], label["category"])
        for doc in manifest["documents"]
        for label in doc["labels"]
    )
    documents = collections.Counter(doc["lang"] for doc in manifest["documents"])
    spans = collections.Counter()
    for (lang, _category), n in counts.items():
        spans[lang] += n
    lines = [
        f"documents: {sum(documents.values())}  (" 
        + ", ".join(f"{lang}={n}" for lang, n in sorted(documents.items())) + ")",
        f"labelled spans: {sum(spans.values())}  ("
        + ", ".join(f"{lang}={n}" for lang, n in sorted(spans.items())) + ")",
    ]
    for key in sorted(counts):
        lines.append(f"  {key[0]}  {key[1]:30s} {counts[key]}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed corpus matches this generator instead of rewriting it",
    )
    args = parser.parse_args()

    manifest, texts = build()
    if args.check:
        problems = check(manifest, texts)
        if problems:
            print("eval corpus is out of date with eval/generate_corpus.py:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            print("\nRegenerate with: python3 eval/generate_corpus.py", file=sys.stderr)
            raise SystemExit(1)
        print("eval corpus matches eval/generate_corpus.py")
        print(summarise(manifest))
        return

    write(manifest, texts)
    print(f"wrote {len(texts)} documents to {CORPUS_DIR} and {LABELS_PATH}")
    print(summarise(manifest))


if __name__ == "__main__":
    main()
