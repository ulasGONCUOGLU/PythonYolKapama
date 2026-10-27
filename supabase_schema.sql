-- ============================================================
-- YOL KAPAMA — Supabase Şeması (TEK DOSYA, sıfırdan kurulum)
-- ============================================================
-- Bu dosya Supabase SQL Editor'e yapıştırılıp tek seferde çalıştırılacak
-- şekilde tasarlandı. Yeni bir Supabase projesinde projeyi baştan ayağa
-- kaldırmak için kullan.
--
-- ⚠️  UYARI — VERİ KAYBI:
-- Bu script `closures` tablosunu DROP edip yeniden oluşturuyor.
-- Eğer bunu, içinde gerçek veri olan MEVCUT bir projede çalıştırırsan
-- o veriler SİLİNİR. Sadece şu durumlarda çalıştır:
--   (a) Yepyeni bir Supabase projesinde ilk kurulum yapıyorsan, veya
--   (b) Mevcut projedeki closures verisini bilerek sıfırlamak
--       istiyorsan.
-- `settings` ve `sync_runs` tabloları `create table if not exists`
-- kullanır — zaten varsa dokunulmaz, veri kaybı olmaz.
--
-- RLS ÖZETİ (bu dosya çalıştıktan sonraki nihai durum):
--   closures   -> anon/authenticated: sadece SELECT. INSERT/UPDATE/
--                 DELETE YOK. Yazma işlemleri SADECE service_role ile
--                 (Python backend ve Nuxt'un server/api route'ları)
--                 yapılır — service_role zaten RLS'i atlar, bu yüzden
--                 closures için hiçbir yazma policy'si TANIMLANMAMALI.
--   settings   -> anon/authenticated: HİÇBİR ERİŞİM YOK (ne okuma ne
--                 yazma). Sadece service_role erişir (Nuxt'un
--                 server/api/yolkapama/ayarlar.get|post.ts route'ları
--                 ve Python'un AI_düzenleme.py'si üzerinden).
--   sync_runs  -> anon/authenticated: sadece SELECT (Nuxt'un polling
--                 için okuması gerekiyor). INSERT/UPDATE YOK — sadece
--                 service_role (Nuxt'un calistir.post.ts'i satırı
--                 oluşturur, Flask/Python durumunu günceller).
-- ============================================================


-- ============================================================
-- 1) settings
-- ============================================================

create table if not exists public.settings (
  key text primary key,
  value text,
  updated_at timestamptz not null default now()
);

comment on table public.settings is
  'Uygulama ayarları (ör. system_prompt) - sadece service_role ile (Nuxt sunucu route + Python) erişilir';

alter table public.settings enable row level security;

-- Kasıtlı olarak HİÇ policy eklenmiyor: anon/authenticated hiçbir şey
-- okuyup yazamaz. Ayarlar sayfası okuma/yazmayı Nuxt sunucu route'u
-- üzerinden (service_role ile) yapar.

insert into public.settings (key, value) values (
  'system_prompt',
  'Sen Denizli''deki belediyelerin yol kapatma duyurularını analiz eden bir konum analiz sistemisin. Gelen metinleri inceleyip kapatılan caddeleri, sokakları, başlangıç ve bitiş tarihlerini çıkar.'
)
on conflict (key) do nothing;


-- ============================================================
-- 2) sync_runs
-- ============================================================

do $$
begin
  if not exists (select 1 from pg_type where typname = 'sync_run_status') then
    create type public.sync_run_status as enum ('beklemede', 'calisiyor', 'basarili', 'hata');
  end if;
end
$$;

create table if not exists public.sync_runs (
  id bigint generated always as identity primary key,
  durum public.sync_run_status not null default 'beklemede',
  baslangic timestamptz,
  bitis timestamptz,
  hata_mesaji text,
  olusturulma timestamptz not null default now()
);

comment on table public.sync_runs is
  'Python veri çekme script''inin çalıştırma geçmişi ve anlık durumu (Nuxt bunu polling ile izler)';

alter table public.sync_runs enable row level security;

drop policy if exists "sync_runs_public_read" on public.sync_runs;

-- Nuxt tarayıcıdan durumu okuyabilsin diye herkese açık SELECT.
-- insert/update yalnızca service_role: satırı Nuxt sunucu route'u
-- oluşturur, durumunu Flask/Python günceller.
create policy "sync_runs_public_read"
  on public.sync_runs
  for select
  to anon, authenticated
  using (true);

create index if not exists idx_sync_runs_olusturulma
  on public.sync_runs (olusturulma desc);


-- ============================================================
-- 3) closures
-- ============================================================
-- DROP + CREATE: yukarıdaki uyarıyı oku. Bu, tablonun üzerindeki TÜM
-- policy'leri de (elle eklenmiş olanlar dahil) temizleyip sıfırdan
-- kurar.

drop table if exists public.closures cascade;

create table public.closures (
  id bigint generated always as identity primary key,

  -- Tekilleştirme anahtarı: guzergah+tarih+neden hash'i
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

-- SADECE SELECT. Tarayıcıdan (anon key) doğrudan okunabilsin diye.
-- INSERT/UPDATE/DELETE için KASITLI OLARAK policy YOK: bu tabloya
-- yazma SADECE service_role ile yapılır (Python backend + Nuxt'un
-- server/api/yolkapama/kapama/[id].patch.ts route'u). service_role
-- zaten RLS'i atlar, bu yüzden ona ayrıca bir policy tanımlamaya
-- gerek yoktur ve anon/authenticated'a asla UPDATE/INSERT/DELETE
-- policy'si EKLENMEMELİDİR (anon key tarayıcıda herkese açıktır).
create policy "closures_public_read"
  on public.closures
  for select
  to anon, authenticated
  using (true);

create index idx_closures_active on public.closures (active);
create index idx_closures_ai_status on public.closures (ai_status);
create index idx_closures_geocode_status on public.closures (geocode_status);


-- ============================================================
-- 4) RPC: refresh_active_status
-- ============================================================
-- main.py her çalıştığında tüm kayıtların 'active' durumunu bugünün
-- tarihine göre tek seferde yeniden hesaplar. Supabase REST API'si
-- "tüm satırları koşulsuz güncelle" işlemini native desteklemediği
-- için bunu bir Postgres fonksiyonu (RPC) olarak tanımlıyoruz.
--
-- `where true`: Postgres bazı bağlantı modlarında WHERE'siz UPDATE'i
-- güvenlik önlemi olarak reddediyor ("UPDATE requires a WHERE clause");
-- `where true` mantığı değiştirmeden bu kısıtlamayı karşılar.

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
  where true;
$$;

revoke execute on function public.refresh_active_status() from public;
revoke execute on function public.refresh_active_status() from anon;
revoke execute on function public.refresh_active_status() from authenticated;
grant execute on function public.refresh_active_status() to service_role;


-- ============================================================
-- KURULUM SONRASI DOĞRULAMA (opsiyonel, elle çalıştırılabilir)
-- ============================================================
-- Aşağıdaki sorgu, her tabloda hangi rollere hangi izinlerin
-- tanımlı olduğunu gösterir. closures ve sync_runs'ta sadece
-- "SELECT" / "anon,authenticated" satırlarını görmelisin;
-- settings'te hiç satır görmemelisin.
--
-- select schemaname, tablename, policyname, roles, cmd
-- from pg_policies
-- where schemaname = 'public'
-- order by tablename, policyname;
