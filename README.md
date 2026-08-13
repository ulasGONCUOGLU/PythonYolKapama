# Yol Kapama

Belediyelerin yol kapama / yol çalışması duyurularındaki verileri işleyerek, ilerleyen aşamalarda harita üzerinde göstermeyi amaçlayan Python projesidir.

> **Mevcut sürüm (V1):** Belirlenen web sitesine HTTP GET isteği gönderilir ve alınan ham HTML içeriği yerel olarak `output/page.html` dosyasına kaydedilir.

---

## Gereksinimler

- Python 3.12.x
- Git
- Windows PowerShell

Proje Python sanal ortamı (`venv`) kullanmaktadır.

Python sürümünün proje geliştirme ortamındaki sürümle aynı olması önerilir.

---

## Projeyi İndirme

Projeyi GitHub üzerinden bilgisayarınıza indirin:

```powershell
git clone https://github.com/ulasGONCUOGLU/PythonYolKapama.git
```
Proje klasörüne girin:
```
cd PythonYolKapama
```
Sanal Ortamı Oluşturma

Python 3.12 kullanarak sanal ortam oluşturun:
```
py -3.12 -m venv .venv
```
Sanal Ortamı Aktifleştirme

PowerShell kullanıyorsanız:
```
.venv\Scripts\Activate.ps1
```

Tek paket eklemek için
```
python -m pip install folium
```

Paketleri kurmak için:
```
python -m pip install -r requirements.txt
```