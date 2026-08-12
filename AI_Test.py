import os

from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel
from typing import Literal


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
5049 Sokak ile 6673 Sokak arasında kalan Süleyman Demirel Caddesi’ nin yan tali yolunda (Denizli Özel Cerrahi Hastanesi ile Petrol Ofisi arasında kalan kısım)
"""


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