param(
  [string]$Root = "backend\models",
  [string[]]$Include = @("*.py")
)

Write-Host "🔎 Scanning $Root for empty try/except/finally blocks..." -ForegroundColor Cyan

$files = Get-ChildItem -Path $Root -Recurse -Include $Include -File -ErrorAction SilentlyContinue
$totalHits = 0

foreach ($f in $files) {
  $L = Get-Content -Path $f.FullName -Encoding UTF8
  for ($i = 0; $i -lt $L.Count; $i++) {
    $line = $L[$i]

    # Tafuta header za block: try / except / finally zilizo na ':' mwishoni
    if ($line -match '^(?<indent>\s*)(try|except(\s+.+?)?|finally)\s*:\s*(#.*)?$') {
      $indent = $Matches['indent']

      # tafuta mstari unaofuata usio tupu wala comment
      $j = $i + 1
      while ($j -lt $L.Count -and ($L[$j] -match '^\s*$' -or $L[$j] -match '^\s*#')) { $j++ }

      if ($j -ge $L.Count -or $L[$j] -match '^\s*(except|finally|elif|else|@|def|class|with|for|while|try)\b') {
        if ($totalHits -eq 0) { Write-Host "" }
        $totalHits++
        Write-Host ("{0,-6} {1}" -f ("L"+($i+1)), $f.FullName) -ForegroundColor Yellow
        Write-Host ("        {0}" -f $line.TrimEnd())
      }
    }
  }
}

if ($totalHits -eq 0) {
  Write-Host "✅ No empty try/except/finally blocks found." -ForegroundColor Green
} else {
  Write-Host "`n⚠️  Found $totalHits empty blocks. Run Fix-EmptyTry.ps1 to auto-fix." -ForegroundColor DarkYellow
}
