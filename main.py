import os
import requests
from dotenv import load_dotenv
from bs4 import BeautifulSoup

load_dotenv()

url = os.getenv("URL")

if not url:
    raise ValueError("URL .env dosyasında bulunamadı.")

print("URL:", url)

response = requests.get(url, timeout=30)

print(f"HTTP Durum Kodu: {response.status_code}")

os.makedirs("output", exist_ok=True)

# 1. Orijinal HTML'i sakla
with open("output/page.html", "w", encoding="utf-8") as file:
    file.write(response.text)

print("Orijinal HTML output/page.html konumuna kaydedildi.")

# 2. HTML'i BeautifulSoup ile oku
soup = BeautifulSoup(response.text, "html.parser")

# 3. Gerekli sütun başlıklarını içeren tabloyu bul
target_table = None

for table in soup.find_all("table"):
    headers = [
        cell.get_text(" ", strip=True).upper()
        for cell in table.find_all(["th", "td"])
    ]

    if (
        "GÜZERGAH" in headers
        and "TARİH" in headers
        and "KAPANMA NEDENİ" in headers
    ):
        target_table = table
        break

if target_table is None:
    raise ValueError(
        "GÜZERGAH, TARİH ve KAPANMA NEDENİ sütunlarını içeren tablo bulunamadı."
    )

# 4. Sadece bulunan tabloyu TXT'ye kaydet
with open("output/soup.txt", "w", encoding="utf-8") as file:
    file.write(target_table.prettify())

print("Sadece ilgili tablo output/soup.txt konumuna kaydedildi.")