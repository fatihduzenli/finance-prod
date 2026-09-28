# Tablo İncelemesi — "Corp Stores" (Google E-Tablolar)

**Hazırlanma tarihi:** 10 Eylül 2026. Canlı Google tablosu, Google Drive bağlantısı üzerinden okundu.
**Tablo:** "Corp Stores". Sahibi: işletme sahibinin kişisel Google hesabı; talep eden kişiyle paylaşılmış. Oluşturulma: 16.01.2025; son değişiklik: 10.09.2026.
**Bu dosyanın amacı:** Prompt 1 için 1 numaralı girdi (kapsam dokümanı + sütun eşleştirmesi). Sahibinin onaylaması gereken konular **❓** ile işaretlendi ve §9'da toplandı.

> Not: Sekme ve sütun adları tablodaki orijinal (İngilizce) hâlleriyle bırakıldı, böylece tabloda kolayca bulunabilirler.

---

## 0. Yöntem ve sınırlar (önce bunu okuyun)

| Görebildiklerim | GÖREMEDİKLERİM |
|---|---|
| 16 sekmenin tamamı, her satır, **görüntülenen değerler** (hücrenin gösterdiği değer) | **Formüller.** Bağlantı yalnızca görüntülenen değerleri veriyor. "Hesaplanan" dediğim yerlerde hesabı örnek satırlarda kontrol ettim; formülün kendisini görmedim. |
| Tam başlık metinleri (çok satırlı başlıklar birleştirildi) | **Düzenleme geçmişi / hangi hücreyi kimin düzenlediği.** Düzenleyenler, not sütunlarının adlarından (Ahmed, Zahir, Fatih) tahmin edildi. |
| Her hücreyi kart/banka bilgisi için taramaya yetecek kadar veri | **Sürüm geçmişi.** Tablonun eski sürümlerinde, sonradan silinmiş veriler hâlâ bulunabilir. |
| | Hücre renkleri ve biçimlendirme (çalışanlar rengi "ödendi" veya "gecikti" anlamında kullanıyorsa, bu anlam bana görünmüyor) ❓ |

Dışa aktarılan metin, içinde virgül bulunan hücreleri (ör. `$2,500.00`, `SEALY, TX`) tırnak içine almıyor. Bu yüzden **aşağıdaki satır ve sütun sayıları yaklaşıktır (±%5)**. Taşıma betiği tabloyu bu metinden değil, Sheets API veya XLSX dışa aktarımı üzerinden okumalı.

---

## 1. İşletmenin yaptığı iş (tablodan çıkarılan) ❓ onaylayın

Şirket, ev sahibi mağazaların içindeki küçük perakende alanların kira sözleşmelerini elinde tutuyor. Bunlar çoğunlukla **Walmart** ("WM RENT"), ayrıca **Kroger**, **Stop & Shop**, **HEB** ve **AVM**'ler. Bu alanlara, aralarında **Techy** ve **Talk n Fix** markalarının da bulunduğu **işletmecileri** (bağımsız işletmeler) yerleştiriyor. Ev sahibi mağazanın kirasını (ör. "WM RENT") kendisi ödüyor ve işletmecilerden şunları tahsil ediyor:

- **Aylık kira** (genellikle mağaza başına 2.000–4.000 $) ve 75 $'lık küçük bir aylık **"RC"** ücreti ❓ anlamı nedir?
- **Tek seferlik / kurulum ücretleri:** tabela (ve mimar), depozito veya erişim ücreti, **ilk yıl sigortası** (565 $; ayrıca 695 $ ve 1.130 $ tutarları da görüldü), işletme ödemesi, pazarlama malzemesi, çeşitli.
- **Yıllık sigorta yenilemeleri** (genellikle 565 $, çoğunlukla haziran ayında alınıyor).
- **Denetim cezaları** (150 $ denetim programı, 2.500 $ cezalar). **IFX-1002 … IFX-1294** numaralarıyla faturalandırılmış (≈247 fatura).
- **Fesih ödemeleri** (kira sözleşmesi sona erdiğinde): fesih ücreti + ödenmemiş kira ± tabela/sigorta düzeltmeleri; örneklerde 22 bin – 47 bin $.

Tahsilat esas olarak **işletmecilerin banka hesaplarından ACH ile çekim** yoluyla yapılıyor; birçok işletmeci için ACH platformu olarak "STAX" yazıyor. Bunun yanında **havale (wire)**, **Zelle** ve **çek** de kullanılıyor. **Geri dönen (başarısız) ACH ödemeleri** ve bunların yeniden işlenmesi, sürekli tekrarlanan büyük bir manuel iş yükü.

Bir işletmeci birden fazla mağaza işletebiliyor ve mağazaların işletmecisi değişebiliyor ("sold to another operator", "ANWAR → asim").

**Ölçek (yaklaşık):** Rents sekmesinde ~516 farklı mağaza numarası; Insurance Tracker'da ~418. Aktif mağazaların çoğu ayda 2.000–4.000 $ ödüyorsa, yıllık işlem hacmi Stax'in 250.000 $'lık kademesinin çok üzerindedir ❓ aylık toplam tahsilatı onaylayın.

---

## 2. Hassas veri bulguları (kontrol noktasındaki 3. maddeye yanıt)

| Kontrol | Sonuç |
|---|---|
| **Tam kart numaraları** (Luhn kontrolünden geçen 13–19 haneli sayılar) | 16 sekmenin hiçbirinde **bulunmadı**. Luhn'dan geçen dokuz adet 13 haneli sayı var, hepsi **Bank Accounts** sekmesinde. Bunlar banka hesap numaraları ("3" ile başlayan kart numaraları 13 değil, 14–16 hanelidir), yani kart numarası yok. |
| **CVV / son kullanma tarihi** anahtar kelimeleri veya değerleri | **Bulunmadı.** |
| **Kartın "son 4" hanesi** | **Sheet17**'de mevcut (142 satır). Kısaltılmış son 4 hane PCI DSS'e göre kart sahibi verisi **sayılmaz**; bu kabul edilebilir. Bir ACH dışa aktarımındaki banka hesabının son 4 hanesi gibi görünüyor ❓. |
| **Banka hesap + routing numaraları** | ⚠️ **EVET.** **Bank Accounts** sekmesinde, ~450 satırın kabaca **200'ünde** tam hesap numaraları ve 9 haneli routing numaraları var; yanlarında işletmeci adları, e-postaları, telefonları ve adresleri bulunuyor. |
| Kişisel iletişim verileri (PII) | İşletmeci e-postaları, telefon numaraları ve adresleri (Bank Accounts); fatura adları (Sheet17). |
| Sürüm geçmişi | **Kontrol edilmedi ve benim erişimim yok.** Sahibi Dosya → Sürüm geçmişi bölümünü kontrol etmeli ❓. |

**Prompt 1 için eşleştirme kuralı:**
- **Banka hesap ve routing numaraları uygulamaya ASLA taşınmaz.** Uygulama yalnızca Stax müşteri/ödeme yöntemi referansını ve en fazla son 4 haneyi saklar.
  - Stax'te ACH için zaten kayıtlı olan işletmecilerin banka bilgileri Stax'te kalır.
  - Henüz Stax'te olmayan işletmeciler banka bilgilerini Stax'in barındırdığı bir sayfada yeniden girer; böylece numaralar hiçbir zaman uygulamanın içinden geçmez.
- **Uygulamadan bağımsız, hemen yapılması gereken:** Bank Accounts sekmesine erişimi şimdi bir veya iki kişiyle sınırlayın ya da sekmeyi herkesle paylaşılan tablodan çıkarın.
  - Hesap ve routing numaralarının paylaşılan bir tabloda durması, bulunan en büyük veri riski.
  - Nacha kuralları, banka hesaplarından çekim yapan işletmelerin bu verileri güvende tutmasını bekler.
  - Yeni sisteme geçişten sonra bu sekmeyi, eski sürümleri de dahil olmak üzere silin.

---

## 3. Sekme envanteri (16 sekme)

**Tür** açıklaması: `T` = elle yazılıyor · `C` = hesaplanıyor (aritmetik örneklerde doğrulandı) · `C?` = muhtemelen hesaplanıyor, doğrulanmadı · `L?` = başka bir sekmeden çekiliyor olabilir · `?` = bilinmiyor.

### 3.1 Rents: *ana aylık kira defteri*
~570 satır (≈516 farklı mağaza numarası; ayrıca "MALL" satırları ve "LEASE TERMINATED IN JUNE", "NON RENEWAL 1/31/2026" gibi durum/bölüm satırları). A–EL sütunları.

| Sütun | Başlık | Veri tipi | Tür | Notlar |
|---|---|---|---|---|
| A | Store | mağaza numarası (tam sayı) | T | Bazı satırlarda numara yerine "MALL" veya metin notu var |
| B | LOCATION, STATE | metin (şehir, bazen "Şehir, EY") | T | C sütununu tekrarlıyor |
| C | STATE | 2 harfli eyalet kodu | T | |
| D | Operator Name | metin | T/L? | İşletmecinin takma adı/ilk adı; tüzel kişilik adı değil |
| E | Open Date | tarih **veya** "open" metni | T | Karışık tipler |
| F | RENT | sayı (aylık kira) | T | Bazı mağazalarda 0 |
| G | Rec. Date | sayı (ör. 1) | T | ❓ Kiranın çekildiği ayın günü mü? |
| H | Ins | sayı, çoğu zaman negatif (ör. −1130, −565) | T? | ❓ Negatifin anlamı |
| I | Sign | sayı, çoğu zaman negatif | T? | ❓ |
| J | Deposit or Access | sayı, çoğu zaman negatif | T? | ❓ |
| K | NOTES | serbest metin | T | ör. "NP", "Rent Ju…", "deposit…" |
| L–EL | Sep 2024 … Jul 2035 (131 aylık sütun) | aylık sayı | T | Yaklaşık Eylül 2026'ya kadar dolu; gelecek aylar önceden açılmış ve boş. Negatif değerler var (ör. bir mağazada Kasım 2025'ten itibaren −2000) ❓ geri dönen/ödenmemiş mi, alacak mı? |

**Uygulamadaki karşılığı:** `stores` (mağazalar), `operators` (işletmeciler), `leases` (mağaza × işletmeci × aylık kira × başlangıç/bitiş) ve bir `charges` / `payments` (borç / ödeme) defteri. 131 aylık sütun, sütun değil satır olur.

### 3.2 Payments Balance: *mağaza başına tek seferlik / kurulum ücretleri*
~455 satır. A–N sütunları.

| Sütun | Başlık | Veri tipi | Tür | Notlar |
|---|---|---|---|---|
| A | STORE # | tam sayı | T | |
| B | LOCATION, STATE | metin | T | |
| C | STATE | metin | T | |
| D | Operator Name | metin | T/L? | Bazı mağazalarda Rents'tekinden farklı (ör. 1796 numaralı mağaza) ❓ güncel olan hangisi? |
| E | Store Open Date | tarih / "open" | T | |
| F | Sign | para | T | |
| G | 1st Year Insurance | para | T | |
| H | Deposit | para | T | |
| I | Access Fee | para | T | |
| J | Business Payment Balance | para | T | ❓ bu nedir? |
| K | Marketing Material | para | T | |
| L | Misc | para | T | |
| M | MISC. NOTES | metin | T | ör. "15K is Store Build Out Balance" |
| N | TOTAL | para | C? | Muhtemelen F+…+L |

**Uygulamadaki karşılığı:** sign / insurance_first_year / deposit / access_fee / business_payment / marketing / misc türünde tek seferlik `charges` kayıtları.

### 3.3 Insurance Tracker: *yıllık sigorta ücretleri*
~449 satır (≈418 farklı mağaza). A–DB sütunları.

| Sütun | Başlık | Veri tipi | Tür | Notlar |
|---|---|---|---|---|
| A | STORE # | tam sayı | T | Ayrıca "HEB 749", "MALL" ve not satırları |
| B | CITY | metin | T | |
| C | STATE | metin | T | |
| D | STORE OPENED | ay-yıl metni | T | |
| E | *(boş başlık)* | — | ? | ❓ |
| F | NOTES | metin | T | |
| G–DB | Aug 2022 … Nov 2030 (100 aylık sütun) | sayı (565, −565) veya metin ("Process…") | T | Ücretler çoğunlukla her yıl haziranda; negatifler ❓ iade/geri dönen mi? |

**Uygulamadaki karşılığı:** Her mağaza için yıl dönümünde veya sabit bir ayda oluşturulan, insurance türünde yıllık tekrarlayan `charges` kayıtları ❓.

### 3.4 Monthly Balances: *ödenmemiş / geri dönen kira kaydı*
~392 satır. A–G sütunları. "RENT BALANCES" bölüm satırıyla başlıyor.

| Sütun | Başlık | Veri tipi | Tür |
|---|---|---|---|
| A | LOCATION | mağaza numarası + ad/metin | T |
| B | BALANCE FOR | metin, ör. "MAY 2024 RENT" | T |
| C | AMOUNT | para | T |
| D | PAYMENT STATUS | metin, ör. "RETURNED", "RETURNED AGAIN on 3/26" | T |
| E | FATIH NOTE | metin (bir kısmı Türkçe) | T |
| F | ZAHIR'S NOTE | metin, ör. "He sent zelle $4000", "CALL MONDAY" | T |
| G | AHMED NOTE | metin | T |

**Uygulamadaki karşılığı:** Defterden otomatik oluşturulan açık bakiye ve tahsilat ekranları, ayrıca işletmeci ve mağaza başına bir `notes` / `activity` (not / hareket) akışı.

### 3.5 Old Balances: *eski geri dönen ödemeler (2025 başı)*
~23 satır: LOCATION, CITY, BALANCE FOR, AMOUNT, PAYMENT STATUS, Note (ör. "returned on Feb 1", "need to collect prior store transfer"). Hepsi elle yazılmış. **Eski (legacy) açık bakiyeler olarak taşınır.**

### 3.6 Receivable: *küçük alacak listesi*
~30 satır: LOCATION, BALANCE FOR (ör. "September RC", "Insurance"), AMOUNT (75 $, 565 $), NOTE ("Outstanding"), STATUS ("NEED BANK ACCOUNT !"), ADDITIONAL NOTES (e-postalar içeriyor). Hepsi elle yazılmış. ❓ "RC" nedir?

### 3.7 SignInsDep: *tabela / sigorta / depozito bakiyeleri, **başlıkta "Up to date - DEC 26, 2023" yazıyor***
~335 satır: LOCATION (mağaza no), STORE (şehir), BALANCE FOR (Sign, 1ST YEAR insurance, …), AMOUNT, STATUS, AHMED NOTE, ZAHIR NOTE, FATIH NOTES ve bir kontrol sütunu ("CHECKED"). ❓ Bu sekme hâlâ güncelleniyor mu, yoksa 2023'ten kalma eski bir anlık görüntü mü? Eskiyse yalnızca eski kayıt/referans olarak taşınır.

### 3.8 Audit Fees: *bugün gerçek faturalandırmaya en yakın bölüm*
~248 satır. A–L sütunları.

| Sütun | Başlık | Veri tipi | Tür | Notlar |
|---|---|---|---|---|
| A | *(başlıkta "2" yazıyor)* | şehir/konum metni | T | Başlık hücresi yanlış etiketlenmiş görünüyor ❓ |
| B | State | metin | T | |
| C | Store number | tam sayı | T | |
| D | Billed to | kişi adı (işletmeci/sahip) | T | |
| E | Incident Date | tarih | T | |
| F | Date invoice sent | tarih | T | |
| G | Invoice | `IFX-####` (1002–1294) | T | **Mevcut fatura numaralandırması: bu sıra korunmalı** |
| H | Amount | para (150 $, 2.500 $) | T | |
| I | Due Date | tarih | T/C? | Genellikle gönderimden ~30 gün sonra |
| J | 90 Days Last Reminder | tarih | C? | ≈ Vade tarihi + 90 gün |
| K | Concept | metin ("Audit program", "$2500 Fines") | T | |
| L | Payment | durum metni ("Invoice sent on 6/18", "No payment yet") | T | |

**Uygulamadaki karşılığı:** Stax e-postasıyla gönderilen, IFX numarası referans olarak kullanılan, audit_fine türünde `invoices` (faturalar). **İlk sürüm (MVP) için güçlü aday:** faturalar ayrık, yaşam döngüsü net ve bugünkü süreç gönder → bekle → hatırlat şeklinde.

### 3.9 Termination Fees: *kira sözleşmesi fesih ödemeleri*
~10 satır: Store, City, STATE, Operator Name, Termination Month, TOTAL TERMINATION FEE, Sign DUE, Insurance, Deposit, Rent DUE (dönem metni, ör. "Nov 25 - May 26 (7 months)"), Total Rent, TOTAL DUE, Notes.

- **TOTAL DUE hesaplanıyor (C):** TOTAL DUE = TOTAL TERMINATION FEE + Total Rent + Sign DUE + Insurance (+ Deposit).
  - İki satırda doğrulandı: 19.000 + 19.950 − 2.159,19 − 1.130 = 35.660,81 ve 33.250 + 16.200 − 1.723,46 − 695 = 47.031,54.
  - Negatif Sign/Insurance değerleri borcu **azaltıyor**, yani işletmeci lehine alacak ❓ onaylayın; ayrıca negatiflerin Rents sekmesinde de aynı anlama gelip gelmediğini belirtin.

⚠️ Bu tutarlar (22 bin – 47 bin $) büyük olasılıkla Stax'in varsayılan işlem başı limitini aşıyor (bkz. §7).

### 3.10 Techy / 3.11 Talk n Fix / 3.12 Kroger / 3.13 Stop&Shop: *marka/zincir bazında mağaza kurulum anlaşması kâr-zararı*
Techy ~190 satır, Talk n Fix ~42, Kroger ~25 (6/1/2026'da açılan 2 mağaza), Stop&Shop ~32 (neredeyse boş şablon).

Ortak sütunlar:
- Store # / Location
- City
- Open Date
- **Sign (/Architect) We Paid** (tabela/mimar için bizim ödediğimiz)
- **Dep/Access We Paid** (depozito/erişim için bizim ödediğimiz)
- **TOTAL OUR COST** (toplam maliyetimiz) (C)
- **WE COLLECTED** (tahsil ettiğimiz; iki sütun, tabela ve depozito/erişim ❓ tam etiketler kesik)
- **OUR PROFIT / LOSS** (kârımız / zararımız) (C)
- **Deal Break Down / Break Out** (serbest metin, ör. "2000 our rent…")
- **SIGN ORDER STATUS** (tabela sipariş durumu)
- Çalışan notları (Fatih, Zahir, Ahmed)

- **TOTAL OUR COST = Sign We Paid + Dep/Access We Paid.** Doğrulandı: 2.143,17 + 8.500 = 10.643,17 ve 1.977,51 + 6.000 = 7.977,51.
- **OUR PROFIT/LOSS = (WE COLLECTED toplamı) − TOTAL OUR COST.** Doğrulandı: 8.500 − 10.643,17 = −2.143,17 ve 2.144,78 + 6.565 − 7.977,51 = 732,27.

Bu sekmeler **şirket içi anlaşma muhasebesi** (şirketin ev sahibine/tabela firmasına ödediği ile işletmeciden geri aldığı karşılaştırması). Müşteri faturalandırması değil. ❓ Uygulamanın ilk sürümüne dahil mi, yoksa sonraki bir "anlaşmalar" modülü mü?

### 3.14 Bank Accounts: *işletmeci banka + iletişim rehberi* ⚠️ hassas
~450 satır. İki satırlık başlık:
- STORE #, LOCATION, STATE, NOTES, BANK ACCOUNT NAME, **ACC NUMBER**, **ROUTING**, TYPE / Bank name
- **WM RENT**, RENT START DATE, **OPERATOR RENT**, ADDRESS, OPERATOR EMAIL, PHONE #, **ACH PLATFORM** (birkaç satırda "STAX")

Ayrıca bölüm satırları (ör. "ACCOUNTS NEED TO TRANSFER TO…") ve "Need account information" notları içeriyor.

**Taşınacaklar:** mağaza ↔ işletmeci bağlantıları, işletmeci iletişim bilgileri (e-posta, telefon, adres), WM RENT (şirketin ev sahibine ödediği kira), OPERATOR RENT, RENT START DATE, ACH PLATFORM. **Asla taşınmayacaklar:** ACC NUMBER, ROUTING (bkz. §2).

### 3.15 Sheet17: *ödeme işlemcisi dışa aktarımı, 5–11 Haziran 2026*
~142 satır: Received Date, Received Time, Status ("successful - funded" ×123, "unsuccessful - rejected" ×19), Customer ID (işletmeci firma adı + mağaza), Bill To First/Last Name, Amount, Last 4, Authorization Info ("Auth #…"), Principal, CURRENT STATUS OF RETURNS ("re-processed", "Settled", "will re-schedule").

Geri dönen ödemeleri **elle tek tek işlemek** için kullanılan bir haftalık ACH toplu işlem dökümü. ❓ Hangi platform üretti (Stax mı, başka biri mi)? O haftaki çekimlerin yaklaşık %13'ü reddedilmiş, dolayısıyla **geri dönen ödemelerin yönetimi temel bir gereksinim.**

### 3.16 Stores with Issues: *yapılacaklar listesi*
~16 satır: Store, LOCATION, STATE, Operator Name, Open Date, ISSUE ("Two months behind the rent", "Three months behind"), NEED TO DO ("Ahmed talk to Zahir about this store"). Hepsi elle yazılmış. **Uygulamadaki karşılığı:** otomatik "gecikmedeki mağazalar" raporu (N ay geride) + görevler/notlar.

---

## 4. Tablodan çıkan varlıklar (sütun eşleştirmesi için)

| Varlık | Kaynağı | Anahtar |
|---|---|---|
| **Store** (mağaza/konum) | Rents A–C, E; Insurance Tracker A–D; Bank Accounts A–C | Mağaza numarası (+ zincir: Walmart/Kroger/Stop&Shop/HEB/AVM). Mağaza numaraları zincirler arasında çakışabilir ❓ |
| **Operator** (işletmeci; Stax'teki "müşteri") | Operator Name (Rents D, Payments Balance D), Bank Accounts iletişim sütunları, Sheet17 Customer ID | ❓ Bugün işletmeci numarası yok; adlar takma ad. Taşıma sırasında oluşturulmalı ve Stax'teki mevcut müşterilerle eşleştirilmeli |
| **Lease / occupancy** (kira sözleşmesi / kullanım) | Mağaza × İşletmeci × aylık kira (Rents F, Bank Accounts OPERATOR RENT) × başlangıç (Open Date / RENT START DATE) × bitiş (fesih) | Zamanla değişir (mağaza devirleri) |
| **Host lease cost** (ev sahibi kira maliyeti) | Bank Accounts WM RENT | Şirket içi maliyet; işletmecilere faturalandırılmaz |
| **Charge** (borç: ne ödenmeli) | Rents aylık hücreleri, Insurance Tracker hücreleri, Payments Balance sütunları, Audit Fees, Termination Fees, Receivable | tür + mağaza + işletmeci + dönem + tutar |
| **Payment** (ödeme: ne geldi) | Rents aylık hücreleri (❓ borç mu, ödeme mi), Sheet17, notlar ("sent zelle $4000", havaleler, çekler) | Uygun olan yerde Stax işlem numarası; Zelle/havale/çek için elle girilen kayıtlar |
| **Return / failed payment** (geri dönen / başarısız ödeme) | Sheet17'deki reddedilen satırlar, Monthly Balances "RETURNED", Old Balances | Bir ödemeye bağlı |
| **Invoice** (resmi fatura) | Audit Fees (IFX-####), Termination Fees | IFX sırası |
| **Deal cost record** (anlaşma maliyet kaydı) | Techy / Talk n Fix / Kroger / Stop&Shop | Mağaza |
| **Note / activity** (not / hareket) | Tüm "X NOTE" sütunları | yazan + tarih |

---

## 5. Tablodan çıkarılan manuel iş akışları

1. **Aylık kira tahsilatı:** Her işletmecinin kirasını "Rec. Date" civarında ACH ile çekmek (kısmen Stax üzerinden); tutarı Rents'te o ayın sütununa yazmak.
2. **Geri dönen ödemeler:** İşlemcinin toplu dökümünü almak (Sheet17). Reddedilen her çekimi Monthly Balances'a yazmak, işletmeciyi takip etmek (telefon, Zelle, havale), yeniden işlemek ve durum notlarını güncellemek.
3. **Yıllık sigorta:** Mağaza başına 565 $ almak (çoğunlukla haziranda) ve Insurance Tracker'a yazmak.
4. **Yeni mağaza kurulumu:** Tabela/mimar ve ev sahibi depozito/erişim ücretini ödemek; işletmeciye tabela, ilk yıl sigortası, depozito/erişim ve pazarlama ücretlerini fatura etmek; geri alınan tutarları marka sekmelerinde (Techy vb.) ve Payments Balance'ta takip etmek.
5. **Denetim cezaları:** IFX faturası oluşturmak (tablonun dışında mı? ❓ hangi araç gönderiyor), gönderim tarihini, vade tarihini ve 90 günlük hatırlatmayı yazmak ve takip etmek.
6. **Fesih:** Ödeme tutarını hesaplamak (ücret + ödenmemiş kira ± tabela/sigorta alacakları) ve tahsilatı takip etmek.
7. **Gecikme incelemesi:** N ay geride olan mağazaları listelemek (Stores with Issues) ve takip işlerini Ahmed, Zahir ve Fatih arasında paylaştırmak.
8. **Banka bilgisi toplama:** Banka bilgisi için işletmecileri takip etmek ("NEED BANK ACCOUNT !"), sonra bilgiyi Bank Accounts'a ve ACH platformuna girmek.

## 6. Çalışanların dayandığı raporlar (tahmini) ❓ onaylayın
- Kimin kirası gecikmede, kaç ay ve ne kadar (Stores with Issues, Monthly Balances).
- Geri dönen ödemeler ve yeniden işlenme durumları (Sheet17, Monthly Balances, Old Balances).
- Mağaza başına açık tek seferlik bakiyeler (Payments Balance, SignInsDep, Receivable).
- Denetim cezalarının durumu (Audit Fees).
- Yeni mağaza başına anlaşma kârlılığı (marka sekmeleri).

## 7. Uygulama ve Stax açısından sonuçlar (Prompt 1'e girdi)

- **Uygulamanın merkezi, tek seferlik kart faturaları değil, işletmecilerden çoğunlukla ACH ile yapılan düzenli tahsilat.**
  - İlk sürüm şunları kapsamalı: kira sözleşmesi başına aylık kira borçları, ödeme/geri dönüş takibi, işletmeci ve mağaza başına bakiyeler, denetim cezaları ve fesihler için resmi faturalar.
  - Kartla ödemeler ikinci planda.
- **ACH fiyatlandırması bu işletmenin lehine:** %1, en fazla 10 $; yani 3.500 $'lık bir kira çekim başına yaklaşık 10 $ tutar. Geri dönen ödemelerin ayrıca ücreti olabilir ❓ Stax'e sorun.
- **Geri dönen ödemeler temel konu.** Stax'in ACH ret raporu ve işlem webhook'ları kullanılmalı. Yeniden deneme yalnızca işletmecinin onayıyla yapılmalı.
  - Stax'in bildirilen "saatte 50 başarısız işlem" sınırına dikkat: ayda ~450 çekim ve ~%13 ret oranıyla, toplu çekimler zamana yayılmalı.
- **İşlem limitleri canlıya geçişin önünde engel.** 3.500–4.208 $'lık kiralar ve 47 bin $'a varan fesih ödemeleri, STAX_ANSWERS.md'deki 2.000 $'lık geçici limiti aşıyor. Kira tahsilatı bölümü geliştirilmeden önce gerçek işlem başı limit (kart ve ACH) öğrenilmeli.
- **Bu işletmecilerin bir kısmı zaten Stax'te** ("ACH PLATFORM: STAX"). Taşıma, yeni kopyalar oluşturmak yerine **mevcut Stax müşterilerini ve kayıtlı banka yöntemlerini bulup bağlamalı.**
- **IFX-#### fatura numaralandırması korunmalı.**
- **Çalışan notları iş akışının bir parçası.** Uygulamada işletmeci ve mağaza başına, yazanı ve tarihi olan notlar olmalı. Notların bir kısmı Türkçe; arama ve görüntüleme yalnızca İngilizceye göre tasarlanmamalı.
- **İşletmeci kimlikleri dağınık** (takma adlar, devirler, "A → B"). Taşımada sahibin yardımıyla yapılacak bir eşleştirme adımı planlanmalı.

## 8. Taşıma sırasında düzeltilecek veri kalitesi sorunları
- Tekrarlanan/birleşik konum sütunları ("LOCATION, STATE" ile "STATE").
- Tarih sütunlarında karışık tipler ("open", "Cancelled", "3/1 - 5/1").
- Verilerin arasına karışmış bölüm ve yorum satırları ("MALL", "LEASE TERMINATED IN JUNE", "ACCOUNTS NEED TO TRANSFER TO").
- Zincirler arasında benzersiz olmayan mağaza numaraları (HEB, AVM satırları).
- Sekmeler arasında tutarsız işletmeci adları (ör. 1796 numaralı mağaza Rents'te ve Payments Balance'ta farklı işletmeci gösteriyor).
- Negatif sayıların işaret anlamı belirsiz ve sekmeden sekmeye farklı olabilir.
- Canlı sekmelerin yanında eski bir sekme (SignInsDep, Aralık 2023 tarihli).
- Önceden açılmış gelecek ay sütunları: Rents'te 131 (2035'e kadar), Insurance Tracker'da 100 (2030'a kadar).
- Audit Fees'te yanlış etiketlenmiş bir başlık ("2").

## 9. Sahibine açık sorular (numaralı; yanıtları altına yazın)

1. §1'deki işletme tanımı doğru mu? **"RC"** (aylık 75 $) tam olarak nedir?
2. **Rents aylık hücreleri:** **alınması gereken** tutarı mı, yoksa **fiilen tahsil edilen** tutarı mı gösteriyor? **Negatif** değer ne anlama geliyor (geri dönen, alacak, iade)?
3. **Rents H–J (Ins, Sign, Deposit or Access):** negatif sayılar işletmecinin o tutarı **borçlu** olduğunu mu, yoksa **alacaklı** olduğunu mu gösteriyor? Termination Fees'te de aynı kural mı geçerli (orada negatifler toplam borcu azaltıyor)?
4. **Rec. Date:** kiranın çekildiği ayın günü mü?
5. Hangi sekmeler **canlı**, hangileri **geçmiş/referans**? Özellikle: SignInsDep (Aralık 2023), Old Balances, Receivable, Sheet17.
6. **Hangi sekmeyi kim düzenliyor?** (Ahmed / Zahir / Fatih / başkaları?) Yeni uygulamada iade yapma veya geri dönen ödemeyi yeniden işleme yetkisi kimde olmalı?
7. **IFX denetim faturalarını** bugün hangi araç gönderiyor ve IFX sırası 1294'ten devam ediyor mu?
8. **Sheet17:** bu dökümü hangi ödeme işlemcisi üretti? **Tüm** ACH işlemleri zaten Stax üzerinden mi geçiyor, yoksa başka bir platform da var mı?
9. Bugün kaç **aktif** mağaza ve işletmeci var ve aylık **toplam tahsilat** kabaca ne kadar?
10. **Anlaşma kâr-zarar sekmeleri** (Techy, Talk n Fix, Kroger, Stop&Shop) ilk sürüme dahil olmalı mı, yoksa sonraki bir aşamaya mı kalmalı?
11. Hücre **renkleri** bir anlam taşıyor mu (ödendi, gecikti, kapandı)?
12. İşletmecilerin banka bilgileri Stax'te doğrulandıktan sonra **Bank Accounts sekmesinin (ve sürüm geçmişinin) kısıtlanmasını ve ardından silinmesini** onaylıyor musunuz?
13. Tablonun **sürüm geçmişi** kart numaraları için kontrol edildi mi? (Ben yalnızca mevcut içeriği tarayabildim: kart numarası bulunmadı.)
