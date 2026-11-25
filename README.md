# Sortowanie

Desktopowa aplikacja dla Windows do porządkowania plików w wybranym folderze.

Funkcje:

- **Trzy sposoby sortowania**
  - według typu: `Obrazy`, `Dokumenty`, `Wideo`, `Muzyka`, `Archiwa`, `Programy`, `Kod`, `Inne`
  - według daty modyfikacji: `2024\03`, `2024\04`, …
  - według rozszerzenia: `PDF`, `JPG`, …
- **Podgląd** – przed przeniesieniem widać dokładnie, który plik trafi do którego folderu.
- **Bezpieczne nazwy** – pliki nigdy nie są nadpisywane; przy kolizji dostają sufiks `nazwa (1).jpg`.
- **Cofnij ostatnie sortowanie** – historia ostatniej operacji jest zapisywana w
  `%APPDATA%\Sortowanie\historia_sortowania.json`.
- Opcjonalnie uwzględnia podfoldery.
- Brak zewnętrznych zależności – działa na samej bibliotece standardowej Pythona (Tkinter).

## Wymagania

- Windows 10 lub 11
- [Python 3.10+](https://www.python.org/downloads/windows/) – przy instalacji zaznacz
  **„Add python.exe to PATH”** (instalator z python.org zawiera Tkinter domyślnie).

Sprawdź w wierszu poleceń (`cmd`) lub PowerShell:

```bat
py --version
```

## Uruchomienie ze źródeł

```bat
git clone https://github.com/skipperpvp-sudo/Sortowanie.git
cd Sortowanie
py main.py
```

Alternatywnie: `py -m sortowanie`.

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
│   ├── __init__.py       # nazwa i wersja aplikacji
│   ├── __main__.py       # umożliwia: py -m sortowanie
│   ├── core.py           # logika sortowania, historia, cofanie
│   └── gui.py            # okno aplikacji (Tkinter)
├── tests/                # testy jednostkowe (unittest)
├── sortowanie.spec       # konfiguracja budowania .exe
├── build.bat             # skrypt budujący .exe
├── requirements-dev.txt  # zależności do budowania
└── .env.example          # wzór pliku konfiguracyjnego (bez sekretów)
```

## Prywatność i bezpieczeństwo repozytorium

Plik `.gitignore` wyklucza z repozytorium:

- klucze API i sekrety (`.env`, `*.key`, `*.pem`, `secrets.*`, `credentials*.json`, …),
- prywatne materiały i dane robocze (`prywatne/`, `private/`, `dane/`, `data/`, `wyniki/`, `output/`, `logs/`),
- cache Pythona i narzędzi (`__pycache__/`, `.pytest_cache/`, …),
- środowiska wirtualne (`.venv/`),
- pliki wynikowe budowania (`build/`, `dist/`, `*.exe`).

Jeśli aplikacja będzie kiedyś potrzebować klucza API, skopiuj `.env.example` jako `.env`
i wpisz klucz tylko lokalnie – plik `.env` nie trafi do repozytorium.
