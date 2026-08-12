import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import date, datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


# ============================================================
# AYARLAR
# ============================================================

load_dotenv()

URL = os.getenv("URL")

if not URL:
    raise ValueError("URL .env dosyasında bulunamadı.")

DB_PATH = "output/yol_kapama.db"

# Nominatim için uygulamayı tanımlayan User-Agent
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

USER_AGENT = "YolKapama/1.0 (local development project)"

# Nominatim kamu sunucusunu zorlamamak için
GEOCODER_DELAY = 1.1

# OSRM
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"


# ============================================================
# VERİTABANI
# ============================================================

def create_database():
    os.makedirs("output", exist_ok=True)

    connection = sqlite3.connect(DB_PATH)

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS road_closures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            record_hash TEXT NOT NULL UNIQUE,

            -- Kaynaktan gelen ham veriler
            guzergah_raw TEXT NOT NULL,
            tarih_raw TEXT NOT NULL,
            kapanma_nedeni_raw TEXT NOT NULL,

            -- Python tarafından ayrıştırılan tarih
            start_date TEXT,
            end_date TEXT,
            active INTEGER NOT NULL DEFAULT 0,

            -- AI sonucu
            ai_status TEXT NOT NULL DEFAULT 'pending',
            ai_location_type TEXT,
            ai_query TEXT,
            ai_start_query TEXT,
            ai_end_query TEXT,
            ai_result TEXT,

            -- Geocoder sonucu
            geocode_status TEXT NOT NULL DEFAULT 'pending',
            geocode_result TEXT,

            -- Harita geometrisi
            geometry_type TEXT,
            geometry TEXT,

            -- Sistem
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    connection.commit()

    return connection


# ============================================================
# METİN TEMİZLEME
# ============================================================

def clean_text(text: str) -> str:
    """
    HTML içerisindeki gereksiz boşlukları normalize eder.

    Önemli:
    Anlamlı kelimeleri silmez.
    Sadece çoklu whitespace karakterlerini tek boşluğa indirir.
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

    Dönen değer:

    start_date, end_date
    """

    matches = re.findall(
        r"\b(\d{2}\.\d{2}\.\d{4})\b",
        text
    )

    if not matches:
        return None, None

    parsed_dates = []

    for value in matches:
        try:
            parsed_dates.append(
                datetime.strptime(value, "%d.%m.%Y").date()
            )
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

def is_active(start_date: Optional[date], end_date: Optional[date]) -> int:
    """
    Bilgisayarın bugünkü tarihini kullanır.

    Bugün başlangıç ve bitiş tarihi arasındaysa:
        1

    Değilse:
        0
    """

    if start_date is None or end_date is None:
        return 0

    today = date.today()

    if start_date <= today <= end_date:
        return 1

    return 0


# ============================================================
# KAYIT HASH
# ============================================================

def create_record_hash(
    guzergah: str,
    tarih: str,
    kapanma_nedeni: str
) -> str:

    raw_value = (
        guzergah.strip()
        + "|"
        + tarih.strip()
        + "|"
        + kapanma_nedeni.strip()
    )

    return hashlib.sha256(
        raw_value.encode("utf-8")
    ).hexdigest()


# ============================================================
# HTML TABLOSUNU BUL
# ============================================================

def find_target_table(soup: BeautifulSoup):
    """
    GÜZERGAH, TARİH ve KAPANMA NEDENİ başlıklarını içeren
    tabloyu bulur.
    """

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

    Böylece sütun sırası değişse bile kod mümkün olduğunca
    çalışmaya devam eder.
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

            return {
                "guzergah": values.index("GÜZERGAH"),
                "tarih": values.index("TARİH"),
                "kapanma_nedeni": values.index("KAPANMA NEDENİ"),
                "header_row": row
            }

    raise ValueError(
        "GÜZERGAH, TARİH ve KAPANMA NEDENİ başlıkları bulunamadı."
    )


# ============================================================
# TABLO KAYITLARINI ÇIKAR
# ============================================================

def extract_records(table):
    """
    Target table içerisinden yalnızca istediğimiz üç sütunu çıkarır.
    """

    columns = find_column_indexes(table)

    guzergah_index = columns["guzergah"]
    tarih_index = columns["tarih"]
    neden_index = columns["kapanma_nedeni"]

    records = []

    for row in table.find_all("tr"):

        if row is columns["header_row"]:
            continue

        cells = row.find_all("td")

        # Veri satırı değilse geç
        max_index = max(
            guzergah_index,
            tarih_index,
            neden_index
        )

        if len(cells) <= max_index:
            continue

        guzergah = clean_text(
            cells[guzergah_index].get_text(" ", strip=True)
        )

        tarih = clean_text(
            cells[tarih_index].get_text(" ", strip=True)
        )

        kapanma_nedeni = clean_text(
            cells[neden_index].get_text(" ", strip=True)
        )

        # Tamamen boş satırları alma
        if not guzergah and not tarih and not kapanma_nedeni:
            continue

        records.append(
            {
                "guzergah": guzergah,
                "tarih": tarih,
                "kapanma_nedeni": kapanma_nedeni
            }
        )

    return records


# ============================================================
# SQLITE'A KAYDET
# ============================================================

def save_record(connection, record):
    """
    Daha önce aynı kayıt eklenmişse tekrar eklemez.

    record_hash:
        guzergah + tarih + kapanma_nedeni
    """

    guzergah = record["guzergah"]
    tarih = record["tarih"]
    kapanma_nedeni = record["kapanma_nedeni"]

    record_hash = create_record_hash(
        guzergah,
        tarih,
        kapanma_nedeni
    )

    start_date, end_date = parse_dates(tarih)

    active = is_active(
        start_date,
        end_date
    )

    now = datetime.now().isoformat(timespec="seconds")

    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO road_closures (
            record_hash,
            guzergah_raw,
            tarih_raw,
            kapanma_nedeni_raw,
            start_date,
            end_date,
            active,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_hash,
            guzergah,
            tarih,
            kapanma_nedeni,
            start_date.isoformat() if start_date else None,
            end_date.isoformat() if end_date else None,
            active,
            now,
            now
        )
    )

    connection.commit()

    if cursor.rowcount == 0:
        return False

    return True


# ============================================================
# AI SONUCU SQLITE'A YAZ
# ============================================================

def save_ai_result(
    connection,
    record_id,
    ai_result
):
    """
    AI'nın döndürdüğü sonucu mevcut kayda bağlar.

    AI'nın tarih veya kapanma nedenini değiştirmesine izin verilmez.
    """

    connection.execute(
        """
        UPDATE road_closures
        SET
            ai_status = ?,
            ai_location_type = ?,
            ai_query = ?,
            ai_start_query = ?,
            ai_end_query = ?,
            ai_result = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            "success",
            ai_result.get("location_type"),
            ai_result.get("query"),
            ai_result.get("start_query"),
            ai_result.get("end_query"),
            json.dumps(
                ai_result,
                ensure_ascii=False
            ),
            datetime.now().isoformat(timespec="seconds"),
            record_id
        )
    )

    connection.commit()


# ============================================================
# NOMINATIM GEOCODER
# ============================================================

def geocode(query: str):
    """
    Nominatim üzerinden konum arar.

    Sonuç:
        {
            "latitude": ...,
            "longitude": ...,
            ...
        }

    """

    if not query:
        return None

    headers = {
        "User-Agent": USER_AGENT
    }

    params = {
        "q": query,
        "format": "jsonv2",
        "limit": 1,
        "countrycodes": "tr"
    }

    response = requests.get(
        NOMINATIM_URL,
        params=params,
        headers=headers,
        timeout=30
    )

    response.raise_for_status()

    results = response.json()

    # Nominatim kamu servisini zorlamamak için
    time.sleep(GEOCODER_DELAY)

    if not results:
        return None

    result = results[0]

    return {
        "latitude": float(result["lat"]),
        "longitude": float(result["lon"]),
        "display_name": result.get("display_name"),
        "osm_type": result.get("osm_type"),
        "osm_id": result.get("osm_id")
    }


# ============================================================
# GEOCODER SONUCUNU SQLITE'A YAZ
# ============================================================

def save_geocode_result(
    connection,
    record_id,
    result
):
    if result is None:

        connection.execute(
            """
            UPDATE road_closures
            SET
                geocode_status = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                "not_found",
                datetime.now().isoformat(timespec="seconds"),
                record_id
            )
        )

        connection.commit()

        return

    connection.execute(
        """
        UPDATE road_closures
        SET
            geocode_status = ?,
            geocode_result = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            "success",
            json.dumps(
                result,
                ensure_ascii=False
            ),
            datetime.now().isoformat(timespec="seconds"),
            record_id
        )
    )

    connection.commit()


# ============================================================
# OSRM ROUTE GEOMETRY
# ============================================================

def get_route_geometry(
    start_lat,
    start_lon,
    end_lat,
    end_lon
):
    """
    İki koordinat arasındaki gerçek yol geometrisini alır.

    OSRM GeoJSON döndürür.
    """

    coordinates = (
        f"{start_lon},{start_lat};"
        f"{end_lon},{end_lat}"
    )

    url = f"{OSRM_URL}/{coordinates}"

    params = {
        "overview": "full",
        "geometries": "geojson"
    }

    response = requests.get(
        url,
        params=params,
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    if data.get("code") != "Ok":
        return None

    routes = data.get("routes", [])

    if not routes:
        return None

    return routes[0]["geometry"]


# ============================================================
# GEOMETRİYİ SQLITE'A YAZ
# ============================================================

def save_geometry(
    connection,
    record_id,
    geometry
):
    if geometry is None:
        return

    connection.execute(
        """
        UPDATE road_closures
        SET
            geometry_type = ?,
            geometry = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            geometry.get("type"),
            json.dumps(
                geometry,
                ensure_ascii=False
            ),
            datetime.now().isoformat(timespec="seconds"),
            record_id
        )
    )

    connection.commit()


# ============================================================
# YENİ KAYITLARI GETİR
# ============================================================

def get_pending_ai_records(connection):

    cursor = connection.execute(
        """
        SELECT
            id,
            guzergah_raw
        FROM road_closures
        WHERE
            active = 1
            AND ai_status = 'pending'
        ORDER BY id
        """
    )

    return cursor.fetchall()


# ============================================================
# ANA PROGRAM
# ============================================================

def main():

    print("Yol Kapama uygulaması başlatılıyor...")
    print(f"URL: {URL}")

    # --------------------------------------------------------
    # SQLite
    # --------------------------------------------------------

    connection = create_database()

    print(f"SQLite: {DB_PATH}")

    # --------------------------------------------------------
    # Web sitesi
    # --------------------------------------------------------

    response = requests.get(
        URL,
        timeout=30
    )

    response.raise_for_status()

    print(
        f"HTTP Durum Kodu: {response.status_code}"
    )

    # --------------------------------------------------------
    # BeautifulSoup
    # --------------------------------------------------------

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    target_table = find_target_table(soup)

    if target_table is None:
        raise RuntimeError(
            "GÜZERGAH, TARİH ve KAPANMA NEDENİ "
            "sütunlarını içeren tablo bulunamadı."
        )

    print("Hedef tablo bulundu.")

    # --------------------------------------------------------
    # 3 sütunu çıkar
    # --------------------------------------------------------

    records = extract_records(target_table)

    print(
        f"Toplam {len(records)} kayıt bulundu."
    )

    # --------------------------------------------------------
    # SQLite
    # --------------------------------------------------------

    new_count = 0
    existing_count = 0

    for record in records:

        inserted = save_record(
            connection,
            record
        )

        if inserted:
            new_count += 1
        else:
            existing_count += 1

    print(
        f"Yeni kayıt: {new_count}"
    )

    print(
        f"Zaten kayıtlı: {existing_count}"
    )

    # --------------------------------------------------------
    # Aktif kayıtlar
    # --------------------------------------------------------

    cursor = connection.execute(
        """
        SELECT COUNT(*)
        FROM road_closures
        WHERE active = 1
        """
    )

    active_count = cursor.fetchone()[0]

    print(
        f"Aktif kayıt: {active_count}"
    )

    # --------------------------------------------------------
    # AI'YA GİDECEK KAYITLAR
    # --------------------------------------------------------

    pending_records = get_pending_ai_records(
        connection
    )

    print(
        f"AI analizi bekleyen aktif kayıt: "
        f"{len(pending_records)}"
    )

    # --------------------------------------------------------
    # AI
    # --------------------------------------------------------
    #
    # Burada henüz Gemini/OpenAI vb. bağlamıyoruz.
    #
    # AI'ya sadece:
    #
    #     guzergah_raw
    #
    # gönderilecek.
    #
    # AI sonucunda örneğin:
    #
    # {
    #     "location_type": "route",
    #     "query": "Süleyman Demirel Caddesi, Denizli",
    #     "start_query": "5049 Sokak, Denizli",
    #     "end_query": "6673 Sokak, Denizli"
    # }
    #
    # dönecek.
    #
    # Sonrasında save_ai_result() kullanılacak.
    #
    # --------------------------------------------------------

    print(
        "AI katmanı hazır. "
        "AI sağlayıcısı henüz bağlanmadı."
    )

    connection.close()

    print("İşlem tamamlandı.")


if __name__ == "__main__":
    main()