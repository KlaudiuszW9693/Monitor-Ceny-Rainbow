# Monitor ceny oferty Rainbow (r.pl)

Sprawdza cenę wybranej oferty co 10 minut i wysyła Ci wiadomość na WhatsAppie
(opcjonalnie także maila), gdy cena się zmieni.

---

## Jak to działa

Skrypt pobiera stronę oferty i czyta cenę z danych strukturalnych, które r.pl
umieszcza w HTML. Nie potrzebuje przeglądarki, więc jedno sprawdzenie trwa
około 5 sekund. Ostatnią odczytaną cenę trzyma w pliku `state/price.json`,
a historię zmian dopisuje do `state/history.csv`.

Powiadomienie leci wtedy, gdy:

- cena się zmieni (w górę lub w dół),
- albo cena przestanie się dać odczytać przez 3 sprawdzenia z rzędu — to sygnał,
  że oferta zniknęła lub strona się przebudowała. Bez tego monitor mógłby
  „zamilknąć” i wyglądałby na działający, mimo że nic już nie sprawdza.

---

## Krok 1 — WhatsApp (2 minuty, jednorazowo)

1. Dodaj do kontaktów w telefonie numer **+34 644 87 21 57** (bot CallMeBot).
2. Wyślij mu na WhatsAppie dokładnie taką wiadomość:
   `I allow callmebot to send me messages`
3. Odpisze Ci wiadomością z Twoim **API key** — zapisz go.

To darmowe do użytku prywatnego.

---

## Krok 2 — test lokalnie na Twoim komputerze

Uzupełnij w pliku `.env`:

```
CALLMEBOT_PHONE=+48TWOJNUMER
CALLMEBOT_APIKEY=klucz_od_bota
```

Potem w terminalu:

```powershell
py -m pip install -r requirements.txt
py monitor.py --test        # powinieneś dostać testową wiadomość na WhatsAppie
py monitor.py               # jedno prawdziwe sprawdzenie ceny
```

Jeśli chcesz, żeby chodziło na Twoim komputerze bez chmury:

```powershell
py monitor.py --loop        # sprawdza co 10 minut, dopóki nie zamkniesz okna
```

---

## Krok 3 — uruchomienie 24/7 na GitHub Actions

Całość robi się w przeglądarce, bez terminala. Około 10 minut.

### 3.1 Utwórz repozytorium

Wejdź na <https://github.com/new> i wypełnij:

- **Repository name:** `monitor-rainbow`
- **Public** ← zaznacz to, nie „Private”
- **Add a README file** ← *nie* zaznaczaj, ma zostać puste
- kliknij **Create repository**

#### Dlaczego Public, a nie Private

Darmowe minuty GitHub Actions liczą się **tylko dla repozytoriów prywatnych** —
limit to 2000 minut miesięcznie. Publiczne mają limit nieograniczony.

Każdy przebieg to ~30–60 sekund, ale GitHub zaokrągla go w górę do pełnej
minuty, więc licz 1 minutę na sprawdzenie:

| Interwał | Przebiegów/mc | Minut/mc | Zmieści się w Private? |
|---|---|---|---|
| co 10 min | 4320 | ~4320 | nie, przekracza dwukrotnie |
| co 30 min | 1440 | ~1440 | tak, z zapasem |

Czyli Private jest możliwe, ale wtedy trzeba zmienić w pliku
`.github/workflows/monitor.yml` linię `- cron: "*/10 * * * *"` na `"*/30 * * * *"`
i pogodzić się z wolniejszą reakcją na zmianę ceny.

#### Co jest widoczne w publicznym repo

**Nie jest widoczne:** klucz CallMeBot, Twój numer telefonu, dane SMTP — leżą
w sekretach GitHuba, zaszyfrowane i maskowane w logach. Link do oferty też nie:
trzyma go zmienna `OFFER_URL`, a zakładka Settings nie jest publiczna. Skrypt
celowo nie zapisuje adresu do plików stanu ani nie wypisuje go w logach — nawet
komunikaty błędów mają zasłonięte parametry adresu.

**Jest widoczne:** sam kod skryptu, ten README, nazwa hotelu i historia ceny
(w `state/`). Nic z tego nie jest tajne.

### 3.2 Wgraj pliki — najpierw te zwykłe

Na stronie pustego repozytorium kliknij **uploading an existing file**
(link w zdaniu „…or upload an existing file”).

Otwórz folder `Secret` w Eksploratorze plików i przeciągnij na stronę **5 plików**:

- `monitor.py`
- `requirements.txt`
- `requirements-browser.txt`
- `README.md`
- `.gitignore`

**Nie przeciągaj `.env`** — tam jest Twój klucz WhatsApp. `.env.example` możesz
wgrać, w nim nie ma nic tajnego. Folderu `state` też nie wgrywaj — pierwszy
przebieg w chmurze sam go utworzy i dzięki temu dostaniesz wiadomość startową
jako potwierdzenie, że wszystko działa.

Na dole kliknij **Commit changes**.

### 3.3 Wgraj plik workflow — tu jest jedyny haczyk

Folder `.github` zaczyna się od kropki i Eksplorator lubi z nim płatać figle,
więc ten jeden plik utwórz ręcznie — jest to pewniejsze niż przeciąganie:

1. W repozytorium kliknij **Add file → Create new file**.
2. W pole z nazwą pliku wpisz dokładnie:

   ```
   .github/workflows/monitor.yml
   ```

   Ukośniki `/` same zamienią się w foldery — zobaczysz, jak nazwa rozbija się
   na kolejne katalogi. Tak ma być.
3. Otwórz u siebie plik `.github\workflows\monitor.yml` w Notatniku,
   zaznacz wszystko (Ctrl+A), skopiuj (Ctrl+C) i wklej w duże pole na stronie.
4. Kliknij **Commit changes** → **Commit changes**.

### 3.4 Ustaw dane konfiguracyjne

W repozytorium: **Settings** (górne menu) → w lewej kolumnie
**Secrets and variables** → **Actions**.

Zobaczysz dwie zakładki. Kolejność ma znaczenie tylko taką, że jedne są jawne,
a drugie zaszyfrowane.

**Zakładka „Variables”** — przycisk **New repository variable**, dodaj po kolei:

| Name | Value |
|------|-------|
| `OFFER_URL` | pełny link do oferty — skopiuj z pliku `.env`, cały, razem z `?unikalnyKluczOferty=…` |
| `USE_BROWSER` | `false` |
| `PRICE_LABEL` | zostaw pole Value puste (albo wpisz `Cena razem`) |

`PRICE_SELECTOR` pomiń, nie jest potrzebny.

**Zakładka „Secrets”** — przycisk **New repository secret**, dodaj dwa:

| Name | Secret |
|------|--------|
| `CALLMEBOT_PHONE` | Twój numer z plusem, np. `+48601234567` |
| `CALLMEBOT_APIKEY` | klucz, który odpisał Ci bot |

### 3.5 Odpal pierwszy przebieg

1. Wejdź w zakładkę **Actions** (górne menu repozytorium).
2. Jeśli zobaczysz zieloną planszę „Workflows aren't being run on this
   forked repository” albo pytanie o włączenie Actions — kliknij przycisk
   potwierdzenia. W nowym własnym repo zwykle się nie pojawia.
3. W lewej kolumnie kliknij **Monitor ceny Rainbow**.
4. Po prawej **Run workflow** → zielony przycisk **Run workflow**.
5. Odśwież stronę po kilkunastu sekundach. Pojawi się przebieg — kliknij go,
   potem **sprawdz**, i możesz na żywo patrzeć, co robi.

Jeśli wszystko gra, dostaniesz na WhatsAppie wiadomość „Monitoring wystartowal”
z ceną 2 979 zł. Od tej chwili sprawdza się sam co 10 minut i milczy, dopóki
cena się nie zmieni.

### Jeśli coś nie zadziała

**Krok „Zapisz stan w repo” świeci na czerwono z błędem 403** — GitHub nie dał
workflowowi prawa zapisu. Napraw tak: **Settings → Actions → General** →
zjedź do **Workflow permissions** → zaznacz **Read and write permissions** →
**Save**. Potem odpal przebieg ponownie.

**Krok „Sprawdz cene” pisze „Zaden kanal powiadomien nie jest skonfigurowany”** —
sekrety `CALLMEBOT_PHONE` / `CALLMEBOT_APIKEY` nie doszły. Sprawdź, czy nazwy
są wpisane dokładnie tak jak w tabeli i czy trafiły do zakładki **Secrets**,
a nie **Variables**.

**Krok „Sprawdz cene” pisze „brak OFFER_URL”** — zmienna `OFFER_URL` wylądowała
w **Secrets** zamiast w **Variables**, albo ma literówkę w nazwie.

**Przebieg nie startuje sam co 10 minut** — harmonogram działa tylko z gałęzi
głównej (`main`) i rusza dopiero po kilku minutach od wgrania pliku. GitHub
przy dużym ruchu opóźnia start crona o kilka minut i tego nie da się wyłączyć;
dla śledzenia ceny wycieczki nie ma to znaczenia.

---

## Mail zamiast lub obok WhatsAppa (opcjonalnie)

Skrypt umie wysyłać też maila przez SMTP Twojego serwera pocztowego. Nie musi
przy tym stać na serwerze pocztowym — łączy się z nim po sieci, tak jak Twój
program pocztowy.

Dodaj te sekrety w GitHubie (albo wypełnij w `.env` przy pracy lokalnej):

| Nazwa           | Co wpisać |
|-----------------|-----------|
| `SMTP_HOST`     | adres serwera poczty wychodzącej, np. `smtp.liderstal.pl` |
| `SMTP_PORT`     | `587` (albo `465`, jeśli Twój serwer używa SSL) |
| `SMTP_USER`     | login do skrzynki (zwykle pełny adres e-mail) |
| `SMTP_PASSWORD` | hasło do skrzynki |
| `MAIL_FROM`     | adres, z którego ma wychodzić mail |
| `MAIL_TO`       | adres, na który mają przychodzić powiadomienia |

Te dane znajdziesz w konfiguracji swojego programu pocztowego, w sekcji
„Serwer poczty wychodzącej (SMTP)”. Dopóki `SMTP_HOST` jest puste, maile są
po prostu wyłączone i działa tylko WhatsApp.

---

## Którą kwotę śledzi

Domyślnie cenę **za osobę** — teraz 2 979 zł. Strona podaje przy niej jeszcze
kilka innych kwot, a monitor wypisuje je wszystkie w treści powiadomienia,
więc widzisz pełny obraz bez wchodzenia na stronę.

Jeśli wolisz, żeby monitor pilnował innej z nich, ustaw `PRICE_LABEL` w `.env`
(albo jako zmienną w GitHubie):

| `PRICE_LABEL` | Śledzona kwota |
|---------------|----------------|
| *(puste)* | 2 979 zł — za osobę |
| `Cena razem` | 5 958 zł — za obie osoby |
| `Cena regularna` | 3 579 zł — cena przed obniżką |
| `Zaliczka` | 590 zł |

Dla samego wykrywania zmian nie ma to większego znaczenia — te kwoty zmieniają
się razem. Wpływa tylko na to, która liczba jest w powiadomieniu główna.

## Gdyby cena zaczęła być odczytywana źle

```powershell
py monitor.py --discover
```

Wypisze wszystkie kwoty znalezione na stronie wraz ze źródłem, z którego
pochodzą, oraz gotowe wartości do `PRICE_LABEL`. Pierwsza pozycja na liście to
ta, którą monitor faktycznie śledzi. Zapisze też stronę do
`state/debug_page.html`, gdybyś chciał poszukać w niej własnego selektora CSS
(wtedy `PRICE_SELECTOR`).

---

## Pliki

| Plik | Rola |
|------|------|
| `monitor.py` | cały monitor |
| `.env` | Twoja konfiguracja lokalna (nie idzie do repo) |
| `.env.example` | wzór konfiguracji z opisem pól |
| `.github/workflows/monitor.yml` | harmonogram co 10 minut w GitHub Actions |
| `state/price.json` | ostatnia znana cena (bez adresu oferty) |
| `state/history.csv` | historia zmian ceny: czas, kwota, waluta |
