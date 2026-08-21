import hashlib
import json
import os
import re
import sqlite3
from datetime import date, datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import folium

from AI_düzenleme import analyze_route
from konum import konum_bul_ai


# ============================================================
# AYARLAR
# ============================================================

load_dotenv()

URL = os.getenv("URL")

if not URL:
    raise ValueError("URL .env dosyasında bulunamadı.")

DB_PATH = "output/yol_kapama.db"

MAP_PATH = "output/road_map.html"


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

            -- Kaynak sitedeki "İLÇE-MAHALLE" hücresi
            -- (AI tahminine gerek kalmadan, sitenin verdiği
            -- kesin ilçe/mahalle bilgisi)
            ilce_mahalle_raw TEXT,
            ilce TEXT,
            mahalle TEXT,

            -- Python tarafından ayrıştırılan tarih
            start_date TEXT,
            end_date TEXT,
            active INTEGER NOT NULL DEFAULT 0,

            -- AI sonucu
            ai_status TEXT NOT NULL DEFAULT 'pending',
            ai_location_type TEXT,
            ai_neighborhood TEXT,
            ai_district TEXT,
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

    # --------------------------------------------------------
    # Eski veritabanlarında ai_neighborhood / ai_district
    # sütunları olmayabilir (tablo daha önce CREATE TABLE IF
    # NOT EXISTS ile oluşturulmuş olabilir). Var olan bir
    # veritabanını bozmadan sütunları sonradan ekliyoruz.
    # --------------------------------------------------------

    for column_def in (
        "ai_neighborhood TEXT",
        "ai_district TEXT",
        "ilce_mahalle_raw TEXT",
        "ilce TEXT",
        "mahalle TEXT"
    ):
        try:
            connection.execute(
                f"ALTER TABLE road_closures ADD COLUMN {column_def}"
            )
        except sqlite3.OperationalError:
            # Sütun zaten var, sorun değil.
            pass

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
# AKTİF DURUMUNU GÜNCELLE
# ============================================================

def refresh_active_status(connection):
    """
    Tüm kayıtların active durumunu BUGÜNÜN tarihine göre yeniden
    hesaplar.

    save_record() içindeki active değeri sadece kayıt ilk eklenirken
    hesaplanıyor; zaman geçip end_date'i dolan kayıtlar bu fonksiyon
    çalıştırılmadan active=1 olarak kalmaya devam eder ve haritada
    süresi geçmiş yol kapamaları görünmeye devam eder.

    Bu fonksiyon her çalıştırmada (harita oluşturulmadan ve AI/konum
    kuyrukları çekilmeden ÖNCE) çağrılmalıdır.
    """

    today = date.today().isoformat()
    now = datetime.now().isoformat(timespec="seconds")

    connection.execute(
        """
        UPDATE road_closures
        SET
            active = CASE
                WHEN start_date IS NOT NULL
                     AND end_date IS NOT NULL
                     AND start_date <= ?
                     AND end_date >= ?
                THEN 1
                ELSE 0
            END,
            updated_at = ?
        """,
        (today, today, now)
    )

    connection.commit()


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

            indexes = {
                "guzergah": values.index("GÜZERGAH"),
                "tarih": values.index("TARİH"),
                "kapanma_nedeni": values.index("KAPANMA NEDENİ"),
                "header_row": row
            }

            # İLÇE-MAHALLE sütunu her sitede olmayabilir,
            # bulunamazsa None bırakıyoruz; extract_records
            # bu durumu ele alır.
            if "İLÇE-MAHALLE" in values:
                indexes["ilce_mahalle"] = values.index("İLÇE-MAHALLE")
            else:
                indexes["ilce_mahalle"] = None

            return indexes

    raise ValueError(
        "GÜZERGAH, TARİH ve KAPANMA NEDENİ başlıkları bulunamadı."
    )


# ============================================================
# TABLO KAYITLARINI ÇIKAR
# ============================================================

def extract_records(table):
    """
    Target table içerisinden istediğimiz sütunları çıkarır.
    """

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

        # Veri satırı değilse geç
        required_indexes = [
            guzergah_index,
            tarih_index,
            neden_index
        ]

        if ilce_mahalle_index is not None:
            required_indexes.append(ilce_mahalle_index)

        max_index = max(required_indexes)

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

        ilce_mahalle_raw = None
        ilce = None
        mahalle = None

        if ilce_mahalle_index is not None:

            ilce_mahalle_raw = clean_text(
                cells[ilce_mahalle_index].get_text(" ", strip=True)
            )

            ilce, mahalle = split_ilce_mahalle(ilce_mahalle_raw)

        # Tamamen boş satırları alma
        if not guzergah and not tarih and not kapanma_nedeni:
            continue

        records.append(
            {
                "guzergah": guzergah,
                "tarih": tarih,
                "kapanma_nedeni": kapanma_nedeni,
                "ilce_mahalle_raw": ilce_mahalle_raw,
                "ilce": ilce,
                "mahalle": mahalle
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

    Format beklenenden farklıysa (tire yoksa) ilçe olarak
    tüm metni döner, mahalle None kalır.
    """

    if not ilce_mahalle_raw:
        return None, None

    # Sadece ilk tireden ayır; mahalle adının içinde
    # tire geçme ihtimaline karşı (örn. "Kaklık-Yeni Mahalle"
    # gibi bir durum olursa mahalle kısmı bozulmasın).
    parts = ilce_mahalle_raw.split("-", 1)

    if len(parts) == 2:
        ilce = parts[0].strip()
        mahalle = parts[1].strip()
        return ilce or None, mahalle or None

    return ilce_mahalle_raw.strip() or None, None


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
    ilce_mahalle_raw = record.get("ilce_mahalle_raw")
    ilce = record.get("ilce")
    mahalle = record.get("mahalle")

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
            ilce_mahalle_raw,
            ilce,
            mahalle,
            start_date,
            end_date,
            active,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_hash,
            guzergah,
            tarih,
            kapanma_nedeni,
            ilce_mahalle_raw,
            ilce,
            mahalle,
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
    AI_düzenleme.analyze_route()'un döndürdüğü sonucu mevcut
    kayda bağlar.

    AI'nın tarih veya kapanma nedenini değiştirmesine izin verilmez.
    """

    connection.execute(
        """
        UPDATE road_closures
        SET
            ai_status = ?,
            ai_location_type = ?,
            ai_neighborhood = ?,
            ai_district = ?,
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
            ai_result.get("neighborhood"),
            ai_result.get("district"),
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
# AI SONUCU KAYDEDEMEDİĞİMİZ DURUM
# ============================================================

def save_ai_error(connection, record_id, error_message):
    """
    AI çağrısı başarısız olduğunda kaydı 'error' olarak işaretler.

    Böylece bir sonraki çalıştırmada tekrar denenebilir hale
    getirmek istersen ai_status = 'pending' yapılabilir; şimdilik
    başarısız denemeleri ayırt etmek için 'error' kullanıyoruz.
    """

    connection.execute(
        """
        UPDATE road_closures
        SET
            ai_status = ?,
            ai_result = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            "error",
            json.dumps(
                {"error": error_message},
                ensure_ascii=False
            ),
            datetime.now().isoformat(timespec="seconds"),
            record_id
        )
    )

    connection.commit()


# ============================================================
# GEOCODER SONUCUNU SQLITE'A YAZ
# ============================================================

def save_geocode_result(
    connection,
    record_id,
    result
):
    """
    konum.konum_bul_ai()'nin döndürdüğü AdresSonucu'ndan türetilen
    sade bir sözlüğü (ya da bulunamadıysa None'ı) kaydeder.
    """

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
    """
    AI analizi hiç yapılmamış (pending) VEYA daha önce yapılmış ama
    başarısız/eksik kalmış (error, ya da ai_status='success' olduğu
    halde ai_location_type/ai_result boş kalmış) aktif kayıtları getirir.

    Böylece sadece yeni eklenen kayıtlar değil, DB'de AI hücresi boş
    kalmış eski kayıtlar da bir sonraki çalıştırmada tekrar AI'ya
    gönderilir.
    """

    cursor = connection.execute(
        """
        SELECT
            id,
            guzergah_raw,
            ilce,
            mahalle
        FROM road_closures
        WHERE
            active = 1
            AND (
                ai_status IS NULL
                OR ai_status != 'success'
                OR ai_result IS NULL
                OR ai_location_type IS NULL
            )
        ORDER BY id
        """
    )

    return cursor.fetchall()


# ============================================================
# KONUMU EKSİK/BAŞARISIZ KAYITLARI GETİR
# ============================================================

def get_pending_geocode_records(connection):
    """
    AI analizi başarıyla tamamlanmış (dolayısıyla road_query/start_query/
    end_query gibi bilgiler elde) ama konum/koordinat hâlâ bulunamamış
    veya geometry alanı boş kalmış aktif kayıtları getirir.

    Bu kayıtlar için AI tekrar çağrılmaz; sadece daha önce kaydedilmiş
    ai_result JSON'ı kullanılarak konum.py yeniden denenir.
    """

    cursor = connection.execute(
        """
        SELECT
            id,
            guzergah_raw,
            ilce,
            mahalle,
            ai_result
        FROM road_closures
        WHERE
            active = 1
            AND ai_status = 'success'
            AND ai_result IS NOT NULL
            AND (
                geocode_status IS NULL
                OR geocode_status != 'success'
                OR geocode_result IS NULL
                OR geometry IS NULL
            )
        ORDER BY id
        """
    )

    return cursor.fetchall()


# ============================================================
# KONUM BULMA (TEK KAYIT İÇİN - ORTAK ADIM)
# ============================================================

def _process_konum_for_record(
    connection,
    record_id,
    ai_result,
    web_mahalle,
    web_ilce
):
    """
    Elde zaten bir ai_result varken (ister AI'dan yeni gelmiş, ister
    daha önce DB'ye kaydedilmiş) konum.konum_bul_ai() çağırıp sonucu
    kaydeden ortak adım.

    Kaynak sitedeki İLÇE-MAHALLE hücresi AI'nin metinden tahmin
    ettiğinden çok daha güvenilir (kesin veri), bu yüzden
    web_mahalle/web_ilce konum.py'ye ayrıca gönderilir ve orada AI
    tahminine göre önceliklendirilir.
    """

    effective_neighborhood = web_mahalle or ai_result.get("neighborhood")
    effective_district = web_ilce or ai_result.get("district")

    print(f"AI konum tipi : {ai_result.get('location_type')}")
    print(f"Mahalle       : {effective_neighborhood} (AI: {ai_result.get('neighborhood')}, Web: {web_mahalle})")
    print(f"İlçe          : {effective_district} (AI: {ai_result.get('district')}, Web: {web_ilce})")
    print(f"Confidence    : {ai_result.get('confidence')}")

    konum_sonucu = konum_bul_ai(
        ai_result,
        web_mahalle=web_mahalle,
        web_ilce=web_ilce,
    )

    if konum_sonucu is None:
        print("Konum bulunamadı.")
        save_geocode_result(connection, record_id, None)
        return

    print(f"Konum bulundu ({konum_sonucu.kaynak}): {konum_sonucu.gosterim_adi}")

    if not konum_sonucu.tam_eslesme:
        print(f"UYARI: Yaklaşık/şüpheli eşleşme - {konum_sonucu.uyari}")

    save_geocode_result(
        connection,
        record_id,
        {
            "latitude": konum_sonucu.lat,
            "longitude": konum_sonucu.lon,
            "display_name": konum_sonucu.gosterim_adi,
            "tam_eslesme": konum_sonucu.tam_eslesme,
            "uyari": konum_sonucu.uyari,
            "kaynak": konum_sonucu.kaynak,
        }
    )

    if konum_sonucu.geojson:
        save_geometry(connection, record_id, konum_sonucu.geojson)
    else:
        point_geometry = {
            "type": "Point",
            "coordinates": [konum_sonucu.lon, konum_sonucu.lat],
        }
        save_geometry(connection, record_id, point_geometry)


# ============================================================
# AI + KONUM (BEKLEYEN/EKSİK KAYITLARI İŞLE)
# ============================================================

def process_pending_ai(connection, pending_records):
    """
    Her bekleyen/eksik kayıt için:

    1. AI_düzenleme.analyze_route() ile güzergah metnini analiz ettirir
       ve sonucu (ai_result dict) SQLite'a yazar.
    2. _process_konum_for_record() ile konum.py'yi çağırıp sonucu kaydeder.

    main.py; Gemini, Nominatim veya OSRM detaylarını bilmez, sadece
    iki modülün döndürdüğü sonuçları SQLite'a yazar.
    """

    if not pending_records:
        print("AI analizi bekleyen/eksik kayıt yok.")
        return

    print(f"\n{len(pending_records)} kayıt için AI analizi başlıyor...")

    for row in pending_records:

        record_id = row[0]
        guzergah_raw = row[1]
        web_ilce = row[2]
        web_mahalle = row[3]

        print(f"\n[{record_id}] {guzergah_raw}")

        if web_ilce or web_mahalle:
            print(f"Kaynak ilçe/mahalle: {web_ilce} / {web_mahalle}")

        # --------------------------------------------------
        # 1) AI ANALİZİ (AI_düzenleme.py'den)
        # --------------------------------------------------

        ai_result = analyze_route(guzergah_raw)

        if ai_result is None:
            print("AI analizi başarısız, kayıt atlanıyor.")

            save_ai_error(
                connection,
                record_id,
                "AI analizi başarısız oldu."
            )

            continue

        save_ai_result(
            connection,
            record_id,
            ai_result
        )

        # --------------------------------------------------
        # 2) KONUM BULMA (konum.py'den)
        # --------------------------------------------------

        _process_konum_for_record(
            connection,
            record_id,
            ai_result,
            web_mahalle,
            web_ilce
        )


# ============================================================
# SADECE KONUM (AI ZATEN BAŞARILI AMA KOORDİNAT/GEOMETRİ EKSİK)
# ============================================================

def process_pending_geocode(connection, pending_records):
    """
    AI analizi daha önce başarıyla tamamlanmış ama konum/koordinat
    hâlâ bulunamamış (ya da geometry alanı boş kalmış) kayıtları,
    AI'yı TEKRAR ÇAĞIRMADAN, DB'deki kayıtlı ai_result JSON'ını
    kullanarak sadece konum.py üzerinden yeniden dener.

    Böylece Nominatim/OSRM geçici olarak cevap vermediği ya da eski
    kayıtlarda koordinat hiç bulunamamışsa, gereksiz yere AI'yı tekrar
    çağırmadan sadece eksik kalan koordinat/geometri tamamlanır.
    """

    if not pending_records:
        print("Konumu eksik/başarısız kayıt yok.")
        return

    print(f"\n{len(pending_records)} kayıt için konum yeniden deneniyor...")

    for row in pending_records:

        record_id, guzergah_raw, web_ilce, web_mahalle, ai_result_raw = row

        print(f"\n[{record_id}] {guzergah_raw}")

        try:
            ai_result = json.loads(ai_result_raw)
        except (TypeError, ValueError):
            print("Kayıtlı AI sonucu okunamadı (bozuk JSON), atlanıyor.")
            continue

        _process_konum_for_record(
            connection,
            record_id,
            ai_result,
            web_mahalle,
            web_ilce
        )


# ============================================================
# DİNAMİK HARİTA
# ============================================================

def build_map(connection):
    """
    Veritabanındaki konumlanmış tüm aktif kayıtları tek bir
    HTML haritada gösterir.

    Her çalıştırmada önce eski harita dosyası silinir, sonra SADECE
    o an (bugünün tarihine göre, bkz. refresh_active_status) aktif
    olan kayıtlarla yeniden oluşturulur. Böylece tarihi geçmiş yol
    kapamaları haritada asılı kalmaz.
    """

    if os.path.exists(MAP_PATH):
        os.remove(MAP_PATH)
        print(f"\nEski harita silindi: {MAP_PATH}")

    cursor = connection.execute(
        """
        SELECT
            id,
            guzergah_raw,
            geocode_result,
            geometry_type,
            geometry
        FROM road_closures
        WHERE
            active = 1
            AND (geocode_result IS NOT NULL OR geometry IS NOT NULL)
        """
    )

    rows = cursor.fetchall()

    if not rows:
        print("\nHaritada gösterilecek konumlanmış aktif kayıt yok.")
        return

    # İlk bulunan noktayı haritanın merkezi yap
    first_point = None

    for row in rows:

        geocode_result = row[2]

        if geocode_result:
            data = json.loads(geocode_result)
            first_point = (data["latitude"], data["longitude"])
            break

    if first_point is None:
        print("\nHarita için merkez nokta bulunamadı.")
        return

    m = folium.Map(
        location=list(first_point),
        zoom_start=13
    )

    for row in rows:

        record_id, guzergah_raw, geocode_result, geometry_type, geometry = row

        if geocode_result:

            data = json.loads(geocode_result)

            folium.Marker(
                location=[data["latitude"], data["longitude"]],
                popup=guzergah_raw
            ).add_to(m)

        if geometry and geometry_type == "LineString":

            geo = json.loads(geometry)

            coords = [
                [lat, lon]
                for lon, lat in geo["coordinates"]
            ]

            folium.PolyLine(
                locations=coords,
                weight=5,
                popup=guzergah_raw
            ).add_to(m)

    os.makedirs("output", exist_ok=True)

    m.save(MAP_PATH)

    print(f"\nHarita oluşturuldu: {MAP_PATH}")


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
    # AKTİF DURUMUNU BUGÜNE GÖRE YENİDEN HESAPLA
    #
    # Sadece yeni eklenen kayıtlar değil, süresi dolmuş eski
    # kayıtlar da burada active=0'a çekilir; böylece AI/konum
    # kuyrukları ve harita her zaman bugüne göre doğru olur.
    # --------------------------------------------------------

    refresh_active_status(connection)

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
    #
    # Sadece yeni eklenen (pending) değil, DB'de AI hücresi boş
    # kalmış veya daha önce hata almış aktif kayıtlar da dahildir.
    # --------------------------------------------------------

    pending_records = get_pending_ai_records(
        connection
    )

    print(
        f"AI analizi bekleyen/eksik aktif kayıt: "
        f"{len(pending_records)}"
    )

    # --------------------------------------------------------
    # AI + KONUM (AI_düzenleme.py ve konum.py üzerinden)
    # --------------------------------------------------------

    process_pending_ai(
        connection,
        pending_records
    )

    # --------------------------------------------------------
    # SADECE KONUMU EKSİK KALAN KAYITLAR
    #
    # AI'sı başarılı ama koordinat/geometri hâlâ boş kalmış (Nominatim/
    # OSRM o an cevap vermemiş, ya da eski kayıtlarda hiç bulunamamış)
    # kayıtlar; AI tekrar çağrılmadan sadece konum.py ile denenir.
    # --------------------------------------------------------

    pending_geocode_records = get_pending_geocode_records(
        connection
    )

    print(
        f"Konumu eksik/başarısız aktif kayıt: "
        f"{len(pending_geocode_records)}"
    )

    process_pending_geocode(
        connection,
        pending_geocode_records
    )

    # --------------------------------------------------------
    # HARİTA
    # --------------------------------------------------------

    build_map(connection)

    connection.close()

    print("İşlem tamamlandı.")


if __name__ == "__main__":
    main()