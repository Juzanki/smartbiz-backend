param(
  [string]$ModelsRoot = "backend\models",
  [switch]$Apply
)

function Read-Text($path) {
  if (!(Test-Path $path)) { throw ("File not found: {0}" -f $path) }
  Get-Content $path -Raw
}
function Save-Text($path, $text) { Set-Content -Path $path -Value $text -Encoding UTF8 }
function Backup-Once($path, $ext) { if (!(Test-Path ($path + $ext))) { Copy-Item $path ($path + $ext) } }

# Tafuta faili lenye class fulani (mf. "Notification")
function Find-ClassFile([string]$className, [string]$root) {
  $files = Get-ChildItem -Path $root -Filter *.py -Recurse -ErrorAction SilentlyContinue
  foreach ($f in $files) {
    $t = Get-Content $f.FullName -Raw
    if ($t -match ("class\s+" + [regex]::Escape($className) + "\s*\(")) { return $f.FullName }
  }
  return $null
}

# Pata majina ya columns zilizo ForeignKey -> users.*
function Get-UserFKs([string]$filePath) {
  $t = Read-Text $filePath
  $rx = '^\s*(\w+)\s*:\s*Mapped\[.*?\]\s*=\s*mapped_column\([^)]*ForeignKey\(\s*["'']users\.[^"'']+["'']'
  $matches = [regex]::Matches($t, $rx, 'Singleline, Multiline')
  $names = @()
  foreach ($m in $matches) { $names += $m.Groups[1].Value }
  return ($names | Sort-Object -Unique)
}

# Chagua FK bora kulingana na jina la attribute au back_populates
function Choose-FK($relName, $backPop, $fkList) {
  $cands = New-Object System.Collections.Generic.List[string]
  if ($backPop) {
    $cands.Add($backPop + "_id"); $cands.Add($backPop + "_user_id")
  }
  if ($relName) {
    $cands.Add($relName + "_id"); $cands.Add($relName + "_user_id")
  }
  $map = @{
    "user"="user"; "author"="author"; "awarded_by"="awarded_by"; "approver"="approved_by"; "approved_by"="approved_by";
    "assignee"="assignee"; "actor"="actor"; "viewer"="viewer"; "owner"="owner"; "host"="host"; "cohost"="cohost";
    "guest"="guest"; "sender"="sender"; "recipient"="recipient"; "referrer"="referrer"; "referred_user"="referred_user";
    "staff"="staff_user"; "clicked_by"="clicked_by"; "target"="target_user"; "target_user"="target_user";
    "last_editor"="last_editor"; "last_actor"="last_actor"; "created_by"="created_by"; "updated_by"="updated_by"
  }
  foreach ($k in $map.Keys) {
    if ($relName -eq $k -or $backPop -eq $k) {
      $stem = $map[$k]; $cands.Add($stem + "_id"); $cands.Add($stem + "_user_id")
    }
  }
  $cands.Add("user_id")  # fallback

  foreach ($c in $cands) {
    if ($fkList -contains $c) { return $c }
    $hit = $fkList | Where-Object { $_ -like ("*" + $c + "*") }
    if ($hit) { return $hit[0] }
  }
  return $null
}

# Rudi index ya ')' inayofunga call ya relationship(...) kuanzia offset
function Find-CallEnd([string]$text, [int]$startIdx) {
  $depth = 0
  for ($i=$startIdx; $i -lt $text.Length; $i++) {
    $ch = $text[$i]
    if ($ch -eq '(') { $depth++ }
    elseif ($ch -eq ')') {
      $depth--
      if ($depth -eq 0) { return $i }
    }
  }
  return -1
}

# Tafuta relationship(...) zote na attr yake ya kushoto
function Find-Relationships([string]$text) {
  $list = @()
  $relRegex = [regex]'relationship\(\s*["'']([A-Za-z_]\w*)["'']'
  $m = $relRegex.Match($text)
  while ($m.Success) {
    $cls = $m.Groups[1].Value
    $callStart = $m.Index
    $parenOpen = $text.LastIndexOf('(', $callStart)
    if ($parenOpen -lt 0) { $parenOpen = $callStart }

    $callEnd = Find-CallEnd -text $text -startIdx $parenOpen
    if ($callEnd -lt 0) { $m = $m.NextMatch(); continue }

    # pata attr name kwa kurudi nyuma ~600 chars
    $backWindowStart = [Math]::Max(0, $parenOpen - 600)
    $prefix = $text.Substring($backWindowStart, $parenOpen - $backWindowStart)
    $attr = $null

    $attrM = [regex]::Match($prefix, '([A-Za-z_]\w*)\s*:\s*Mapped\[[^\]]*\]\s*=\s*$', 'Singleline')
    if (-not $attrM.Success) {
      $attrM = [regex]::Match($prefix, '([A-Za-z_]\w*)\s*=\s*$', 'Singleline')
    }
    if ($attrM.Success) { $attr = $attrM.Groups[1].Value }

    $callText = $text.Substring($parenOpen, $callEnd - $parenOpen + 1)
    $hasFK = $false
    if ($callText -match 'foreign_keys\s*=') { $hasFK = $true }

    $bpM = [regex]::Match($callText, 'back_populates\s*=\s*["''](\w+)["'']')
    $backPop = $null
    if ($bpM.Success) { $backPop = $bpM.Groups[1].Value }

    $list += [pscustomobject]@{
      Attr      = $attr
      Class     = $cls
      CallStart = $parenOpen
      CallEnd   = $callEnd
      CallText  = $callText
      HasFK     = $hasFK
      BackPop   = $backPop
    }

    $m = $m.NextMatch()
  }
  return $list
}

# weka foreign_keys="Class.fk" mara baada ya jina la class
function Inject-FK([string]$callText, [string]$cls, [string]$fk) {
  $p1 = 'relationship\(\s*"' + [regex]::Escape($cls) + '"\s*,'
  $p2 = 'relationship\(\s*"' + [regex]::Escape($cls) + '"\s*'

  if ([regex]::IsMatch($callText, $p1)) {
    return [regex]::Replace(
      $callText, $p1,
      ('relationship("' + $cls + '", foreign_keys="' + $cls + '.' + $fk + '", '),
      1
    )
  } elseif ([regex]::IsMatch($callText, $p2)) {
    return [regex]::Replace(
      $callText, $p2,
      ('relationship("' + $cls + '", foreign_keys="' + $cls + '.' + $fk + '" '),
      1
    )
  } else {
    return $callText
  }
}

# ---------------- MAIN ----------------
try {
  $userFile = Join-Path $ModelsRoot "user.py"
  $allFiles = Get-ChildItem -Path $ModelsRoot -Filter *.py -Recurse -ErrorAction SilentlyContinue

  $planned = @()

  foreach ($f in $allFiles) {
    $t = Read-Text $f.FullName
    $rels = Find-Relationships -text $t
    if (-not $rels -or $rels.Count -eq 0) { continue }

    foreach ($r in $rels) {
      if ($r.HasFK) { continue }

      # Case A: relationship(... "User" ...) ndani ya file lolote
      if ($r.Class -eq "User") {
        $fkList = Get-UserFKs -filePath $f.FullName
        if ($fkList.Count -lt 2) { continue }  # si ambiguous

        $fk = Choose-FK -relName $r.Attr -backPop $r.BackPop -fkList $fkList
        if (-not $fk) {
          Write-Host ("[WARN] {0}:{1} -> cannot choose FK among [{2}]" -f $f.Name, $r.Attr, ($fkList -join ", ")) -ForegroundColor Yellow
          continue
        }
        $planned += [pscustomobject]@{
          File = $f.FullName; Class = $r.Class; FK = $fk; Start = $r.CallStart; End = $r.CallEnd; Attr = $r.Attr
        }
        continue
      }

      # Case B: relationship(... "SomeChild" ...) NDANI YA user.py
      if ($f.FullName -ieq $userFile) {
        $childFile = Find-ClassFile -className $r.Class -root $ModelsRoot
        if (-not $childFile) {
          Write-Host ("[SKIP] user.py -> class {0} not found" -f $r.Class) -ForegroundColor DarkGray
          continue
        }
        $fkList = Get-UserFKs -filePath $childFile
        if ($fkList.Count -lt 2) { continue }  # si ambiguous

        $fk = Choose-FK -relName $r.Attr -backPop $r.BackPop -fkList $fkList
        if (-not $fk) {
          Write-Host ("[WARN] user.py {0} -> cannot choose FK for {1} among [{2}]" -f $r.Attr, $r.Class, ($fkList -join ", ")) -ForegroundColor Yellow
          continue
        }
        $planned += [pscustomobject]@{
          File = $f.FullName; Class = $r.Class; FK = $fk; Start = $r.CallStart; End = $r.CallEnd; Attr = $r.Attr
        }
      }
    }
  }

  if (-not $planned -or $planned.Count -eq 0) {
    Write-Host "Hakuna relationship iliyoonekana kuhitaji foreign_keys= (kulingana na kanuni hizi)." -ForegroundColor Green
    return
  }

  Write-Host "Zitarekebishwa zifuatazo:" -ForegroundColor Cyan
  foreach ($p in $planned) {
    $a = $p.Attr
    if (-not $a) { $a = "<attr>" }
    Write-Host (" - {0} :: {1}.{2} -> foreign_keys=""{1}.{3}""" -f $p.File, $p.Class, $a, $p.FK)
  }

  if ($Apply) {
    $byFile = $planned | Group-Object File
    foreach ($grp in $byFile) {
      $path = $grp.Name
      $text = Read-Text $path
      $sorted = $grp.Group | Sort-Object Start -Descending
      foreach ($p in $sorted) {
        $orig = $text.Substring($p.Start, $p.End - $p.Start + 1)
        $patched = Inject-FK -callText $orig -cls $p.Class -fk $p.FK
        $text = $text.Substring(0,$p.Start) + $patched + $text.Substring($p.End + 1)
      }
      Backup-Once $path ".autofk2.bak"
      Save-Text $path $text
      Write-Host ("Patched: {0}" -f $path)
    }
    Write-Host "DONE. Backups: *.autofk2.bak" -ForegroundColor Green
  } else {
    Write-Host "`nDRY-RUN tu. Tumia -Apply kuandika mabadiliko." -ForegroundColor Yellow
  }

} catch {
  Write-Error $_
}
