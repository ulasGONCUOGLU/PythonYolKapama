import os
import requests
from dotenv import load_dotenv

load_dotenv()

url = os.getenv("URL")

if not url:
    raise ValueError("URL .env dosyasında bulunamadı.")

print("URL:", url)

response = requests.get(url, timeout=30)

print(f"HTTP Durum Kodu: {response.status_code}")

os.makedirs("output", exist_ok=True)

with open("output/page.html", "w", encoding="utf-8") as file:
    file.write(response.text)

print("HTML dosyası output/page.html konumuna kaydedildi.")