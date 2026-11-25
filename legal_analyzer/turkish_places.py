"""Place words that may begin a Turkish court venue name.

This is a SPAN BOUNDARY signal and nothing else. No DetectionRule reads it, no
finding is ever emitted for a place name on its own, and no risk level is
derived from it: its only job is to tell `privacy._court_start()` where the
venue name begins -- in "Ankara 5. Asliye Hukuk Mahkemesi", in "İstanbul
Cumhuriyet Başsavcılığı", and in "İstanbul Barosu Sicil No: 45678". Every
court_or_authority rule and the bar-registration rule resolve through it.

It exists because a structural capitalised-word prefix cannot tell a place from
an ordinary word, which two review rounds measured from both sides. A two-word
prefix absorbed the word in front of the venue in ordinary prose -- "Müvekkil
Kadıköy 2. Noterliği", "Dosya Ankara 5. Asliye Hukuk", "Ayrıca Beşiktaş 1.
Asliye Ticaret Mahkemesi" -- and narrowing it to one word did not fix that: it
only moved the failure to venues carrying no place word at all, where "Ayrıca 5.
Asliye Hukuk Mahkemesi", "Dosya 5. ..." and "Karar 5. ..." still came out as
three different venues. Both shapes cost more than the over-redaction, because
`_placeholder_for` keys on the whole sample: one venue then gets a different
pseudonym at every mention, which actively misleads a reader following the
document, and the absorbed word is additionally deleted from the deliverable.
Narrowing also dropped "İstanbul" from the repository's own
`tests/fixtures/sample_commercial_case.txt`.

The residual, stated plainly: a real Turkish place that is NOT in this list is
outside the venue span and still ships in the delivered file, exactly as the
whole corpus did before the list existed. That is a bounded-list maintenance
gap, not a hidden hole -- the held-out measurement harness can quantify it, and
the repair is to add the place here FROM THE DIRECTORY CITED BELOW, never from
the held-out corpus.

That distinction is load-bearing, and there is a live case proving it was kept:
`eval/corpus/tr_poa_2.txt` names "ALSANCAK 27. Noterliği", and after sourcing
DISTRICTS from the Ministry's directory that is STILL a full-span miss --
"ALSANCAK" is inside no finding and ships. It stays that way on purpose.
Alsancak is a mahalle inside Konak district, one level below the level at which
judicial locations are designated: zero of the directory's entries is
"Alsancak", "Konak" is one of them, and the office the corpus labels is
officially "İzmir 27. Noterliği". A list fitted to the held-out corpus would
have closed that miss; this one does not, which is what keeps that corpus a
measurement of the product rather than of our reaction to it. The same gap also silently disarms the
venue-split guard in `privacy._assign_grouped_placeholders`, which counts the
places actually CAPTURED: two courthouses in different cities collapse into one
pseudonym when the list holds only one of the two names. So a missing place
costs a leak AND a wrong identity, not just a leak. There is deliberately no positional,
line-position or word-count fallback: two rounds measured that approach
absorbing ordinary words, and a third found it reaching past a place the list
had already recognised ("Sayın Ankara 5. Asliye Hukuk Mahkemesi").

The SECOND residual points the other way. Many judicial locations are also
ordinary Turkish surnames, so an entry appearing BARE immediately before a venue
used to be absorbed into the court span: "Rapor Av. Selim Çınar tarafından
hazırlandı. Çınar 5. Asliye Hukuk Mahkemesine sundu" gave natural_person_name
"Selim Çınar" -> [PERSON_1] and a court finding starting at the second "Çınar",
so ONE PERSON CARRIED TWO PSEUDONYMS in one document.

Collisions measured in this list: Çınar, Kartal, Kavak, Kiraz, Ceyhan, Dicle,
Meriç, Selim, Defne, Can, Bor, Kale, Bahçe, Çay, Adalar, Sakarya, Seyhan; and
intersecting it with the repository's own `turkish_names.GIVEN_NAMES` adds
Defne, Fatih, Nilüfer, Selim, Selçuk.

`privacy._venue_surname_mentions` now gives such a word back to the person when
it is the surname of a multi-word name detected in the same document, and
emits it as that person's natural_person_name finding -- the release and the
finding together, so the word is never handed to nobody. What remains:

  * A bare surname whose full name was never detected establishes nothing and
    is still absorbed into the venue. The word is removed from the deliverable
    either way; what is lost is consistency, not privacy. A name of three or
    more words whose only full mention stands directly before a venue is in
    the same position, because its last word may be a city the detector read
    into it. So is a two-word name ending in one of the 81 PROVINCES.
  * A one-word party name followed by a district that is not a province
    ("Davacı Ali Kadıköy 2. Asliye ...") can still establish that district as
    the party's surname, so a later capitalised "Kadıköy" reads as the party.
    Over-redaction only; nothing ships in cleartext.
  * The mention's sample is one word, so the export removes it only where it
    stands as a capitalised whole word (`docx_redactor.is_standalone_sample`):
    "borcunu", "kalemine" and "can güvenliği" are left as written. A surname
    written lower-case or welded to a suffix without an apostrophe is not
    removed by that finding.
  * A party whose surname IS the venue's district ("Ali Ceyhan ... Ceyhan 1.
    Asliye Hukuk Mahkemesi") has the district read as [PERSON_n] in front of an
    unplaced court. The text alone cannot tell the two apart.
  * Excluding the colliding words from this list is still not available: every
    one is a genuine judicial location ("Çınar 1. Asliye Hukuk Mahkemesi" in
    Diyarbakır is real), so excluding them reopens the defect this list exists
    to close. Do not add a name blocklist.

And the correction a reader will otherwise get wrong: THIS LIST IS NOT THE
CAUSE. The branch's earlier structural {0,2} capitalised-word prefix absorbed
the same word in the same position -- on the probe above, that code yields
court_or_authority "Çınar 5. Asliye Hukuk", identical. The list NARROWED the
class from "any capitalised word" to "only genuine judicial locations". A
future reader who treats the list as the cause will reach for the wrong fix.
"""

from __future__ import annotations

from legal_analyzer.turkish_names import tr_fold

# The judicial locations themselves, sourced from the Ministry of Justice's own
# courthouse directory rather than typed by hand:
#
#   Adli Yargı Rehberi, https://rayp.adalet.gov.tr/resimler/351/dosya/
#   adliye-rehberi08-05-20242-35-pm.pdf  (21 pages)
#
# Its second column, "Mahal / İlçe", is exactly the administrative level at
# which Turkish courts and notaries are designated, so it is the authority on
# which place words can lead a venue name.
#
# Extraction, so the next person can reproduce this rather than trust it:
#
#     doc = pymupdf.open(pdf)
#     rows = [r for page in doc for t in page.find_tables().tables
#             for r in t.extract() if re.fullmatch(r"\d+", (r[0] or "").strip())]
#     mahal = [r[1].strip() for r in rows]          # the "Mahal / İlçe" column
#     assert len(rows) == max(int(r[0]) for r in rows) == 1001
#
# Use `page.find_tables()`, the structured reader -- NOT flattened page text
# with a guess about which lines are places, which silently drops rows.
#
# THE LAST ASSERTION IS THE POINT and must survive any re-derivation: the row
# count has to equal the directory's own maximum "Sıra No". A list derived from
# a document needs a self-test that the derivation was COMPLETE, because a
# lossy parse looks exactly like a shorter directory. Measured here: 1001 rows,
# sequence numbers contiguous 1..1001, zero empty Mahal cells, 1001 distinct
# values. Stripping the parenthetical disambiguators the directory uses for
# duplicate names ("Gölbaşı (Adıyaman)") leaves 977 distinct entries, of which
# the 974 single-word ones are listed here.
#
# The three multi-word entries are deliberately NOT listed, because the lookup
# is per word and a multi-word key could never match: "Ankara Batı" and
# "İstanbul Anadolu" are covered instead by their province plus VENUE_QUALIFIERS
# below, and "Ondokuz Mayıs" (a real Samsun district) is a known uncovered case.
#
# Against the 41 hand-picked names this replaced, the directory is a superset
# except for "Sincan" -- see SOURCE_GAP_PLACES below, which adds it back. That
# set difference was COMPUTED, not eyed: asserting it from a handful of
# remembered names is how the Sincan narrowing shipped in the first place.
# "Hakkari" is present via the directory's "Hakkâri" (the keys fold together).
#
# The 81 provinces are all in here too; PROVINCES below names them separately
# for the one caller that must tell a province from a district, and is not part
# of the venue lookup.
DISTRICTS: frozenset[str] = frozenset(
    """
    Abana Acıgöl Acıpayam Adaklı Adalar Adana Adapazarı Adilcevaz Adıyaman
    Afyonkarahisar Afşin Ahlat Ahmetli Ahırlı Akdağmadeni Akdeniz Akhisar
    Akkuş Akkışla Akpınar Aksaray Akseki Aksu Akyaka Akyazı Akyurt Akçaabat
    Akçadağ Akçakale Akçakent Akçakoca Akören Akıncılar Akşehir Alaca
    Alacakaya Aladağ Alanya Alaplı Alaçam Alaşehir Aliağa Almus Alpu
    Altunhisar Altıeylül Altındağ Altınekin Altınordu Altınova Altıntaş
    Altınyayla Altınözü Alucra Amasra Amasya Anamur Andırın Ankara Antakya
    Antalya Araban Araklı Aralık Arapgir Araç Ardahan Ardanuç Ardeşen Arguvan
    Arhavi Arifiye Armutlu Arnavutköy Arpaçay Arsin Arsuz Artova Artuklu
    Artvin Arıcak Asarcık Aslanapa Atabey Atakum Ataşehir Atkaracalar Avanos
    Avcılar Ayancık Ayaş Aybastı Aydın Aydıncık Aydıntepe Ayrancı Ayvacık
    Ayvalık Azdavay Aziziye Ağaçören Ağlasun Ağlı Ağrı Ağın Aşkale Babadağ
    Babaeski Bafra Bahçe Bahçelievler Bahçesaray Bahşılı Baklan Bakırköy Balya
    Balâ Balçova Balıkesir Balışeyh Banaz Bandırma Bartın Baskil Batman
    Battalgazi Bayat Bayburt Baykan Bayraklı Bayramiç Bayrampaşa Bayramören
    Bayındır Bağcılar Bağlar Başakşehir Başiskele Başkale Başmakçı Başyayla
    Başçiftlik Bekilli Belen Bergama Besni Beyağaç Beydağ Beykoz Beylikdüzü
    Beylikova Beyoğlu Beypazarı Beytüşşebap Beyşehir Beşikdüzü Beşiktaş Beşiri
    Biga Bigadiç Bilecik Bingöl Birecik Bismil Bitlis Bodrum Bolu Bolvadin Bor
    Bornova Borçka Boyabat Bozcaada Bozdoğan Bozkurt Bozkır Bozova Boztepe
    Bozyazı Bozüyük Boğazkale Boğazlıyan Buca Bucak Buharkent Bulancak Bulanık
    Buldan Burdur Burhaniye Bursa Bünyan Büyükorhan Büyükçekmece Canik Ceyhan
    Ceylânpınar Cide Cihanbeyli Cizre Cumayeri Daday Dalaman Damal Darende
    Dargeçit Darıca Datça Dazkırı Defne Delice Demirci Demirköy Demirözü Demre
    Denizli Derbent Derebucak Dereli Derepazarı Derik Derince Derinkuyu
    Dernekpazarı Develi Devrek Devrekâni Dicle Didim Digor Dikili Dikmen
    Dilovası Dinar Divriği Diyadin Diyarbakır Dodurga Domaniç Doğanhisar
    Doğankent Doğanyol Doğanyurt Doğanşar Doğanşehir Doğubayazıt Dulkadiroğlu
    Dumlupınar Durağan Dursunbey Dörtdivan Dörtyol Döşemealtı Düzce Düziçi
    Düzköy Eceabat Edirne Edremit Efeler Eflâni Ekinözü Elazığ Elbeyli
    Elbistan Eldivan Eleşkirt Elmadağ Elmalı Emet Emirdağ Emirgazi Enez Erbaa
    Erciş Erdek Erdemli Erenler Ereğli Erfelek Ergani Ergene Ermenek Eruh
    Erzin Erzincan Erzurum Esenler Esenyurt Eskil Eskipazar Eskişehir Espiye
    Etimesgut Evciler Evren Eynesil Eyyübiye Eyüpsultan Ezine Eğil Eğirdir
    Eşme Fatih Fatsa Feke Felâhiye Ferizli Fethiye Finike Foça Fındıklı
    Gaziantep Gaziemir Gaziosmanpaşa Gazipaşa Gebze Gediz Gelendost Gelibolu
    Gemerek Gemlik Genç Gercüş Gerede Gerger Germencik Gerze Gevaş Geyve
    Giresun Göksun Gökçeada Gökçebey Gölbaşı Gölcük Göle Gölhisar Gölköy
    Gölmarmara Gölova Gölpazarı Gölyaka Gömeç Gönen Gördes Görele Göynücek
    Göynük Güce Güdül Gülağaç Gülnar Gülyalı Gülşehir Gümüşhacıköy Gümüşhane
    Gümüşova Gündoğmuş Güney Güneysu Güneysınır Güngören Günyüzü Gürgentepe
    Güroymak Gürpınar Gürsu Gürün Güzelbahçe Güzelyurt Güçlükonak Hacıbektaş
    Hacılar Hadim Hafik Hakkâri Halfeti Haliliye Halkapınar Hamamözü Hamur Han
    Hanak Hani Hanönü Harmancık Harran Hasanbeyli Hasankeyf Hasköy Hassa Hatay
    Havran Havsa Havza Haymana Hayrabolu Hayrat Hazro Hekimhan Hemşin Hendek
    Hilvan Hisarcık Hizan Hocalar Honaz Hopa Horasan Hozat Hüyük Hınıs Ilgaz
    Ilgın Isparta Iğdır Kabadüz Kabataş Kadirli Kadıköy Kadınhanı Kadışehri
    Kahramankazan Kahramanmaraş Kale Kalecik Kalkandere Kaman Kandıra Kangal
    Kapaklı Karabağlar Karaburun Karabük Karacabey Karacasu Karahallı
    Karaisalı Karakeçili Karakoyunlu Karakoçan Karaköprü Karaman Karamanlı
    Karamürsel Karapürçek Karapınar Karasu Karatay Karataş Karayazı Karaçoban
    Karesi Kargı Karkamış Karlıova Karpuzlu Kars Kartal Kartepe Karşıyaka
    Kastamonu Kavak Kavaklıdere Kayapınar Kaynarca Kaynaşlı Kayseri
    Kazımkarabekir Kağıthane Kağızman Kaş Keban Keles Kelkit Kemah Kemaliye
    Kemalpaşa Kemer Kepez Kepsut Keskin Kestel Keçiborlu Keçiören Keşan Keşap
    Kilimli Kilis Kiraz Kiğı Kocaali Kocaeli Kocaköy Kocasinan Kofçaz Konak
    Konya Konyaaltı Korgan Korgun Korkut Korkuteli Kovancılar Koyulhisar
    Kozaklı Kozan Kozlu Kozluk Koçarlı Kula Kulp Kulu Kuluncak Kumlu Kumluca
    Kumru Kurtalan Kurucaşile Kurşunlu Kuyucak Kuşadası Kâhta Köprübaşı
    Köprüköy Körfez Köse Köyceğiz Köşk Küre Kürtün Kütahya Küçükçekmece
    Kıbrıscık Kınık Kırkağaç Kırklareli Kırıkhan Kırıkkale Kırşehir
    Kızılcahamam Kızıltepe Kızılören Kızılırmak Lalapaşa Lice Lâdik Lâpseki
    Lâçin Lüleburgaz Maden Mahmudiye Malatya Malazgirt Malkara Maltepe Mamak
    Manavgat Manisa Manyas Mardin Marmara Marmaraereğlisi Marmaris Mazgirt
    Mazıdağı Maçka Mecitözü Melikgazi Menderes Menemen Mengen Menteşe Meram
    Meriç Merkezefendi Mersin Merzifon Mesudiye Mezitli Midyat Mihalgazi
    Mihalıççık Milâs Mucur Mudanya Mudurnu Muradiye Muratlı Muratpaşa Murgul
    Musabeyli Mustafakemalpaşa Mut Mutki Muğla Muş Nallıhan Narlıdere Narman
    Nazilli Nazımiye Nevşehir Niksar Nilüfer Nizip Niğde Nurdağı Nurhak
    Nusaybin Odunpazarı Of Oltu Olur Onikişubat Ordu Orhaneli Orhangazi Orta
    Ortaca Ortahisar Ortaköy Osmancık Osmaneli Osmangazi Osmaniye Otlukbeli
    Ovacık Oğuzeli Oğuzlar Palandöken Palu Pamukkale Pamukova Pasinler Patnos
    Payas Pazar Pazarcık Pazarlar Pazaryeri Pazaryolu Pehlivanköy Pendik
    Pertek Pervari Perşembe Piraziz Polateli Polatlı Posof Pozantı Pursaklar
    Pülümür Pütürge Pınarbaşı Pınarhisar Refahiye Reyhanlı Reşadiye Rize
    Safranbolu Saimbeyli Sakarya Salihli Salıpazarı Samandağ Samsat Samsun
    Sancaktepe Sandıklı Sapanca Saray Saraydüzü Saraykent Sarayköy Sarayönü
    Saruhanlı Sarıcakaya Sarıgöl Sarıkamış Sarıkaya Sarıoğlan Sarıveliler
    Sarıyahşi Sarıyer Sarız Sarıçam Sason Savaştepe Savur Seben Seferihisar
    Selendi Selim Selçuk Selçuklu Senirkent Serdivan Serik Serinhisar
    Seydikemer Seydiler Seydişehir Seyhan Seyitgazi Siirt Silifke Silivri
    Silopi Silvan Simav Sinanpaşa Sincik Sinop Sivas Sivaslı Siverek Sivrice
    Sivrihisar Solhan Soma Sorgun Sulakyurt Sultanbeyli Sultandağı Sultangazi
    Sultanhisar Suluova Sulusaray Sumbas Sungurlu Sur Suruç Susurluk Susuz
    Suşehri Söke Söğüt Söğütlü Süleymanpaşa Süloğlu Sürmene Sütçüler Sındırgı
    Talas Taraklı Tarsus Tatvan Tavas Tavşanlı Taşkent Taşköprü Taşlıçay
    Taşova Tefenni Tekirdağ Tekkeköy Tekman Tepebaşı Tercan Termal Terme Tillo
    Tire Tirebolu Tokat Tomarza Tonya Toprakkale Torbalı Toroslar Tortum Torul
    Tosya Trabzon Tufanbeyli Tunceli Turgutlu Turhal Tut Tutak Tuzla Tuzluca
    Tuzlukçu Tuşba Türkeli Türkoğlu Ula Ulaş Ulubey Uluborlu Uludere Ulukışla
    Ulus Urla Uzundere Uzunköprü Uğurludağ Uşak Vakfıkebir Van Varto
    Vezirköprü Viranşehir Vize Yahyalı Yahşihan Yakakent Yakutiye Yalova
    Yalvaç Yalıhüyük Yapraklı Yatağan Yavuzeli Yayladağı Yayladere Yazıhan
    Yağlıdere Yedisu Yenice Yenifakılı Yenimahalle Yenipazar Yeniçağa
    Yenişarbademli Yenişehir Yerköy Yeşilhisar Yeşilli Yeşilova Yeşilyurt
    Yomra Yozgat Yumurtalık Yunak Yunusemre Yusufeli Yüksekova Yüreğir
    Yıldırım Yıldızeli Yığılca Zara Zeytinburnu Zile Zonguldak Çal Çaldıran
    Çamardı Çamaş Çameli Çamlıdere Çamlıhemşin Çamlıyayla Çamoluk Çan
    Çanakkale Çanakçı Çandır Çankaya Çankırı Çardak Çarşamba Çarşıbaşı Çat
    Çatak Çatalca Çatalpınar Çatalzeytin Çavdarhisar Çavdır Çay Çaybaşı
    Çaycuma Çayeli Çaykara Çayıralan Çayırlı Çayırova Çağlayancerit Çekerek
    Çekmeköy Çelebi Çelikhan Çeltik Çeltikçi Çemişgezek Çerkezköy Çerkeş
    Çermik Çeşme Çifteler Çiftlik Çiftlikköy Çilimli Çine Çivril Çiçekdağı
    Çiğli Çobanlar Çorlu Çorum Çubuk Çukurca Çukurova Çumra Çüngüş Çıldır
    Çınar Çınarcık Ödemiş Ömerli Özalp Özvatan Ümraniye Ünye Ürgüp Üsküdar
    Üzümlü İbradı İdil İhsangazi İhsaniye İkizce İkizdere İliç İlkadım
    İmamoğlu İmranlı İncesu İncirliova İnebolu İnegöl İnhisar İnönü İpekyolu
    İpsala İscehisar İskenderun İskilip İslâhiye İspir İstanbul İvrindi
    İyidere İzmir İzmit İznik Şabanözü Şahinbey Şalpazarı Şanlıurfa Şaphane
    Şarkikaraağaç Şarköy Şarkışla Şavşat Şebinkarahisar Şefaatli Şehitkamil
    Şehzadeler Şemdinli Şenkaya Şenpazar Şereflikoçhisar Şile Şiran Şirvan
    Şişli Şuhut Şırnak
    """.split()
)

# The only two province-plus-qualifier designations in the whole directory:
# "İstanbul Anadolu" and "Ankara Batı". A qualifier is part of the venue name
# without being a settlement -- "İstanbul Anadolu 3. Asliye Ticaret Mahkemesi"
# names one court and dropping "Anadolu" names a different one.
#
# "Doğu", "Kuzey", "Güney" and "Merkez" were here too and are dropped: grepping
# the directory, "Kuzey" has zero entries and the other three appear only
# inside settlement NAMES ("Doğubayazıt", "Güney", "Güneysu", "Güneysinir",
# "Merkezefendi"), which DISTRICTS above now covers properly. They are ordinary
# direction words, not court-naming qualifiers; do not re-add them on intuition.
#
# Dropping one of the two that remain costs more than the qualifier: the
# right-to-left walk in `privacy._venue_prefix_start` stops at the first
# non-place token, so without "Batı" the walk halts there and gives back the
# PROVINCE behind it as well -- "Ankara Batı 5. Asliye Hukuk Mahkemesi" then
# delivers "Ankara Batı [COURT_AUTHORITY_1]", leaking both words.
VENUE_QUALIFIERS: frozenset[str] = frozenset({"Anadolu", "Batı"})

# Real judicial locations the directory does NOT list. Kept separate from
# DISTRICTS on purpose: these are hand additions, and the next person has to be
# able to see that they are deliberate rather than unsourced leftovers.
#
# "Sincan" is the one we know of, and it is a fact about the source rather than
# about the parse -- zero of the 1001 structured rows names it. Sincan
# Adliyesi's courts were taken over by Ankara Batı Adliyesi in 2014 and the
# directory lists CURRENT judicial organisation, so the district lost its row
# while Etimesgut (where that courthouse now sits) and "Ankara Batı" both keep
# theirs. That is INCONSISTENT with how the same directory treats other merged
# units: 255 of its 1001 rows carry a "Birleştirildi" (merged) status and keep
# their name; Sincan was dropped instead. Meanwhile Sincan is still a real
# Ankara district of roughly half a million people, and legal documents cite
# historical venues -- "Sincan 2. İcra Müdürlüğü" on a pre-2014 enforcement
# file is an ordinary citation, and without this entry the district name sits
# outside every finding and ships beside the pseudonymised venue.
#
# The bound this implies, stated so the gap is not mistaken for a one-off: the
# directory carries 1001 judicial entries against Turkey's 81 provinces plus
# 973 districts, so roughly 53 administrative units have no judicial row at
# all. Sincan is simply the one a reviewer caught. Add another the same way --
# named here, with the reason -- rather than by widening DISTRICTS, and never
# from the held-out corpus.
SOURCE_GAP_PLACES: frozenset[str] = frozenset({"Sincan"})

# Invented places, used by the bundled sample and the fixture corpus and by
# nothing else. `samples/documents/turkish_petition.docx` -- the document every
# new user opens first, and the objective's own headline example -- names
# "Masalkent 3. Asliye Hukuk Mahkemesi"; the sample declares itself
# "uydurulmuştur", so no real gazetteer can ever hold it and the product's first
# impression would otherwise demonstrate the defect. Venue place words only:
# the sample's street and neighbourhood words ("Hayal", "Bulut") are address
# parts, and listing them would let the prefix absorb them.
SYNTHETIC_CORPUS_PLACES: frozenset[str] = frozenset({"Masalkent"})

# The diacritics tr_fold leaves behind, collapsed to their ASCII letter.
_VENUE_PLACE_DIACRITICS = str.maketrans("ışğçöüâîû", "isgcouaiu")


def _venue_place_key(word: str) -> str:
    """The key a venue place word is looked up under, diacritic-insensitive.

    LAYERS on turkish_names.tr_fold -- it is not a second fold. tr_fold does the
    case folding, including the dotted/dotless pairs an all-caps Turkish heading
    turns on ("İZMİR" lower-cases to "izmir" only there, never under
    str.lower()); this collapses what is left.

    The collapse is needed because this repository's own OCR path produces
    diacritic-stripped text -- tests/fixtures/ocr_scan_noisy.txt opens with
    "IZMIR CUMHURIYET BASSAVCILIGI", and every court and party rule in privacy.py
    already carries its ASCII alternate. Without it "IZMIR 12. İCRA
    MÜDÜRLÜĞÜ" folded to "ızmır", missed the list, and the city shipped beside a
    pseudonymised court: the objective's defect verbatim, on scanned documents.

    Composing rather than pre-mapping the source words is what makes it work.
    Spelling the list in ASCII and folding that does NOT: tr_fold sends an ASCII
    "I" to the dotless "ı", so "Izmir" folds to "ızmir" while OCR's "IZMIR" folds
    to "ızmır" -- they differ in the second letter and the lookup still misses.
    Folding case first and collapsing afterwards sends every spelling of a place
    to one key.
    """
    return tr_fold(word).translate(_VENUE_PLACE_DIACRITICS)


PLACE_TOKENS: frozenset[str] = frozenset(
    _venue_place_key(word)
    for word in DISTRICTS | SOURCE_GAP_PLACES | VENUE_QUALIFIERS | SYNTHETIC_CORPUS_PLACES
)


def is_venue_place_token(word: str) -> bool:
    return _venue_place_key(word) in PLACE_TOKENS


# The 81 provinces (il), each spelled as its own row of the directory above, so
# the set is a subset of DISTRICTS. A province is the place most often read
# into a party's name in front of a venue ("Davacı Ali Ankara 5. Asliye ..."),
# and `privacy._venue_surname_mentions` will not take one for a surname there.
PROVINCES: frozenset[str] = frozenset(
    """
    Adana Adıyaman Afyonkarahisar Ağrı Amasya Ankara Antalya Artvin Aydın
    Balıkesir Bilecik Bingöl Bitlis Bolu Burdur Bursa Çanakkale Çankırı Çorum
    Denizli Diyarbakır Edirne Elazığ Erzincan Erzurum Eskişehir Gaziantep
    Giresun Gümüşhane Hakkâri Hatay Isparta Mersin İstanbul İzmir Kars
    Kastamonu Kayseri Kırklareli Kırşehir Kocaeli Konya Kütahya Malatya Manisa
    Kahramanmaraş Mardin Muğla Muş Nevşehir Niğde Ordu Rize Sakarya Samsun
    Siirt Sinop Sivas Tekirdağ Tokat Trabzon Tunceli Şanlıurfa Uşak Van Yozgat
    Zonguldak Aksaray Bayburt Karaman Kırıkkale Batman Şırnak Bartın Ardahan
    Iğdır Yalova Karabük Kilis Osmaniye Düzce
    """.split()
)

_PROVINCE_KEYS: frozenset[str] = frozenset(_venue_place_key(word) for word in PROVINCES)


def is_province(word: str) -> bool:
    return _venue_place_key(word) in _PROVINCE_KEYS
