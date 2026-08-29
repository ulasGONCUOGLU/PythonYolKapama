-- ============================================================
-- GÜNCELLEME: closures tablosu — gerçek Python script şemasına göre
-- ============================================================
-- Daha önce oluşturduğumuz closures tablosu, arayüz mock verisine göre
-- tasarlanmıştı. main.py'nin gerçek SQLite şemasını görünce, script'in
-- çok daha zengin alanlar kullandığı ortaya çıktı (record_hash ile
-- tekilleştirme, ai_status/geocode_status ile pipeline takibi, ayrı
-- geometry_type/geometry alanı vb.). Bu yüzden tabloyu script'in
-- gerçek ihtiyacına göre yeniden kuruyoruz.
--
-- NOT: Tabloda henüz veri olmadığı için DROP + CREATE güvenli.
-- ============================================================

drop policy if exists "closures_public_read" on public.closures;
drop table if exists public.closures;

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

-- Nuxt tarayıcıdan (anon key) doğrudan okuyabilsin diye herkese açık SELECT.
-- Yazma policy'si YOK: sadece Python'un kullandığı service_role key
-- (RLS'i zaten atlar) yazabilir.
create policy "closures_public_read"
  on public.closures
  for select
  to anon, authenticated
  using (true);

create index idx_closures_active on public.closures (active);
create index idx_closures_ai_status on public.closures (ai_status);
create index idx_closures_geocode_status on public.closures (geocode_status);


-- ============================================================
-- RPC: refresh_active_status
-- ============================================================
-- main.py her çalıştığında tüm kayıtların 'active' durumunu bugünün
-- tarihine göre tek seferde yeniden hesaplar. Supabase REST API'si
-- "tüm satırları koşulsuz güncelle" işlemini native desteklemediği
-- için bunu bir Postgres fonksiyonu (RPC) olarak tanımlıyoruz.
-- ============================================================

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
    updated_at = now();
$$;

revoke execute on function public.refresh_active_status() from public;
revoke execute on function public.refresh_active_status() from anon;
revoke execute on function public.refresh_active_status() from authenticated;
grant execute on function public.refresh_active_status() to service_role;





ALTER TABLE public.settings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "public_settings_select_all"
ON public.settings
FOR SELECT
TO PUBLIC
USING (true);

CREATE POLICY "public_settings_insert_all"
ON public.settings
FOR INSERT
TO PUBLIC
WITH CHECK (true);

CREATE POLICY "public_settings_update_all"
ON public.settings
FOR UPDATE
TO PUBLIC
USING (true)
WITH CHECK (true);

CREATE POLICY "public_settings_delete_all"
ON public.settings
FOR DELETE
TO PUBLIC
USING (true);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.settings TO anon, authenticated;




ALTER TABLE public.sync_runs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "public_sync_runs_select_all"
ON public.sync_runs
FOR SELECT
TO PUBLIC
USING (true);

CREATE POLICY "public_sync_runs_insert_all"
ON public.sync_runs
FOR INSERT
TO PUBLIC
WITH CHECK (true);

CREATE POLICY "public_sync_runs_update_all"
ON public.sync_runs
FOR UPDATE
TO PUBLIC
USING (true)
WITH CHECK (true);

CREATE POLICY "public_sync_runs_delete_all"
ON public.sync_runs
FOR DELETE
TO PUBLIC
USING (true);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.sync_runs TO anon, authenticated;




ALTER TABLE public.closures ENABLE ROW LEVEL SECURITY;

CREATE POLICY "public_closures_select_all"
ON public.closures
FOR SELECT
TO PUBLIC
USING (true);

CREATE POLICY "public_closures_insert_all"
ON public.closures
FOR INSERT
TO PUBLIC
WITH CHECK (true);

CREATE POLICY "public_closures_update_all"
ON public.closures
FOR UPDATE
TO PUBLIC
USING (true)
WITH CHECK (true);

CREATE POLICY "public_closures_delete_all"
ON public.closures
FOR DELETE
TO PUBLIC
USING (true);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.closures TO anon, authenticated;