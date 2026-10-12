# Akaryakıt Zam Takibi

Zam/indirim **olmadan önce** haber vermek için:

- Google News'ten "motorine zam bekleniyor" gibi sızıntı haberlerini toplar.
- Her başlıktan yakıt türü, yön ve tutarı çıkarır; **aynı rakamı veren farklı site sayısını** sayar.
- 3 ve üzeri site aynı rakamı veriyorsa ntfy ile bildirim atar (iptal haberlerinde de).
- TCMB dolar/euro kurunu kaydeder.
- Sonucu `docs/index.html` olarak GitHub Pages'te yayınlar.

Her 30 dakikada bir GitHub Actions ile çalışır.

Ayar: `NTFY_TOPIC` repository secret.
