# -*- coding: utf-8 -*-
"""
main.py
=======
Belediye yol kapama duyurularını çeken, AI ile analiz eden, konum
bulan ve Supabase'e yazan ana pipeline.

DEĞİŞİKLİK (Supabase entegrasyonu):
- Kalıcılık katmanı artık SQLite değil, db_supabase.py üzerinden Supabase.
- Harita artık burada üretilmiyor (folium kaldırıldı) — harita Nuxt
  tarafında Leaflet ile doğrudan Supabase'ten okunan verilerle çiziliyor.
- Giriş noktası `run_pipeline()` olarak adlandırıldı; Flask (app.py) bu
  fonksiyonu import edip çağırıyor. Script tek başına da çalıştırılabilir
  (`python main.py`), böylece Flask olmadan da yerelde test edilebilir.

main.py; Gemini, Nominatim, OSRM veya Supabase SDK detaylarını bilmez —
sadece AI_düzenleme.py, konum.py ve db_supabase.py modüllerinin
döndürdüğü sonuçları birbirine bağlar.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import date, datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import db_supabase as db
from AI_düzenleme import analyze_route
from konum import konum_bul_ai

# ============================================================
# AYARLAR
# ============================================================

load_dotenv()

URL = os.getenv("URL")

if not URL:
    raise ValueError("URL .env dosyasında bulunamadı.")


# ============================================================
# METİN TEMİZLEME
# ============================================================

def clean_text(text: str) -> str:
    """
    HTML içerisindeki gereksiz boşlukları normalize eder.

    Önemli: anlamlı kelimeleri silmez, sadece çoklu whitespace
    karakterlerini tek boşluğa indirir.
    """

    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)

    return text.strip()


# ============================================================
# TARİH AYRIŞTIRMA
# ============================================================

def parse_dates(text: str):
    """
    Aşağıdaki gibi tarihleri destekler:

    04.08.2026 - 28.08.2026
    04.08.2026 – 28.08.2026 tarihleri arasında
    10.08.2026 tarihinde

    Dönen değer: start_date, end_date
    """

    matches = re.findall(r"\b(\d{2}\.\d{2}\.\d{4})\b", text)

    if not matches:
        return None, None

    parsed_dates = []

    for value in matches:
        try:
            parsed_dates.append(datetime.strptime(value, "%d.%m.%Y").date())
        except ValueError:
            continue

    if not parsed_dates:
        return None, None

    if len(parsed_dates) == 1:
        return parsed_dates[0], parsed_dates[0]

    return parsed_dates[0], parsed_dates[1]


# ============================================================
# AKTİF KAYIT KONTROLÜ
# ============================================================

def is_active(start_date: Optional[date], end_date: Optional[date]) -> bool:
    """Bugün, start_date ile end_date arasındaysa True döner."""

    if start_date is None or end_date is None:
        return False

    today = date.today()

    return start_date <= today <= end_date


# ============================================================
# KAYIT HASH
# ============================================================

def create_record_hash(guzergah: str, tarih: str, kapanma_nedeni: str) -> str:
    raw_value = f"{guzergah.strip()}|{tarih.strip()}|{kapanma_nedeni.strip()}"
    return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()


# ============================================================
# HTML TABLOSUNU BUL
# ============================================================

def find_target_table(soup: BeautifulSoup):
    """GÜZERGAH, TARİH ve KAPANMA NEDENİ başlıklarını içeren tabloyu bulur."""

    for table in soup.find_all("table"):

        cells = table.find_all(["th", "td"])

        headers = [
            clean_text(cell.get_text(" ", strip=True)).upper()
            for cell in cells
        ]

        if (
            "GÜZERGAH" in headers
            and "TARİH" in headers
            and "KAPANMA NEDENİ" in headers
        ):
            return table

    return None


# ============================================================
# TABLO BAŞLIKLARINI BUL
# ============================================================

def find_column_indexes(table):
    """
    Sütunların kaçıncı sırada olduğunu başlık adına göre bulur.
    Böylece sütun sırası değişse bile kod çalışmaya devam eder.
    """

    rows = table.find_all("tr")

    if not rows:
        raise ValueError("Tabloda satır bulunamadı.")

    for row in rows:

        cells = row.find_all(["th", "td"])

        values = [
            clean_text(cell.get_text(" ", strip=True)).upper()
            for cell in cells
        ]

        if (
            "GÜZERGAH" in values
            and "TARİH" in values
            and "KAPANMA NEDENİ" in values
        ):

            indexes = {
                "guzergah": values.index("GÜZERGAH"),
                "tarih": values.index("TARİH"),
                "kapanma_nedeni": values.index("KAPANMA NEDENİ"),
                "header_row": row,
            }

            if "İLÇE-MAHALLE" in values:
                indexes["ilce_mahalle"] = values.index("İLÇE-MAHALLE")
            else:
                indexes["ilce_mahalle"] = None

            return indexes

    raise ValueError("GÜZERGAH, TARİH ve KAPANMA NEDENİ başlıkları bulunamadı.")


# ============================================================
# TABLO KAYITLARINI ÇIKAR
# ============================================================

def extract_records(table):
    """Target table içerisinden istediğimiz sütunları çıkarır."""

    columns = find_column_indexes(table)

    guzergah_index = columns["guzergah"]
    tarih_index = columns["tarih"]
    neden_index = columns["kapanma_nedeni"]
    ilce_mahalle_index = columns["ilce_mahalle"]

    records = []

    for row in table.find_all("tr"):

        if row is columns["header_row"]:
            continue

        cells = row.find_all("td")

        required_indexes = [guzergah_index, tarih_index, neden_index]

        if ilce_mahalle_index is not None:
            required_indexes.append(ilce_mahalle_index)

        max_index = max(required_indexes)

        if len(cells) <= max_index:
            continue

        guzergah = clean_text(cells[guzergah_index].get_text(" ", strip=True))
        tarih = clean_text(cells[tarih_index].get_text(" ", strip=True))
        kapanma_nedeni = clean_text(cells[neden_index].get_text(" ", strip=True))

        ilce_mahalle_raw = None
        ilce = None
        mahalle = None

        if ilce_mahalle_index is not None:
            ilce_mahalle_raw = clean_text(
                cells[ilce_mahalle_index].get_text(" ", strip=True)
            )
            ilce, mahalle = split_ilce_mahalle(ilce_mahalle_raw)

        if not guzergah and not tarih and not kapanma_nedeni:
            continue

        records.append(
            {
                "guzergah": guzergah,
                "tarih": tarih,
                "kapanma_nedeni": kapanma_nedeni,
                "ilce_mahalle_raw": ilce_mahalle_raw,
                "ilce": ilce,
                "mahalle": mahalle,
            }
        )

    return records


# ============================================================
# İLÇE-MAHALLE HÜCRESİNİ AYRIŞTIR
# ============================================================

def split_ilce_mahalle(ilce_mahalle_raw: str | None):
    """
    Kaynak sitedeki "İLÇE-MAHALLE" hücresini ayrıştırır.

    Örnek:
        "Pamukkale-Pınarkent"   -> ("Pamukkale", "Pınarkent")
        "Merkezefendi-Şemikler" -> ("Merkezefendi", "Şemikler")
    """

    if not ilce_mahalle_raw:
        return None, None

    parts = ilce_mahalle_raw.split("-", 1)

    if len(parts) == 2:
        ilce = parts[0].strip()
        mahalle = parts[1].strip()
        return ilce or None, mahalle or None

    return ilce_mahalle_raw.strip() or None, None


# ============================================================
# KONUM BULMA (TEK KAYIT İÇİN - ORTAK ADIM)
# ============================================================

def _process_konum_for_record(
    record_id,
    ai_result,
    web_mahalle,
    web_ilce,
):
    """
    Elde zaten bir ai_result varken konum.konum_bul_ai() çağırıp
    sonucu Supabase'e kaydeden ortak adım.
    """

    effective_neighborhood = web_mahalle or ai_result.get("neighborhood")
    effective_district = web_ilce or ai_result.get("district")

    print(f"AI konum tipi : {ai_result.get('location_type')}")
    print(
        f"Mahalle       : {effective_neighborhood} "
        f"(AI: {ai_result.get('neighborhood')}, Web: {web_mahalle})"
    )
    print(
        f"İlçe          : {effective_district} "
        f"(AI: {ai_result.get('district')}, Web: {web_ilce})"
    )
    print(f"Confidence    : {ai_result.get('confidence')}")

    konum_sonucu = konum_bul_ai(
        ai_result,
        web_mahalle=web_mahalle,
        web_ilce=web_ilce,
    )

    if konum_sonucu is None:
        print("Konum bulunamadı.")
        db.save_geocode_result(record_id, None)
        return

    print(f"Konum bulundu ({konum_sonucu.kaynak}): {konum_sonucu.gosterim_adi}")

    if not konum_sonucu.tam_eslesme:
        print(f"UYARI: Yaklaşık/şüpheli eşleşme - {konum_sonucu.uyari}")

    db.save_geocode_result(
        record_id,
        {
            "latitude": konum_sonucu.lat,
            "longitude": konum_sonucu.lon,
            "display_name": konum_sonucu.gosterim_adi,
            "tam_eslesme": konum_sonucu.tam_eslesme,
            "uyari": konum_sonucu.uyari,
            "kaynak": konum_sonucu.kaynak,
        },
    )

    if konum_sonucu.geojson:
        db.save_geometry(record_id, konum_sonucu.geojson)
    else:
        point_geometry = {
            "type": "Point",
            "coordinates": [konum_sonucu.lon, konum_sonucu.lat],
        }
        db.save_geometry(record_id, point_geometry)


# ============================================================
# AI + KONUM (BEKLEYEN/EKSİK KAYITLARI İŞLE)
# ============================================================

def process_pending_ai(pending_records):
    """
    Her bekleyen/eksik kayıt için:
    1. AI_düzenleme.analyze_route() ile analiz ettirir, sonucu Supabase'e yazar.
    2. _process_konum_for_record() ile konum.py'yi çağırıp sonucu kaydeder.
    """

    if not pending_records:
        print("AI analizi bekleyen/eksik kayıt yok.")
        return

    print(f"\n{len(pending_records)} kayıt için AI analizi başlıyor...")

    for row in pending_records:

        record_id = row["id"]
        guzergah_raw = row["guzergah_raw"]
        web_ilce = row.get("ilce")
        web_mahalle = row.get("mahalle")

        print(f"\n[{record_id}] {guzergah_raw}")

        if web_ilce or web_mahalle:
            print(f"Kaynak ilçe/mahalle: {web_ilce} / {web_mahalle}")

        ai_result = analyze_route(guzergah_raw)

        if ai_result is None:
            print("AI analizi başarısız, kayıt atlanıyor.")
            db.save_ai_error(record_id, "AI analizi başarısız oldu.")
            continue

        db.save_ai_result(record_id, ai_result)

        _process_konum_for_record(record_id, ai_result, web_mahalle, web_ilce)


# ============================================================
# SADECE KONUM (AI ZATEN BAŞARILI AMA KOORDİNAT/GEOMETRİ EKSİK)
# ============================================================

def process_pending_geocode(pending_records):
    """
    AI analizi başarıyla tamamlanmış ama konum/koordinat hâlâ
    bulunamamış kayıtları, AI'yı TEKRAR ÇAĞIRMADAN, kayıtlı ai_result
    kullanarak sadece konum.py üzerinden yeniden dener.
    """

    if not pending_records:
        print("Konumu eksik/başarısız kayıt yok.")
        return

    print(f"\n{len(pending_records)} kayıt için konum yeniden deneniyor...")

    for row in pending_records:

        record_id = row["id"]
        guzergah_raw = row["guzergah_raw"]
        web_ilce = row.get("ilce")
        web_mahalle = row.get("mahalle")
        ai_result = row.get("ai_result")

        print(f"\n[{record_id}] {guzergah_raw}")

        if not ai_result:
            print("Kayıtlı AI sonucu bulunamadı, atlanıyor.")
            continue

        _process_konum_for_record(record_id, ai_result, web_mahalle, web_ilce)


# ============================================================
# ANA PROGRAM (Flask'ın çağırdığı giriş noktası)
# ============================================================

def run_pipeline() -> None:
    """
    Tüm pipeline'ı bir kez baştan sona çalıştırır:
    scrape -> Supabase'e kaydet -> AI analizi -> konum bulma.

    Flask (app.py) bu fonksiyonu arka plan thread'inde çağırır.
    Script tek başına da çalıştırılabilir: `python main.py`
    """

    print("Yol Kapama pipeline'ı başlatılıyor...")
    print(f"URL: {URL}")

    # --------------------------------------------------------
    # Web sitesi
    # --------------------------------------------------------

    response = requests.get(URL, timeout=30)
    response.raise_for_status()

    print(f"HTTP Durum Kodu: {response.status_code}")

    # --------------------------------------------------------
    # BeautifulSoup
    # --------------------------------------------------------

    soup = BeautifulSoup(response.text, "html.parser")

    target_table = find_target_table(soup)

    if target_table is None:
        raise RuntimeError(
            "GÜZERGAH, TARİH ve KAPANMA NEDENİ sütunlarını içeren tablo bulunamadı."
        )

    print("Hedef tablo bulundu.")

    # --------------------------------------------------------
    # 3 sütunu çıkar
    # --------------------------------------------------------

    records = extract_records(target_table)

    print(f"Toplam {len(records)} kayıt bulundu.")

    # --------------------------------------------------------
    # Supabase'e kaydet
    # --------------------------------------------------------

    new_count = 0
    existing_count = 0

    for record in records:

        record_hash = create_record_hash(
            record["guzergah"], record["tarih"], record["kapanma_nedeni"]
        )

        start_date_value, end_date_value = parse_dates(record["tarih"])
        active = is_active(start_date_value, end_date_value)

        inserted = db.save_record(
            record, record_hash, start_date_value, end_date_value, active
        )

        if inserted:
            new_count += 1
        else:
            existing_count += 1

    print(f"Yeni kayıt: {new_count}")
    print(f"Zaten kayıtlı: {existing_count}")

    # --------------------------------------------------------
    # AKTİF DURUMUNU BUGÜNE GÖRE YENİDEN HESAPLA
    # --------------------------------------------------------

    db.refresh_active_status()

    # --------------------------------------------------------
    # AI'YA GİDECEK KAYITLAR
    # --------------------------------------------------------

    pending_records = db.get_pending_ai_records()

    print(f"AI analizi bekleyen/eksik aktif kayıt: {len(pending_records)}")

    process_pending_ai(pending_records)

    # --------------------------------------------------------
    # SADECE KONUMU EKSİK KALAN KAYITLAR
    # --------------------------------------------------------

    pending_geocode_records = db.get_pending_geocode_records()

    print(f"Konumu eksik/başarısız aktif kayıt: {len(pending_geocode_records)}")

    process_pending_geocode(pending_geocode_records)

    print("İşlem tamamlandı.")


if __name__ == "__main__":
    run_pipeline()
