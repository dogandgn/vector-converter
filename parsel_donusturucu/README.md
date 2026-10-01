# Vector Converter / Parsel Dönüştürücü

QGIS Desktop için toplu vektör ve parsel dönüştürme eklentisi. Dosyaları tek klasörden tarar; kayıtları karşılaştırır, kaynak seçimini ve öznitelik düzenlemeyi sağlar, seçilen koordinat sisteminde dışa aktarır. Özgün alanlar, kaynak ilişkileri ve işlem sonuçları raporlarda korunur.

**English summary:** A QGIS Desktop tool for reviewing and batch-converting 2D point, line, polygon and parcel data. Inputs: Shapefile/ZIP, KML/KMZ and GeoJSON. Outputs: GeoPackage, FileGDB, GeoJSON and KML. Choose a workflow, scan a folder, review records and attributes, select the output coordinate system and save. FileGDB writing requires Windows and OpenFileGDB support. Install the plugin package through **Plugins → Manage and Install Plugins → Install from ZIP**; open it from the **Vector** menu. Python 3.9+, QGIS 3.34–4.x and GDAL 3.6+ are required. GPL-3.0-or-later.

## Özellikler

- **Genel vektör:** İki boyutlu nokta, çizgi, poligon ve aynı türün çok parçalı geometrileri; parsel numarası gerekmez.
- **Parsel dönüşümü:** Poligon kayıtlarını idari parsel kimliği ve geometriyle karşılaştırma.
- Dosya ve kayıt seçimi; kaynak türü önceliği; tekrarları ayrı tutma veya birleştirme.
- Ortak sütunları inceleme, alan eşleme, sütun seçimi, adlandırma ve tür belirleme.
- Seçilen çıktı koordinatını bütün seçili kayıtlara uygulama.
- GeoPackage, FileGDB, GeoJSON ve KML çıktıları; sonuç tablosu, CSV ve HTML/JSON raporları.

## Kurulum

Eklentinin kurulum ZIP'ini kullanın. GitHub'dan indirirken **Releases → Assets** altındaki eklenti paketini seçin. **Code → Download ZIP** ve **Source code (zip)** kaynak kod arşivleridir; doğrudan QGIS'e kurulmaz.

Kurulum ZIP'i elinizdeyse QGIS → Eklentiler → Eklentileri Yönet ve Yükle → ZIP'ten Yükle yolunu kullanın. Vektör → Parsel Dönüştürücü menüsünden açın.

Her yeni oturumda çalışma türü, girdi klasörü ve çıktı yolu boştur. Önce **Çalışma türü** seçin; girdi, koordinat ve çıktı bölümleri etkinleşir. Ardından kendi girdi klasörünüzü seçin ve çıktı yoluna tıklayarak kayıt konumunu belirleyin. Eski profil tercihleri başlangıç alanlarına doldurulmaz. Aynı açık pencereyi kapatıp yeniden açmak devam eden oturumu korur. İş verileri kurulum klasörüne yazılmaz; son kullanılan çıktı klasörü yalnız aynı kullanıcının sonraki çıktı adı önerilerinde kullanılabilir.

Gerekli Python, Qt ve GDAL/OGR bileşenleri QGIS kurulumundan alınır; ek modül veya hesap kurulumu gerekmez.

## Hızlı kullanım

1. **Çalışma türü** listesinden Genel vektör veya Parsel dönüşümü seçin.
2. Girdi klasörünü seçin; **Tara ve karşılaştır** ile dosyaları inceleyin.
3. Dosya türlerini, kayıtları ve tekrarlar için seçim kuralını belirleyin.
4. Gerekiyorsa **Öznitelikleri incele / çıktı sütunlarını düzenle** penceresinde alanları düzenleyin.
5. Çıktı biçimini ve koordinat sistemini seçin; **Çıktı yolu** alanına tıklayarak dosya adını ve konumunu belirleyin.
6. **Çıktı oluştur** düğmesine basın. Sonuç tablosunu ve işlem raporunu inceleyin; gerektiğinde CSV kaydedin.

## Seçim ve çıktı

Çalışma türünü değiştirmek önizleme, seçim ve sütun planını sıfırlar; klasör yeniden taranmalıdır. Parsel modu poligonlarda idari kimlik ve geometriyi kullanır. Genel modda parsel numarası gerekmez; iki boyutlu Point/MultiPoint, LineString/MultiLineString ve Polygon/MultiPolygon desteklenir.

Genel modda **Kayıtlar ve tekrarlar** kesin eşleşmeleri, **Katman benzerlikleri** inceleme önerilerini gösterir. Otomatik tekrar grubu için aynı geometri ve aynı dolu öznitelikler gerekir. Yalnız geometri, ortak sütunlar veya katman adı yeterli değildir. Aynı geometride farklı/eksik alan değerleri ayrı tutulur; ayrıntıda diğer gruplar ve kaynak değerleri görülür. Boş öznitelikli kayıtlar otomatik tekilleştirilmez. Metin ve sayı değerleri (örneğin `001`, `1`, `1.0`) birbirine varsayımla çevrilmez. Çizgi yönü ve parça sırası korunur; ters yönlü çizgiler otomatik tekrar sayılmaz. Yakınlık toleransı veya sınır eritme uygulanmaz.

Katman benzerliği 0–100 yardımcı puanıdır; doğruluk/olasılık değildir. Puanın en fazla 55'i eş geometri, 20'si ortak alan adları, 20'si eş geometri ve öznitelik kayıtları, 5'i katman adı benzerliğidir. Geometri türü uyuşmayan veya yalnız adı benzeyen kaynaklar önerilmez. Bu tablo hiçbir kaydı kendiliğinden elemez; öncelik ve birleştirme yalnız kesin tekrar gruplarını kullanır.

Genel modda GeoPackage/GDB aynı veritabanında `noktalar`, `cizgiler`, `poligonlar` katmanlarını gerektiği kadar oluşturur. Parsel modunda katman adı `parseller`dir. Ana veri katmanlarında yalnız seçilen kaynak öznitelikleri bulunur; eklentinin kayıt kimliği, kaynak sayısı, ölçüm yöntemi, hedef koordinatı, kontrol durumu, alan farkı veya JSON izleme sütunları eklenmez. İşlem tabloları ana veritabanına eklenmez; kaynak ilişkileri ve denetim bilgileri raporlar ile `.veri.json` yan dosyasındadır. GeoJSON karma geometri içerebilen tek FeatureCollection, KML geometri türlerine göre klasörler içerir. Tek çıktı koordinat seçimi tüm seçili kayıtlara uygulanır. KML/GeoJSON WGS84 derece kuralı değişmez.

Geometri ölçümü için poligonlarda eksikse `Shape_Area` ve `Shape_Length`, çizgilerde eksikse `Shape_Length` oluşturulur. Noktalara ölçüm sütunu eklenmez. Yeni alan her zaman **m²**, yeni uzunluk/çevre **metre** cinsindedir. Derece koordinatlı çıktıda, kaydedilen geometrinin çıktı koordinat sistemine ait elipsoid üzerinde alanı/uzunluğu hesaplanır; derece² veya derece ölçümü yazılmaz. Metre koordinatlı çıktıda düzlemsel alan/uzunluk hesaplanır. Projeksiyonun düzlemsel ölçümü ile elipsoid ölçümü aynı olmak zorunda değildir. FileGDB'de yeni ölçümler kaydedilen ızgara geometrisinden hesaplanır.

Aynı adlı kaynak ölçüm sütunları büyük/küçük harf farkı gözetmeden korunur; değerleri yeniden hesaplanıp değiştirilmez. Bu mevcut sütunların birimini kaynak veri belirler; eklenti bunları otomatik olarak m²/metreye çevirmez. Ölçüm yöntemi/birimi raporda bulunur, ana katmana işlem sütunu olarak eklenmez. Veritabanının zorunlu `fid`/`OBJECTID` kayıt numarası ve geometri sütunu sürücü tarafından yönetilir.

KML'de parsel kayıtlarının görünen adı **il / ilçe / mahalle — Ada …, Parsel …** biçimindedir; her parsel için ayrı veri katmanı oluşturulmaz. Genel vektör modunda mevcut KML adı veya kaynak ad sütunu kullanılır; ad yoksa kaynak katman adı ve kayıt numarası gösterilir. Yalnız Genel vektör + KML seçiliyken **KML kayıt adı** listesinden başka bir çıktı sütunu seçebilirsiniz; boş değerlerde katman/kayıt adı kullanılır. Bu seçim öznitelik adını veya değerini değiştirmez. KML'de kayda tıklanınca açılan bilgi balonu için seçilen öznitelikler ve geometri ölçümlerinden bir tablo hazırlanır. Ad ve bilgi tablosu KML'nin standart gösterim öğeleridir; `ExtendedData` içine ek bir ad/açıklama sütunu yazılmaz. GeoPackage/GDB katman düzeni ve GeoJSON'un tek koleksiyon yapısı korunur. KML okuyucuları standart Name/description ve diğer sunum alanlarını gösterebilir.

Her iki modda kaynak sütunlarının birleşimi varsayılan çıktı şemasıdır; öznitelik penceresinde sütun seçebilir, adlandırabilir ve eşleyebilirsiniz. Taşınabilir sütun adı kuralları ve kullanıcı eşlemeleri uygulanır; kaynak değerleri korunur. Aynı adlı fakat uyumsuz türlerdeki değerler, mantıksal ve iç içe JSON değerleri ve 32 bit sınırını aşan tam sayılar ortak taşınabilir şemada metin olarak korunur; özgün türler/değerler `.veri.json` içinde saklanır. KML'nin NULL ve alan türü sınırları nedeniyle tam tipli değerler de bu yan dosyada tutulur; bunlar için ana KML'ye JSON sütunu eklenmez. Gerçek Z/M ve tek kayıtta karma GeometryCollection açık uyarıyla reddedilir. Genel mod bilinmeyen kaynak koordinatını eş dosyadan tahmin etmez; kaynak tanımı eksikse tanım dosyası tamamlanmalı veya girdi seçimden çıkarılmalıdır. Aşağıdaki eş KML/GeoJSON üzerinden kaynak tanımı çıkarma kuralı yalnız parsel modu içindir.

Seçimi temizle bütün kayıtları kapatır. Önceliğe göre seç, açık dosya türleri ve seçilen sıraya göre seçimi baştan kurar; elle yapılan seçim ve elemeleri sıfırlar. Diğer modlarda Uygun kayıtların tümünü seç aynı işi yapar. Tür veya öncelik değişikliği ise elle yapılan elemeleri korur.

Girdi koordinatları yalnızca listelenir; bu bölümde sistem seçme veya tanım atama kontrolü yoktur. Kullanıcı yalnızca çıktı koordinat sistemini seçer; dönüşüm dışa aktarım sırasında yapılır.

Koordinat tanımı olmayan SHP dosyasının bütün kayıtları, klasördeki geçerli KML/GeoJSON kayıtlarıyla tam parsel kimliği ve birebir yatay sınır koordinatları üzerinden eşleşirse WGS84 kaynak sistemi bu eş kayıtlardan çıkarılır. Bu dayanak listede ve işlem raporunda açıkça belirtilir; kaynak dosyaya tanım yazılmaz. Aynı kimliğin farklı geometrileri, eksik eşleşme veya yalnız koordinat aralığı sistem belirlemek için yeterli sayılmaz. Eş dosyalar çıktı seçiminin dışında kalsa da koordinat dayanağı olabilir; aktarım öncesi dosya özetleri tekrar denetlenir.

Kaynak sistemi belirlenemeyen seçili girdiler aktarımı durdurur. Koordinat tanım dosyası veya eş KML/GeoJSON kaydı klasöre eklenip yeniden taranabilir; gerektiğinde ilgili girdi seçimden çıkarılabilir. Çıktı sistemini değiştirmek bilinmeyen kaynak koordinatını değiştirmez.

Çıktı koordinatları listesinde **veriden okunan sistemler** ve **hazır çıktı sistemleri** ayrı başlıklarla gösterilir. Çıktı şablonlarında desteklenmeyen bir kaynak tanımı bilgi olarak görünür; o satır çıktı hedefi olarak seçilemez. **Çıktı yolu** alanına tıklayarak dosya adını ve konumunu seçin. KML/GeoJSON çıktısında WGS84 derece sabittir.

## Sonuçlar ve raporlar

**Girdi koordinat dökümü** dosyaları, koordinatlarını, tanım dayanaklarını, kayıt sayılarını ve durumlarını salt okunur tabloda gösterir. **Sonuç tablosu** aktarım sonunda açılır; girdileri çıktıyla karşılaştırır, seçim dışı, aktarılan ve aktarılamayan kayıtları ayrı gösterir. Toplam çıktı sayısı gerçek üretilen kayıt sayısıdır: örneğin aynı parselleri içeren 51 girdi kaydı birleştirilince 17 parsel oluşabilir. Bir sonraki seçim değişikliği son işlem tablosunu değiştirmez.

Her iki tablo **CSV olarak kaydet** ile dışa aktarılır (Türkçe uyumlu UTF-8, noktalı virgül ayırıcı, son satırda toplamlar). Mevcut dosyanın üzerine yazılmaz; kaynak ve eklenti klasörleri çıktı konumu olamaz. Sonuç karşılaştırması HTML/JSON işlem raporlarında da bulunur. Başarısız veya iptal edilmiş işlem, sonuç penceresinde sıfır çıktı olarak gösterilir; CSV'si istenirse kaydedilir.

## Dosya biçimleri

| İşlem | Desteklenen biçimler |
| --- | --- |
| Girdi | Shapefile ve Shapefile içeren ZIP, KML/KMZ, GeoJSON |
| Çıktı | GeoPackage (.gpkg), FileGDB (.gdb), GeoJSON (.geojson), KML (.kml) |

Shapefile bileşenlerini birlikte tutun: `.shp`, `.shx`, `.dbf` ve varsa koordinat tanım dosyası `.prj` ile kodlama dosyası `.cpg`. GeoPackage/FileGDB girdisi ve Shapefile çıktısı desteklenmez.

GeoPackage/FileGDB çıktılarında parsel modunun mekânsal katmanı `parseller` adını taşır. Genel modda veri türlerine göre `noktalar`, `cizgiler`, `poligonlar` oluşturulur. Her iki modda yalnız bu veri katmanları veritabanına eklenir. Kaynak izleme ve işlem tabloları `.veri.json` yan dosyasındadır.

KML/GeoJSON WGS84 derece kullanır. Bütün çıktı biçimlerinin yanında oluşan `.veri.json` dosyasını koruyun; kaynak ilişkileri ve tip bilgisi gerektiğinde ana veriyle birlikte paylaşın. Belirsiz kaynak sistemi çıktı seçiminden veya sayı aralığından tahmin edilmez. Saha doğruluğu veya resmî kadastro belgesi üretmez; dinamik epok dönüşümü yapmaz.

## Sistem gereksinimleri ve uyumluluk

QGIS Desktop 3.34–4.x, Python 3.9+ ve GDAL 3.6+ gerekir. Doğrulanmış ortamlar Windows QGIS 3.44.13/Qt5 ve 4.2.1/Qt6'dır. Diğer QGIS sürümleri ve Linux/macOS için uyumluluk doğrulanmamıştır. FileGDB yazımı Windows ve OpenFileGDB yazma desteği gerektirir. QGIS Server, mobil ve web kullanımı desteklenmez.

Gerçek yükseklik/ölçü boyutları (Z/M), tek kayıtta farklı geometri türlerinin birleşimi, dinamik sistemlerde epoka bağlı dönüşüm ve toleransla yakın kayıtları birleştirme desteklenmez. Kaynak verinin saha veya kadastro doğruluğu kullanıcı tarafından değerlendirilmelidir.

Eklentideki **Kullanım / kurulum** düğmesi çevrimdışı yardım açar. Kullanılan teknolojiler [CREDITS.md](CREDITS.md) dosyasında belirtilmiştir.

## Destek ve kaldırma

Sorun bildirirken QGIS ve GDAL sürümünü, seçilen çalışma türünü, dosya biçimlerini ve hata mesajını belirtin. [Hata bildirimleri](https://github.com/dogandgn/vector-converter/issues).

Eklentiyi QGIS eklenti yöneticisinden kaldırabilirsiniz. Üretilen veriler korunur. Klasör tercihlerini sıfırlamak için QGIS kapalıyken ilgili profilin `parsel_donusturucu.ini` dosyasını kaldırın.

## Lisans

GPL-3.0-or-later. Bu yazılım GNU Genel Kamu Lisansı'nın 3. sürümü veya daha sonraki bir sürümünün koşulları altında dağıtılır. Tam metin [LICENSE](LICENSE) dosyasındadır.

Geliştirici sitesi: [doganaric.com](https://doganaric.com).
