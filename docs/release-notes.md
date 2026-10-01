## Vector Converter 0.9.0

QGIS Desktop için toplu vektör ve parsel dönüşümü. QGIS menüsündeki adı **Parsel Dönüştürücü**dür.

- Genel vektör ve Parsel dönüşümü modları.
- SHP/ZIP, KML/KMZ ve GeoJSON girdileri; GeoPackage, FileGDB, GeoJSON ve KML çıktıları.
- Kesin tekrar karşılaştırması, dosya türü önceliği ve kayıt seçimi.
- Öznitelik seçimi/eşleme, toplu çıktı koordinatı ve 25 hazır şablon.
- Ana veride kaynak öznitelikleri ve eksikse m²/metre Shape ölçümleri.
- KML kayıt adları, tıklanabilir bilgi tablosu ve gerçek tipli öznitelikler.
- Yeniden okuma kontrolü, kaynak yan dosyası, HTML/JSON raporları ve isteğe bağlı CSV.

### Kurulum

**Assets** altındaki **parsel_donusturucu-0.9.0.zip** dosyasını indirin. QGIS → **Eklentiler → Eklentileri Yönet ve Yükle → ZIP'ten Yükle** yoluyla kurun; **Vektör → Parsel Dönüştürücü** menüsünden açın. **Source code** arşivleri kurulum paketi değildir.

`SHA256SUMS.txt` paket içerik özetini gösterir. [Görselli kullanım ve teknik açıklamalar](https://github.com/dogandgn/vector-converter#readme).

### Uyumluluk

QGIS 3.34–4.x, Python 3.9+, GDAL 3.6+. Windows QGIS 3.44.13/Qt5 ve 4.2.1/Qt6'da toplam **304 çalışma kontrolü** geçmiştir. Diğer ortamlar doğrulanmamıştır. FileGDB için Windows ve yazılabilir OpenFileGDB gerekir. KML/GeoJSON çıktıları WGS84 derecedir.

Bu 0.9.0 paketi QGIS metadata'sında **experimental** olarak işaretlidir. Lisans: GPL-3.0-or-later.
