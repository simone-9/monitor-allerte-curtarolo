#!/usr/bin/env python3
"""
Monitoraggio del Bollettino / Avviso di allerta regionale (CFD Veneto) per la zona E1 (Curtarolo).

A ogni esecuzione:
  1. legge la pagina dei bollettini del CFD e trova i PDF "Bollettino/Avviso di allerta regionale" (BAR_ / AAR_);
  2. per ogni documento nuovo scarica il PDF e legge i livelli della zona per OGNI tipo di rischio;
  3. salva i dati su Firestore (le schede dell'app si aggiornano da sole);
  4. invia Telegram per allerte ARANCIONE/ROSSA (e per il rientro da quei livelli), senza ripetizioni;
  5. se un documento non si legge con certezza segna "DA VERIFICARE" e avvisa: mai "verde" per default.

Prova locale su un PDF (nessun accesso a Firebase):
    python monitor_allerte.py --pdf BAR_261022.pdf
"""
import argparse, hashlib, io, json, os, re, sys
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

CFD_URL = "https://www.regione.veneto.it/web/protezione-civile/cfd"
ZONA = os.environ.get("CFD_ZONA", "E1").strip().upper()
NOME_ZONA = os.environ.get("CFD_NOME_ZONA", "Pianura centrale")
SOGLIA_TELEGRAM = os.environ.get("SOGLIA_TELEGRAM", "ARANCIONE").strip().upper()
USER_AGENT = "ProtezioneCivileCurtarolo-Monitor/1.0 (monitoraggio allerte, protezione civile comunale)"
MAX_DOCUMENTI = 6
TIMEOUT = 40
TZ = ZoneInfo("Europe/Rome")

LIVELLI = ["VERDE", "GIALLA", "ARANCIONE", "ROSSA"]
RANGO = {l: i for i, l in enumerate(LIVELLI)}
MESI = {"gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7,
        "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12}
# intestazione colonna (senza "RISCHIO") -> (chiave, etichetta)
RISCHI_NOTI = {
    "IDRAULICO": ("idraulico", "Idraulico"),
    "IDROGEOLOGICO": ("idrogeologico", "Idrogeologico"),
    "IDROGEOLOGICO TEMPORALI": ("temporali", "Idrogeologico per temporali"),
    "VALANGHE": ("valanghe", "Valanghe"),
    "NEVE": ("neve", "Neve"),
    "GHIACCIO AL SUOLO": ("ghiaccio", "Ghiaccio al suolo"),
    "VENTO": ("vento", "Vento"),
    "ONDATE DI CALORE": ("calore", "Ondate di calore"),
}


class ErroreLettura(Exception):
    """Documento non letto con certezza: va verificato a mano."""


def data_it(g, mese_nome, anno):
    mese = MESI.get(mese_nome.lower())
    if not mese:
        raise ErroreLettura(f"mese non riconosciuto: {mese_nome}")
    return f"{int(g)}/{mese}/{int(anno)}"       # stesso formato di toLocaleDateString('it-IT') nell'app


def giorno_da_tabella(tabella):
    """tabella = righe di pdfplumber. Ritorna {rischio: livello|None} per la zona configurata."""
    if not tabella or not tabella[0] or (tabella[0][0] or "").strip().upper() != "ZONA":
        raise ErroreLettura("intestazione della tabella non riconosciuta")
    nomi = []
    for c in tabella[0][1:]:
        t = re.sub(r"\s+", " ", (c or "")).strip().upper()
        t = re.sub(r"^RISCHIO\s+", "", t)
        nomi.append(t)
    riga = next((r for r in tabella[1:] if r and (r[0] or "").strip().upper() == ZONA), None)
    if riga is None:
        raise ErroreLettura(f"riga della zona {ZONA} non trovata")
    valori = [re.sub(r"\s+", "", (c or "")).upper() for c in riga[1:]]
    if len(valori) != len(nomi):
        raise ErroreLettura(f"{len(nomi)} colonne ma {len(valori)} valori per la zona {ZONA}")
    rischi = {}
    for nome, val in zip(nomi, valori):
        if val not in LIVELLI and val != "-":
            raise ErroreLettura(f"valore non riconosciuto per {nome}: '{val}'")
        chiave, etichetta = RISCHI_NOTI.get(nome, (re.sub(r"\W+", "_", nome.lower()), nome.capitalize()))
        rischi[chiave] = {"nome": etichetta, "livello": None if val == "-" else val}
    if not any(v["livello"] for v in rischi.values()):
        raise ErroreLettura("nessun livello leggibile per la zona")
    return rischi


def livello_max(rischi):
    return max((v["livello"] for v in rischi.values() if v["livello"]), key=lambda l: RANGO[l])


def leggi_pdf(data: bytes):
    import pdfplumber
    giorni, testi = [], []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pag in pdf.pages:
            testo = pag.extract_text() or ""
            testi.append(testo)
            m = re.search(r"ALLERTA\s+PREVISTA\s+PER\s+(OGGI|DOMANI|DOPODOMANI)\s*[-–—]\s*\S+\s+(\d{1,2})\s+([^\W\d_]+)\s+(\d{4})", testo, re.I)
            if not m:
                continue
            tabelle = pag.extract_tables()
            if len(tabelle) != 1:
                raise ErroreLettura(f"attesa 1 tabella, trovate {len(tabelle)}")
            rischi = giorno_da_tabella(tabelle[0])
            giorni.append({"giorno": m.group(1).upper(), "data": data_it(m.group(2), m.group(3), m.group(4)),
                           "livello": livello_max(rischi), "rischi": rischi})
    tutto = "\n".join(testi)
    mt = re.search(r"(BOLLETTINO|AVVISO)\s+DI\s+ALLERTA\s+REGIONALE", tutto, re.I)
    if not mt:
        raise ErroreLettura("non sembra un documento di allerta regionale")
    if not any(g["giorno"] == "OGGI" for g in giorni):
        raise ErroreLettura("tabella dell'allerta di oggi non trovata")
    mn = re.search(r"Documento\s+n\.?\s*(\d+)\s*/\s*(\d{4})", tutto, re.I)
    me = re.search(r"Emissione:\s*\S+\s+(\d{1,2})\s+([^\W\d_]+)\s+(\d{4})\s+ore:?\s*(\d{1,2})[:.](\d{2})", tutto, re.I)
    emissione = None
    if me and MESI.get(me.group(2).lower()):
        emissione = datetime(int(me.group(3)), MESI[me.group(2).lower()], int(me.group(1)), int(me.group(4)), int(me.group(5)), tzinfo=TZ)
    fen = re.search(r"FENOMENI\s+PREVISTI\s*(.*?)\s*TENDENZA\s*(.*?)\s*(?:Note\b|Agenzia\s+Regionale)", tutto, re.S)
    return {"tipo": "Bollettino" if mt.group(1).upper() == "BOLLETTINO" else "Avviso",
            "numero": f"{int(mn.group(1))}/{mn.group(2)}" if mn else None, "emissione": emissione, "giorni": giorni,
            "fenomeni": re.sub(r"\s+", " ", fen.group(1)).strip() if fen else "",
            "tendenza": re.sub(r"\s+", " ", fen.group(2)).strip() if fen else ""}


# ------------------------------------------------------------------ pagina CFD
def trova_documenti(html):
    from bs4 import BeautifulSoup
    soup, visti, docs = BeautifulSoup(html, "lxml"), set(), []
    for a in soup.find_all("a", href=True):
        if re.search(r"/(BAR|AAR)_\d{6}[^/]*\.pdf", a["href"], re.I):
            url = urljoin(CFD_URL, a["href"])
            if url not in visti:
                visti.add(url)
                docs.append({"url": url, "testo": " ".join(a.get_text().split())})
    return docs[:MAX_DOCUMENTI]


def http_get(url):
    import requests
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    r.raise_for_status()
    return r


# ------------------------------------------------------------------ Telegram
def messaggio_telegram(g, doc, url, prec_liv):
    icone = {"ARANCIONE": "\U0001F7E0", "ROSSA": "\U0001F534"}
    liv = g["livello"]
    if RANGO[liv] < RANGO[SOGLIA_TELEGRAM]:
        testa = f"✅ Allerta rientrata (era {prec_liv}): ora {liv} per {g['giorno']} ({g['data']})"
    else:
        testa = f"{icone.get(liv, '')} ALLERTA {liv} - {g['giorno']} ({g['data']})"
    righe = [testa, f"Zona {ZONA} - {NOME_ZONA} (Curtarolo)", ""]
    attivi = [v for v in g["rischi"].values() if v["livello"] and v["livello"] != "VERDE"]
    if attivi:
        righe.append("Rischi:")
        righe += [f"• {v['nome']}: {v['livello']}" for v in attivi]
    em = doc["emissione"].strftime("%d/%m/%Y ore %H:%M") if doc["emissione"] else "n.d."
    righe += ["", f"Fonte: {doc['tipo']} di allerta regionale n. {doc['numero'] or '?'} - emissione {em}", url]
    return "\n".join(righe)


def invia_telegram(testo):
    import requests
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("Telegram non configurato: messaggio non inviato")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": testo, "disable_web_page_preview": True}, timeout=TIMEOUT)
        ok = bool(r.ok and r.json().get("ok"))
        if not ok:
            print("Errore Telegram:", r.text[:200])
        return ok
    except Exception as e:
        print("Errore Telegram:", e)
        return False


# ------------------------------------------------------------------ archivi
class ArchivioFirestore:
    def __init__(self):
        import firebase_admin
        from firebase_admin import credentials, firestore
        firebase_admin.initialize_app(credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])))
        self.db = firestore.client()

    def leggi(self, p):
        d = self.db.document(p).get()
        return d.to_dict() if d.exists else None

    def scrivi(self, p, dati):
        self.db.document(p).set(dati, merge=True)

    def aggiungi(self, coll, dati):
        self.db.collection(coll).add(dati)


class ArchivioMemoria:
    def __init__(self):
        self.doc, self.liste = {}, {}

    def leggi(self, p):
        return self.doc.get(p)

    def scrivi(self, p, dati):
        self.doc[p] = {**self.doc.get(p, {}), **dati}

    def aggiungi(self, coll, dati):
        self.liste.setdefault(coll, []).append(dati)


# ------------------------------------------------------------------ elaborazione
def firma(g):
    return g["livello"] + "|" + ";".join(sorted(f"{k}:{v['livello']}" for k, v in g["rischi"].items() if v["livello"] and v["livello"] != "VERDE"))


def elabora(archivio, url, testo_link, pdf_bytes, invia=invia_telegram):
    doc_id, ora = hashlib.sha1(url.encode()).hexdigest()[:20], datetime.now(TZ).isoformat()
    try:
        doc = leggi_pdf(pdf_bytes)
    except Exception as e:          # qualsiasi problema = "da verificare", mai verde
        archivio.scrivi(f"cfd_documenti/{doc_id}", {"url": url, "stato": "errore", "errore": str(e), "elaborato": ora})
        archivio.scrivi("cfd_stato/monitor", {"daVerificare": {"url": url, "documento": testo_link, "motivo": str(e), "quando": ora}})
        invia(f"⚠️ Monitoraggio allerte: non riesco a leggere con certezza \"{testo_link}\".\nMotivo: {e}\nVerifica a mano: {url}")
        print("DA VERIFICARE:", url, e)
        return False
    emis = (doc["emissione"] or datetime.now(TZ)).isoformat()
    nome = f"{doc['tipo']} di allerta regionale n. {doc['numero'] or '?'}"
    firme = dict((archivio.leggi("cfd_stato/notifiche") or {}).get("firme", {}))
    for g in doc["giorni"]:
        f_new, f_old = firma(g), firme.get(g["data"])
        liv_old = f_old.split("|")[0] if f_old else "VERDE"
        notifica = f_new != f_old and (RANGO[g["livello"]] >= RANGO[SOGLIA_TELEGRAM] or RANGO[liv_old] >= RANGO[SOGLIA_TELEGRAM])
        inviata = invia(messaggio_telegram(g, doc, url, liv_old)) if notifica else False
        firme[g["data"]] = f_new
        attivo = g["livello"] != "VERDE"
        archivio.aggiungi("allerte_cfd_veneto", {
            "data": g["data"], "giorno": g["giorno"], "zona": f"{ZONA} ({NOME_ZONA})", "livello": g["livello"],
            "rischi": g["rischi"], "timestamp": emis, "documento": nome, "pdfUrl": url,
            "fenomeni": doc["fenomeni"], "tendenza": doc["tendenza"], "inseritoDa": "Monitoraggio automatico CFD",
            "fonte": "AUTO", "telegramInviato": inviata,
            "esitoWA": "Notifica Telegram inviata" if inviata else ("Registrato (senza invio)" if attivo else "Registrato")})
    archivio.scrivi("cfd_stato/notifiche", {"firme": firme})
    archivio.scrivi(f"cfd_documenti/{doc_id}", {"url": url, "stato": "ok", "numero": doc["numero"], "tipo": doc["tipo"], "emissione": emis, "elaborato": ora})
    archivio.scrivi("cfd_stato/monitor", {"daVerificare": None, "ultimoDocumento": nome, "ultimoDocumentoUrl": url, "ultimaEmissione": emis})
    print("Elaborato:", nome, [(g["giorno"], g["data"], g["livello"]) for g in doc["giorni"]])
    return True


def esegui(archivio, invia=invia_telegram):
    ora = datetime.now(TZ).isoformat()
    try:
        docs = trova_documenti(http_get(CFD_URL).text)
        if not docs:
            raise RuntimeError("nessun documento di allerta trovato nella pagina (struttura cambiata?)")
        nuovi = 0
        for d in reversed(docs):                      # dal piu' vecchio al piu' recente
            if archivio.leggi(f"cfd_documenti/{hashlib.sha1(d['url'].encode()).hexdigest()[:20]}"):
                continue
            elabora(archivio, d["url"], d["testo"], http_get(d["url"]).content, invia)
            nuovi += 1
        archivio.scrivi("cfd_stato/monitor", {"ultimoControllo": ora, "esito": "ok", "messaggio": f"{nuovi} nuovi documenti", "erroriConsecutivi": 0})
        return 0
    except Exception as e:
        n = int((archivio.leggi("cfd_stato/monitor") or {}).get("erroriConsecutivi", 0)) + 1
        archivio.scrivi("cfd_stato/monitor", {"ultimoControllo": ora, "esito": "errore", "messaggio": str(e)[:300], "erroriConsecutivi": n})
        print("ERRORE:", e)
        if n == 3:
            invia(f"⚠️ Monitoraggio allerte: 3 controlli consecutivi falliti ({str(e)[:150]}).\nVerifica a mano: {CFD_URL}")
        return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", help="legge un PDF locale e stampa il risultato (senza Firebase)")
    a = ap.parse_args()
    if a.pdf:
        d = leggi_pdf(open(a.pdf, "rb").read())
        d["emissione"] = d["emissione"].isoformat() if d["emissione"] else None
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0
    if not os.environ.get("FIREBASE_SERVICE_ACCOUNT"):
        print("Manca FIREBASE_SERVICE_ACCOUNT")
        return 2
    return esegui(ArchivioFirestore())


if __name__ == "__main__":
    sys.exit(main())
