# -*- coding: utf-8 -*-
"""
app.py
======
Yol Kapama pipeline'ını (main.py) tetikleyen ince Flask sarmalayıcısı.

Bu servisin TEK işi tetiklemek. Veri okuma/yazma işlerinin hiçbiri
buradan geçmiyor — Nuxt tarafı `closures`/`settings` verisini doğrudan
Supabase'ten okuyor/yazıyor. Flask sadece:

    POST /run   -> pipeline'ı arka plan thread'inde başlatır, hemen döner
    GET  /health -> canlılık kontrolü

Güvenlik: /run endpoint'i paylaşılan bir gizli anahtar (INTERNAL_API_TOKEN)
ile korunur. Bu anahtar SADECE Nuxt'un kendi sunucu route'unda tutulmalı,
tarayıcıya asla sızmamalı. Böylece bu servis internete açık olsa bile
sadece Nuxt sunucusu tetikleyebilir.

Gerekli ortam değişkenleri (.env):
    URL                     (main.py'nin scrape edeceği belediye sitesi)
    GEMINI_API_KEY          (AI_düzenleme.py için)
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
    INTERNAL_API_TOKEN      (Nuxt <-> Flask arası paylaşılan gizli anahtar)

Yerel çalıştırma (geliştirme):
    python app.py
    # veya
    flask --app app run --port 5000

ÖNEMLİ: /run endpoint'i işlem sürerken aynı anda ikinci bir
çalıştırmayı engellemek için process-içi bir kilit (_calisiyor_mu)
kullanıyor. gunicorn ile birden fazla worker çalıştırılırsa bu kilit
worker'lar arasında paylaşılmaz — bu yüzden bu servisi TEK worker ile
çalıştırın (bkz. README).
"""

from __future__ import annotations

import os
import threading
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, request

import db_supabase as db
from main import run_pipeline

load_dotenv()

INTERNAL_API_TOKEN = os.getenv("INTERNAL_API_TOKEN")

if not INTERNAL_API_TOKEN:
    raise ValueError("INTERNAL_API_TOKEN .env dosyasında bulunamadı.")

app = Flask(__name__)

_calisma_kilidi = threading.Lock()
_calisiyor_mu = False


def _arka_planda_calistir(run_id: int) -> None:
    """
    Thread içinde çalışır: pipeline'ı çalıştırır, sonucuna göre
    sync_runs satırını 'basarili' ya da 'hata' olarak günceller.
    """

    global _calisiyor_mu

    try:
        run_pipeline()

        db.supabase.table("sync_runs").update(
            {
                "durum": "basarili",
                "bitis": datetime.now().isoformat(),
            }
        ).eq("id", run_id).execute()

    except Exception as e:
        print(f"Pipeline hatası: {e}")

        db.supabase.table("sync_runs").update(
            {
                "durum": "hata",
                "hata_mesaji": str(e),
                "bitis": datetime.now().isoformat(),
            }
        ).eq("id", run_id).execute()

    finally:
        with _calisma_kilidi:
            _calisiyor_mu = False


@app.route("/run", methods=["POST"])
def run():
    global _calisiyor_mu

    token = request.headers.get("X-Internal-Token")

    if token != INTERNAL_API_TOKEN:
        return jsonify({"error": "Yetkisiz."}), 401

    with _calisma_kilidi:
        if _calisiyor_mu:
            return jsonify({"error": "Pipeline zaten çalışıyor."}), 409
        _calisiyor_mu = True

    run_row = (
        db.supabase.table("sync_runs")
        .insert(
            {
                "durum": "calisiyor",
                "baslangic": datetime.now().isoformat(),
            }
        )
        .execute()
    )

    run_id = run_row.data[0]["id"]

    thread = threading.Thread(
        target=_arka_planda_calistir,
        args=(run_id,),
        daemon=True,
    )
    thread.start()

    return jsonify({"status": "started", "run_id": run_id}), 202


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
