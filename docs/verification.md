# Doğrulama kapsamı

Kurulum paketi iki bağımsız Windows/QGIS ortamında denenir. Her ortamda **152**, toplam **304 çalışma kontrolü** bulunur. Sayısal özet ve paket içerik özeti [verification.json](verification.json) dosyasındadır.

| Ortam | Arayüz | Çalışma kontrolü |
| --- | --- | --- |
| Windows / QGIS 3.44.13 | Qt 5.15.13 | 152 |
| Windows / QGIS 4.2.1 | Qt 6.11.0 | 152 |

## Kontrol grupları

| Grup | Bir ortamda | Kapsam |
| --- | ---: | --- |
| Koordinat gidiş-dönüşü | 16 | Dört çıktı, iki mod, nokta/çizgi/poligon, birebir yatay koordinat ve değişmiş geometriyi reddetme |
| KML gösterimi | 12 | Parsel ve genel kayıt adları, HTML bilgi tablosu, tipli öznitelik, adı değiştirmeden sütun seçimi |
| Ölçümler | 25 | Elipsoid, metre, delikler, çok parçalı geometri, kayıt ızgarası ve kaynak Shape değerleri |
| Ana veri şeması | 17 | Kaynak öznitelikleri, eksik Shape alanları ve işlem sütunu/tablolarının ana veriden ayrılması |
| Kaynak ve seçim akışı | 16 | Tür filtreleri, öncelik, kayıt seçimi ve kaynak koordinat dayanağı |
| Sonuç ve CSV | 14 | Girdi/çıktı ilişkileri, durumlar, toplamlar ve CSV |
| Genel vektör | 23 | Geometri aileleri, kesin tekrarlar, farklı öznitelikler ve benzerlik önerileri |
| İlk açılış | 10 | Boş yollar, çalışma türü seçimi, taşınabilir profil ve hazır olma durumu |
| Temiz ZIP kurulumu | 10 | QGIS'in gerçek ZIP yükleyicisiyle bağımsız profil, yükleme, işlem, kaldırma ve yeniden kurulum |
| Eklenti yaşam döngüsü | 3 | Başlatma, menü kaydı ve kaldırma |
| XML okuma | 6 | Sınırlandırılmış ve güvenli XML/KML işleme |

Temiz kurulum testinde yükleme ve metadata keşfi QGIS'in gerçek yükleyicisiyle yapılır; masaüstü ana pencere/menü arayüzü bir test nesnesidir. Test girdileri sentetiktir. Çalışma kontrolü sayısı, kullanıcı verisinin arazi doğruluğunu ifade etmez.

## Ölçüm ve yeniden okuma

Çıktı yeniden açılıp kayıt sayısı, geometri türü/yapısı, öznitelikler ve yatay koordinatlar incelenir. Izgarasız çıktılarda XY değerleri ve tekrarlı nokta sayıları birebir denetlenir; FileGDB'nin tanımlı kayıt ızgarası ayrı değerlendirilir. Gerçek sınır, delik veya köşe değişiklikleri kabul edilmez. Elipsoid ölçümü, kaydedilmiş koordinatlar ve ilgili sistemin elipsoidiyle GeographicLib referansına karşı değerlendirilir.

## Paket ve kod

Paket tek `parsel_donusturucu/` klasörü içerir. Kaynakla birebir eşleşme, UTF-8, Python 3.9 sözdizimi, metadata, lisans, dosya izin listesi, kişisel yol ve yerel bağlantı bağımlılıkları denetlenir. Black, Flake8, Bandit ve sır taraması çalıştırılır. Paket doğrulaması [tools/check_package.py](../tools/check_package.py) ile tekrar yapılabilir.

## Depodaki testleri çalıştırma

QGIS'in Python ortamında çalıştırın. QGIS'in bağımsız Python'u gerekir; standart Python'da PyQGIS bulunmayabilir. Örneğin OSGeo4W terminalinde:

```powershell
python-qgis.bat tests/test_coordinate_roundtrip.py
python-qgis.bat tests/test_kml_presentation.py
```

LTR kurulumu için karşılık gelen `python-qgis-ltr.bat` kullanılabilir. Bu iki paylaşılmış grup 28 kontrol içerir; tam 304 kontrolün sayısal kapsamı yukarıdaki tabloda belirtilmiştir. Testler bağımsız profilleri ve örneklerini `.test_artifacts/` altında oluşturur; açık QGIS projesini kullanmaz.

```powershell
python tools/build_package.py
python tools/check_package.py
```

Paket araçları yalnız Python standart kütüphanesini kullanır. `build_package.py` yeni çıktı için `--output` kabul eder; var olan dosya değişmiyorsa aynı içerik özetini doğrular, farklı içeriğin üzerine yazmaz.

## Uyumluluk sınırı

QGIS 3.34–4.x metadata aralığıdır; bütün ara sürümlerin denenmiş olduğu anlamına gelmez. Diğer işletim sistemleri doğrulanmamıştır. FileGDB yazımı Windows/OpenFileGDB desteğine bağlıdır. Z/M, karma GeometryCollection, dinamik epok dönüşümü, otomatik onarma ve yakınlıkla tekilleştirme kapsam dışında kalır.
