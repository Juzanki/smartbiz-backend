@'
param(
  [string]$EnvFile = ".\.env.production",
  [string]$ProjectId,
  [string]$Service,
  [switch]$DryRun,
  [switch]$IncludePort
)

Write-Host "🚀 Loading env from: $EnvFile"

if (!(Test-Path -LiteralPath $EnvFile)) {
  Write-Error "❌ Env file haipo: $EnvFile"
  exit 1
}

# Railway CLI check
try {
  $null = & railway --version 2>$null
} catch {
  Write-Error "❌ Railway CLI haipo. Install: winget install Railway.Railway ; kisha run 'railway login'."
  exit 1
}

# Read & parse
$lines = Get-Content -LiteralPath $EnvFile -Encoding UTF8
$vars = @{}

foreach ($line in $lines) {
  $l = $line
  if ($null -eq $l) { continue }
  $l = $l.Trim()
  if ($l -eq "" -or $l.StartsWith("#")) { continue }

  $pair = $l -split '=', 2
  if ($pair.Count -lt 2) { continue }

  $k = $pair[0].Trim()
  $v = $pair[1].Trim()

  if (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'"))) {
    $v = $v.Substring(1, $v.Length-2)
  }

  if (-not $IncludePort -and $k -ieq "PORT") { continue }
  if ($k -ne "") { $vars[$k] = $v }
}

if ($vars.Keys.Count -eq 0) {
  Write-Warning "⚠️  Hakuna variables zilizopatikana."
  exit 0
}

Write-Host "🔎 Zimepatikana $($vars.Keys.Count) vars."
if ($DryRun) {
  foreach ($k in $vars.Keys) { Write-Host (" - {0}={1}" -f $k, $vars[$k]) }
  Write-Host "🧪 DryRun: hakuna kilichotumwa. Ondoa -DryRun kutuma."
  exit 0
}

# Build base args
$baseArgs = @("variables","set")
if ($ProjectId) { $baseArgs += @("--project", $ProjectId) }
if ($Service)   { $baseArgs += @("--service", $Service) }

$errors = 0
foreach ($k in $vars.Keys) {
  $kv = "$k=$($vars[$k])"
  try {
    & railway @baseArgs $kv | Out-Null
    Write-Host "✅ Set $k"
  } catch {
    Write-Host "❌ Fail $k : $($_.Exception.Message)"
    $errors++
  }
}

if ($errors -gt 0) {
  Write-Error "⛔ Makosa: $errors"
  exit 1
} else {
  Write-Host "🎉 Done!"
}
'@ | Out-File -FilePath .\upload_railway_env.min.ps1 -Encoding UTF8 -Force
