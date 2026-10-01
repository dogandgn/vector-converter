# Vector Converter

A QGIS Desktop plugin for inspecting, comparing and batch-converting 2D vector and parcel files. The QGIS menu entry is **Parsel Dönüştürücü**. Its interface is in Turkish.

[Download the installation ZIP](https://github.com/dogandgn/vector-converter/releases/download/v0.9.0/parsel_donusturucu-0.9.0.zip) · [Illustrated documentation](../README.md)

## Installation

1. Download `parsel_donusturucu-0.9.0.zip` from the release assets.
2. In QGIS, open **Plugins → Manage and Install Plugins → Install from ZIP**.
3. Select the downloaded package and install it.
4. Open **Vector → Parsel Dönüştürücü**.

**Code → Download ZIP** and GitHub's **Source code** archives contain the repository, not the installable plugin layout. Use the named release asset above. Python, Qt and GDAL/OGR come from QGIS; no separate Python environment or online account is needed.

## Workflow

| Interface label | Action |
| --- | --- |
| **Çalışma türü** | Choose General vector (**Genel vektör**) or Parcel conversion (**Parsel dönüşümü**). |
| **Tara ve karşılaştır** | Scan the input folder and compare its files and records. |
| **Girdi ve kayıt seçimi** | Select file types and records; review exact duplicates and source priority. |
| **Öznitelikleri incele / çıktı sütunlarını düzenle** | Inspect common fields and map, select or rename output columns. |
| **Çıktı koordinatları** | Select the output coordinate system; input systems are read-only. |
| **Çıktı yolu** | Click to choose the destination and filename. |
| **Çıktı oluştur** | Export, reopen and validate the result. |
| **CSV olarak kaydet** | Save the input inventory or completed comparison table as CSV. |

At first launch, the workflow and paths are empty. Controls become available after choosing a workflow. Changing workflows clears the preview and field plan; scan again. A disabled export button has a readiness message explaining the missing setting or unsuitable source.

## Modes and duplicate selection

**General vector** supports Point/MultiPoint, LineString/MultiLineString and Polygon/MultiPolygon. Automatic duplicate groups require the same geometry and the same nonempty typed attributes. Empty attributes, a similar name, geometry alone, or conflicting values do not trigger automatic deduplication. Reversed lines are kept distinct. Layer similarity is an inspection aid and does not exclude records.

**Parcel conversion** supports polygons. It compares administrative parcel identity and horizontal geometry; differing boundaries remain separate. KML record names use the available province, district, neighbourhood, block and parcel fields.

Three rules are available: keep selected records separate, merge exact duplicate sources, or select one source per exact group by format priority. Priority falls back to the next enabled format actually present, preserving unique records. **Tümü** means all three input formats; deselecting one removes that check. Clearing and then applying the selection rule creates a fresh selection; merely changing format filters or priority retains manual exclusions.

## Formats and coordinates

| Direction | Supported formats |
| --- | --- |
| Input | Shapefile, ZIP containing Shapefile, KML/KMZ, GeoJSON |
| Output | GeoPackage, FileGDB, GeoJSON, KML |

Keep Shapefile components together: `.shp`, `.shx`, `.dbf` and, when present, `.prj` and `.cpg`. KMZ must contain one KML. GeoPackage/FileGDB input and Shapefile output are not supported.

GeoPackage and FileGDB use the selected output system. GeoJSON and KML always use WGS84 degrees. There are 25 output presets covering WGS84, TUREF and ED50 geographic systems, Turkish 3-degree TM zones and UTM zones 35N–38N. TUREF refers to the ITRF96 relationship at reference epoch 2005.0; dynamic epoch/velocity transformations are not performed.

Selecting an output system does not identify an unknown input system. In parcel mode only, a Shapefile without a coordinate definition can obtain a WGS84 source basis from matching valid KML/GeoJSON peers when every record matches both full parcel identity and exact horizontal coordinates. Partial or conflicting evidence is insufficient; input files are never rewritten. General vector mode does not infer a missing system from another file.

## Attributes, geometry and output structure

The default output schema combines source fields. Field selection, naming and mapping affect the output, not the originals. Unsupported or incompatible attribute types can be represented as text in a portable schema; original typed values remain in the `.veri.json` companion.

The main data contains selected source attributes and missing geometry measurements only. Audit identities, method, target coordinate system, check status and area differences are kept out of its attribute table. Driver-managed IDs and geometry fields still exist.

| Geometry | Generated only if missing | Units |
| --- | --- | --- |
| Polygon | `Shape_Area`, `Shape_Length` | m², metres |
| Line | `Shape_Length` | metres |
| Point | None | — |

Geographic outputs are measured on their coordinate system's ellipsoid. Projected metre outputs use planar measurement. FileGDB measurements use the stored grid geometry. Existing source Shape fields retain their values and source-defined units. Geometry measurements do not establish legal parcel area or source survey accuracy.

Parcel GeoPackage/FileGDB output contains `parseller`; general output contains the required `noktalar`, `cizgiler` and `poligonlar` layers. GeoJSON is one FeatureCollection. KML includes presentation names, a clickable HTML attribute table and typed SchemaData; the table is text, not an image. General KML can use a selected output field for display names. KML readers may expose standard presentation fields such as `Name` and `description`.

Each output is accompanied by `.veri.json`, `.rapor.html` and `.rapor.json`. The completed comparison shows source/target states, excluded records and totals. CSV is optional, UTF-8 with semicolon separators and a total row. Existing output files are not overwritten. Keep companion data when original types or source relationships are needed.

## Requirements and verification

QGIS Desktop 3.34–4.x, Python 3.9+ and GDAL 3.6+ are required. Verified configurations are **Windows QGIS 3.44.13 / Qt5** and **4.2.1 / Qt6**, with 152 runtime checks per environment, 304 total. FileGDB export requires Windows and writable OpenFileGDB support. Other operating systems/version combinations are not verified.

True Z/M, heterogeneous GeometryCollection, automatic geometry repair, proximity-based merging and dynamic epoch conversion are unsupported. [Verification scope](verification.md).

## License and support

[GPL-3.0-or-later](../LICENSE) · [Issue tracker](https://github.com/dogandgn/vector-converter/issues)

Developer website: [doganaric.com](https://doganaric.com).
