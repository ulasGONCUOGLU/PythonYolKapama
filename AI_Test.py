import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai


# AI_Test.py'nin bulunduğu klasörü bul
BASE_DIR = Path(__file__).resolve().parent

# .env dosyasının tam yolunu oluştur
env_file = BASE_DIR / ".env"

print("Proje klasörü :", BASE_DIR)
print(".env yolu    :", env_file)
print(".env mevcut mu:", env_file.exists())


# .env dosyasını özellikle bu dosyadan yükle
load_dotenv(env_file)

api_key = os.getenv("GEMINI_API_KEY")

print("API key bulundu mu:", bool(api_key))

if not api_key:
    raise RuntimeError(
        "GEMINI_API_KEY bulunamadı. .env içeriğini ve değişken adını kontrol et."
    )


client = genai.Client(api_key=api_key)

response = client.models.generate_content(
    model="gemini-3.6-flash",
    # contents="Merhaba. Sadece 'Gemini bağlantısı başarılı.' yaz."
    contents="Sana Kaç kez soru sordum?"
)

print("\nGemini cevabı:")
print(response.text)