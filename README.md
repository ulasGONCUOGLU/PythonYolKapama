# Yol Kapama — Backend (Python + Flask + Supabase)

Belediyelerin yol kapama duyurularını çeken, Gemini ile analiz eden,
Nominatim/OSRM ile konumlandıran ve **Supabase**'e yazan pipeline.

## Bu sürümde neler değişti

Önceki sürüme göre (bkz. eski `README.md`, SQLite tabanlıydı):

- **SQLite kaldırıldı**, yerine **Supabase** (Postgres) geldi. Kalıcılık
  katmanı artık `db_supabase.py` içinde.
- **Harita üretimi kaldırıldı** (`folium`, `build_map()`). Harita artık
  Nuxt tarafında Leaflet ile, doğrudan Supabase'ten okunan `closures`
  verisiyle çiziliyor. Python'un harita dosyası üretmesine gerek kalmadı.
- **Flask eklendi** (`app.py`): pipeline'ı bir HTTP isteğiyle tetiklemek
  için ince bir sarmalayıcı. Nuxt'taki "Verileri Çek" butonu bunu çağırır.
- **Sistem Talimatı artık Supabase'ten okunuyor**: `AI_düzenleme.py`,
  Gemini prompt'unun giriş cümlesini `settings` tablosundan (`system_prompt`
  anahtarı) okuyor. Nuxt'taki Ayarlar sayfasından değiştirilebilir.
- `main.py`'nin giriş fonksiyonu `main()` yerine `run_pipeline()` oldu
  (Flask'ın import edip çağırabilmesi için). Script tek başına da
  çalıştırılabilir: `python main.py`.

## Kurulum

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

`.env.example` dosyasını `.env` olarak kopyalayıp doldurun:

```powershell
copy .env.example .env
```

Doldurman gereken alanlar: `URL`, `GEMINI_API_KEY`, `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`, `INTERNAL_API_TOKEN`.

## Supabase şeması

`supabase_schema_update.sql` dosyasını Supabase Dashboard → SQL Editor'e
yapıştırıp çalıştırın. Bu, daha önce kurduğumuz `closures` tablosunu
script'in gerçek ihtiyacına göre yeniden tanımlar (mock veri yapısından
farklı — `record_hash`, `ai_status`, `geocode_status` gibi pipeline'a
özel alanlar içeriyor) ve `refresh_active_status()` adında bir RPC
fonksiyonu ekler.

> `settings` ve `sync_runs` tabloları önceki adımda zaten kurulmuştu,
> bu dosya sadece `closures`'ı günceller.

`settings` tablosunda `system_prompt` anahtarının bir satırı olduğundan
emin olun (önceki SQL'de `insert ... on conflict do nothing` ile zaten
eklenmişti).

## Çalıştırma

**Doğrudan (Flask'sız, hızlı test için):**

```powershell
python main.py
```

**Flask üzerinden (Nuxt'un kullanacağı şekil):**

```powershell
python app.py
```

Bu, `http://localhost:5000` üzerinde ayağa kalkar. Test etmek için:

```powershell
curl -X POST http://localhost:5000/run -H "X-Internal-Token: .env'deki INTERNAL_API_TOKEN"
```

Hemen `{"status": "started", "run_id": ...}` döner, pipeline arka planda
çalışmaya devam eder. İlerlemeyi Supabase Dashboard'da `sync_runs`
tablosundan izleyebilirsiniz.

## Üretim / Docker notu

İleride Docker'a taşınırsa:

- `gunicorn` zaten `requirements.txt`'te var. Container **içinde**
  Docker'ın kendi ağ izolasyonuyla çalıştığı için o zaman `0.0.0.0`
  container içi ağ arayüzünü ifade eder (host makineye/dış ağa
  otomatik açılmaz, hangi portun dışarı açılacağını `docker run -p`
  veya `docker-compose`'daki `ports:` ayarı belirler):
  ```
  gunicorn --workers 1 --bind 0.0.0.0:5000 app:app
  ```
  **Şu an, yerel makinende doğrudan `python app.py` ile çalıştırdığın
  sürece bu geçerli değil** — `app.py` içinde `host="127.0.0.1"`
  olduğu için servis sadece kendi bilgisayarından erişilebilir, ağdaki
  hiçbir cihaza (ve dolayısıyla ağ üzerindeki port çakışması riskine)
  açılmaz.
- **`--workers 1` şart** — `/run` endpoint'indeki "aynı anda ikinci
  çalıştırmayı engelle" kilidi process-içi bir Python kilidi
  (`threading.Lock`), birden fazla worker arasında paylaşılmaz. Birden
  fazla worker gerekiyorsa bu kilidi Postgres advisory lock'a taşımak
  gerekir — şimdilik gerek yok.
- `URL`, `GEMINI_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
  `INTERNAL_API_TOKEN` container'a ortam değişkeni olarak geçilmeli
  (`.env` dosyasını image'a gömmeyin).

## Dosyalar

```
main.py                      Ana pipeline (scrape -> Supabase -> AI -> konum)
AI_düzenleme.py               Gemini ile güzergah analizi (system_prompt Supabase'ten)
konum.py                      Nominatim/OSRM geocoding (değişmedi)
db_supabase.py                Supabase kalıcılık katmanı (SQLite'ın yerini alıyor)
app.py                        Flask sarmalayıcısı (POST /run, GET /health)
supabase_schema_update.sql    closures tablosunun güncel şeması + RPC
requirements.txt
.env.example
```
