# Kopyya - IBKR gateway setup for a subscriber's own Windows PC.
#
# Does, once: installs Java if missing (via winget), downloads IBKR's Client
# Portal Gateway, allows the Kopyya server's Tailscale address in the gateway's
# config, and starts the gateway in the background. Re-running it is safe.
#
# Run from PowerShell (right-click Start > "Windows PowerShell" or "Terminal"):
#   powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\ibkr-gateway-setup.ps1" 100.118.121.100
#
# Afterwards: open https://localhost:5000 in a browser, sign in with your IBKR
# username, close the tab. Do that again every trading day - IBKR ends the
# session at midnight New York time.
param(
    [Parameter(Mandatory = $true)][string]$ServerIp
)
$ErrorActionPreference = "Stop"

if ($ServerIp -notmatch '^100\.\d+\.\d+\.\d+$') {
    Write-Host "Usage: ibkr-gateway-setup.ps1 <Kopyya server Tailscale address, e.g. 100.118.121.100>"
    exit 1
}

$GwDir  = Join-Path $env:USERPROFILE "clientportal.gw"
$ZipUrl = "https://download2.interactivebrokers.com/portal/clientportal.gw.zip"

function Say($msg) { Write-Host ""; Write-Host "==> $msg" }

# -- Java -----------------------------------------------------------------
if (-not (Get-Command java -ErrorAction SilentlyContinue)) {
    Say "Installing Java (Temurin 21) with winget - approve the prompt if one appears"
    winget install --id EclipseAdoptium.Temurin.21.JRE -e --accept-package-agreements --accept-source-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    if (-not (Get-Command java -ErrorAction SilentlyContinue)) {
        Write-Host "Java was installed but is not on PATH yet. Close this window, open a new PowerShell, and run the script again."
        exit 1
    }
}
# java prints its version on stderr; under $ErrorActionPreference=Stop a direct
# "2>&1" turns that into a terminating error, so read it through cmd instead.
$javaVersion = (cmd /c "java -version 2>&1" | Select-Object -First 1)
Say "Java: $javaVersion"

# -- Gateway download -----------------------------------------------------
if (-not (Test-Path (Join-Path $GwDir "bin\run.bat"))) {
    Say "Downloading IBKR Client Portal Gateway"
    New-Item -ItemType Directory -Force -Path $GwDir | Out-Null
    $zip = Join-Path $GwDir "gw.zip"
    Invoke-WebRequest -Uri $ZipUrl -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $GwDir -Force
    Remove-Item $zip -Force
} else {
    Say "Gateway already present in $GwDir"
}

# -- Allow-list: only the Kopyya server (plus this PC) may call the API -----
$conf = Join-Path $GwDir "root\conf.yaml"
if (-not (Test-Path "$conf.orig")) { Copy-Item $conf "$conf.orig" }
$text = Get-Content $conf -Raw
$newBlock = "    ips:`n      allow:`n        - 127.0.0.1`n        - $ServerIp`n      deny: []`n"
$pattern = '(?m)^    ips:\r?\n(?:(?:      |        ).*\r?\n)+'
if ($text -notmatch $pattern) {
    Write-Host "Could not find the ips: block in conf.yaml - edit it by hand so 'allow:' lists 127.0.0.1 and $ServerIp"
    exit 1
}
$text = [regex]::Replace($text, $pattern, $newBlock, 1)
Set-Content -Path $conf -Value $text -NoNewline
Say "allow-list set: 127.0.0.1 and $ServerIp"

# -- Start (or restart) the gateway in the background ----------------------
$running = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*clientportal.gw*" }
if ($running) {
    Say "Stopping the running gateway so the new allow-list applies"
    $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}
Say "Starting the gateway"
Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "bin\run.bat root\conf.yaml" -WorkingDirectory $GwDir -WindowStyle Minimized
$up = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        [System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
        Invoke-WebRequest -Uri "https://localhost:5000/sso/Login" -UseBasicParsing -TimeoutSec 3 | Out-Null
        $up = $true; break
    } catch { }
}
if (-not $up) { Write-Host "The gateway did not answer on port 5000 yet; give it a few more seconds, then open https://localhost:5000" }

$tsExe = "C:\Program Files\Tailscale\tailscale.exe"
$tsIp = ""
if (Test-Path $tsExe) { try { $tsIp = (cmd /c "`"$tsExe`" ip -4 2>nul" | Select-Object -First 1) } catch { } }
if (-not $tsIp) { $tsIp = "<your Tailscale 100.x address, shown in the Tailscale app>" }

Say "Done. Next steps:"
Write-Host "  1. Open https://localhost:5000 in your browser, click Advanced > Proceed on the"
Write-Host "     certificate warning, sign in with your IBKR username, and close the tab when"
Write-Host "     it says 'Client login succeeds'. Repeat this every trading day."
Write-Host "  2. In Kopyya > Broker > IBKR, use this gateway address:"
Write-Host "       https://${tsIp}:5000"
Write-Host ""
Write-Host "  The gateway runs in a minimized window titled 'cmd'. Closing that window stops it."
Write-Host "  Start it again any time by running this script again."
