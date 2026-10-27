-- ============================================================
-- Yol Kapama — Supabase şeması
-- ============================================================
-- Supabase Dashboard → SQL Editor'e yapıştırıp çalıştırın.
--
-- Bu dosya TEKRAR ÇALIŞTIRILABİLİR (idempotent) olacak şekilde yazıldı:
-- hiçbir tabloyu DROP etmez, var olan veriyi silmez.
--
-- NOT: `sync_runs` ve `settings` tablolarının şeması, web arayüzünün
-- kullandığı sütunlardan (durum, baslangic, bitis, hata_mesaji,
-- olusturulma / key, value, updated_at) çıkarılmıştır. Python backend'iniz
-- farklı ek sütunlar kullanıyorsa ona göre genişletin.
-- ============================================================


-- ------------------------------------------------------------
-- closures: belediye yol kapatma duyuruları
-- ------------------------------------------------------------
create table if not exists public.closures (
  id bigint generated always as identity primary key,

  -- Tekilleştirme anahtarı: guzergah + tarih + neden hash'i
  record_hash text not null unique,

  -- Kaynaktan gelen ham veriler
  guzergah_raw text not null,
  tarih_raw text not null,
  kapanma_nedeni_raw text not null,

  -- Kaynak sitedeki "İLÇE-MAHALLE" hücresi (AI tahminine gerek kalmadan)
  ilce_mahalle_raw text,
  ilce text,
  mahalle text,

  -- Python tarafından ayrıştırılan tarih aralığı
  start_date date,
  end_date date,
  active boolean not null default false,

  -- AI (Gemini) analiz sonucu
  ai_status text not null default 'pending',       -- pending | success | error
  ai_location_type text,
  ai_neighborhood text,
  ai_district text,
  ai_query text,
  ai_start_query text,
  ai_end_query text,
  ai_result jsonb,

  -- Geocoder (Nominatim/OSRM) sonucu
  geocode_status text not null default 'pending',   -- pending | success | not_found
  geocode_result jsonb,

  -- Harita geometrisi (Point veya LineString GeoJSON)
  geometry_type text,
  geometry jsonb,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table public.closures is
  'Belediye yol kapatma duyuruları - Python pipeline (main.py) tarafından dolduruluyor';
comment on column public.closures.geometry is
  'GeoJSON: {"type":"Point","coordinates":[lon,lat]} veya {"type":"LineString","coordinates":[[lon,lat],...]}';

alter table public.closures enable row level security;

-- Tarayıcıdan (anon key) herkes OKUYABİLİR; YAZMA policy'si yoktur.
-- Sadece service_role anahtarı (RLS'i atlar) yazabilir.
drop policy if exists "closures_public_read" on public.closures;
create policy "closures_public_read"
  on public.closures
  for select
  to anon, authenticated
  using (true);

create index if not exists idx_closures_active on public.closures (active);
create index if not exists idx_closures_ai_status on public.closures (ai_status);
create index if not exists idx_closures_geocode_status on public.closures (geocode_status);


-- ------------------------------------------------------------
-- sync_runs: "Verileri Çek" çalıştırmalarının durumu
-- (arayüz bu tabloyu anon key ile okuyup 3 sn'de bir poll eder)
-- ------------------------------------------------------------
create table if not exists public.sync_runs (
  id bigint generated always as identity primary key,
  durum text not null default 'beklemede'
    check (durum in ('beklemede', 'calisiyor', 'basarili', 'hata')),
  baslangic timestamptz,
  bitis timestamptz,
  hata_mesaji text,
  olusturulma timestamptz not null default now()
);

alter table public.sync_runs enable row level security;

drop policy if exists "sync_runs_public_read" on public.sync_runs;
create policy "sync_runs_public_read"
  on public.sync_runs
  for select
  to anon, authenticated
  using (true);


-- ------------------------------------------------------------
-- settings: anahtar/değer ayarları (ör. AI sistem talimatı)
-- RLS açık, HİÇ policy yok: anon/authenticated ne okuyabilir ne yazabilir.
-- Sadece sunucu tarafı (service_role) erişir.
-- ------------------------------------------------------------
create table if not exists public.settings (
  key text primary key,
  value text not null default '',
  updated_at timestamptz not null default now()
);

alter table public.settings enable row level security;

-- ayarlar.post.ts satırı UPDATE ettiği için satırın var olması gerekir.
-- Varsayılan metin, Python tarafındaki DEFAULT_SYSTEM_PROMPT ile aynıdır
-- (AI_düzenleme.py). Satır zaten varsa dokunulmaz (kullanıcının metni korunur).
insert into public.settings (key, value)
values (
  'system_prompt',
  'Sen Denizli''deki belediyelerin yol kapatma duyurularını analiz eden bir konum analiz sistemisin.'
)
on conflict (key) do nothing;


-- ------------------------------------------------------------
-- RPC: refresh_active_status
-- ------------------------------------------------------------
-- Python pipeline her çalıştığında tüm kayıtların 'active' durumunu
-- bugünün tarihine göre tek seferde yeniden hesaplar.
-- ------------------------------------------------------------
create or replace function public.refresh_active_status()
returns void
language sql
security definer
set search_path = public
as $$
  update public.closures
  set
    active = case
      when start_date is not null
        and end_date is not null
        and start_date <= current_date
        and end_date >= current_date
      then true
      else false
    end,
    updated_at = now()
  -- Supabase'in pg_safeupdate eklentisi WHERE'siz UPDATE'i reddeder (21000).
  where true;
$$;

revoke execute on function public.refresh_active_status() from public;
revoke execute on function public.refresh_active_status() from anon;
revoke execute on function public.refresh_active_status() from authenticated;
grant execute on function public.refresh_active_status() to service_role;


-- ============================================================
-- ESKİ AÇIK POLİTİKALARI TEMİZLE (mevcut veritabanları için)
-- ============================================================
-- Eski şema dosyası (supabase_schema_update.sql) closures, settings ve
-- sync_runs tablolarına "TO PUBLIC" INSERT/UPDATE/DELETE izni veriyordu.
-- Anon anahtar tarayıcıda göründüğü için bu, internete erişimi olan HERKESİN
-- bu tabloları silebilmesi/değiştirebilmesi (ve settings içindeki AI
-- talimatını okuyup yazabilmesi) demekti.
--
-- Gerçekte buna gerek yok: yazma işlemlerini Flask ve Nuxt sunucu route'ları
-- service_role anahtarıyla yapıyor (RLS'i zaten atlar). Tarayıcı yalnızca
-- closures ve sync_runs'ı OKUR. Aşağıdaki blok eski politikaları siler ve
-- yetkileri bu duruma geri çeker. Temiz kurulumda hiçbir şey yapmaz.
-- ============================================================

drop policy if exists "public_closures_select_all" on public.closures;
drop policy if exists "public_closures_insert_all" on public.closures;
drop policy if exists "public_closures_update_all" on public.closures;
drop policy if exists "public_closures_delete_all" on public.closures;

drop policy if exists "public_sync_runs_select_all" on public.sync_runs;
drop policy if exists "public_sync_runs_insert_all" on public.sync_runs;
drop policy if exists "public_sync_runs_update_all" on public.sync_runs;
drop policy if exists "public_sync_runs_delete_all" on public.sync_runs;

drop policy if exists "public_settings_select_all" on public.settings;
drop policy if exists "public_settings_insert_all" on public.settings;
drop policy if exists "public_settings_update_all" on public.settings;
drop policy if exists "public_settings_delete_all" on public.settings;

-- Supabase, yeni tablolara anon/authenticated için varsayılan olarak TÜM
-- yetkileri verir. "revoke all" bunların hepsini (TRUNCATE, TRIGGER,
-- REFERENCES dahil) geri çeker; TRUNCATE'i RLS denetlemediği için sadece
-- INSERT/UPDATE/DELETE'i geri çekmek yetmezdi: anon rolü tabloyu boşaltabilirdi.
revoke all on public.closures from anon, authenticated;
revoke all on public.sync_runs from anon, authenticated;
revoke all on public.settings from anon, authenticated;

-- Okuma yetkisi (RLS policy'leri yukarıda "closures_public_read" ve
-- "sync_runs_public_read" adıyla zaten tanımlı).
grant select on public.closures to anon, authenticated;
grant select on public.sync_runs to anon, authenticated;
-- ============================================================
