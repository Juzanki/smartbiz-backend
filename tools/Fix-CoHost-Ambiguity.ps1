param(
  [string]$UserFile = "backend\models\user.py",
  [string]$CoHostFile = "backend\models\co_host.py",
  [switch]$Apply
)

function Read-Text($path) {
  if (!(Test-Path $path)) { throw "File not found: $path" }
  return Get-Content $path -Raw
}

function Save-Backup($path, $ext) {
  if (!(Test-Path ($path + $ext))) {
    Copy-Item $path ($path + $ext)
  }
}

# ===== helper: find relationship( … ) block boundaries starting from index =====
function Find-CallBlock {
  param([string]$text, [int]$startIndex)
  $i = $text.IndexOf("relationship(", $startIndex, [StringComparison]::Ordinal)
  if ($i -lt 0) { return $null }
  # move to start of line
  $lineStart = $text.LastIndexOf("`n", $i); if ($lineStart -lt 0) { $lineStart = 0 } else { $lineStart++ }
  $j = $i + "relationship(".Length
  $depth = 1
  while ($j -lt $text.Length -and $depth -gt 0) {
    $ch = $text[$j]
    if ($ch -eq '(') { $depth++ }
    elseif ($ch -eq ')') { $depth-- }
    $j++
  }
  $callEnd = $j
  while ($callEnd -lt $text.Length -and $text[$callEnd] -ne "`n") { $callEnd++ }
  return @{ Start = $lineStart; End = $callEnd }
}

# ===== 1) USER.PY: remove/replace co_host_entries relationship =====
function Fix-User {
  param([string]$path, [string]$fkHost, [string]$fkCohost)

  $text = Read-Text $path
  $changed = $false

  # a) comment out an old relationship named co_host_entries (if any)
  $m = [regex]::Match($text, 'co_host_entries\s*:\s*Mapped\[.*?\]\s*=\s*relationship\(', 'Singleline')
  if ($m.Success) {
    $blk = Find-CallBlock -text $text -startIndex $m.Index
    if ($blk) {
      $before = $text.Substring(0, $blk.Start)
      $mid    = $text.Substring($blk.Start, $blk.End - $blk.Start)
      $after  = $text.Substring($blk.End)
      $midLines = $mid -split "`r?`n" | ForEach-Object { "# COHOSTFIX: $_" }
      $text = $before + ($midLines -join "`n") + $after
      $changed = $true
      Write-Host "user.py: commented old co_host_entries relationship"
    }
  }

  # b) ensure the new split relationships exist; if not, append after commented block or at end of class
  if ($text -notmatch 'co_host_as_host\s*:\s*Mapped\[') {
    $snippet = @"
    # ==== CoHost relationships (fixed) ====
    co_host_as_host: Mapped[list["CoHost"]] = relationship(
        "CoHost",
        back_populates="host",
        foreign_keys="CoHost.$fkHost",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    co_host_as_cohost: Mapped[list["CoHost"]] = relationship(
        "CoHost",
        back_populates="cohost",
        foreign_keys="CoHost.$fkCohost",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    @property
    def co_host_entries(self) -> list["CoHost"]:
        return (self.co_host_as_host or []) + (self.co_host_as_cohost or [])
"@
    # we try to put it near other relationships: search for "bots:" as anchor; else append before file end
    $anchor = [regex]::Match($text, '^\s*bots\s*:\s*Mapped\[', 'Multiline')
    if ($anchor.Success) {
      $insertAt = $anchor.Index
      $text = $text.Substring(0, $insertAt) + $snippet + "`n" + $text.Substring($insertAt)
    } else {
      $text = $text + "`n" + $snippet + "`n"
    }
    $changed = $true
    Write-Host "user.py: inserted co_host_as_host/co_host_as_cohost + property"
  }

  if ($changed) {
    Save-Backup $path ".cohostfix.bak"
    Set-Content -Path $path -Value $text -Encoding UTF8
  } else {
    Write-Host "user.py: nothing to change"
  }
}

# ===== 2) CO_HOST.PY: add host/cohost relationships with proper foreign_keys =====
function Fix-CoHost {
  param([string]$path)

  $text = Read-Text $path

  # find CoHost class name
  $mClass = [regex]::Match($text, 'class\s+(\w+)\(Base\)\s*:', 'Singleline')
  if (-not $mClass.Success) { throw "Could not find class ... (Base): in $path" }
  $cls = $mClass.Groups[1].Value

  # get FK column variable names that reference users.id (2 of them)
  $fkMatches = [regex]::Matches($text, '^\s*(\w+)\s*:\s*Mapped\[.*?\]\s*=\s*mapped_column\([^)]*ForeignKey\(\s*["'']users\.id["'']', 'Singleline, Multiline')
  $fkNames = @()
  foreach ($m in $fkMatches) { $fkNames += $m.Groups[1].Value }
  $fkNames = $fkNames | Sort-Object -Unique
  if ($fkNames.Count -lt 2) { throw "Could not detect two FK columns to users.id in $path" }

  $fkHost = $fkNames[0]
  $fkCohost = $fkNames[1]
  Write-Host ("co_host.py: detected FK columns -> host={0}, cohost={1}" -f $fkHost, $fkCohost)

  $changed = $false

  # a) comment out any relationship("User", ...) blocks inside this file (to avoid conflicting back_populates)
  $idx = 0
  $toComment = @()
  while ($true) {
    $i = $text.IndexOf('relationship("User"', $idx, [StringComparison]::Ordinal)
    if ($i -lt 0) { break }
    $blk = Find-CallBlock -text $text -startIndex $i
    if ($blk) {
      $toComment += $blk
      $idx = $blk.End
    } else { break }
  }
  if ($toComment.Count -gt 0) {
    # comment from bottom upwards
    $toComment = $toComment | Sort-Object Start -Descending
    foreach ($b in $toComment) {
      $before = $text.Substring(0, $b.Start)
      $mid    = $text.Substring($b.Start, $b.End - $b.Start)
      $after  = $text.Substring($b.End)
      $midLines = $mid -split "`r?`n" | ForEach-Object { "# COHOSTFIX: $_" }
      $text = $before + ($midLines -join "`n") + $after
    }
    $changed = $true
    Write-Host "co_host.py: commented old User relationships"
  }

  # b) add new host/cohost relationships inside class
  if ($text -notmatch 'back_populates\s*=\s*["'']co_host_as_host["'']') {
    # find insertion point: after last users.id FK line
    $lastFK = $null
    foreach ($m in $fkMatches) { $lastFK = $m }  # last one
    # compute the end-of-line of that FK line
    $tmpIdx = $text.IndexOf("`n", $lastFK.Index); if ($tmpIdx -lt 0) { $tmpIdx = $lastFK.Index + $lastFK.Length }
    $insertAt = $tmpIdx + 1

    # determine indentation (4 spaces)
    $indent = "    "

    $snippet = @"
${indent}# ==== fixed relationships to User ====
${indent}host = relationship(
${indent}    "User",
${indent}    back_populates="co_host_as_host",
${indent}    foreign_keys=lambda: [${cls}.${fkHost}],
${indent}    passive_deletes=True,
${indent})
${indent}cohost = relationship(
${indent}    "User",
${indent}    back_populates="co_host_as_cohost",
${indent}    foreign_keys=lambda: [${cls}.${fkCohost}],
${indent}    passive_deletes=True,
${indent})
"@
    $text = $text.Substring(0, $insertAt) + $snippet + $text.Substring($insertAt)
    $changed = $true
    Write-Host "co_host.py: inserted host/cohost relationships"
  }

  if ($changed) {
    Save-Backup $path ".cohostfix.bak"
    Set-Content -Path $path -Value $text -Encoding UTF8
  } else {
    Write-Host "co_host.py: nothing to change"
  }

  # return FK names so we can wire user.py
  return @{ FKHost = $fkHost; FKCohost = $fkCohost }
}

# ===== main =====
try {
  if (-not $Apply) {
    Write-Host "DRY-RUN: no changes will be written. Use -Apply to modify files." -ForegroundColor Yellow
  }

  $fkInfo = Fix-CoHost -path $CoHostFile
  if ($Apply) {
    Fix-User -path $UserFile -fkHost $fkInfo.FKHost -fkCohost $fkInfo.FKCohost
  } else {
    Write-Host ("Would wire user.py with foreign_keys CoHost.{0} / CoHost.{1}" -f $fkInfo.FKHost, $fkInfo.FKCohost)
  }

  if ($Apply) {
    Write-Host "`nDONE. Backups: *.cohostfix.bak" -ForegroundColor Green
  } else {
    Write-Host "`nDRY-RUN finished (no changes)." -ForegroundColor DarkGray
  }
}
catch {
  Write-Error $_
}
