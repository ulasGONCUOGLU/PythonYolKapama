# -*- coding: utf-8 -*-
"""
AI_düzenleme.py
================
Güzergah metnini (ör. "75 Sokak") yapay zeka (Gemini) ile analiz edip,
konum.py'nin (geocoder) kullanabileceği sorgu bileşenlerine ayıran modül.

main.py bu modülden SADECE `analyze_route(guzergah_raw)` fonksiyonunu
çağırır ve karşılığında aşağıdaki gibi bir sözlük (dict) alır — ya da
hata durumunda None:

    {
        "location_type": "road" | "road_segment" | "intersection" |
                          "address" | "landmark" | "unknown",
        "road_name": str | None,
        "neighborhood": str | None,
        "district": str | None,
        "query": str | None,        # road_query
        "start_query": str | None,
        "end_query": str | None,
        "confidence": float,
    }

Gemini çağrısı, prompt oluşturma ve pydantic şeması tamamen bu dosyada
tutulur; main.py bunların hiçbirini bilmek zorunda değildir.
"""

from __future__ import annotations

import os
from typing import Literal, Optional

from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel

# ============================================================
# AYARLAR
# ============================================================

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY .env dosyasında bulunamadı.")

GEMINI_MODEL = "gemini-3.6-flash"

gemini_client = genai.Client(
    api_key=GEMINI_API_KEY
)


# ============================================================
# AI VERİ MODELİ
# ============================================================

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

    # Adres bağlamı: mahalle ve ilçe
    # Geocoder'a HER zaman bu bilgilerle birlikte gidilecek,
    # aksi halde aynı isimli sokak/cadde başka bir mahallede
    # bulunabiliyor (örn. Denizli'de birden fazla "5049 Sokak").
    neighborhood: str | None
    district: str | None

    road_query: str | None
    start_query: str | None
    end_query: str | None

    confidence: float


# ============================================================
# AI PROMPT
# ============================================================

def build_ai_prompt(guzergah_raw: str) -> str:

    return f"""
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
13. Metinde bir mahalle adı geçiyorsa (örn. "Yenişehir Mahallesi",
    "Şirinköy Mahallesi") bunu neighborhood alanına yaz.
    "Mahallesi" / "Mah." gibi ekleri kaldırıp sadece mahalle
    adını yaz (örn. "Yenişehir").
14. Metinde bir ilçe adı geçiyorsa (örn. "Merkezefendi",
    "Pamukkale", "Honaz") bunu district alanına yaz.
15. road_query, start_query ve end_query alanlarına SADECE
    yol/cadde/sokak adını yaz (örn. "75 Sokak"). Mahalle ve
    ilçe bilgisini bu alanlara ekleme; onlar ayrı ayrı
    neighborhood ve district alanlarına yazılacak ve geocoder
    sorgusu program tarafından birleştirilecek.
16. Mahalle veya ilçe metinde hiç geçmiyorsa neighborhood veya
    district alanını null bırak, tahmin etme.

Güzergah metni:

{guzergah_raw}
"""


# ============================================================
# AI ÇAĞRISI (dahili)
# ============================================================

def _analyze_route_with_ai(guzergah_raw: str) -> Optional[RoadAnalysis]:
    """
    Gemini'ye güzergah metnini gönderir, RoadAnalysis olarak
    parse edilmiş sonucu döndürür.

    Hata durumunda None döner.
    """

    prompt = build_ai_prompt(guzergah_raw)

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": RoadAnalysis,
            },
        )

        return response.parsed

    except Exception as e:
        print("AI çağrısı hatası:")
        print(type(e).__name__)
        print(e)

        return None


# ============================================================
# DIŞARIYA AÇILAN TEK FONKSİYON
# ============================================================

def analyze_route(guzergah_raw: str) -> Optional[dict]:
    """
    main.py'nin çağırdığı tek giriş noktası.

    Güzergah metnini AI ile analiz eder, sonucu sade bir sözlük
    (dict) olarak döndürür. AI çağrısı başarısız olursa None
    döner; main.py bu durumu 'error' olarak kaydedip devam eder.
    """

    analysis = _analyze_route_with_ai(guzergah_raw)

    if analysis is None:
        return None

    return {
        "location_type": analysis.location_type,
        "road_name": analysis.road_name,
        "neighborhood": analysis.neighborhood,
        "district": analysis.district,
        "query": analysis.road_query,
        "start_query": analysis.start_query,
        "end_query": analysis.end_query,
        "confidence": analysis.confidence,
    }
