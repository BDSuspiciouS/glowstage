# Renders the README's banner and screenshots into docs/images with headless Chrome.
# The screenshots show the app with sample lights: serve glowstage/ui first, for example
#   python -m http.server 8765 --directory glowstage/ui
param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$chrome = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
            "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
$out = Join-Path $root "docs\images"
$profile = Join-Path $env:TEMP "glowstage-shots"
function Shot($url, $file, $w, $h) {
    & $chrome --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=2 --user-data-dir="$profile" `
        --window-size="$w,$h" --virtual-time-budget=4000 --screenshot="$(Join-Path $out $file)" $url 2>$null | Out-Null
}
Shot ("file:///" + (Join-Path $PSScriptRoot "banner.html").Replace('\', '/')) "banner.png" 1280 400
Shot "http://localhost:$Port/?page=show" "light-show.png" 1360 860
Shot "http://localhost:$Port/?page=home&groups" "home.png" 1360 860
Shot "http://localhost:$Port/?page=presets" "presets.png" 1360 860
Get-ChildItem $out -Filter *.png | ForEach-Object { "{0} {1:N0} KB" -f $_.Name, ($_.Length / 1KB) }
