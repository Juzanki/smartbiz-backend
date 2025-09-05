# backend/tools/Fix-PythonEmptyBlocks.ps1
[CmdletBinding()]
param(
  [string]$Root = "backend\models",   # badilisha ukipenda
  [switch]$Preview                  # -Preview = onyesha nini kingeandikwa bila kuandika
)

function Get-IndentLen([string]$s) {
  if ($s -match '^[ \t]*') { return $Matches[0].Length } else { return 0 }
}

$files   = Get-ChildItem -Path $Root -Recurse -Filter *.py
$patched = 0

foreach ($f in $files) {
  $raw   = Get-Content -LiteralPath $f.FullName -Raw
  if (-not $raw) { continue }

  # tambua newline style
  $nl = ($raw -match "`r`n") ? "`r`n" : "`n"

  # gawa mistari
  $lines = $raw -split "`r?`n", 0
  $out   = New-Object System.Collections.Generic.List[string]
  $i     = 0
  $changed = $false

  while ($i -lt $lines.Length) {
    $line = $lines[$i]
    $out.Add($line)

    # Mechi ya 'try:' au 'except ...:' mwisho wa mstari (comments zina ruhusiwa)
    if ($line -match '^(?<indent>[ \t]*)(?:try|except\b[^:]*):\s*(?:#.*)?$') {
      $indent = $Matches['indent']
      $indentBlock = $indent + '    '

      # angalia mistari inayofuata iliyo #/tupu tu
      $j = $i + 1
      while ($j -lt $lines.Length) {
        $l2 = $lines[$j]
        if ($l2 -match '^[ \t]*(#.*)?$') {
          $out.Add($l2)
          $j++
          continue
        }
        break
      }

      # tathmini kama block iko tupu (hakuna mstari ulio-INDENT > indent ya mzazi)
      $needPass = $false
      if ($j -ge $lines.Length) {
        $needPass = $true
      } else {
        $next = $lines[$j]
        $nextIndentLen = Get-IndentLen $next
        $trimNext = $next.TrimStart()
        if ($nextIndentLen -le (Get-IndentLen $line) -or $trimNext -match '^(except\b|finally\b)') {
          $needPass = $true
        }
      }

      if ($needPass) {
        $out.Add($indentBlock + 'pass')
        $changed = $true
      }

      $i = $j
      continue
    }

    $i++
  }

  if ($changed -and -not $Preview) {
    Copy-Item -LiteralPath $f.FullName -Destination ($f.FullName + ".bak") -Force
    [IO.File]::WriteAllText($f.FullName, ($out -join $nl), [Text.UTF8Encoding]::new($false))
    Write-Host "Patched: $($f.FullName)" -ForegroundColor Green
    $patched++
  }
  elseif ($changed -and $Preview) {
    Write-Host "Would patch: $($f.FullName)" -ForegroundColor Yellow
    $patched++
  }
}

Write-Host ("Done. " + ($Preview ? "Files to patch: " : "Files patched: ") + $patched) -ForegroundColor Cyan
