#!/usr/bin/env python3
"""
Akaryakit zam erken uyari sistemi (v1)

- Google News RSS'ten "zam/indirim bekleniyor" haberlerini toplar
- Her basliktan yakit turu, yon (zam/indirim) ve tutari cikarir
- Ayni tutari veren FARKLI site sayisini sayar (3+ = guclu beklenti)
- Guclu beklenti ve iptal haberlerinde ntfy ile bildirim atar
- TCMB'den dolar/euro kurunu kaydeder
- docs/index.html sayfasini yeniden uretir (GitHub Pages)

Ortam degiskenleri:
  NTFY_TOPIC   ntfy konu adi (GitHub Secret)
  TEST_BILDIRIM=1  calisinca deneme bildirimi gonderir
"""
import calendar
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse

import feedparser
import requests

TR = timezone(timedelta(hours=3))
SIMDI = datetime.now(TR)
KOK = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(KOK, "data")
DOCS = os.path.join(KOK, "docs")
UA = {"User-Agent": "Mozilla/5.0 (akaryakit-takip; +github actions)"}

GUCLU_ESIK = 3          # bu kadar farkli site ayni tutari verirse bildirim
HABER_PENCERE_SAAT = 30  # bundan eski haberler dikkate alinmaz
AA_ADI = "Anadolu Ajansı"  # resmi ajans: sayimda 2 site degerinde
OPET_ILLER = {"34": "İstanbul", "25": "Erzurum"}  # plaka kodu -> ad

ARAMALAR = [
    "motorine zam bekleniyor",
    "benzine zam bekleniyor",
    "motorine indirim bekleniyor",
    "benzine indirim bekleniyor",
    "LPG zam bekleniyor",
    "akaryakıt zam iptal",
    "akaryakıt tabela değişiyor",
]

durum_kaynak = {}  # kaynak adi -> "ok" / hata mesaji


# ---------------------------------------------------------------- yardimcilar
def json_oku(ad, varsayilan):
    yol = os.path.join(DATA, ad)
    try:
        with open(yol, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return varsayilan


def json_yaz(ad, veri):
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, ad), "w", encoding="utf-8") as f:
        json.dump(veri, f, ensure_ascii=False, indent=1)


def kucult(s):
    """Turkce kucuk harf (I->ı, İ->i)."""
    return s.replace("I", "ı").replace("İ", "i").lower()


# ---------------------------------------------------------------- ntfy
def ntfy(baslik_ascii, mesaj, oncelik="default", etiket=""):
    konu = os.environ.get("NTFY_TOPIC", "").strip()
    if not konu:
        print("NTFY_TOPIC yok, bildirim atlanadi:", mesaj)
        return False
    # Header'lar ASCII olmali; Turkce metin govdede (UTF-8)
    basliklar = {"Title": baslik_ascii.encode("ascii", "ignore").decode(),
                 "Priority": oncelik}
    if etiket:
        basliklar["Tags"] = etiket
    try:
        r = requests.post(f"https://ntfy.sh/{konu}", data=mesaj.encode("utf-8"),
                          headers=basliklar, timeout=20)
        r.raise_for_status()
        return True
    except Exception as e:  # bildirim hatasi isi durdurmasin
        print("ntfy hatasi:", e)
        return False


# ---------------------------------------------------------------- kur (TCMB)
def kur_al():
    try:
        r = requests.get("https://www.tcmb.gov.tr/kurlar/today.xml", headers=UA, timeout=20)
        r.raise_for_status()
        kok = ET.fromstring(r.content)
        sonuc = {"tarih": kok.attrib.get("Tarih", "")}
        for c in kok.findall("Currency"):
            kod = c.attrib.get("Kod")
            if kod in ("USD", "EUR"):
                sonuc[kod] = float(c.findtext("ForexSelling").replace(",", "."))
        if "USD" not in sonuc:
            raise ValueError("USD bulunamadi")
        durum_kaynak["TCMB kur"] = "ok"
        return sonuc
    except Exception as e:
        durum_kaynak["TCMB kur"] = f"hata: {e}"
        return None


# ---------------------------------------------------------------- haber analizi
YAKITLAR = [("motorin", r"motorin|mazot|dizel"), ("benzin", r"benzin"), ("lpg", r"lpg|otogaz")]
ZAM = r"\bzam|artış|yükseliyor|pahalan"
INDIRIM = r"indirim|düşüş|ucuzl|geriliyor"
ILERI = r"bekleni|geliyor|gelecek|yolda|kapıda|öngörülüyor|bu gece|gece yarısı|yarından|değişiyor|uygulanacak|olacak|belli oldu|sinyali"
GECMIS = r"\bgeldi\b|yapıldı|yansıdı|uygulandı"
IPTAL = r"iptal|gelmeyecek|ertelendi|vazgeçildi|suya düştü"


def tutar_bul(metin):
    """'6 lira 40 kuruş', '6,40 TL', '96 kuruş', '5 TL' -> float (TL)."""
    m = re.search(r"(\d+)\s*lira\s*(\d{1,2})\s*kuruş", metin)
    if m:
        return int(m.group(1)) + int(m.group(2)) / 100
    m = re.search(r"(\d+)[.,](\d{1,2})\s*(?:tl|lira|₺)", metin)
    if m and int(m.group(1)) < 30:  # 30 TL ustu tutar degil, fiyattir
        return int(m.group(1)) + int(m.group(2).ljust(2, "0")) / 100
    m = re.search(r"(\d+)\s*kuruş", metin)
    if m:
        return int(m.group(1)) / 100
    m = re.search(r"(\d+)\s*(?:tl|lira|₺)\b", metin)
    if m and int(m.group(1)) < 30:  # 30 TL ustu tutar degil, fiyattir
        return float(m.group(1))
    return None


def baslik_coz(baslik):
    """Bir basliktan [(yakit, yon, tutar)] listesi cikarir."""
    t = kucult(baslik)
    konumlar = []
    for ad, desen in YAKITLAR:
        for m in re.finditer(desen, t):
            konumlar.append((m.start(), ad))
    konumlar.sort()
    if not konumlar:
        return []
    on_ek = t[:konumlar[0][0]]
    sonuc, gorulen = [], set()
    for i, (bas, ad) in enumerate(konumlar):
        son = konumlar[i + 1][0] if i + 1 < len(konumlar) else len(t)
        parca = t[bas:son]
        # "motorin ve benzine zam" gibi bos parcalari sonraki parcayla birlestir
        if not re.search(ZAM + "|" + INDIRIM, parca) and i + 1 < len(konumlar):
            sonraki_son = konumlar[i + 2][0] if i + 2 < len(konumlar) else len(t)
            parca = t[bas:sonraki_son]
        yon = None
        zm, im = re.search(ZAM, parca), re.search(INDIRIM, parca)
        if zm and im:
            yon = "zam" if zm.start() < im.start() else "indirim"
        elif zm:
            yon = "zam"
        elif im:
            yon = "indirim"
        else:
            if re.search(ZAM, on_ek):
                yon = "zam"
            elif re.search(INDIRIM, on_ek):
                yon = "indirim"
        if not yon or ad in gorulen:
            continue
        gorulen.add(ad)
        sonuc.append((ad, yon, tutar_bul(parca)))
    return sonuc


def haberleri_topla():
    sinir = SIMDI - timedelta(hours=HABER_PENCERE_SAAT)
    haberler, gorulen_link = [], set()
    hata = 0
    for sorgu in ARAMALAR:
        url = ("https://news.google.com/rss/search?q=" + quote_plus(sorgu + " when:2d")
               + "&hl=tr&gl=TR&ceid=TR:tr")
        try:
            r = requests.get(url, headers=UA, timeout=25)
            r.raise_for_status()
            akis = feedparser.parse(r.content)
        except Exception as e:
            hata += 1
            print("RSS hatasi:", sorgu, e)
            continue
        for g in akis.entries:
            if not getattr(g, "published_parsed", None):
                continue
            zaman = datetime.fromtimestamp(calendar.timegm(g.published_parsed), timezone.utc).astimezone(TR)
            if zaman < sinir:
                continue
            baslik = g.title
            kaynak = ""
            if getattr(g, "source", None):
                kaynak = g.source.get("title", "") or urlparse(g.source.get("href", "")).netloc
            if " - " in baslik:
                govde, son = baslik.rsplit(" - ", 1)
                if not kaynak:
                    kaynak = son
                baslik = govde
            anahtar = kucult(baslik) + "|" + kaynak
            if anahtar in gorulen_link:
                continue
            gorulen_link.add(anahtar)
            haberler.append({"baslik": baslik, "kaynak": kaynak.strip() or "?",
                             "link": g.link, "zaman": zaman.isoformat(timespec="minutes")})
        time.sleep(1)
    durum_kaynak["Google News"] = "ok" if hata == 0 else f"{hata}/{len(ARAMALAR)} arama hatali"

    # Anadolu Ajansi ekonomi akisi (dogrudan, Google'dan bagimsiz)
    try:
        r = requests.get("https://www.aa.com.tr/tr/rss/default?cat=ekonomi", headers=UA, timeout=25)
        r.raise_for_status()
        akis = feedparser.parse(r.content)
        for g in akis.entries:
            if not getattr(g, "published_parsed", None):
                continue
            zaman = datetime.fromtimestamp(calendar.timegm(g.published_parsed), timezone.utc).astimezone(TR)
            if zaman < sinir or not re.search(r"motorin|benzin|lpg|otogaz|akaryakıt|mazot", kucult(g.title)):
                continue
            anahtar = kucult(g.title) + "|" + AA_ADI
            if anahtar in gorulen_link:
                continue
            gorulen_link.add(anahtar)
            haberler.append({"baslik": g.title, "kaynak": AA_ADI, "link": g.link,
                             "zaman": zaman.isoformat(timespec="minutes")})
        durum_kaynak["AA RSS"] = "ok"
    except Exception as e:
        durum_kaynak["AA RSS"] = f"hata: {e}"
    return haberler


def agirlik(g):
    """Farkli site sayisi; Anadolu Ajansi 2 site sayilir."""
    return len(g["kaynaklar"]) + (1 if AA_ADI in g["kaynaklar"] else 0)


def analiz(haberler):
    """Beklentileri (yakit, yon, tutar) gruplar; iptal haberlerini ayirir."""
    gruplar, iptaller = {}, []
    for h in haberler:
        t = kucult(h["baslik"])
        if re.search(IPTAL, t) and re.search(ZAM + "|" + INDIRIM, t):
            iptaller.append(h)
            continue
        if re.search(GECMIS, t) and not re.search(ILERI, t):
            continue  # olmus bir degisiklik, erken uyari degil
        if not re.search(ILERI, t):
            continue
        for yakit, yon, tutar in baslik_coz(h["baslik"]):
            anahtar = f"{yakit}|{yon}|{tutar if tutar is not None else '?'}"
            g = gruplar.setdefault(anahtar, {"yakit": yakit, "yon": yon, "tutar": tutar,
                                             "kaynaklar": {}, "haberler": []})
            g["kaynaklar"].setdefault(h["kaynak"], h["zaman"])
            g["haberler"].append(h)
    # 6,39 ile 6,40 gibi cok yakin tutarlari ayni beklenti say
    degisti = True
    while degisti:
        degisti = False
        anahtarlar = [k for k in gruplar if not k.endswith("|?")]
        for a in anahtarlar:
            for b in anahtarlar:
                if a == b or a not in gruplar or b not in gruplar:
                    continue
                ga, gb = gruplar[a], gruplar[b]
                if (ga["yakit"], ga["yon"]) == (gb["yakit"], gb["yon"]) and \
                        abs(ga["tutar"] - gb["tutar"]) <= 0.06 and \
                        len(ga["kaynaklar"]) >= len(gb["kaynaklar"]):
                    for k, z in gb["kaynaklar"].items():
                        ga["kaynaklar"].setdefault(k, z)
                    ga["haberler"].extend(gb["haberler"])
                    del gruplar[b]
                    degisti = True
    # tutarsiz ("?") gruplari, ayni yakit+yon icin tutarli grup varsa ona kat
    for anahtar in [k for k in gruplar if k.endswith("|?")]:
        yakit, yon, _ = anahtar.split("|")
        hedef = [k for k in gruplar if k.startswith(f"{yakit}|{yon}|") and not k.endswith("|?")]
        if len(hedef) == 1:
            kaynak_grup = gruplar.pop(anahtar)
            g = gruplar[hedef[0]]
            for k, z in kaynak_grup["kaynaklar"].items():
                g["kaynaklar"].setdefault(k, z)
            g["haberler"].extend(kaynak_grup["haberler"])
    liste = sorted(gruplar.values(), key=lambda g: -agirlik(g))
    return liste, iptaller


# ---------------------------------------------------------------- Opet resmi fiyatlar
OPET_API = "https://api.opet.com.tr/api/fuelprices"


def opet_urun_sec(fiyatlar):
    """Opet urun listesinden standart benzin/motorin/lpg fiyatini secer."""
    sec = {}
    for u in fiyatlar:
        ad = kucult(u.get("productName", ""))
        tutar = u.get("amount")
        if tutar is None:
            continue
        if "benzin" in ad and "95" in ad and "benzin" not in sec:
            sec["benzin"] = float(tutar)
        elif "motorin" in ad and "ultra" not in ad and "motorin" not in sec:
            sec["motorin"] = float(tutar)
        elif ("otogaz" in ad or "lpg" in ad) and "lpg" not in sec:
            sec["lpg"] = float(tutar)
    return sec


def opet_al():
    """{il_kodu: {"il":..., "fiyat": {...}, "tum": [...]}} ve son guncelleme."""
    try:
        son = requests.get(f"{OPET_API}/lastupdate", headers=UA, timeout=20).json()
        sonuc = {}
        for kod, il in OPET_ILLER.items():
            r = requests.get(f"{OPET_API}/prices?ProvinceCode={kod}&IncludeAllProducts=true",
                             headers=UA, timeout=20)
            r.raise_for_status()
            veri = r.json()
            urunler = veri[0]["prices"] if veri else []
            sonuc[kod] = {"il": il, "fiyat": opet_urun_sec(urunler),
                          "tum": {u.get("productName"): u.get("amount") for u in urunler}}
        for v in sonuc.values():
            print("Opet", v["il"], "urunler:", v["tum"], "-> secilen:", v["fiyat"])
        durum_kaynak["Opet"] = "ok"
        return sonuc, (son or {}).get("lastUpdateDate")
    except Exception as e:
        durum_kaynak["Opet"] = f"hata: {e}"
        return None, None


def opet_degisim_kontrol(opet, son_guncelleme, durum):
    """Onceki kayitla karsilastirir; gece yarisindan ONCE gorulen degisimi bildirir."""
    if not opet:
        return []
    onceki = durum.get("opet_son", {})
    degisimler = []
    for kod, v in opet.items():
        eski = onceki.get(kod, {}).get("fiyat", {})
        for yakit, yeni in v["fiyat"].items():
            if yakit in eski and abs(eski[yakit] - yeni) >= 0.01:
                degisimler.append({"il": v["il"], "yakit": yakit, "eski": eski[yakit], "yeni": yeni})
    if degisimler and onceki:
        # Tarife gece 00:00'da yururluge girer; 12:00-23:59 arasi gorulen degisim erken haberdir
        erken = 12 <= SIMDI.hour <= 23
        satirlar = [f"{d['il']} {d['yakit']}: {d['eski']:.2f} → {d['yeni']:.2f} TL "
                    f"({d['yeni'] - d['eski']:+.2f})" for d in degisimler]
        if erken:
            ntfy("OPET YENI FIYAT GIRDI", "Opet yeni tarifeyi sisteme girdi (gece yarısından önce):\n"
                 + "\n".join(satirlar), "high", "fuelpump,rotating_light")
        print("Opet degisim:", satirlar, "(erken)" if erken else "(sonra)")
        # isabet olcumu: Opet'in degisimi ne zaman gosterdigini kaydet
        kayit = json_oku("opet_degisim.json", [])
        kayit.append({"zaman": SIMDI.isoformat(timespec="minutes"), "lastUpdate": son_guncelleme,
                      "erken": erken, "degisimler": degisimler})
        json_yaz("opet_degisim.json", kayit[-300:])
    durum["opet_son"] = {k: {"il": v["il"], "fiyat": v["fiyat"]} for k, v in opet.items()}
    durum["opet_urun_adlari"] = sorted({ad for v in opet.values() for ad in v["tum"]})
    durum["opet_lastupdate"] = son_guncelleme
    return degisimler


# ---------------------------------------------------------------- bildirim mantigi
def tutar_yazi(t):
    return f"{t:.2f}".replace(".", ",") + " TL" if t is not None else "tutar belirsiz"


def bildirimleri_gonder(beklentiler, iptaller, durum):
    gonderilen = durum.setdefault("gonderilen", {})
    # 3 gunden eski kayitlari temizle
    esik = (SIMDI - timedelta(days=3)).isoformat()
    for k in [k for k, v in gonderilen.items() if v < esik]:
        del gonderilen[k]

    for g in beklentiler:
        n = agirlik(g)
        if n < GUCLU_ESIK or (g["tutar"] is None and n < GUCLU_ESIK + 2):
            continue
        anahtar = f"{SIMDI:%Y-%m-%d}|{g['yakit']}|{g['yon']}|{g['tutar']}"
        dun = f"{SIMDI - timedelta(days=1):%Y-%m-%d}|{g['yakit']}|{g['yon']}|{g['tutar']}"
        if anahtar in gonderilen or dun in gonderilen:
            continue
        yakit = {"motorin": "Motorine", "benzin": "Benzine", "lpg": "LPG'ye"}[g["yakit"]]
        mesaj = (f"{yakit} {tutar_yazi(g['tutar'])} {g['yon']} bekleniyor.\n"
                 f"{len(g['kaynaklar'])} farklı site aynı rakamı veriyor: {', '.join(list(g['kaynaklar'])[:5])}")
        baslik = f"{g['yakit'].upper()} {'ZAM' if g['yon'] == 'zam' else 'INDIRIM'} BEKLENIYOR"
        if ntfy(baslik, mesaj, "high" if g["yon"] == "zam" else "default",
                "fuelpump,warning" if g["yon"] == "zam" else "fuelpump,chart_with_downwards_trend"):
            gonderilen[anahtar] = SIMDI.isoformat()

    for h in iptaller:
        anahtar = "iptal|" + kucult(h["baslik"])[:80]
        if anahtar in gonderilen:
            continue
        if ntfy("Beklenen degisiklik iptal olabilir", f"{h['baslik']}\n({h['kaynak']})",
                "default", "fuelpump,x"):
            gonderilen[anahtar] = SIMDI.isoformat()


# ---------------------------------------------------------------- HTML
def html_uret(beklentiler, iptaller, kur, kur_gecmis, opet=None, opet_guncel=None):
    e = html.escape
    satirlar = []
    for g in beklentiler:
        n = agirlik(g)
        seviye = "guclu" if n >= GUCLU_ESIK else "soylenti"
        etiket = "Güçlü beklenti" if n >= GUCLU_ESIK else "Söylenti"
        linkler = "".join(
            f'<li><a href="{e(h["link"])}" target="_blank" rel="noopener">{e(h["baslik"])}</a>'
            f' <span class="k">{e(h["kaynak"])} · {e(h["zaman"][11:16])}</span></li>'
            for h in sorted(g["haberler"], key=lambda h: h["zaman"])[:8])
        satirlar.append(f"""
<div class="kart {seviye} {g['yon']}">
  <div class="ust"><b>{e(g['yakit'].capitalize())}</b>
  <span class="yon">{'▲ zam' if g['yon'] == 'zam' else '▼ indirim'}</span>
  <span class="tutar">{e(tutar_yazi(g['tutar']))}</span></div>
  <div class="alt">{etiket} · {len(g['kaynaklar'])} farklı site{' (AA dahil)' if AA_ADI in g['kaynaklar'] else ''}</div>
  <details><summary>Haberler</summary><ul>{linkler}</ul></details>
</div>""")
    if not satirlar:
        satirlar.append('<p class="bos">Son 30 saatte zam/indirim beklentisi haberi yok.</p>')

    iptal_html = "".join(
        f'<li><a href="{e(h["link"])}" target="_blank" rel="noopener">{e(h["baslik"])}</a>'
        f' <span class="k">{e(h["kaynak"])}</span></li>' for h in iptaller[:5])
    iptal_blok = f"<h2>İptal haberleri</h2><ul>{iptal_html}</ul>" if iptaller else ""

    kur_html = "Kur alınamadı"
    if kur:
        kur_html = (f"<div class='kur'><span>USD <b>{kur['USD']:.4f}</b></span>"
                    f"<span>EUR <b>{kur.get('EUR', 0):.4f}</b></span>"
                    f"<span class='k'>TCMB satış · {e(kur.get('tarih', ''))}</span></div>")

    # basit kur cizgisi (son 30 kayit)
    seri = [k["USD"] for k in kur_gecmis[-30:] if "USD" in k]
    svg = ""
    if len(seri) >= 2:
        mn, mx = min(seri), max(seri)
        aralik = (mx - mn) or 1
        noktalar = " ".join(f"{i * 300 / (len(seri) - 1):.1f},{58 - (v - mn) / aralik * 52:.1f}"
                            for i, v in enumerate(seri))
        svg = (f'<svg viewBox="0 0 300 62" class="grafik" role="img" aria-label="USD/TL son {len(seri)} gün">'
               f'<polyline points="{noktalar}" fill="none" stroke="currentColor" stroke-width="2"/></svg>'
               f'<div class="k">USD/TL son {len(seri)} gün: {mn:.2f} – {mx:.2f}</div>')

    opet_html = ""
    if opet:
        satir = "".join(
            f"<tr><td>{e(v['il'])}</td>"
            + "".join(f"<td>{v['fiyat'][y]:.2f}</td>" if y in v["fiyat"] else "<td>–</td>"
                      for y in ("benzin", "motorin", "lpg")) + "</tr>"
            for v in opet.values())
        opet_html = (f"<h2>Pompa fiyatı (Opet)</h2><table><tr><th></th><th>Benzin</th><th>Motorin</th>"
                     f"<th>LPG</th></tr>{satir}</table>"
                     f"<div class='k'>Opet son güncelleme: {e(str(opet_guncel or '?'))}</div>")

    durum = " · ".join(f"{e(k)}: {'✓' if v == 'ok' else '✗ ' + e(v)}" for k, v in durum_kaynak.items())

    sayfa = f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Akaryakıt Zam Takibi</title>
<style>
:root{{--bg:#f6f5f2;--fg:#1d1d1b;--muted:#6b6a66;--card:#fff;--line:#e3e1dc;--zam:#c0392b;--ind:#1e7d4f;--warn:#b7791f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe7;--muted:#9d9b95;--card:#1f1f1d;--line:#33322f;--zam:#ff7b6b;--ind:#5fd39a;--warn:#f0b54a}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
main{{max-width:640px;margin:0 auto;padding:16px}}
h1{{font-size:1.35rem;margin:.2rem 0}}h2{{font-size:1.05rem;margin:1.4rem 0 .5rem}}
.k{{color:var(--muted);font-size:.85rem}}
.kart{{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--muted);border-radius:10px;padding:12px 14px;margin:10px 0}}
.kart.guclu.zam{{border-left-color:var(--zam)}}.kart.guclu.indirim{{border-left-color:var(--ind)}}
.kart.soylenti{{border-left-color:var(--warn)}}
.ust{{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;font-size:1.1rem}}
.zam .yon{{color:var(--zam)}}.indirim .yon{{color:var(--ind)}}.tutar{{font-weight:700;margin-left:auto}}
.alt{{color:var(--muted);font-size:.9rem;margin-top:2px}}
details{{margin-top:6px}}summary{{cursor:pointer;color:var(--muted);font-size:.9rem}}
ul{{padding-left:18px;margin:.4rem 0}}li{{margin:.35rem 0;font-size:.92rem}}a{{color:inherit}}
.kur{{display:flex;gap:16px;flex-wrap:wrap;align-items:baseline;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}}
th,td{{padding:8px 10px;text-align:right;border-bottom:1px solid var(--line)}}th:first-child,td:first-child{{text-align:left}}
.grafik{{width:100%;height:62px;margin-top:8px}}.bos{{color:var(--muted)}}
footer{{margin-top:24px;font-size:.8rem;color:var(--muted)}}
</style></head><body><main>
<h1>Akaryakıt Zam Takibi</h1>
<div class="k">Son güncelleme: {SIMDI:%d.%m.%Y %H:%M} (TSİ)</div>
<h2>Beklenen değişiklikler</h2>
{''.join(satirlar)}
{iptal_blok}
{opet_html}
<h2>Kur</h2>
{kur_html}
{svg}
<footer>
<p>“Güçlü beklenti”: en az {GUCLU_ESIK} farklı site aynı rakamı veriyor. Haberler sektör kaynaklarına dayanır; zam son anda iptal edilebilir.</p>
<p>Kaynak durumu: {durum}</p>
</footer>
</main></body></html>"""
    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as f:
        f.write(sayfa)


# ---------------------------------------------------------------- ana akis
def main():
    durum = json_oku("durum.json", {})

    if os.environ.get("TEST_BILDIRIM") == "1":
        ok = ntfy("Akaryakit takip calisiyor", "Deneme bildirimi: sistem kuruldu ve çalışıyor.",
                  "default", "white_check_mark")
        print("Test bildirimi:", "gonderildi" if ok else "GONDERILEMEDI")

    kur = kur_al()
    kur_gecmis = json_oku("kur.json", [])
    if kur and (not kur_gecmis or kur_gecmis[-1].get("tarih") != kur["tarih"]):
        kur_gecmis.append(kur)
        kur_gecmis = kur_gecmis[-400:]
        json_yaz("kur.json", kur_gecmis)

    opet, opet_guncel = opet_al()
    opet_degisim_kontrol(opet, opet_guncel, durum)

    haberler = haberleri_topla()
    beklentiler, iptaller = analiz(haberler)

    for g in beklentiler:
        print(f"{g['yakit']:8} {g['yon']:8} {tutar_yazi(g['tutar']):>14}  {len(g['kaynaklar'])} site (agirlik {agirlik(g)})")

    bildirimleri_gonder(beklentiler, iptaller, durum)

    # sadece guclu beklentileri kayit altina al (isabet olcumu icin)
    arsiv = json_oku("beklenti_arsiv.json", [])
    for g in beklentiler:
        if agirlik(g) >= GUCLU_ESIK:
            kayit = {"gun": f"{SIMDI:%Y-%m-%d}", "yakit": g["yakit"], "yon": g["yon"],
                     "tutar": g["tutar"], "siteler": sorted(g["kaynaklar"])}
            if kayit not in arsiv:
                arsiv.append(kayit)
    json_yaz("beklenti_arsiv.json", arsiv[-500:])

    durum["son_calisma"] = SIMDI.isoformat()
    json_yaz("durum.json", durum)
    html_uret(beklentiler, iptaller, kur, kur_gecmis, opet, opet_guncel)
    print("Kaynak durumu:", durum_kaynak)
    return 0


if __name__ == "__main__":
    sys.exit(main())
