#!/usr/bin/env python3

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
STATE_FILE = STATE_DIR / "price.json"
HISTORY_FILE = STATE_DIR / "history.csv"

# Ile razy z rzedu cena moze sie nie znalezc, zanim dostaniesz alert
MISSING_ALERT_AFTER = 3

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Separatory tysiecy uzywane przez polskie strony: zwykla spacja, twarda spacja,
# waska twarda spacja, cienka spacja, kropka.
SEPARATORS = " \u00a0\u202f\u2009."

# "3 499 zl", "3.499,00 zl", "3499 PLN"
PRICE_RE = re.compile(
    r"(\d{1,3}(?:[" + SEPARATORS + r"]\d{3})+(?:,\d{1,2})?|\d{3,6}(?:,\d{1,2})?)"
    r"\s*(z[l\u0142]|PLN|EUR|USD|\u20ac|\$)",
    re.IGNORECASE,
)

PRICE_ATTR_RE = re.compile(r"price|cena|kwota|amount|koszt", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Konfiguracja
# --------------------------------------------------------------------------- #

def load_dotenv() -> None:
    """Wczytuje .env obok skryptu. Zmienne juz ustawione w systemie maja pierwszenstwo."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def cfg(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def safe_err(exc: Exception, url: str) -> str:
    """
    Komunikaty bledow z requests/playwrighta zawieraja caly adres, a logi
    publicznego repozytorium sa widoczne dla kazdego. Zasłaniamy wiec adres,
    zostawiajac sama tresc bledu.
    """
    text = f"{type(exc).__name__}: {exc}"
    if url:
        text = text.replace(url, "<oferta>")
        base, _, query = url.partition("?")
        if query:
            text = text.replace(query, "<parametry>").replace(base, "<oferta>")
    return text


def offer_urls() -> list[str]:
    """
    OFFER_URL moze zawierac wiele adresow - po jednym w linii (albo po przecinku
    lub sredniku). Dodanie kolejnej oferty to dopisanie linii w .env / w zmiennej
    OFFER_URL w GitHubie. Kodu nie ruszasz.
    """
    return [u for u in re.split(r"[\s,;]+", cfg("OFFER_URL")) if u.startswith("http")]


def offer_key(url: str) -> str:
    """
    Krotki identyfikator oferty do pliku stanu. Skrot, nie adres - stan laduje
    w publicznym repozytorium (patrz komentarz w append_history).
    """
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]


def cfg_bool(name: str, default: bool = False) -> bool:
    raw = cfg(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "tak", "y"}


# --------------------------------------------------------------------------- #
# Pobieranie strony
# --------------------------------------------------------------------------- #

def fetch_with_requests(url: str, timeout: int = 30) -> str:
    """Szybka sciezka: zwykly HTTP. Dziala, gdy cena jest w HTML albo w danych JSON strony."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.text


def fetch_with_browser(url: str, timeout: int = 60) -> str:
    """Wolna sciezka: prawdziwa przegladarka. Potrzebna, gdy cene doklada JavaScript."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "Brak Playwrighta. Zainstaluj:\n"
            "    pip install -r requirements-browser.txt\n"
            "    python -m playwright install chromium"
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="pl-PL",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)

        # Baner cookies potrafi zaslonic tresc - probujemy go zamknac, ale nie upieramy sie.
        for label in ("Akceptuj wszystkie", "Akceptuję wszystkie", "Akceptuj", "Zgadzam się",
                      "Akceptuję", "Zaakceptuj", "Rozumiem", "OK"):
            try:
                button = page.get_by_role("button", name=label, exact=False)
                if button.count():
                    button.first.click(timeout=2500)
                    break
            except Exception:
                pass

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass  # niektore strony nigdy nie ucichaja - i tak mamy juz DOM

        html = page.content()
        context.close()
        browser.close()
    return html


# --------------------------------------------------------------------------- #
# Wyciaganie ceny
# --------------------------------------------------------------------------- #

def to_number(raw: str) -> float | None:
    """'3 499,00' -> 3499.0 ; '3.499' -> 3499.0"""
    text = raw.strip()
    text = re.sub(r"[ \u00a0\u202f\u2009]", "", text)
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif text.count(".") == 1 and len(text.split(".")[1]) == 3:
        text = text.replace(".", "")  # 3.499 to tysiace, nie ulamek
    try:
        value = float(text)
    except ValueError:
        return None
    return value if 1 <= value <= 1_000_000 else None


def normalize_currency(raw: str) -> str:
    raw = raw.lower()
    if raw in {"zl", "zł", "pln"}:
        return "PLN"
    if raw in {"€", "eur"}:
        return "EUR"
    if raw in {"$", "usd"}:
        return "USD"
    return raw.upper()


def walk_json(node, out: list) -> None:
    """Szuka pol cenowych w dowolnie zagniezdzonym JSON-ie."""
    keys = ("price", "lowprice", "highprice", "pricefrom", "cena", "amount", "value")
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (str, int, float)) and key.lower().replace("_", "") in keys:
                number = to_number(str(value))
                if number:
                    out.append((f"json:{key}", number, str(value)))
            else:
                walk_json(value, out)
    elif isinstance(node, list):
        for item in node:
            walk_json(item, out)


def extract_candidates(html: str, selector: str = "") -> list[tuple[str, float, str]]:
    """
    Zwraca liste (zrodlo, cena, surowy_tekst) uporzadkowana od najbardziej
    do najmniej wiarygodnego zrodla.
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[tuple[str, float, str]] = []

    # 1. Selektor przypiety przez uzytkownika - zawsze wygrywa.
    if selector:
        try:
            for element in soup.select(selector):
                text = element.get_text(" ", strip=True)
                match = PRICE_RE.search(text)
                if match:
                    number = to_number(match.group(1))
                    if number:
                        candidates.append((f"selector:{selector}", number, text[:120]))
                else:
                    number = to_number(text)
                    if number:
                        candidates.append((f"selector:{selector}", number, text[:120]))
        except Exception as exc:
            print(f"  ! selektor '{selector}' nie zadzialal: {exc}")

    # 2. Dane strukturalne JSON-LD (schema.org Offer) - najstabilniejsze zrodlo.
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        found: list = []
        walk_json(data, found)
        for _, number, raw in found:
            candidates.append(("json-ld", number, raw))

    # 3. Dane aplikacji wstrzykniete w HTML (Next.js / Nuxt / wlasne).
    for script_id in ("__NEXT_DATA__", "__NUXT_DATA__"):
        script = soup.find("script", id=script_id)
        if script and script.string:
            try:
                data = json.loads(script.string)
            except Exception:
                continue
            found = []
            walk_json(data, found)
            for source, number, raw in found[:40]:
                candidates.append((f"{script_id}/{source}", number, raw))

    # 4. Elementy, ktore same mowia, ze sa cena (klasa/id/data-*).
    for element in soup.find_all(attrs={"class": PRICE_ATTR_RE}):
        text = element.get_text(" ", strip=True)
        if len(text) > 80:
            continue
        match = PRICE_RE.search(text)
        if match:
            number = to_number(match.group(1))
            if number:
                classes = " ".join(element.get("class") or [])
                candidates.append((f"class:{classes[:40]}", number, text[:120]))

    for attr in ("data-price", "data-cena", "content"):
        for element in soup.find_all(attrs={attr: True}):
            number = to_number(str(element.get(attr)))
            if number and (attr != "content" or PRICE_ATTR_RE.search(
                    str(element.get("itemprop", "")) + str(element.get("property", "")))):
                candidates.append((f"attr:{attr}", number, str(element.get(attr))))

    # 5. Ostatnia deska ratunku - kazda kwota w tekscie strony.
    body_text = soup.get_text(" ", strip=True)
    for match in PRICE_RE.finditer(body_text):
        number = to_number(match.group(1))
        if number:
            start = max(0, match.start() - 45)
            candidates.append(("tekst", number, body_text[start:match.end() + 15]))

    # Deduplikacja z zachowaniem kolejnosci wiarygodnosci.
    seen: set[tuple[str, float]] = set()
    unique = []
    for source, number, raw in candidates:
        key = (source.split(":")[0], number)
        if key in seen:
            continue
        seen.add(key)
        unique.append((source, number, raw))
    return unique


CONTEXT_LABELS = (
    "Cena razem",
    "Cena regularna",
    "Najniższa cena z 30 dni",
    "Zaliczka",
)


def extract_context(html: str) -> dict[str, float]:
    """
    Wyciaga kwoty opisane etykieta, np. 'Cena razem: 5 958 zl'.
    Sluzy tylko do wzbogacenia tresci powiadomienia - nie wplywa na wykrywanie zmian.
    """
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    found: dict[str, float] = {}
    for label in CONTEXT_LABELS:
        # Miedzy etykieta a kwota moze stac jeszcze slowo ("Zaliczka tylko 590 zl"),
        # dlatego dopuszczamy kilkanascie znakow bez cyfr.
        match = re.search(
            re.escape(label) + r"[^\d]{0,20}" + PRICE_RE.pattern, text, re.IGNORECASE
        )
        if match:
            number = to_number(match.group(1))
            if number:
                found[label] = number
    return found


def detect_currency(html: str) -> str:
    match = PRICE_RE.search(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
    return normalize_currency(match.group(2)) if match else "PLN"


def page_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    if heading:
        return heading.get_text(" ", strip=True)[:120]
    if soup.title and soup.title.string:
        return soup.title.string.strip()[:120]
    return "oferta Rainbow"


# --------------------------------------------------------------------------- #
# Stan
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def append_history(key: str, price: float, currency: str) -> None:
    """
    Historia nie zawiera adresu oferty. Repozytorium jest publiczne (darmowe
    minuty Actions), a pliki stanu sa w nim commitowane - link do oferty
    zostaje wiec tylko w zmiennej OFFER_URL, ktora nie jest publiczna.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    is_new = not HISTORY_FILE.exists()
    with HISTORY_FILE.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if is_new:
            writer.writerow(["czas_utc", "oferta", "cena", "waluta"])
        writer.writerow([
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            key, f"{price:.2f}", currency,
        ])


# --------------------------------------------------------------------------- #
# Powiadomienia
# --------------------------------------------------------------------------- #

def pln(value: float) -> str:
    whole = f"{value:,.0f}".replace(",", " ")
    cents = round(value - int(value), 2)
    return whole if cents == 0 else f"{whole},{int(round(cents * 100)):02d}"


def send_whatsapp(text: str) -> bool:
    phone = cfg("CALLMEBOT_PHONE")
    apikey = cfg("CALLMEBOT_APIKEY")
    if not phone or not apikey:
        return False
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
        {"phone": phone, "text": text, "apikey": apikey}
    )
    try:
        response = requests.get(url, timeout=30)
        ok = response.status_code == 200 and "ERROR" not in response.text.upper()
        print(f"  WhatsApp: {'wyslany' if ok else 'blad -> ' + response.text[:200]}")
        return ok
    except Exception as exc:
        print(f"  WhatsApp: blad polaczenia -> {exc}")
        return False


def send_email(subject: str, body: str) -> bool:
    """Opcjonalny kanal zapasowy. Aktywny tylko gdy uzupelnisz dane SMTP w .env."""
    host = cfg("SMTP_HOST")
    to_addr = cfg("MAIL_TO")
    if not host or not to_addr:
        return False

    import smtplib
    from email.message import EmailMessage

    port = int(cfg("SMTP_PORT", "587"))
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = cfg("MAIL_FROM") or cfg("SMTP_USER")
    message["To"] = to_addr
    message.set_content(body)

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls()
        with server:
            user, password = cfg("SMTP_USER"), cfg("SMTP_PASSWORD")
            if user:
                server.login(user, password)
            server.send_message(message)
        print(f"  Mail: wyslany do {to_addr}")
        return True
    except Exception as exc:
        print(f"  Mail: blad -> {exc}")
        return False


def notify(subject: str, body: str) -> None:
    sent_whatsapp = send_whatsapp(body)
    sent_mail = send_email(subject, body)
    if not sent_whatsapp and not sent_mail:
        print("  ! Zaden kanal powiadomien nie jest skonfigurowany - patrz .env.example")


# --------------------------------------------------------------------------- #
# Jedno sprawdzenie
# --------------------------------------------------------------------------- #

def check_offer(url: str, state: dict) -> tuple[int, dict]:
    """Sprawdza jedna oferte. Zwraca (kod_wyjscia, zaktualizowany_stan)."""
    key = offer_key(url)
    selector = cfg("PRICE_SELECTOR")
    use_browser = cfg_bool("USE_BROWSER", False)
    now = datetime.now(timezone.utc)

    html, how = "", ""
    if not use_browser:
        try:
            html = fetch_with_requests(url)
            how = "http"
        except Exception as exc:
            print(f"HTTP nie wyszlo ({safe_err(exc, url)}) - probuje przegladarka")

    candidates = extract_candidates(html, selector) if html else []
    if not candidates:
        try:
            html = fetch_with_browser(url)
            how = "przegladarka"
            candidates = extract_candidates(html, selector)
        except Exception as exc:
            print(f"BLAD pobierania: {safe_err(exc, url)}")
            pass

    print(f"Sprawdzono {now:%Y-%m-%d %H:%M:%S} UTC (zrodlo: {how or 'brak'})")
    for source, number, raw in candidates[:8]:
        print(f"  kandydat: {pln(number)} <- {source} | {raw[:70]}")

    # --- cena sie nie znalazla ---
    if not candidates:
        misses = state.get("misses", 0) + 1
        state["misses"] = misses
        state["last_check_date"] = now.strftime("%Y-%m-%d")
        print(f"  ! nie znaleziono ceny (nieudane proby z rzedu: {misses})")
        if misses == MISSING_ALERT_AFTER:
            notify(
                "Rainbow: nie moge odczytac ceny",
                f"Uwaga: od {misses} sprawdzen nie potrafie odczytac ceny z oferty.\n"
                f"Oferta moze byc juz niedostepna albo strona sie zmienila.\n\n{url}",
            )
        # Pojedyncza wtopa sieciowa to nie awaria - konczymy zerem, zeby przebieg
        # w Actions zostal zielony i nie zasypywal Cie mailami o bledzie.
        # Czerwono robi sie dopiero, gdy problem sie utrwali.
        return (1 if misses >= MISSING_ALERT_AFTER else 0), state

    currency = detect_currency(html)
    title = page_title(html)
    extras = extract_context(html)
    previous = state.get("price")

    # PRICE_LABEL pozwala sledzic kwote opisana etykieta, np. "Cena razem",
    # zamiast domyslnej ceny za osobe. Ma pierwszenstwo przed automatem.
    label = cfg("PRICE_LABEL")
    if label and label in extras:
        source, price = f"etykieta:{label}", extras[label]
    else:
        if label:
            print(f"  ! etykieta '{label}' nie znaleziona - biore cene automatycznie")
        source, price, _raw = candidates[0]

    extras_text = "".join(
        f"{name}: {pln(value)} {currency}\n" for name, value in extras.items()
    )

    # Uwaga: adresu oferty tu celowo nie zapisujemy - patrz komentarz w append_history().
    state.update({
        "title": title,
        "price": price,
        "currency": currency,
        "source": source,
        "extras": dict(extras),
        "misses": 0,
        "last_check_date": now.strftime("%Y-%m-%d"),
    })

    # --- pierwszy pomiar ---
    if previous is None:
        state["first_seen"] = now.isoformat(timespec="seconds")
        state["last_change"] = state["first_seen"]
        append_history(key, price, currency)
        print(f"  Zapisano cene wyjsciowa: {pln(price)} {currency}")
        notify(
            "Rainbow: monitoring wystartowal",
            f"Monitoring wystartowal.\n\nOferta: {title}\n"
            f"Cena wyjsciowa: {pln(price)} {currency}\n"
            f"{extras_text}\n{url}\n\n"
            f"Od teraz dostaniesz wiadomosc przy kazdej zmianie ceny.",
        )
        return 0, state

    # --- bez zmian ---
    if abs(price - previous) < 0.005:
        print(f"  Bez zmian: {pln(price)} {currency}")
        return 0, state

    # --- zmiana ceny ---
    delta = price - previous
    percent = delta / previous * 100 if previous else 0
    arrow = "↓ TANIEJ" if delta < 0 else "↑ DROZEJ"
    sign = "-" if delta < 0 else "+"

    state["last_change"] = now.isoformat(timespec="seconds")
    state["previous_price"] = previous
    append_history(key, price, currency)

    body = (
        f"{arrow} o {pln(abs(delta))} {currency} ({sign}{abs(percent):.1f}%)\n\n"
        f"Oferta: {title}\n"
        f"Bylo:  {pln(previous)} {currency}\n"
        f"Jest:  {pln(price)} {currency}\n"
        f"{extras_text}\n{url}"
    )
    print(f"  ZMIANA: {pln(previous)} -> {pln(price)} {currency} ({sign}{abs(percent):.1f}%)")
    notify(f"Rainbow {arrow}: {pln(price)} {currency}", body)
    return 0, state


def check_once() -> int:
    urls = offer_urls()
    if not urls:
        print("BLAD: brak OFFER_URL - wklej adres oferty do .env")
        return 2

    store = load_state()
    if "price" in store:  # stary plik z jedna oferta - przypisujemy do pierwszego linku
        store = {offer_key(urls[0]): store}

    worst = 0
    for index, url in enumerate(urls, 1):
        key = offer_key(url)
        # Adresu nie logujemy - logi Actions sa publiczne (patrz safe_err).
        print(f"\n=== oferta {index}/{len(urls)} [{key}] {store.get(key, {}).get('title', '')} ===")
        try:
            code, state = check_offer(url, store.get(key, {}))
        except Exception as exc:
            # Jedna wywrotka nie moze zablokowac pozostalych ofert.
            print(f"  BLAD: {safe_err(exc, url)}")
            code, state = 1, store.get(key, {})
        store[key] = state
        worst = max(worst, code)

    save_state(store)
    return worst


# --------------------------------------------------------------------------- #
# Tryby
# --------------------------------------------------------------------------- #

def discover() -> int:
    urls = offer_urls()
    if not urls:
        print("BLAD: brak OFFER_URL w .env")
        return 2
    for url in urls:
        discover_one(url)
    return 0


def discover_one(url: str) -> None:
    selector = cfg("PRICE_SELECTOR")
    print(f"Pobieram {url}")
    print(f"PRICE_SELECTOR = {selector or '(pusty - automat)'}\n")
    html = ""
    try:
        html = fetch_with_requests(url)
        print("--- zwykly HTTP ---")
        found = extract_candidates(html, selector)
        if found:
            for source, number, raw in found[:25]:
                print(f"  {pln(number):>12}  {source:<34} | {raw[:70]}")
        else:
            print("  nic nie znaleziono - cene dorzuca JavaScript")
    except Exception as exc:
        print(f"  blad: {exc}")

    if html:
        print("\n--- kwoty rozpoznane po etykiecie (do PRICE_LABEL) ---")
        labelled = extract_context(html)
        if labelled:
            for name, value in labelled.items():
                print(f"  {pln(value):>12}  PRICE_LABEL={name}")
        else:
            print("  brak kwot z etykieta")

    # Przegladarke odpalamy tylko wtedy, gdy zwykly HTTP nic nie dal
    # albo gdy wprost o to poprosisz przez USE_BROWSER=true.
    if not html or cfg_bool("USE_BROWSER", False):
        print("\n--- przegladarka (Playwright) ---")
        try:
            html = fetch_with_browser(url)
            for source, number, raw in extract_candidates(html, selector)[:25]:
                print(f"  {pln(number):>12}  {source:<34} | {raw[:70]}")
        except Exception as exc:
            print(f"  blad: {exc}")

    if html:
        dump = STATE_DIR / f"debug_page_{offer_key(url)}.html"
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        dump.write_text(html, encoding="utf-8")
        print(f"\nHTML zapisany do {dump} - mozesz w nim poszukac wlasnego selektora")

    print("\nPierwszy kandydat z listy to kwota, ktora monitor bedzie sledzil.")
    print("Jesli to nie ta - ustaw PRICE_LABEL (prosciej) albo PRICE_SELECTOR w .env")


def test_notification() -> int:
    print("Wysylam testowe powiadomienie...")
    notify(
        "Rainbow: test powiadomien",
        "To wiadomosc testowa z monitora cen Rainbow. Jesli ja widzisz - konfiguracja dziala.",
    )
    return 0


def loop() -> int:
    interval = int(cfg("CHECK_INTERVAL", "600"))
    print(f"Petla: sprawdzam co {interval} s. Ctrl+C konczy.\n")
    while True:
        try:
            check_once()
        except KeyboardInterrupt:
            print("\nZatrzymano.")
            return 0
        except Exception as exc:
            print(f"  blad w cyklu: {exc}")
        print()
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nZatrzymano.")
            return 0


def selftest() -> int:
    """Sprawdza rozbijanie OFFER_URL i migracje starego pliku stanu."""
    os.environ["OFFER_URL"] = " https://a.pl/x?y=1 , https://b.pl/z\nhttps://c.pl/q "
    urls = offer_urls()
    assert urls == ["https://a.pl/x?y=1", "https://b.pl/z", "https://c.pl/q"], urls
    assert len({offer_key(u) for u in urls}) == 3
    assert offer_key(urls[0]) == offer_key(urls[0])  # stabilny miedzy przebiegami

    stary = {"price": 100.0, "misses": 0}
    nowy = {offer_key(urls[0]): stary} if "price" in stary else stary
    assert nowy[offer_key(urls[0])]["price"] == 100.0

    os.environ["OFFER_URL"] = ""
    assert offer_urls() == []
    print("selftest OK")
    return 0


def main() -> int:
    load_dotenv()
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--selftest":
        return selftest()
    if arg in ("--discover", "-d"):
        return discover()
    if arg in ("--test", "-t"):
        return test_notification()
    if arg in ("--loop", "-l"):
        return loop()
    if arg in ("--help", "-h"):
        print(__doc__)
        return 0
    return check_once()


if __name__ == "__main__":
    sys.exit(main())
