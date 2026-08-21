# -*- coding: utf-8 -*-
"""
konum.py
========
İl / İlçe / Mahalle / Sokak bilgisinden Nominatim (OpenStreetMap) geocoding
servisini kullanarak koordinat (enlem, boylam - WGS84) ve mümkünse yol
geometrisi (LineString) bulan modül.

Bu dosya, önceki "konbul.py" prototipinin üzerine main.py'nin ihtiyaç
duyduğu tek bir giriş fonksiyonunu ekler: `konum_bul_ai(ai_result, ...)`.

main.py bu modülden SADECE `konum_bul_ai(...)` fonksiyonunu çağırır ve
karşılığında bir AdresSonucu nesnesi (ya da bulunamazsa None) alır.
main.py, Nominatim/OSRM detaylarını, sokak varyasyonu deneme mantığını
vb. bilmek zorunda değildir.

Kullanım kuralları (ÖNEMLİ):
- Nominatim'in kullanım politikası gereği saniyede en fazla 1 istek atılmalı
  ve isteklerde tanımlayıcı bir User-Agent gönderilmeli. Bu modül ikisini de
  otomatik yapar (istekler arası minimum bekleme + sabit User-Agent).
- Yoğun/production kullanım için kendi Nominatim sunucunuzu kurmanız ya da
  ücretli bir sağlayıcıya (Google, HERE, Mapbox vb.) geçmeniz önerilir.

Geometri stratejisi (road / road_segment):
1. Önce Nominatim'den, yolun kendi geometrisi (polygon_geojson ->
   LineString) istenir. Bulunursa (tam yol/cadde çizgisi) bu kullanılır,
   OSRM'e gidilmez.
2. road_segment için Nominatim'den tam çizgi gelmezse, start_query ve
   end_query ayrı ayrı geocode edilir ve aralarında OSRM ile sürüş
   rotası (LineString) hesaplanır (yedek/son çare).
3. Hiçbiri olmazsa, bulunabilen tek nokta (Point) döner.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"

TIMEOUT = 10  # saniye (Nominatim)
OSRM_TIMEOUT = 30  # saniye (OSRM)
MIN_ISTEK_ARALIGI = 1.1  # saniye (Nominatim kuralı: sn'de max 1 istek)

# Nominatim, User-Agent'sız veya jenerik User-Agent'lı istekleri engelleyebilir.
# Buradaki adresi kendi proje/iletişim bilginle değiştirmen iyi olur.
HEADERS = {
    "User-Agent": "konum.py/1.0 (adres-koordinat-arac; iletisim: ornek@eposta.com)",
    "Accept-Language": "tr",
}

_son_istek_zamani = 0.0


def _rate_limit_bekle() -> None:
    """Nominatim kuralına uymak için istekler arasında en az MIN_ISTEK_ARALIGI kadar bekler."""
    global _son_istek_zamani
    gecen = time.monotonic() - _son_istek_zamani
    if gecen < MIN_ISTEK_ARALIGI:
        time.sleep(MIN_ISTEK_ARALIGI - gecen)
    _son_istek_zamani = time.monotonic()


@dataclass
class AdresSonucu:
    lat: float
    lon: float
    gosterim_adi: str  # Nominatim'in döndürdüğü tam adres metni
    tip: str = ""       # örn. "residential", "highway" vb. (osm class/type)
    onem: float = 0.0    # Nominatim "importance" skoru (sıralama için)
    geojson: Optional[Dict[str, Any]] = None  # varsa çizgi/alan geometrisi (WGS84)
    arama_terimi: str = ""       # ne aradık (örn. "10157 Sokak")
    bulunan_yol_adi: str = ""    # Nominatim'in gerçekte döndürdüğü sokak/cadde adı
    tam_eslesme: bool = True     # aranan ile bulunan GERÇEKTEN aynı sokak mı?
    uyari: Optional[str] = None  # tam_eslesme False ise açıklayıcı uyarı metni
    kaynak: str = "nominatim"    # "nominatim" ya da "osrm" (rota kaynağı)
    ham_veri: Optional[Dict[str, Any]] = field(default=None)

    @property
    def koordinat(self):
        return (self.lat, self.lon)

    @property
    def cizgi_mi(self) -> bool:
        """Sonuç bir sokak/cadde gibi çizgi (LineString/MultiLineString) mi?"""
        return bool(self.geojson and "LineString" in self.geojson.get("type", ""))

    @property
    def geometry_type(self) -> str:
        """main.py'nin DB'ye yazdığı geometry_type ile uyumlu bir değer döndürür."""
        if self.geojson:
            return self.geojson.get("type", "Point")
        return "Point"

    def __str__(self) -> str:
        geo_bilgi = f" [{self.geojson.get('type')}]" if self.geojson else ""
        durum = "✓ TAM EŞLEŞME" if self.tam_eslesme else f"⚠ YAKLAŞIK ({self.uyari})"
        return f"{self.gosterim_adi} -> {self.lat:.6f}, {self.lon:.6f}{geo_bilgi} [{durum}]"


def _sorgu_metni_olustur(
    sokak: Optional[str] = None,
    mahalle: Optional[str] = None,
    ilce: Optional[str] = None,
    il: str = "Denizli",
    ulke: str = "Türkiye",
) -> str:
    """Verilen adres parçalarını Nominatim'in sevdiği serbest metin formatına birleştirir."""
    parcalar = [p.strip() for p in (sokak, mahalle, ilce, il, ulke) if p and p.strip()]
    return ", ".join(parcalar)


def _nominatim_ara(sorgu: str, limit: int = 5, session: Optional[requests.Session] = None) -> List[Dict[str, Any]]:
    """Nominatim /search uç noktasına istek atar, ham JSON sonuç listesini döndürür."""
    _rate_limit_bekle()
    sess = session or requests
    params = {
        "q": sorgu,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": limit,
        "countrycodes": "tr",
        "polygon_geojson": 1,  # sokak/cadde için LineString, alan için Polygon geometrisi de gelsin
    }
    resp = sess.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _sokak_varyasyonlari(sokak: str) -> List[str]:
    """
    Sokak adını, sonundan kelime kelime kısaltarak varyasyon listesi üretir.

    Örnek: "Fatih Sultan Mehmet /1 Caddesi"
        -> "Fatih Sultan Mehmet /1 Caddesi"
        -> "Fatih Sultan Mehmet /1"
        -> "Fatih Sultan Mehmet"
        -> "Fatih Sultan"
        -> "Fatih"

    Nominatim'de "Caddesi/Sokak/Bulvarı" gibi ek kelimeler bazen adres
    kayıtlarıyla birebir uyuşmuyor; bu yüzden en spesifikten en kısaya doğru
    dener ve ilk eşleşeni kullanır.
    """
    kelimeler = sokak.strip().split()
    if not kelimeler:
        return []

    varyasyonlar = []
    for i in range(len(kelimeler), 0, -1):
        aday = " ".join(kelimeler[:i]).strip(" -–,")
        if aday and aday not in varyasyonlar:
            varyasyonlar.append(aday)
    return varyasyonlar


_YOL_EK_KELIMELERI = {
    "sokak", "sokağı", "sok", "sk", "cadde", "caddesi", "cad", "cd",
    "bulvar", "bulvarı", "bulv", "blv", "mahallesi", "mah",
}


def _normalize_basit(text: str) -> str:
    """Türkçe karakterleri sadeleştirip küçük harfe çevirir (karşılaştırma için)."""
    if not text:
        return ""
    text = text.strip().lower()
    ceviri = str.maketrans({
        "ı": "i", "İ": "i", "ş": "s", "ğ": "g",
        "ü": "u", "ö": "o", "ç": "c",
    })
    return text.translate(ceviri)


def _sayi_cek(text: str) -> Optional[str]:
    """Metindeki ilk sayı dizisini çeker (örn. '10157 Sk' -> '10157')."""
    sayi = ""
    for ch in text:
        if ch.isdigit():
            sayi += ch
        elif sayi:
            break
    return sayi or None


def _cekirdek_isim(text: str) -> str:
    """Yol adından ek kelimeleri (Sokak/Cadde/Bulvar vb.) atıp çekirdek ismi döndürür."""
    kelimeler = _normalize_basit(text).replace(".", "").replace("/", " ").split()
    kalan = [k for k in kelimeler if k not in _YOL_EK_KELIMELERI]
    return " ".join(kalan)


def _eslesme_kontrolu(arama_terimi: str, bulunan_yol_adi: str) -> tuple:
    """
    Aranan sokak adı ile Nominatim'in gerçekte döndürdüğü sokak adını karşılaştırır.

    Döndürür: (tam_eslesme: bool, uyari: Optional[str])
    """
    if not bulunan_yol_adi:
        return True, None  # karşılaştıracak bir şey yok, varsayılan olarak güven

    arama_sayi = _sayi_cek(arama_terimi)
    bulunan_sayi = _sayi_cek(bulunan_yol_adi)

    # --- Numaralı sokak durumu (10157 Sokak gibi) - EN KRİTİK KONTROL ---
    if arama_sayi:
        if bulunan_sayi and bulunan_sayi != arama_sayi:
            return False, (
                f"'{arama_terimi}' arandı ama '{bulunan_yol_adi}' bulundu "
                f"(sokak numarası uyuşmuyor: {arama_sayi} != {bulunan_sayi})"
            )
        if not bulunan_sayi:
            return False, (
                f"'{arama_terimi}' arandı ama bulunan '{bulunan_yol_adi}' "
                f"numarasız/farklı bir sokak"
            )
        return True, None  # sayılar eşleşiyor

    # --- İsimli sokak durumu (Hasan Gönüllü Sokak gibi) ---
    cekirdek_arama = _cekirdek_isim(arama_terimi)
    cekirdek_bulunan = _cekirdek_isim(bulunan_yol_adi)

    if not cekirdek_arama:
        return True, None

    if cekirdek_arama == cekirdek_bulunan:
        return True, None
    if cekirdek_arama in cekirdek_bulunan or cekirdek_bulunan in cekirdek_arama:
        # Kısaltma denemesi (örn. "Fatih Sultan Mehmet" içinde "Fatih Sultan") - kabul et
        return True, None

    return False, (
        f"'{arama_terimi}' arandı ama '{bulunan_yol_adi}' bulundu - isimler örtüşmüyor"
    )


_IDARI_SINIR_TIPLERI = {
    "administrative", "suburb", "neighbourhood", "neighborhood", "quarter",
    "city_district", "borough", "town", "village", "municipality", "city",
    "county", "state_district", "region",
}


def _idari_sinir_mi(item: Dict[str, Any]) -> bool:
    """
    Nominatim sonucunun bir SOKAK/CADDE değil, bir idari sınır
    (mahalle/ilçe/il vb. poligonu) olup olmadığını tespit eder.
    """
    osm_class = (item.get("class") or "").lower()
    osm_type = (item.get("type") or "").lower()
    if osm_class == "boundary":
        return True
    if osm_type in _IDARI_SINIR_TIPLERI:
        return True
    return False


# Nominatim'in address bloğunda mahalle/semt bilgisini taşıyabilecek alanlar.
_MAHALLE_ADRES_ALANLARI = (
    "suburb", "neighbourhood", "quarter", "city_district",
    "borough", "town", "village", "hamlet",
)


def _mahalle_uyusuyor_mu(
    address: Dict[str, Any],
    hedef_mahalle: Optional[str],
) -> Optional[bool]:
    """
    Nominatim sonucunun address bloğundaki mahalle/semt alanlarını hedef
    mahalle ile karşılaştırır.

    Bu kontrol, sokak adı aynı olsa bile YANLIŞ mahalledeki (ör. "Süleyman
    Demirel Caddesi" hem Zeytinköy'de hem Güzelköy'de geçiyorsa) bir sonucun
    sessizce kabul edilmesini engellemek için var.

    Dönüş:
        True  -> en az bir adres alanı hedef mahalle ile eşleşiyor
        False -> adres alanları var ama HİÇBİRİ eşleşmiyor (kesin FARKLI
                 bir mahalle) -> bu sonuç asla kabul edilmemeli
        None  -> hedef mahalle verilmemiş, ya da sonuçta hiç mahalle/semt
                 bilgisi yok (doğrulanamıyor, temkinli kabul edilebilir)
    """
    if not hedef_mahalle:
        return None

    hedef_norm = _normalize_basit(hedef_mahalle)

    if not hedef_norm:
        return None

    bulunan_degerler = [
        address.get(alan)
        for alan in _MAHALLE_ADRES_ALANLARI
        if address.get(alan)
    ]

    if not bulunan_degerler:
        return None  # doğrulanamıyor

    for deger in bulunan_degerler:
        deger_norm = _normalize_basit(deger)
        if hedef_norm == deger_norm or hedef_norm in deger_norm or deger_norm in hedef_norm:
            return True

    return False


def _dene(
    sorgu: str,
    arama_terimi: str = "",
    mahalle: Optional[str] = None,
    debug: bool = False,
    session: Optional[requests.Session] = None,
) -> Optional[AdresSonucu]:
    """
    Tek bir sorgu metnini Nominatim'e gönderir, en iyi (ilk) sonucu
    AdresSonucu olarak döndürür.

    `mahalle` verilmişse (ve sokak aranıyorsa), sonuçlar programatik olarak
    da mahalle bilgisine göre süzülür: adres bloğunda AÇIKÇA FARKLI bir
    mahalle/semt yazan sonuçlar -- sorgu metninde mahalle geçmese bile --
    tamamen elenir. Böylece "sokak + mahalle" ile bulunamayan bir sonuç,
    mahallesiz denemede rastgele başka bir mahalledeki aynı isimli sokağa
    sessizce kaymaz.
    """
    try:
        sonuclar_ham = _nominatim_ara(sorgu, session=session)
    except requests.RequestException as e:
        if debug:
            print(f"[UYARI] '{sorgu}' sorgusunda ağ hatası: {e}")
        return None

    if debug:
        print(f"Sorgu: '{sorgu}' -> {len(sonuclar_ham)} sonuç")
        for s in sonuclar_ham:
            print("  ", s.get("display_name"), "|", s.get("lat"), s.get("lon"),
                  "| importance:", s.get("importance"))

    if not sonuclar_ham:
        return None

    if arama_terimi:
        # SOKAK arıyorsak: mahalle/ilçe/il gibi idari sınır sonuçlarını
        # TAMAMEN ele - bunlar hiçbir zaman (yaklaşık olarak bile) sokak
        # yerine geçmemeli. Sadece yol-seviyeli (highway vb.) sonuçları al.
        aday_liste = [s for s in sonuclar_ham if not _idari_sinir_mi(s)]
        if not aday_liste:
            if debug:
                print("  [!] Yalnızca idari sınır (mahalle/ilçe) sonucu geldi, "
                      "sokak seviyesinde eşleşme yok -> bu sonuç TAMAMEN reddedildi")
            return None

        # --------------------------------------------------------
        # MAHALLE DOĞRULAMASI (kritik): sorgu metninde mahalle geçmese
        # bile, dönen sonucun adres bloğu AÇIKÇA farklı bir mahalleye
        # aitse o aday tamamen elenir. Aynı isimli sokak başka bir
        # mahallede varsa oraya kaymasını engeller.
        # --------------------------------------------------------
        mahalle_belirsiz = False

        if mahalle:
            eslesenler = []
            belirsizler = []

            for s in aday_liste:
                durum = _mahalle_uyusuyor_mu(s.get("address", {}), mahalle)
                if durum is True:
                    eslesenler.append(s)
                elif durum is None:
                    belirsizler.append(s)
                # durum is False -> tamamen ele, listeye alma

            if eslesenler:
                aday_liste = eslesenler
            elif belirsizler:
                aday_liste = belirsizler
                mahalle_belirsiz = True
            else:
                if debug:
                    print(f"  [!] '{mahalle}' mahallesiyle eşleşen sonuç yok, "
                          f"tüm adaylar FARKLI bir mahalleye ait -> TAMAMEN reddedildi")
                return None
    else:
        aday_liste = sonuclar_ham
        mahalle_belirsiz = False

    # Nominatim sonuçları "importance" skoruna göre sıralı gelir; ilkini alıyoruz.
    en_iyi = aday_liste[0]
    bulunan_yol_adi = en_iyi.get("address", {}).get("road") or en_iyi.get("name") or ""

    tam_eslesme, uyari = _eslesme_kontrolu(arama_terimi, bulunan_yol_adi) if arama_terimi else (True, None)

    if mahalle_belirsiz:
        # Mahalle uyuşmazlığı YOK ama doğrulanamadı da (Nominatim'in bu
        # sonucunda hiç mahalle/semt bilgisi yok). Kesin FARKLI mahalle
        # olan adaylar zaten yukarıda tamamen elendi; bu sadece
        # "emin değiliz" uyarısıdır.
        ek_uyari = f"'{mahalle}' mahallesi doğrulanamadı (Nominatim sonucunda mahalle/semt bilgisi yok)"
        tam_eslesme = False
        uyari = f"{uyari}; {ek_uyari}" if uyari else ek_uyari

    if debug and not tam_eslesme:
        print(f"  [!] EŞLEŞME UYARISI: {uyari}")

    return AdresSonucu(
        lat=float(en_iyi["lat"]),
        lon=float(en_iyi["lon"]),
        gosterim_adi=en_iyi.get("display_name", ""),
        tip=en_iyi.get("type", ""),
        onem=float(en_iyi.get("importance", 0.0)),
        geojson=en_iyi.get("geojson"),
        arama_terimi=arama_terimi,
        bulunan_yol_adi=bulunan_yol_adi,
        tam_eslesme=tam_eslesme,
        uyari=uyari,
        kaynak="nominatim",
        ham_veri=en_iyi,
    )


def adres_konumu_bul(
    sokak: Optional[str] = None,
    mahalle: Optional[str] = None,
    ilce: Optional[str] = None,
    il: str = "Denizli",
    ulke: str = "Türkiye",
    debug: bool = False,
) -> Optional[AdresSonucu]:
    """
    İlçe / Mahalle / Sokak bilgisinden en uygun eşleşmeyi bulup koordinat döndürür.

    DAVRANIŞ (önemli):
    - Eğer `sokak` verilmişse: sokak adı sondan kelime kelime kısaltılarak
      (bkz. _sokak_varyasyonlari) her varyasyon Mahalle+İlçe ile birlikte
      denenir.
        * Bulunan sonucun GERÇEK sokak adı (Nominatim'in address.road alanı)
          aranan isimle karşılaştırılır (özellikle numaralı sokaklarda
          "10157 Sokak" arayıp "10147 Sokak" gibi FARKLI bir sokağın sessizce
          dönmesini engellemek için).
        * Bulunan sonucun adres bloğu (suburb/neighbourhood/quarter vb.)
          `mahalle` parametresiyle de karşılaştırılır (bkz.
          _mahalle_uyusuyor_mu). Sorgu metninde mahalle geçse bile
          Nominatim bunu göz ardı edip BAŞKA bir mahalledeki aynı/benzer
          isimli yolu döndürebiliyor; bu kontrol o sonucu tamamen eler.
        * TAM eşleşme (hem yol adı hem mahalle uyuyor) bulunursa hemen o
          döner (`tam_eslesme=True`).
        * Hiçbir varyasyon tam eşleşmezse, en azından bir sonuç bulunmuşsa
          (yaklaşık/belirsiz olabilir - ör. mahalle doğrulanamadı) o
          **işaretli** şekilde döner: `tam_eslesme=False`, `uyari="..."`
          açıklamasıyla.
        * Hiçbir sonuç (yaklaşık dahi) bulunamazsa **None** döner.
    - Eğer `sokak` verilmemişse: Mahalle/İlçe ile arama yapılır.

    Parametreler
    ------------
    sokak, mahalle, ilce : Adres bileşenleri.
    il, ulke             : Varsayılan olarak "Denizli" / "Türkiye".
    debug                : True ise denenen her sorgu ve sonucu ekrana yazdırılır.

    Döndürür
    --------
    AdresSonucu, ya da bulunamazsa None.
    """
    if not (sokak or mahalle or ilce):
        raise ValueError("En az sokak, mahalle veya ilçe bilgisi vermelisin.")

    # --- SOKAK VERİLMİŞSE: kısaltarak dene, önce TAM eşleşme ara ---
    if sokak:
        en_iyi_yaklasik: Optional[AdresSonucu] = None

        for varyasyon in _sokak_varyasyonlari(sokak):
            sorgu = _sorgu_metni_olustur(varyasyon, mahalle, ilce, il, ulke)
            sonuc = _dene(sorgu, arama_terimi=varyasyon, mahalle=mahalle, debug=debug)
            if sonuc:
                if sonuc.tam_eslesme:
                    return sonuc
                if en_iyi_yaklasik is None:
                    en_iyi_yaklasik = sonuc

            # Mahalle bilgisi bazı OSM kayıtlarında eksik/farklı olabilir;
            # aynı sokak varyasyonunu mahallesiz (sadece ilçe ile) de dene.
            # ÖNEMLİ: mahalle metni sorguya eklenmese bile, dönen sonucun
            # adres bloğu yine `mahalle` parametresiyle doğrulanır -
            # aksi halde sonuç sessizce BAŞKA bir mahalledeki aynı isimli
            # sokağa kayabilir (bkz. _mahalle_uyusuyor_mu).
            if mahalle and ilce:
                sorgu2 = _sorgu_metni_olustur(varyasyon, None, ilce, il, ulke)
                sonuc2 = _dene(sorgu2, arama_terimi=varyasyon, mahalle=mahalle, debug=debug)
                if sonuc2:
                    if sonuc2.tam_eslesme:
                        return sonuc2
                    if en_iyi_yaklasik is None:
                        en_iyi_yaklasik = sonuc2

        if en_iyi_yaklasik is not None:
            if debug:
                print(f"\n'{sokak}' için TAM eşleşme bulunamadı, en yakın (İŞARETLİ) sonuç dönülüyor: "
                      f"{en_iyi_yaklasik.uyari}")
            return en_iyi_yaklasik  # tam_eslesme=False olarak işaretli, ama None DEĞİL

        if debug:
            print(f"\n'{sokak}' için hiçbir sonuç (yaklaşık dahi) bulunamadı -> None dönülüyor.")
        return None  # gerçekten hiçbir şey bulunamadı

    # --- SOKAK VERİLMEMİŞSE: mahalle/ilçe seviyesinde ara ---
    for sorgu in filter(None, [
        _sorgu_metni_olustur(None, mahalle, ilce, il, ulke) if (mahalle or ilce) else None,
        _sorgu_metni_olustur(None, None, ilce, il, ulke) if (mahalle and ilce) else None,
    ]):
        sonuc = _dene(sorgu, debug=debug)
        if sonuc:
            return sonuc

    return None


# --------------------------------------------------------------------------- #
# OSRM (sadece road_segment için Nominatim'den çizgi gelmezse yedek olarak)
# --------------------------------------------------------------------------- #

def get_route_geometry(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
) -> Optional[Dict[str, Any]]:
    """
    İki koordinat arasındaki gerçek sürüş rotası geometrisini OSRM'den alır.

    OSRM GeoJSON (LineString) döndürür, hata/rota bulunamama durumunda None.
    """

    coordinates = f"{start_lon},{start_lat};{end_lon},{end_lat}"
    url = f"{OSRM_URL}/{coordinates}"

    params = {
        "overview": "full",
        "geometries": "geojson",
    }

    try:
        response = requests.get(url, params=params, timeout=OSRM_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as e:
        print("OSRM ağ hatası:")
        print(type(e).__name__)
        print(e)
        return None

    if data.get("code") != "Ok":
        return None

    routes = data.get("routes", [])

    if not routes:
        return None

    return routes[0]["geometry"]


def _osrm_sonucu_olustur(
    start_sonuc: AdresSonucu,
    end_sonuc: AdresSonucu,
    geometry: Dict[str, Any],
) -> AdresSonucu:
    """Başlangıç/bitiş nokta sonuçlarını ve OSRM geometrisini tek bir AdresSonucu'na paketler."""

    tam_eslesme = start_sonuc.tam_eslesme and end_sonuc.tam_eslesme

    uyari = None
    if not tam_eslesme:
        parcalar = [u for u in (start_sonuc.uyari, end_sonuc.uyari) if u]
        uyari = "Rota uçlarından en az biri tam eşleşmedi: " + " | ".join(parcalar) if parcalar else \
            "Rota uçlarından en az biri tam eşleşmedi."

    return AdresSonucu(
        lat=start_sonuc.lat,
        lon=start_sonuc.lon,
        gosterim_adi=f"{start_sonuc.gosterim_adi} → {end_sonuc.gosterim_adi}",
        tip="route",
        onem=0.0,
        geojson=geometry,
        arama_terimi=f"{start_sonuc.arama_terimi} - {end_sonuc.arama_terimi}",
        bulunan_yol_adi=start_sonuc.bulunan_yol_adi,
        tam_eslesme=tam_eslesme,
        uyari=uyari,
        kaynak="osrm",
        ham_veri={"start": start_sonuc.ham_veri, "end": end_sonuc.ham_veri},
    )


def _konum_bul_osrm_rota(
    start_query: str,
    end_query: str,
    mahalle: Optional[str],
    ilce: Optional[str],
    il: str,
    ulke: str,
    debug: bool,
) -> Optional[AdresSonucu]:
    """start_query ve end_query'yi ayrı ayrı geocode edip aralarında OSRM rotası ister."""

    start_sonuc = adres_konumu_bul(sokak=start_query, mahalle=mahalle, ilce=ilce, il=il, ulke=ulke, debug=debug)
    end_sonuc = adres_konumu_bul(sokak=end_query, mahalle=mahalle, ilce=ilce, il=il, ulke=ulke, debug=debug)

    if not start_sonuc and not end_sonuc:
        return None

    if not start_sonuc or not end_sonuc:
        # Sadece bir uç bulunabildiyse, en azından onu nokta olarak dön.
        return start_sonuc or end_sonuc

    if start_sonuc.lat == end_sonuc.lat and start_sonuc.lon == end_sonuc.lon:
        # İki uç da aynı noktaya düştü (örn. ikisi de mahalle merkezine
        # denk geldi); anlamsız bir rota istemek yerine noktayı dön.
        return start_sonuc

    geometry = get_route_geometry(
        start_sonuc.lat, start_sonuc.lon,
        end_sonuc.lat, end_sonuc.lon,
    )

    if not geometry:
        # OSRM rota bulamadı, en azından başlangıç noktasını dön.
        return start_sonuc

    return _osrm_sonucu_olustur(start_sonuc, end_sonuc, geometry)


def _konum_bul_yol(
    road_query: Optional[str],
    start_query: Optional[str],
    end_query: Optional[str],
    mahalle: Optional[str],
    ilce: Optional[str],
    il: str,
    ulke: str,
    debug: bool,
    segment: bool,
) -> Optional[AdresSonucu]:
    """location_type 'road' veya 'road_segment' için konum/geometri arar."""

    sokak_adi = road_query or start_query or end_query
    nokta_sonucu: Optional[AdresSonucu] = None

    if sokak_adi:
        nokta_sonucu = adres_konumu_bul(sokak=sokak_adi, mahalle=mahalle, ilce=ilce, il=il, ulke=ulke, debug=debug)

        if nokta_sonucu and nokta_sonucu.cizgi_mi:
            # Nominatim'den yolun kendi çizgisi geldi, yeterli -> OSRM'e gerek yok.
            return nokta_sonucu

    if segment and start_query and end_query:
        osrm_sonucu = _konum_bul_osrm_rota(start_query, end_query, mahalle, ilce, il, ulke, debug)
        if osrm_sonucu and osrm_sonucu.cizgi_mi:
            return osrm_sonucu
        if osrm_sonucu and nokta_sonucu is None:
            nokta_sonucu = osrm_sonucu

    # Çizgi/rota bulunamadı; elimizdeki en iyi nokta sonucunu dön (varsa).
    return nokta_sonucu


def _konum_bul_kesisim(
    road_query: Optional[str],
    start_query: Optional[str],
    end_query: Optional[str],
    mahalle: Optional[str],
    ilce: Optional[str],
    il: str,
    ulke: str,
    debug: bool,
) -> Optional[AdresSonucu]:
    """location_type 'intersection' için iki yol adını birlikte/ayrı ayrı dener."""

    isimler = [q for q in (road_query, start_query, end_query) if q]

    if len(isimler) >= 2:
        birlesik = " ile ".join(isimler[:2])
        sonuc = adres_konumu_bul(sokak=birlesik, mahalle=mahalle, ilce=ilce, il=il, ulke=ulke, debug=debug)
        if sonuc:
            return sonuc

    for isim in isimler:
        sonuc = adres_konumu_bul(sokak=isim, mahalle=mahalle, ilce=ilce, il=il, ulke=ulke, debug=debug)
        if sonuc:
            return sonuc

    return None


# --------------------------------------------------------------------------- #
# main.py'nin ÇAĞIRDIĞI TEK GİRİŞ NOKTASI
# --------------------------------------------------------------------------- #

def konum_bul_ai(
    ai_result: Dict[str, Any],
    web_mahalle: Optional[str] = None,
    web_ilce: Optional[str] = None,
    il: str = "Denizli",
    ulke: str = "Türkiye",
    debug: bool = False,
) -> Optional[AdresSonucu]:
    """
    main.py'den gelen AI analiz sonucunu (ai_result dict) alıp konum/geometri
    bulur. Kaynak sitedeki (web) mahalle/ilçe bilgisi, AI'nin tahmininden
    her zaman daha güvenilir kabul edilir ve önceliklidir.

    ai_result beklenen anahtarlar:
        location_type, neighborhood, district, query, start_query, end_query

    Döndürür
    --------
    AdresSonucu (nokta / çizgi / alan geometrisiyle), ya da bulunamazsa None.
    """

    mahalle = web_mahalle or ai_result.get("neighborhood")
    ilce = web_ilce or ai_result.get("district")

    location_type = ai_result.get("location_type")
    road_query = ai_result.get("query")
    start_query = ai_result.get("start_query")
    end_query = ai_result.get("end_query")

    if location_type in ("road", "road_segment"):
        return _konum_bul_yol(
            road_query, start_query, end_query,
            mahalle, ilce, il, ulke, debug,
            segment=(location_type == "road_segment"),
        )

    if location_type == "intersection":
        return _konum_bul_kesisim(
            road_query, start_query, end_query,
            mahalle, ilce, il, ulke, debug,
        )

    if location_type in ("address", "landmark", "unknown"):
        sorgu = road_query or start_query or end_query
        if not sorgu:
            return None
        return adres_konumu_bul(sokak=sorgu, mahalle=mahalle, ilce=ilce, il=il, ulke=ulke, debug=debug)

    return None


# --------------------------------------------------------------------------- #
# Haritada çizim (Leaflet + OpenStreetMap) - bağımsız test/debug amaçlı
# --------------------------------------------------------------------------- #

_HARITA_HTML_SABLONU = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="utf-8" />
<title>Konum Haritası</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<style>
  html, body, #harita {{ height: 100%; margin: 0; padding: 0; font-family: sans-serif; }}
  .info-kutu {{
    position: absolute; top: 10px; right: 10px; z-index: 1000;
    background: white; padding: 10px 14px; border-radius: 8px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.25); max-width: 280px; font-size: 13px;
  }}
  .info-kutu b {{ display: block; margin-bottom: 4px; color: #2c3e50; }}
</style>
</head>
<body>
<div id="harita"></div>
<div class="info-kutu" id="bilgi-kutu"></div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
  const veriler = {veri_json};

  const harita = L.map('harita');
  L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap katkıda bulunanlar'
  }}).addTo(harita);

  const renkler = ["#e74c3c", "#3498db", "#27ae60", "#f39c12", "#9b59b6", "#1abc9c"];
  const tumSinirlar = [];
  let bilgiHtml = "";

  veriler.forEach((adres, i) => {{
      const renk = renkler[i % renkler.length];
      const cizgiStili = adres.tam_eslesme
          ? {{ color: renk, weight: 6, opacity: 0.85 }}
          : {{ color: renk, weight: 5, opacity: 0.7, dashArray: "10, 8" }};  // yaklaşık -> KESİKLİ çizgi

      if (adres.geojson && adres.geojson.type && adres.geojson.type.includes("LineString")) {{
          const katman = L.geoJSON(adres.geojson, {{ style: cizgiStili }}).addTo(harita);
          tumSinirlar.push(katman.getBounds());
      }} else if (adres.geojson && adres.geojson.type && adres.geojson.type.includes("Polygon")) {{
          const katman = L.geoJSON(adres.geojson, {{
              style: {{ color: renk, weight: 2, fillColor: renk, fillOpacity: 0.2 }}
          }}).addTo(harita);
          tumSinirlar.push(katman.getBounds());
      }} else {{
          const marker = L.marker([adres.lat, adres.lon]).addTo(harita);
          tumSinirlar.push(L.latLngBounds([[adres.lat, adres.lon]]));
      }}

      const uyariSatiri = adres.tam_eslesme
          ? ""
          : `<br><span style="color:#e67e22; font-size:12px;">⚠ Tam eşleşme değil: ${{adres.uyari || ''}}</span>`;
      bilgiHtml += `<b style="color:${{renk}}">${{adres.tam_eslesme ? '●' : '⚠'}} ${{adres.etiket}}</b>${{adres.gosterim_adi}}${{uyariSatiri}}<br><br>`;
  }});

  document.getElementById("bilgi-kutu").innerHTML = bilgiHtml;

  if (tumSinirlar.length === 1) {{
      harita.fitBounds(tumSinirlar[0], {{ padding: [40, 40], maxZoom: 18 }});
  }} else if (tumSinirlar.length > 1) {{
      let birlesikSinir = tumSinirlar[0];
      tumSinirlar.slice(1).forEach(b => birlesikSinir.extend(b));
      harita.fitBounds(birlesikSinir, {{ padding: [40, 40] }});
  }}
</script>
</body>
</html>
"""


def haritada_goster(sonuclar, dosya_yolu: str = "harita.html") -> str:
    """
    Bir veya birden fazla AdresSonucu'nu Leaflet.js ile çizen bağımsız bir
    HTML dosyası üretir (sadece bağımsız test/debug amaçlı; main.py bunu
    kullanmaz, main.py kendi folium haritasını üretir).
    """
    if isinstance(sonuclar, AdresSonucu):
        sonuclar = [sonuclar]

    veri = []
    for i, s in enumerate(sonuclar, start=1):
        veri.append({
            "lat": s.lat,
            "lon": s.lon,
            "gosterim_adi": s.gosterim_adi,
            "geojson": s.geojson,
            "tam_eslesme": s.tam_eslesme,
            "uyari": s.uyari,
            "etiket": f"Sonuç {i}: ",
        })

    html = _HARITA_HTML_SABLONU.format(veri_json=json.dumps(veri, ensure_ascii=False))

    with open(dosya_yolu, "w", encoding="utf-8") as f:
        f.write(html)

    return dosya_yolu


# --------------------------------------------------------------------------- #
# Bağımsız çalıştırma / hızlı test
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    # Örnek: main.py'den gelecek türden bir ai_result ile deneme.
    ornek_ai_result = {
        "location_type": "road_segment",
        "road_name": "Fatih Sultan Mehmet Caddesi",
        "neighborhood": "Adalet",
        "district": "Merkezefendi",
        "query": "Fatih Sultan Mehmet /1 Caddesi",
        "start_query": "10157 Sokak",
        "end_query": "10054 Sokak",
        "confidence": 0.8,
    }

    sonuc = konum_bul_ai(ornek_ai_result, debug=True)

    if sonuc:
        print("\n[BULUNDU]")
        print(sonuc)
        haritada_goster(sonuc, dosya_yolu="harita.html")
        print("Harita oluşturuldu: harita.html")
    else:
        print("\n[BULUNAMADI]")