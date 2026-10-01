"""Named 2D Turkey presets. TUREF is not an arbitrary-epoch ITRF transformation."""

PROFILES = {
    "wgs84": {
        "name": "WGS84 — enlem/boylam, derece (EPSG:4326)",
        "crs": "EPSG:4326",
        "measurement": "ellipsoidal",
    },
    "tm30": {
        "name": "TUREF / TM30 — metre (EPSG:5254)",
        "crs": "EPSG:5254",
        "measurement": "planar",
    },
    "turef_degree": {
        "name": "TUREF — derece (EPSG:5252)",
        "crs": "EPSG:5252",
        "measurement": "ellipsoidal",
    },
    "ed50_degree": {
        "name": "ED50 — derece (EPSG:4230)",
        "crs": "EPSG:4230",
        "measurement": "ellipsoidal",
    },
}
for meridian in range(27, 46, 3):
    if meridian != 30:
        code = 5253 + (meridian - 27) // 3
        PROFILES["turef_tm" + str(meridian)] = {
            "name": f"TUREF / TM{meridian} — metre (EPSG:{code})",
            "crs": f"EPSG:{code}",
            "measurement": "planar",
        }
    code = 2319 + (meridian - 27) // 3
    PROFILES["ed50_tm" + str(meridian)] = {
        "name": f"ED50 / TM{meridian} — metre (EPSG:{code})",
        "crs": f"EPSG:{code}",
        "measurement": "planar",
    }
for zone in range(35, 39):
    for datum, base in [("wgs84", 32600), ("ed50", 23000)]:
        code = base + zone
        PROFILES[f"{datum}_utm{zone}n"] = {
            "name": f'{"WGS84" if datum=="wgs84" else "ED50"} / UTM {zone}N — metre (EPSG:{code})',
            "crs": f"EPSG:{code}",
            "measurement": "planar",
        }

PROFILE_NOTE = (
    "Koordinat sistemi (CRS): konumu yorumlamak için kullanılan referans ve projeksiyon tanımı. "
    "EPSG: koordinat sisteminin kayıt kodu. PRJ (.prj): Shapefile koordinat tanım dosyası. "
    "WGS84: Dünya Jeodezik Sistemi 1984; ED50: Avrupa Datumu 1950; TUREF: Türkiye Ulusal Referans Çerçevesi. "
    "ITRF96: Uluslararası Yersel Referans Çerçevesi 1996. "
    "Türkiye TUREF tanımı ITRF96 ile 2005.0 referans epoğunda uyumludur. "
    "Başka ITRF sürümü veya gözlem epoğu için hız/epok dönüşümü bu araçta yapılmaz. "
    "TM (Transvers Merkator): 3° dilimler; UTM (Evrensel Transvers Merkator): 6° dilimler. Dilim veri konumuna göre seçilir. "
    "Alan/çevre sonuçları her zaman m²/m; derece seçimi geometri koordinat birimidir."
)
