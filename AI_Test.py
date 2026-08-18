import os

from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel
from typing import Literal

from geopy.geocoders import Nominatim
import folium

# .env yükle
load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise RuntimeError("GEMINI_API_KEY bulunamadı.")


# Gemini istemcisi
client = genai.Client(
    api_key=api_key
)


# Gemini'nin döndüreceği veri modeli
class RoadAnalysis(BaseModel):
    location_type: Literal[
        "road",
        "road_segment",
        "intersection",
        "address",
        "landmark",
        "unknown"
    ]

    road_name: str | None
    start_query: str | None
    end_query: str | None
    road_query: str | None

    confidence: float


# Test edeceğimiz gerçek güzergah metni
guzergah = """
Merkezefendi ilçesi Merkez Efendi Mahallesi Kazım kaynak Caddesi ile KZ/6 Sokak kesişimi ve Kazım kaynak Caddesi ile 1766 sokak kesişimi arasında şerit daraltma çalışması yapılacaktır.
"""
# Akhan Kavşağı - Pamukkale Bulvarı kesişimi arası denizli istikameti sol şerit kapatılacaktır


# Gemini'ye gönderilecek talimat
prompt = f"""
Sen Denizli'deki belediyelerin yol kapatma 
duyurularını analiz eden bir konum analiz sistemisin.

Görevin, verilen güzergah metnini analiz ederek geocoding 
işlemi için kullanılabilecek konum bilgilerini çıkarmaktır.

Kurallar:

1. Asla koordinat üretme.
2. Asla koordinat tahmin etme.
3. Metinde bulunmayan bir yol veya adresi gerçekmiş gibi
   uydurma.
4. Yolun tamamı belirtiliyorsa location_type = "road" kullan.
5. Bir yolun başka iki nokta arasındaki bölümü belirtiliyorsa
   location_type = "road_segment" kullan.
6. İki yolun kesiştiği belirtiliyorsa
   location_type = "intersection" kullan.
7. Açık bir adres belirtiliyorsa location_type = "address" kullan.
8. Bir bina, tesis veya bilinen nokta belirtiliyorsa
   location_type = "landmark" kullan.
9. Emin olmadığın durumda location_type = "unknown" kullan.
10. Geocoder için kullanılabilecek sorguları oluştur.
11. confidence değerini 0 ile 1 arasında ver.
12. Metinde olmayan bilgileri uydurmak yerine ilgili alanı null bırak.

Güzergah metni:

{guzergah}
"""


# Gemini çağrısı
response = client.models.generate_content(
    model="gemini-3.6-flash",
    contents=prompt,
    config={
        "response_mime_type": "application/json",
        "response_schema": RoadAnalysis,
    },
)


# Gemini'nin parse edilmiş sonucu
result = response.parsed


print("\n--- GEMINI SONUCU ---")

print("Konum tipi :", result.location_type)
print("Yol adı    :", result.road_name)
print("Başlangıç  :", result.start_query)
print("Bitiş      :", result.end_query)
print("Yol sorgusu:", result.road_query)
print("Confidence :", result.confidence)


# --------------------------------------------------
# GEOCODER
# --------------------------------------------------

print("\n--- GEOCODER ---")

geolocator = Nominatim(
    user_agent="YolKapama/1.0"
)


def geocode_query(label, query):
    """Verilen sorguyu geocode eder ve sonucu ekrana yazdırır."""

    if not query:
        print(f"\n{label}: Sorgu yok.")
        return None

    print(f"\n{label}:")
    print(f"Sorgu: {query}")

    try:
        location = geolocator.geocode(
            query,
            exactly_one=True
        )

        if location:
            print(f"Adres: {location.address}")
            print(f"Latitude: {location.latitude}")
            print(f"Longitude: {location.longitude}")

            return location

        print("Konum bulunamadı.")
        return None

    except Exception as e:
        print("Geocoder hatası:")
        print(type(e).__name__)
        print(e)

        return None


# Ana yol
road_location = geocode_query(
    "ANA YOL",
    result.road_query
)


# Başlangıç noktası
start_location = geocode_query(
    "BAŞLANGIÇ NOKTASI",
    result.start_query
)


# Bitiş noktası
end_location = geocode_query(
    "BİTİŞ NOKTASI",
    result.end_query
)

# --------------------------------------------------
# HARİTA
# --------------------------------------------------



print("\n--- HARİTA ---")


# Haritada kullanılacak noktaları topla
map_points = []

if road_location:
    map_points.append(
        (
            road_location.latitude,
            road_location.longitude,
            "Ana Yol"
        )
    )

if start_location:
    map_points.append(
        (
            start_location.latitude,
            start_location.longitude,
            "Başlangıç"
        )
    )

if end_location:
    map_points.append(
        (
            end_location.latitude,
            end_location.longitude,
            "Bitiş"
        )
    )


if not map_points:

    print("Harita oluşturulamadı. Hiç koordinat bulunamadı.")

else:

    # İlk bulunan noktayı haritanın merkezi yap
    first_point = map_points[0]

    m = folium.Map(
        location=[
            first_point[0],
            first_point[1]
        ],
        zoom_start=15
    )


    # --------------------------------------------------
    # NOKTALARI HARİTAYA EKLE
    # --------------------------------------------------

    for latitude, longitude, label in map_points:

        folium.Marker(
            location=[
                latitude,
                longitude
            ],
            popup=label
        ).add_to(m)


    # --------------------------------------------------
    # BAŞLANGIÇ → BİTİŞ ÇİZGİSİ
    # --------------------------------------------------

    if start_location and end_location:

        folium.PolyLine(
            locations=[
                [
                    start_location.latitude,
                    start_location.longitude
                ],
                [
                    end_location.latitude,
                    end_location.longitude
                ]
            ],
            weight=5,
            popup="Yol kapatma segmenti"
        ).add_to(m)


    # --------------------------------------------------
    # HARİTAYI KAYDET
    # --------------------------------------------------

    map_file = "road_map.html"

    m.save(map_file)

    print(f"Harita oluşturuldu: {map_file}")