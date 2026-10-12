# Akaryakıt Zam Takibi

Zam/indirim **olmadan önce** haber vermek için:

- Google News'ten "motorine zam bekleniyor" gibi sızıntı haberlerini toplar.
- Her başlıktan yakıt türü, yön ve tutarı çıkarır; **aynı rakamı veren farklı site sayısını** sayar.
- 3 ve üzeri site aynı rakamı veriyorsa ntfy ile bildirim atar (iptal haberlerinde de).
- Anadolu Ajansı ekonomi RSS'ini doğrudan tarar (AA 2 site sayılır).
- Opet resmi API'sinden İstanbul ve Erzurum pompa fiyatlarını izler; yeni tarife gece yarısından önce girilirse bildirir.
- Gasoil vekili (NYMEX ULSD HO=F, RBOB RB=F) ve kurla son pompa değişiminden bu yana birikmiş "maliyet baskısı"nı hesaplar; geriye dönük test sayfada.
- TCMB dolar/euro kurunu kaydeder.
- Sonucu `docs/index.html` olarak GitHub Pages'te yayınlar.

Her 30 dakikada bir GitHub Actions ile çalışır.

Ayar: `NTFY_TOPIC` repository secret.
