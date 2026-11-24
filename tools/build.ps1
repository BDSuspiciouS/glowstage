# Builds Glowstage on this PC (no paid build service needed):
#   dist\Glowstage\Glowstage.exe              the app
#   dist\Glowstage-<version>-windows.zip      what goes on the GitHub release
# Needs once: python -m pip install pyinstaller
# -Release also creates a draft GitHub release with the zip (publish it on github.com when ready).

param([switch]$Release)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$version = (python -c "from glowstage.paths import VERSION; print(VERSION)").Trim()
New-Item -ItemType Directory -Force build | Out-Null
python -c "from pathlib import Path; from glowstage.icon import icon_file; icon_file(Path('build'))"

# Windows version info shown in the exe's properties.
$v = ($version.Split('.') + @('0', '0', '0'))[0..3] -join ', '
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($v), prodvers=($v)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'Glowstage'),
    StringStruct('FileDescription', 'Glowstage - light shows for your PC and room lights'),
    StringStruct('FileVersion', '$version'),
    StringStruct('ProductName', 'Glowstage'),
    StringStruct('ProductVersion', '$version'),
    StringStruct('LegalCopyright', 'GPL-3.0')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"@ | Set-Content build\version.txt -Encoding utf8

python -m PyInstaller --noconfirm --clean glowstage.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$zip = "dist\Glowstage-$version-windows.zip"
Compress-Archive -Path dist\Glowstage -DestinationPath $zip -Force
"Built $zip ({0:N1} MB)" -f ((Get-Item $zip).Length / 1MB)

if ($Release) {
    gh release create "v$version" $zip --draft --title "Glowstage $version" --notes-file docs\RELEASE-NOTES.md
}
