param(
  [string]$Root = "backend\models",
  [string]$FkPattern = 'ForeignKey\("users\.id"'
)
Write-Host "🔎 Scanning $Root for multiple FKs -> users.id ..." -ForegroundColor Cyan
$hits = Get-ChildItem $Root -Recurse -Include *.py |
  Select-String -Pattern $FkPattern |
  Group-Object Path | Where-Object { $_.Count -ge 2 }
if (-not $hits) { Write-Host "✅ No files with 2+ FKs to users.id" -ForegroundColor Green; exit 0 }
foreach ($h in $hits) {
  $path = $h.Name; $count = $h.Count
  Write-Host "⚠️ $path has $count FKs to users.id" -ForegroundColor Yellow
  $h.Group | ForEach-Object { "{0}:{1}: {2}" -f $_.Path, $_.LineNumber, ($_.Line.Trim()) }
}
Write-Host "`n👉 For each file above, make sure every relationship specifies foreign_keys=[...]" -ForegroundColor Cyan
