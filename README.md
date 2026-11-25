# Sortowanie

Desktopowa aplikacja dla Windows do archiwizacji materiałów z lotów (dron, kamera):
wczytujesz kartę pamięci, **ręcznie** przypisujesz kategorię, a aplikacja przenosi
filmy i zdjęcia do uporządkowanej struktury folderów.

## Kategorie lotów

Kategorię zawsze wybiera użytkownik – aplikacja jej nie zgaduje. Pola wyboru wyglądają
jak checkboxy, ale dla danej grupy materiałów można zaznaczyć tylko jedną kategorię.
Kategorię można przypisać **wszystkim materiałom z importu** albo **zaznaczonym grupom**
(Ctrl/Shift + klik na liście), np. część karty to Event, a reszta to luźne loty.

| Kategoria | Struktura | Dane |
|---|---|---|
| **Hobby / Luźne loty** | `Hobby/[Miejscowość]/[Obiekt]/WIDEO` i `…/Zdjęcia` | miejscowość i pobliski obiekt rozpoznawane z GPS każdego materiału |
| **Eventy** | `Eventy/[Miejscowość]/[RRRR-MM-DD] [Nazwa]/WIDEO` i `…/Zdjęcia` | nazwa i data wydarzenia (wymagane), miejscowość z GPS do poprawienia |
| **Zlecenia** | `Zlecenia/[Lokalizacja]/[RRRR-MM-DD] [Nazwa]/WIDEO` i `…/Zdjęcia` | lokalizacja, data realizacji, nazwa/opis (wymagane, wpisuje użytkownik) |

Przykłady:

```
Hobby/Rzeszów/Pomnik Czynu Rewolucyjnego/WIDEO/
Hobby/Rzeszów/Zdjęcia/                                   ← brak obiektu w promieniu 250 m
Eventy/Rzeszów/2026-11-11 Bieg Niepodległości/WIDEO/
Zlecenia/Łańcut/2026-11-25 Film promocyjny nieruchomości/Zdjęcia/
```

Zasady:

- **Hobby** – proponowany obiekt w promieniu (domyślnie 250 m) trzeba zatwierdzić; bez
  obiektu w promieniu lub po wyborze „bez obiektu” poziom obiektu jest pomijany. Materiały z różnych miejsc trafiają do różnych folderów.
  Gdy brak GPS lub internetu, można wpisać miejscowość (i opcjonalnie obiekt) ręcznie.
- **Eventy** i **Zlecenia** – wszystkie przypisane materiały trafiają do jednego folderu
  wydarzenia/zlecenia; bez podfolderów obiektów i bez dzielenia według GPS.
- **Data** jest proponowana na podstawie daty wykonania materiałów (EXIF, SRT, nazwa
  pliku DJI, metadane MP4) – zawsze można ją zmienić. Data modyfikacji pliku **nie** jest
  używana jako data nagrania.
- **Kolejne karty** – w Eventach i Zleceniach lista „Dołącz do istniejącego” pokazuje
  foldery już zapisane w archiwum, więc materiały z kilku kart trafiają do tego samego folderu.
- **Powiązane pliki** – film, jego `.SRT`, `.LRF` i inne pliki o tej samej nazwie tworzą
  jedną grupę i zawsze otrzymują tę samą kategorię i to samo miejsce docelowe. Przy
  kolizji nazw cała grupa dostaje ten sam sufiks, np. `DJI_0001 (1).MP4` + `DJI_0001 (1).SRT`.
- **Podgląd na żywo** – docelowa struktura folderów aktualizuje się podczas wypełniania
  formularza. Przycisk „Przenieś materiały” jest nieaktywny, dopóki każda grupa nie ma
  kategorii, a wymagane pola nie są uzupełnione.
- **Bezpieczeństwo** – istniejące pliki nigdy nie są nadpisywane; ostatni import można
  cofnąć przyciskiem „Cofnij ostatni import”.

## Lokalizacja: OpenStreetMap przez Overpass API

Miejscowości i obiekty są rozpoznawane wyłącznie na podstawie danych
[OpenStreetMap](https://www.openstreetmap.org/) pobieranych przez
[Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API) – bez płatnych usług.
Do serwera wysyłane są **wyłącznie współrzędne / prostokąty obszaru** – nigdy zdjęcia,
filmy ani nazwy plików.

**Miejscowość** – zapytanie `is_in` zwraca obszary OSM obejmujące punkt; aplikacja analizuje
ich tagi:

- obszar z `place=city|town|village|hamlet` → miejscowość,
- granica administracyjna bez `place` → miejscowość tylko, gdy potwierdza ją węzeł `place`
  o tej samej nazwie lub `border_type`,
- gminy, powiaty i województwa nie są traktowane jako miejscowość,
- dzielnice i osiedla (`place=suburb|quarter|neighbourhood`, `admin_level` ≥ 9) nie tworzą folderów,
- kilka różnych miejscowości lub brak obszaru → **użytkownik wybiera** z listy propozycji.

**Obiekty** (tylko Hobby) – nazwane pomniki, zabytki, zamki, stadiony, atrakcje, muzea,
kościoły, wieże, mosty, parki itp. w promieniu 250 m. Pobierana jest ich geometria, więc
odległość liczy się do **granic budynków i obszarów** (punkt w parku = 0 m), a nie do środka.
Najbardziej charakterystyczny obiekt jest **tylko propozycją do zatwierdzenia** – GPS drona
nie dowodzi, że obiekt jest w kadrze. Można go zatwierdzić, wybrać inny lub wybrać „bez obiektu”.

**Oszczędne zapytania** – zgodnie z [zasadami publicznej instancji](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html)
(orientacyjnie do 10 000 zapytań i 1 GB dziennie):

- zapytania wykonywane są w tle, pojedynczo, z kolejką i odstępem,
- wielokąty miejscowości są zapisywane – kolejne materiały w ich granicach nie wymagają zapytania,
- obiekty pobierane są kafelkami (~1,1 × 0,7 km) wspólnie dla wszystkich materiałów importu;
  kafelek pobrany raz obsługuje każdy plik, którego promień obejmuje,
- zapytania deklarują małe `[timeout]` i `[maxsize]`,
- HTTP 429: oczekiwanie na wolny slot według `/api/status`, potem ponowienie,
- HTTP 504 (i 502/503): ponowienie po 15, 30, 60, 120 s.

**Własny serwer** – endpoint (domyślnie `https://overpass-api.de/api/interpreter`) zmienisz
bez zmiany kodu: w oknie **Ustawienia…** albo zmienną środowiskową:

```bat
set SORTOWANIE_OVERPASS_URL=https://moj-serwer.example/api/interpreter
py main.py
```

**Cache i decyzje** (w `%APPDATA%\Sortowanie\`):

- `cache_osm.json` – pobrane dane OSM (odświeżane po 180 dniach, gdy jest internet),
- `decyzje_lokalizacji.json` – wybory miejscowości i zatwierdzone/odrzucone obiekty,
  podpowiadane przy kolejnych importach z tego miejsca,
- `ustawienia.json`, `historia_sortowania.json`.

Bez internetu aplikacja korzysta z zapisanych danych i decyzji, a gdy ich brak – pozwala
wpisać miejscowość (i obiekt) ręcznie.

**Źródło danych:** © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright),
licencja ODbL. Oznaczenie jest widoczne w oknie aplikacji i w ustawieniach.

### Diagnostyka na prawdziwych danych

```bat
py narzedzia\sprawdz_lokalizacje.py 50.027082,22.003366 50.040451,21.999130
```

Skrypt wypisuje wysłane zapytania, skrót odpowiedzi serwera, klasyfikację obszarów,
rozpoznaną miejscowość, kandydatów na obiekt (z odległością do granicy) i proponowane foldery.
Opcja `--surowe` pokazuje pełny JSON, `--endpoint` pozwala sprawdzić własny serwer.

## Obsługiwane pliki

- Wideo: MP4, MOV, AVI, MKV, M4V, MTS, INSV…
- Zdjęcia: JPG, DNG, HEIC, PNG, TIFF, RAW (ARW, CR2, CR3, NEF)…
- Pliki towarzyszące: SRT, LRF, LRV, THM, XMP, AAE…

GPS jest odczytywany z napisów SRT dronów DJI, z EXIF zdjęć oraz z metadanych MP4.

## Wymagania

- Windows 10 lub 11
- [Python 3.10+](https://www.python.org/downloads/windows/) – przy instalacji zaznacz
  **„Add python.exe to PATH”** (instalator z python.org zawiera Tkinter domyślnie).

Aplikacja nie ma zewnętrznych zależności – używa wyłącznie biblioteki standardowej Pythona.

## Uruchomienie ze źródeł

```bat
git clone https://github.com/skipperpvp-sudo/Sortowanie.git
cd Sortowanie
py main.py
```

Alternatywnie: `py -m sortowanie`.

### Jak używać

1. **Import** – wybierz kartę pamięci lub folder z materiałami i kliknij „Wczytaj”.
2. **Archiwum** – wybierz folder docelowy (np. `D:\Archiwum dron`).
3. Wybierz zakres: wszystkie materiały albo zaznaczone grupy.
4. Zaznacz kategorię i uzupełnij pola formularza – obserwuj podgląd struktury.
5. Kliknij **„Przenieś materiały”**.

## Budowanie pliku .exe

Najprościej – uruchom skrypt z folderu projektu:

```bat
build.bat
```

Skrypt automatycznie:

1. tworzy środowisko wirtualne `.venv`,
2. instaluje PyInstaller (`requirements-dev.txt`),
3. uruchamia testy,
4. buduje plik **`dist\Sortowanie.exe`** (jeden plik, bez okna konsoli).

Gotowy `Sortowanie.exe` można skopiować na dowolny komputer z Windows – nie wymaga
zainstalowanego Pythona.

### Budowanie ręczne

```bat
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
pyinstaller --clean --noconfirm sortowanie.spec
```

Własną ikonę dodasz, umieszczając plik `.ico` w `assets\` i odkomentowując linię
`icon=...` w `sortowanie.spec`.

> **Uwaga:** Windows Defender / SmartScreen może ostrzegać przed nowym, niepodpisanym
> plikiem `.exe`. To typowe dla programów zbudowanych PyInstallerem. Kliknij
> „Więcej informacji” → „Uruchom mimo to” lub podpisz plik certyfikatem.

## Testy

```bat
py -m unittest discover -s tests -t .
```

## Struktura projektu

```
Sortowanie/
├── main.py               # punkt startowy (również dla PyInstaller)
├── sortowanie/
│   ├── gui.py            # główne okno: import, kategorie, formularze, podgląd
│   ├── widgets.py        # kalendarz i okno ustawień
│   ├── planner.py        # kategorie, walidacja, struktura folderów
│   ├── media.py          # wczytywanie importu i grupowanie powiązanych plików
│   ├── metadata.py       # odczyt GPS i daty (EXIF, SRT, MP4)
│   ├── geo.py            # miejscowość i obiekty z GPS, cache danych i decyzji
│   ├── osm.py            # analiza danych OSM: geometria, klasyfikacja, zapytania
│   ├── overpass.py       # klient Overpass API (kolejka, 429/504, endpoint)
│   ├── config.py         # ustawienia w %APPDATA%\Sortowanie
│   └── core.py           # przenoszenie plików, historia, cofanie
├── narzedzia/           # sprawdz_lokalizacje.py – diagnostyka na prawdziwych danych
├── tests/                # testy jednostkowe (unittest)
├── sortowanie.spec       # konfiguracja budowania .exe
├── build.bat             # skrypt budujący .exe
├── requirements-dev.txt  # zależności do budowania
└── .env.example          # wzór pliku konfiguracyjnego (bez sekretów)
```

## Prywatność repozytorium

Plik `.gitignore` wyklucza z repozytorium:

- klucze API i sekrety (`.env`, `*.key`, `*.pem`, `secrets.*`, `credentials*.json`, …),
- prywatne materiały i dane robocze (`prywatne/`, `private/`, `dane/`, `data/`, `wyniki/`, `output/`, `logs/`),
- lokalne dane aplikacji (`ustawienia.json`, `cache_osm.json`, `decyzje_lokalizacji.json`, `historia_sortowania.json`),
- cache Pythona i narzędzi (`__pycache__/`, `.pytest_cache/`, …),
- środowiska wirtualne (`.venv/`),
- pliki wynikowe budowania (`build/`, `dist/`, `*.exe`).
