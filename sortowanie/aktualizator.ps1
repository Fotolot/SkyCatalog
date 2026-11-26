# SkyCatalog - proces aktualizujacy wersje przenosna (Windows PowerShell 5.1+).
# Uruchamiany przez program tuz przed zamknieciem, jako niezalezny proces.
# Parametry: plik JSON (sciezki, PID-y, wersje) - nic nie jest wklejane w tresc skryptu.
# Tryby: "main" (zwykly uzytkownik), "install" / "restore" (gdy potrzebne uprawnienia - przez UAC).
# Tylko ASCII: tekst skryptu jest wczytywany niezaleznie od strony kodowej systemu.
param([string]$ParamsPath, [string]$Mode = "main")

$ErrorActionPreference = "Stop"
$p = Get-Content -LiteralPath $ParamsPath -Raw -Encoding UTF8 | ConvertFrom-Json
$app = $p.app_dir.TrimEnd('\')
$new = $p.new_dir.TrimEnd('\')
$exe = Join-Path $app $p.exe_name
$backup = Join-Path $app "_backup"
# Ten sam folder w postaci pelnej i krotkiej (8.3) - sciezka procesu moze byc w innej postaci
$appVariants = @(@($app) + @($p.app_dir_variants | Where-Object { $_ }) | ForEach-Object { $_.TrimEnd('\') } | Select-Object -Unique)

function Under-App([string]$path) {
    if (-not $path) { return $false }
    foreach ($v in $appVariants) { if ($path.StartsWith($v + '\', [StringComparison]::OrdinalIgnoreCase)) { return $true } }
    return $false
}

function Log([string]$msg) {
    $line = "[{0}] [aktualizator {1} pid {2}] {3}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Mode, $PID, $msg
    for ($i = 0; $i -lt 5; $i++) {
        try { [IO.File]::AppendAllText($p.log_path, $line + "`r`n", [Text.Encoding]::UTF8); return }
        catch { Start-Sleep -Milliseconds 200 }
    }
}

function Save-Result([hashtable]$r) {
    $r["czas"] = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    $r["wersja_obecna"] = $p.current_version
    $r["wersja_docelowa"] = $p.target_version
    $r["log"] = $p.log_path
    $json = $r | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText($p.result_path, $json, (New-Object Text.UTF8Encoding $false))
}

function Is-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Run-Robocopy([string]$from, [string]$to, [string[]]$extra) {
    # /MT:8 - kopiowanie wielowatkowe; /NFL /NDL - w dzienniku tylko bledy i podsumowanie
    $rc = @($from, $to, "/E", "/IS", "/IT", "/R:3", "/W:2", "/MT:8", "/NP", "/NFL", "/NDL", "/NJH") + $extra
    $out = & robocopy.exe @rc 2>&1
    $code = $LASTEXITCODE
    foreach ($l in $out) { if ("$l".Trim()) { Log ("  robocopy: " + "$l".Trim()) } }
    Log ("robocopy {0} -> {1}: kod {2} (0-3 OK, 4+ blad)" -f $from, $to, $code)
    return $code
}

function Exclusions {
    $xd = @("/XD") + @($p.protected_dirs) + @($backup)
    $xf = @("/XF") + @($p.protected_files)
    return $xd + $xf
}

function Is-Protected($item) {
    # Dane uzytkownika obok programu - nigdy nie sa przenoszone, kopiowane ani usuwane
    if ($item.PSIsContainer) { return @($p.protected_dirs) -contains $item.Name }
    foreach ($pattern in $p.protected_files) { if ($item.Name -like $pattern) { return $true } }
    return $false
}

function Product-Version([string]$path) {
    try { return ((Get-Item -LiteralPath $path).VersionInfo.ProductVersion + "").Trim() } catch { return "" }
}

function Program-Items {
    return @(Get-ChildItem -LiteralPath $app -Force | Where-Object { $_.Name -ine "_backup" -and -not (Is-Protected $_) })
}

function Restore-Backup {
    # Usuwa pliki nowej wersji i przenosi poprzednia wersje z _backup z powrotem (ten sam dysk - szybko)
    if (-not (Test-Path -LiteralPath (Join-Path $backup $p.exe_name))) {
        Log "BLAD: brak kopii zapasowej do przywrocenia"
        return 99
    }
    Log "Przywracanie poprzedniej wersji z _backup"
    $errors = 0
    foreach ($item in Program-Items) {
        try { Remove-Item -LiteralPath $item.FullName -Recurse -Force }
        catch { $errors++; Log ("  nie usunieto " + $item.FullName + ": " + $_.Exception.Message) }
    }
    foreach ($item in @(Get-ChildItem -LiteralPath $backup -Force | Where-Object { $_.Name -ne "PRZYWROC_POPRZEDNIA_WERSJE.cmd" })) {
        $dest = Join-Path $app $item.Name
        try {
            if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Recurse -Force }
            Move-Item -LiteralPath $item.FullName -Destination $dest
        } catch { $errors++; Log ("  nie przywrocono " + $item.Name + ": " + $_.Exception.Message) }
    }
    if ($errors -gt 0) {
        # awaryjnie: kopia plikow, ktorych nie udalo sie przeniesc
        $code = Run-Robocopy $backup $app @("/XF", "PRZYWROC_POPRZEDNIA_WERSJE.cmd")
        if ($code -ge 4) { return $code }
    }
    Log "Poprzednia wersja przywrocona"
    return 0
}

# --- Instalacja (w biezacym procesie albo w procesie z uprawnieniami administratora) ---
function Install {
    Log ("Instalacja: {0} -> {1}" -f $new, $app)
    if (Test-Path -LiteralPath $backup) {
        try { Remove-Item -LiteralPath $backup -Recurse -Force }
        catch { Log ("BLAD: nie mozna usunac starej kopii zapasowej: " + $_.Exception.Message); return @{ status = "blad_kopii"; kod = 98 } }
    }
    New-Item -ItemType Directory -Path $backup | Out-Null
    # 1. Kopia zapasowa przez przeniesienie (zmiana nazwy na tym samym dysku - bez ponownego zapisu plikow)
    $moved = @()
    foreach ($item in Program-Items) {
        try {
            Move-Item -LiteralPath $item.FullName -Destination (Join-Path $backup $item.Name)
            $moved += $item.Name
        } catch {
            Log ("BLAD kopii zapasowej - nie mozna przeniesc " + $item.FullName + ": " + $_.Exception.Message)
            foreach ($name in $moved) {
                try { Move-Item -LiteralPath (Join-Path $backup $name) -Destination (Join-Path $app $name) }
                catch { Log ("  nie cofnieto " + $name + ": " + $_.Exception.Message) }
            }
            try { Remove-Item -LiteralPath $backup -Recurse -Force } catch { }
            return @{ status = "pliki_zablokowane"; kod = 5; blad = $_.Exception.Message }
        }
    }
    Log ("Kopia zapasowa w _backup (przeniesiono: {0})" -f ($moved -join ", "))
    $restoreCmd = "@echo off`r`nrem Przywraca poprzednia wersje SkyCatalog z tego folderu.`r`nrobocopy `"%~dp0.`" `"%~dp0..`" /E /IS /IT /R:3 /W:2 /XD `"%~dp0.`"`r`npause`r`n"
    try { [IO.File]::WriteAllText((Join-Path $backup "PRZYWROC_POPRZEDNIA_WERSJE.cmd"), $restoreCmd, [Text.Encoding]::ASCII) } catch { }
    # 2. Nowa wersja do pustego miejsca (bez mieszania starych i nowych bibliotek)
    $code = Run-Robocopy $new $app (Exclusions)
    if ($code -ge 4) {
        Log "BLAD kopiowania nowej wersji (brak miejsca, uprawnien lub plik zablokowany)"
        $r = Restore-Backup
        return @{ status = "blad_instalacji"; kod = $code; przywrocono = ($r -lt 4) }
    }
    $ver = Product-Version $exe
    Log ("Wersja w metadanych zainstalowanego pliku: '{0}' (oczekiwana '{1}')" -f $ver, $p.target_version)
    if ($ver -ne $p.target_version) {
        Log "BLAD: zainstalowany plik nie raportuje nowej wersji - przywracanie"
        $r = Restore-Backup
        return @{ status = "zla_wersja"; kod = 0; wersja_pliku = $ver; przywrocono = ($r -lt 4) }
    }
    return @{ status = "zainstalowano"; kod = 0 }
}

if ($Mode -eq "install" -or $Mode -eq "restore") {
    # Proces z uprawnieniami administratora - tylko operacje na plikach; wynik do pliku etapu.
    try {
        if ($Mode -eq "install") { $r = Install } else { $code = Restore-Backup; $r = @{ status = "przywrocono"; kod = $code } }
    } catch { Log ("BLAD: " + $_.Exception.Message); $r = @{ status = "blad_instalacji"; kod = 97; blad = $_.Exception.Message } }
    [IO.File]::WriteAllText($p.stage_path, ($r | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))
    exit 0
}

function Elevated-Stage([string]$stage) {
    # Uruchamia etap z uprawnieniami administratora (okno UAC). Zwraca wynik albo $null przy anulowaniu.
    if (Test-Path -LiteralPath $p.stage_path) { Remove-Item -LiteralPath $p.stage_path -Force }
    $cmd = "& ([scriptblock]::Create([IO.File]::ReadAllText('{0}'))) '{1}' '{2}'" -f `
        ($p.script_path -replace "'", "''"), ($ParamsPath -replace "'", "''"), $stage
    try {
        $proc = Start-Process -FilePath "powershell.exe" -Verb RunAs -Wait -PassThru -WindowStyle Hidden `
            -ArgumentList @("-NoProfile", "-NonInteractive", "-Command", $cmd)
        Log ("Proces z uprawnieniami administratora zakonczony, kod {0}" -f $proc.ExitCode)
    } catch {
        $err = $_.Exception
        $native = 0
        if ($err -is [ComponentModel.Win32Exception]) { $native = $err.NativeErrorCode }
        elseif ($err.InnerException -is [ComponentModel.Win32Exception]) { $native = $err.InnerException.NativeErrorCode }
        if ($native -eq 1223) { Log "Uzytkownik anulowal zgode UAC"; return @{ status = "uac_anulowano"; kod = 1223 } }
        Log ("BLAD uruchomienia z uprawnieniami administratora: " + $err.Message)
        return @{ status = "brak_uprawnien"; kod = $native; blad = $err.Message }
    }
    if (-not (Test-Path -LiteralPath $p.stage_path)) { return @{ status = "blad_instalacji"; kod = 96; blad = "brak wyniku etapu" } }
    $s = Get-Content -LiteralPath $p.stage_path -Raw -Encoding UTF8 | ConvertFrom-Json
    $h = @{}; $s.PSObject.Properties | ForEach-Object { $h[$_.Name] = $_.Value }
    return $h
}

function Running-From-App {
    return @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        try { Under-App $_.Path } catch { $false } })
}

function Can-Write([string]$dir) {
    $probe = Join-Path $dir (".skycatalog_zapis_" + [Guid]::NewGuid().ToString("N"))
    try { [IO.File]::WriteAllText($probe, "x"); Remove-Item -LiteralPath $probe -Force; return $true } catch { return $false }
}

function Start-AsUser([string]$path, [string[]]$arguments) {
    # Zwykly kontekst uzytkownika: gdy pomocnik dziala z uprawnieniami administratora,
    # program uruchamia Eksplorator (bez dziedziczenia uprawnien).
    $lua = 0
    try { $lua = (Get-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" -Name EnableLUA -ErrorAction Stop).EnableLUA } catch { }
    $shell = @(Get-Process -Name explorer -ErrorAction SilentlyContinue).Count -gt 0
    if ((Is-Elevated) -and $lua -eq 1 -and $shell -and -not $p.launch_direct) {
        Log "Pomocnik ma uprawnienia administratora - uruchomienie przez explorer.exe (kontekst uzytkownika)"
        Start-Process -FilePath "explorer.exe" -ArgumentList ('"' + $path + '"') | Out-Null
        return $null
    }
    if ($arguments.Count -gt 0) { return Start-Process -FilePath $path -WorkingDirectory $app -ArgumentList $arguments -PassThru }
    return Start-Process -FilePath $path -WorkingDirectory $app -PassThru
}

function Start-Old([string]$why) {
    Log ("Uruchamianie poprzedniej wersji (" + $why + ")")
    try { $proc = Start-AsUser $exe @($p.failed_flag); if ($proc) { Log ("PID poprzedniej wersji: " + $proc.Id) } }
    catch { Log ("BLAD uruchomienia poprzedniej wersji: " + $_.Exception.Message) }
}

function Check-Shortcuts {
    $stale = @(); $ok = @()
    $folders = @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("CommonDesktopDirectory"),
                 [Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("CommonPrograms"))
    try { $shell = New-Object -ComObject WScript.Shell } catch { Log "Nie mozna sprawdzic skrotow (WScript.Shell)"; return @{ nieaktualne = @(); poprawne = @() } }
    foreach ($f in $folders) {
        if (-not $f -or -not (Test-Path -LiteralPath $f)) { continue }
        foreach ($lnk in Get-ChildItem -LiteralPath $f -Filter *.lnk -Recurse -ErrorAction SilentlyContinue) {
            try { $t = $shell.CreateShortcut($lnk.FullName).TargetPath } catch { continue }
            if ($t -and [IO.Path]::GetFileName($t) -ieq $p.exe_name) {
                $dir = [IO.Path]::GetDirectoryName($t)
                if (@($appVariants | Where-Object { $dir -ieq $_ }).Count -gt 0) { $ok += $lnk.FullName }
                else { $stale += ("{0} -> {1}" -f $lnk.FullName, $t); Log ("UWAGA: skrot wskazuje inna kopie programu: {0} -> {1}" -f $lnk.FullName, $t) }
            }
        }
    }
    Log ("Skroty do tej kopii: {0}, do innych kopii: {1}" -f $ok.Count, $stale.Count)
    return @{ nieaktualne = $stale; poprawne = $ok }
}

# --- Tryb glowny ---
try {
    Log ("=== Start aktualizacji {0} -> {1}" -f $p.current_version, $p.target_version)
    Log ("Folder programu: {0}" -f $app)
    Log ("Nowa wersja (rozpakowana): {0}" -f $new)
    Log ("Pomocnik: {0} (uprawnienia administratora: {1})" -f $p.script_path, (Is-Elevated))
    Log ("Procesy programu do zamkniecia: {0}" -f (($p.pids | ForEach-Object { "$_" }) -join ", "))

    # 1. Czekanie na zamkniecie programu i jego procesow pomocniczych (np. QtWebEngineProcess)
    $deadline = (Get-Date).AddSeconds([int]$p.wait_seconds)
    foreach ($id in $p.pids) {
        $proc = Get-Process -Id $id -ErrorAction SilentlyContinue
        if ($proc) {
            $left = [Math]::Max(1, [int]($deadline - (Get-Date)).TotalSeconds)
            try { Wait-Process -Id $id -Timeout $left -ErrorAction Stop } catch { }
        }
    }
    while ((Get-Date) -lt $deadline) {
        if ((Running-From-App).Count -eq 0) { break }
        Start-Sleep -Milliseconds 500
    }
    $still = Running-From-App
    if ($still.Count -gt 0) {
        $names = ($still | ForEach-Object { "{0} (pid {1})" -f $_.Path, $_.Id }) -join "; "
        Log ("BLAD: pliki programu sa nadal uzywane: " + $names)
        Save-Result @{ status = "pliki_zablokowane"; komunikat = "Pliki programu sa nadal uzywane: $names" }
        exit 3
    }
    Log "Program zamkniety - brak procesow z folderu programu"

    # 2. Instalacja (z UAC, gdy brak prawa zapisu)
    if (Can-Write $app) { $r = Install }
    else {
        Log "Brak prawa zapisu w folderze programu - prosba o zgode UAC"
        $r = Elevated-Stage "install"
    }
    Log ("Wynik instalacji: {0} (kod {1})" -f $r.status, $r.kod)
    if ($r.status -ne "zainstalowano") {
        Start-Old $r.status
        $r["komunikat"] = "Aktualizacja nie zostala zainstalowana: " + $r.status
        Save-Result $r
        exit 4
    }

    # 3. Uruchomienie nowej wersji i potwierdzenie, ze raportuje nowa wersje
    if (Test-Path -LiteralPath $p.confirm_path) { Remove-Item -LiteralPath $p.confirm_path -Force }
    $started = $null
    Log ("Uruchamianie nowej wersji: {0}" -f $exe)
    try { $started = Start-AsUser $exe @() } catch { Log ("BLAD uruchomienia nowej wersji: " + $_.Exception.Message) }
    if ($started) { Log ("Uruchomiono nowa wersje: {0} (pid {1})" -f $exe, $started.Id) }
    $confirmed = $null
    $exitSeen = $null
    $until = (Get-Date).AddSeconds([int]$p.confirm_seconds)
    while ((Get-Date) -lt $until) {
        if (Test-Path -LiteralPath $p.confirm_path) {
            try { $confirmed = Get-Content -LiteralPath $p.confirm_path -Raw -Encoding UTF8 | ConvertFrom-Json; break } catch { }
        }
        if ($started -and $started.HasExited) {
            # proces juz sie zakonczyl - chwila na zapis potwierdzenia, potem koniec czekania
            if (-not $exitSeen) { $exitSeen = Get-Date }
            elseif (((Get-Date) - $exitSeen).TotalSeconds -gt 3) { break }
        }
        Start-Sleep -Milliseconds 500
    }
    if ($confirmed -and $confirmed.version -eq $p.target_version) {
        Log ("Potwierdzenie: uruchomiony program raportuje wersje {0} (pid {1}, {2})" -f $confirmed.version, $confirmed.pid, $confirmed.exe)
        $sc = Check-Shortcuts
        Save-Result @{ status = "ok"; komunikat = "Zaktualizowano"; pid_nowej_wersji = $confirmed.pid;
                       skroty_nieaktualne = $sc.nieaktualne; skroty_poprawne = $sc.poprawne }
        Log "=== Aktualizacja zakonczona powodzeniem"
        exit 0
    }
    $why = "brak potwierdzenia uruchomienia"
    if ($confirmed) { $why = "uruchomiony program raportuje wersje " + $confirmed.version }
    elseif ($started -and $started.HasExited) { $why = "nowa wersja zakonczyla sie z kodem " + $started.ExitCode }
    Log ("BLAD: " + $why)
    $running = Running-From-App
    if ($running.Count -gt 0) {
        # Program z folderu dziala, ale nie potwierdzil wersji - nie podmieniamy plikow w trakcie pracy
        # i nie uruchamiamy drugiej kopii.
        Log ("Program dziala ({0}) - bez przywracania poprzedniej wersji; kopia zostaje w _backup" -f `
            (($running | ForEach-Object { "pid " + $_.Id }) -join ", "))
        Save-Result @{ status = "brak_potwierdzenia"; komunikat = $why }
        exit 5
    }
    if (Can-Write $app) { $code = Restore-Backup } else { $s = Elevated-Stage "restore"; $code = $s.kod }
    Start-Old $why
    Save-Result @{ status = "nowa_wersja_nie_dziala"; komunikat = $why; przywrocono = ($code -lt 4) }
    exit 6
} catch {
    Log ("BLAD nieoczekiwany: " + $_.Exception.Message + " | " + $_.ScriptStackTrace)
    try { Save-Result @{ status = "blad"; komunikat = $_.Exception.Message } } catch { }
    exit 9
}
