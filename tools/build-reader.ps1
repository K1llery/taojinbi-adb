$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
$r8Version = '9.5.26'
$r8Path = Join-Path $PSScriptRoot "r8-$r8Version.jar"
if (-not (Test-Path -LiteralPath $r8Path)) {
    Invoke-WebRequest -Uri "https://dl.google.com/dl/android/maven2/com/android/tools/r8/$r8Version/r8-$r8Version.jar" -OutFile $r8Path
}
New-Item -ItemType Directory -Path '.\build' -Force | Out-Null
javac --release 8 -Xlint:-options -encoding UTF-8 -d '.\build' '.\UiDump.java'
if ($LASTEXITCODE -ne 0) { throw 'javac failed' }
java -cp $r8Path com.android.tools.r8.D8 --min-api 26 --output '.\build' '.\build\UiDump.class'
if ($LASTEXITCODE -ne 0) { throw 'D8 failed' }
jar --create --file '.\ui-dump.jar' -C '.\build' classes.dex
if ($LASTEXITCODE -ne 0) { throw 'jar failed' }
Get-FileHash -LiteralPath '.\ui-dump.jar' -Algorithm SHA256
} finally {
    Pop-Location
}
