"""Write the Turkish contract, enforcement-file and notarial-notice practice samples.

The three ``.docx`` files this writes are committed under ``samples/documents/``,
so running the app or the tests never needs this script. It exists so the text of
each sample is reviewable here rather than only inside a binary, and so a change
to a sample is a change to this file. ``tests/test_onboarding.py`` fails when a
committed file no longer extracts to the text below.

Rebuild (python-docx is needed only here, not by the app):

    pip install python-docx
    python3 samples/build_turkish_samples.py

Everything in these samples is invented: the parties, the company, the court,
the enforcement office, the notary, the addresses, the vehicle and the property.
They are the same fictional practice as ``turkish_petition.docx`` -- Masalkent,
MASAL KUTUSU ÖRNEK ANONİM ŞİRKETİ, Deniz Kurgusal and Av. Ekin Düşsel -- so the
Synthetic practice matter reads as one file. National and tax identifiers carry
valid check digits and were generated at random (see ``samples/README.md`` for
what that does and does not mean); phone numbers come from the reserved ranges
in ``eval/README.md``, emails from ``example.com`` / ``example.org``. The IBANs
carry a valid checksum under bank code 00999, and the plate's province code 99
does not exist.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

DOCUMENTS = Path(__file__).resolve().parent / "documents"

# (style, text) per paragraph. "Title" and "Heading 1" are the styles
# turkish_petition.docx uses; everything else is "Normal".
TITLE, HEADING, BODY = "Title", "Heading 1", "Normal"

NUMBERS_NOTE = (
    "Kimlik ve vergi numaraları gösterim amacıyla rastgele üretilmiştir ve yalnızca uydurma "
    "taraflarla eşleştirilmiştir. Hiçbir gerçek kişi veya şirketle ilişkilendirmedik. Kontrol "
    "basamaklarının geçerli olması numaranın kullanılmadığını kanıtlamaz; gerçek bir numarayla "
    "tesadüfi eşleşme dışlanamaz."
)

SAMPLES: dict[str, list[tuple[str, str]]] = {
    "turkish_contract.docx": [
        (TITLE, "Tamamen kurgusal hizmet sözleşmesi"),
        (BODY, "SENTETİK ÖRNEK / SYNTHETIC SAMPLE. Bu belge eğitim için uydurulmuştur. Kişiler, "
               "şirketler, adres ve olaylar gerçek değildir. Hukuki tavsiye veya kullanılabilir bir "
               "sözleşme değildir."),
        (BODY, "DANIŞMANLIK HİZMET SÖZLEŞMESİ"),
        (HEADING, "Madde 1 – Taraflar"),
        (BODY, "1.1. Hizmet Alan: MASAL KUTUSU ÖRNEK ANONİM ŞİRKETİ"),
        (BODY, "Adres: Hayal Mahallesi, Bulut Sokak No: 8, Masalkent (uydurma adres)"),
        (BODY, "Ticaret Sicil No: 48213 (uydurma sicil)"),
        (BODY, "MERSİS No: 0420187039924771 (rastgele gösterim numarası)"),
        (BODY, "VKN: 8072234647 (rastgele gösterim numarası)"),
        (BODY, "Telefon: (0299) 412 35 80"),
        (BODY, "E-posta: hukuk@masalkutusu.example.com"),
        (BODY, "Şirketi temsile yetkili: Selin Yıldızsoy, TCKN: 50732413432 (rastgele gösterim numarası)"),
        (BODY, "1.2. Hizmet Veren: Ekin Rüzgarlı"),
        (BODY, "TCKN: 46197065378 (rastgele gösterim numarası)"),
        (BODY, "Adres: Gökkuşağı Mahallesi, Ay Işığı Caddesi No: 21 Daire: 4, Masalkent (uydurma adres)"),
        (BODY, "Cep telefonu: (0527) 318 44 90"),
        (BODY, "E-posta: ekin.ruzgarli@example.org"),
        (BODY, "IBAN: TR02 0099 9007 4839 5184 4904 08"),
        (HEADING, "Madde 2 – Konu"),
        (BODY, "Bu sözleşmenin konusu, Hizmet Veren'in Hizmet Alan'a kurgusal bir kurumsal arşiv "
               "düzenleme projesi için danışmanlık vermesidir."),
        (HEADING, "Madde 3 – Bedel ve ödeme"),
        (BODY, "Aylık hizmet bedeli 42.000 TL olup her ayın beşinci iş gününe kadar Hizmet Veren'in "
               "yukarıda yazılı IBAN hesabına ödenir. Geç ödemede yasal faiz uygulanır."),
        (HEADING, "Madde 4 – Süre ve fesih"),
        (BODY, "Sözleşme 01.10.2026 tarihinde başlar ve bir yıl sürer. Taraflardan her biri otuz gün "
               "önceden yazılı bildirimde bulunarak sözleşmeyi feshedebilir."),
        (HEADING, "Madde 5 – Bildirimler"),
        (BODY, "Taraflar 1. maddede yazılı adreslerini tebligat adresi olarak kabul eder; adres "
               "değişikliği bildirilmedikçe bu adreslere yapılan bildirimler geçerlidir."),
        (HEADING, "Madde 6 – Uyuşmazlıklar"),
        (BODY, "Bu sözleşmeden doğan uyuşmazlıklarda Masalkent mahkemeleri ve icra daireleri yetkilidir."),
        (BODY, "İşbu sözleşme 28.09.2026 tarihinde iki nüsha olarak düzenlenmiştir. Gerçek bir "
               "sözleşme kurulmamıştır."),
        (BODY, "Hizmet Alan adına Selin Yıldızsoy / İmza atılmamıştır"),
        (BODY, "Hizmet Veren Ekin Rüzgarlı / İmza atılmamıştır"),
        (BODY, "MERSİS numarası ve IBAN da uydurulmuştur. " + NUMBERS_NOTE),
    ],
    "turkish_enforcement_file.docx": [
        (TITLE, "Tamamen kurgusal icra dosyası"),
        (BODY, "SENTETİK ÖRNEK / SYNTHETIC SAMPLE. Bu belge eğitim için uydurulmuştur. Kişiler, "
               "şirketler, icra dairesi, adres, araç ve taşınmaz gerçek değildir. Hukuki tavsiye veya "
               "kullanılabilir bir icra evrakı değildir."),
        (BODY, "MASALKENT 4. İCRA DAİRESİ"),
        (BODY, "Dosya No: 2026/1873"),
        (HEADING, "Ödeme emri (ilamsız takiplerde, Örnek No: 7)"),
        (BODY, "Alacaklı: MASAL KUTUSU ÖRNEK ANONİM ŞİRKETİ"),
        (BODY, "VKN: 8072234647 (rastgele gösterim numarası)"),
        (BODY, "Alacaklı Vekili: Av. Ekin Düşsel, Masalkent Barosu Sicil No: 40517"),
        (BODY, "Borçlu: Arda Pusulacı"),
        (BODY, "TCKN: 44703184674 (rastgele gösterim numarası)"),
        (BODY, "Borçlu Adresi: Yıldız Mahallesi, Fener Sokak No: 3 Daire: 7, Masalkent (uydurma adres)"),
        (BODY, "Borçlu Telefonu: (0522) 604 17 38"),
        (BODY, "Alacak: 86.400 TL asıl alacak ve 3.250 TL işlemiş faiz"),
        (BODY, "Alacağın Nedeni: 14.03.2026 tarihli kurgusal faturaya dayanan cari hesap alacağı"),
        (BODY, "Borçlunun bu ödeme emrinin tebliğinden itibaren yedi gün içinde borca itiraz etmesi, "
               "itiraz etmezse on gün içinde borcu ödemesi gerekir. Aksi halde takibe devam olunur."),
        (BODY, "Ödeme yapılacak hesap: Masalkent 4. İcra Dairesi, IBAN: TR16 0099 9010 5107 1673 5243 26"),
        (HEADING, "Haciz talebi"),
        (BODY, "2026/1873 Esas sayılı dosyada borçluya ait 99 ZZ 1234 plakalı araç ile Masalkent, "
               "Yıldız Mahallesi, 1417 ada, 23 parsel sayılı taşınmaz üzerine haciz konulmasını talep ederiz."),
        (BODY, "Tapu kaydında taşınmaz Ada: 1417 Parsel: 23 olarak geçmektedir."),
        (BODY, "Takip tarihi: 05.10.2026. Bu takip yalnızca inceleme alıştırması için yazılmıştır; "
               "gerçek bir takip açılmamıştır."),
        (BODY, "Alacaklı Vekili Av. Ekin Düşsel / İmza atılmamıştır"),
        (BODY, "IBAN, plaka ve tapu bilgileri de uydurulmuştur; 99 il kodu yoktur. " + NUMBERS_NOTE),
    ],
    "turkish_notary_notice.docx": [
        (TITLE, "Tamamen kurgusal noter ihtarnamesi"),
        (BODY, "SENTETİK ÖRNEK / SYNTHETIC SAMPLE. Bu belge eğitim için uydurulmuştur. Kişiler, "
               "noterlik, adres ve olaylar gerçek değildir. Hukuki tavsiye veya kullanılabilir bir "
               "ihtarname değildir."),
        (BODY, "MASALKENT 7. NOTERLİĞİ"),
        (BODY, "Yevmiye No: 04512"),
        (BODY, "Tarih: 03.09.2026"),
        (HEADING, "İhtarname"),
        (BODY, "Keşideci: Deniz Kurgusal"),
        (BODY, "TCKN: 10757999748 (rastgele gösterim numarası)"),
        (BODY, "Adres: Hayal Mahallesi, Bulut Sokak No: 8, Masalkent (uydurma adres)"),
        (BODY, "Keşideci Vekili: Av. Ekin Düşsel, Masalkent Barosu Sicil No: 40517"),
        (BODY, "Muhatap: Arda Pusulacı"),
        (BODY, "TCKN: 44703184674 (rastgele gösterim numarası)"),
        (BODY, "Muhatap Adresi: Yıldız Mahallesi, Fener Sokak No: 3 Daire: 7, Masalkent (uydurma adres)"),
        (BODY, "Konu: Ödenmeyen kira bedeli nedeniyle temerrüt ihtarıdır."),
        (BODY, "Açıklamalar: Keşideci ile muhatap arasında Masalkent, Yıldız Mahallesi, 1417 ada, "
               "23 parsel sayılı taşınmazdaki 7 numaralı bağımsız bölüm için 01.02.2025 başlangıç "
               "tarihli bir kira sözleşmesi bulunmaktadır. Muhatap, Ağustos 2026 dönemine ait 18.500 TL "
               "kira bedelini ödememiştir. İşbu ihtarnamenin tebliğinden itibaren otuz gün içinde bu "
               "bedelin TR30 0099 9062 3480 2327 5342 04 numaralı hesaba ödenmesini, aksi halde tahliye "
               "için yasal yollara başvurulacağını ihtar ederiz."),
        (BODY, "Sayın Noter; üç nüsha olarak düzenlenen bu ihtarnamenin bir nüshasının muhataba "
               "tebliğini, bir nüshasının dairenizde saklanmasını ve tebliğ şerhli bir nüshasının "
               "tarafımıza verilmesini rica ederiz."),
        (BODY, "Keşideci Vekili Av. Ekin Düşsel / İmza atılmamıştır"),
        (HEADING, "Noter şerhi"),
        (BODY, "İşbu ihtarname 04512 yevmiye numarası ile düzenlenmiş ve muhataba tebliğ edilmek üzere "
               "gönderilmiştir. Masalkent 7. Noteri Nehir Ayyıldız / Mühür ve imza yoktur."),
        (BODY, "IBAN ve tapu bilgileri de uydurulmuştur. " + NUMBERS_NOTE),
    ],
}


def sample_text(name: str) -> str:
    """The text extraction is expected to recover from the committed file."""
    return "\n".join(text for _style, text in SAMPLES[name])


def build(name: str, target: Path) -> None:
    import docx  # Imported here so the tests can read SAMPLES without python-docx.

    document = docx.Document()
    # python-docx's default template carries a customXml part. A sample has
    # nothing in it, so it goes rather than adding a part to every sample that
    # the committed files have never carried.
    for rel_id, rel in list(document.part.rels.items()):
        if rel.reltype.endswith("/customXml"):
            del document.part.rels[rel_id]
    for style, text in SAMPLES[name]:
        document.add_paragraph(text, style=style)
    properties = document.core_properties
    properties.author = properties.last_modified_by = "Legal Redact synthetic samples"
    properties.title = SAMPLES[name][0][1]
    properties.created = properties.modified = datetime(2026, 10, 8, tzinfo=timezone.utc)
    properties.revision = 1
    document.save(target)


def main() -> None:
    for name in SAMPLES:
        build(name, DOCUMENTS / name)
        print(f"wrote {DOCUMENTS / name}")


if __name__ == "__main__":
    main()
