param(
  [string]$ModelsRoot = "backend\models",
  [string]$UserFile   = "backend\models\user.py",
  [switch]$Apply
)

function Read-Text($path) {
  if (!(Test-Path $path)) { throw ("File not found: {0}" -f $path) }
  Get-Content $path -Raw
}
function Save-Text($path, $text) { Set-Content -Path $path -Value $text -Encoding UTF8 }
function Backup-Once($path, $ext) { if (!(Test-Path ($path + $ext))) { Copy-Item $path ($path + $ext) } }

# Pata faili lenye class fulani
function Find-ClassFile([string]$className) {
  $files = Get-ChildItem -Path $ModelsRoot -Filter *.py -Recurse -ErrorAction SilentlyContinue
  foreach ($f in $files) {
    $t = Get-Content $f.FullName -Raw
    if ($t -match ("class\s+" + [regex]::Escape($className) + "\(Base\)\s*:")) { return $f.FullName }
  }
  return $null
}

# Majina ya FK zinazorejea users.*
function Get-UserFKs([string]$filePath) {
  $t = Read-Text $filePath
  $matches = [regex]::Matches(
    $t,
    '^\s*(\w+)\s*:\s*Mapped\[.*?\]\s*=\s*mapped_column\([^)]*ForeignKey\(\s*["'']users\.(?:id|\w+)["'']',
    'Singleline, Multiline'
  )
  $names = @()
  foreach ($m in $matches) { $names += $m.Groups[1].Value }
  ($names | Sort-Object -Unique)
}

# Heuristics kuchagua FK inayofaa
function Choose-FK($relName, $backPop, $fkList) {
  $cands = New-Object System.Collections.Generic.List[string]
  if ($backPop) {
    $cands.Add(($backPop + "_id"))
    $cands.Add(($backPop + "_user_id"))
  }
  if ($relName) {
    $cands.Add(($relName + "_id"))
    $cands.Add(($relName + "_user_id"))
  }
  $map = @{
    "author"="author"; "awarded_by"="awarded_by"; "approver"="approved_by"; "approved_by"="approved_by";
    "assignee"="assignee"; "actor"="actor"; "viewer"="viewer"; "owner"="owner"; "host"="host"; "cohost"="cohost";
    "guest"="guest"; "sender"="sender"; "recipient"="recipient"; "referrer"="referrer"; "referred_user"="referred_user";
    "staff"="staff_user"; "clicked_by"="clicked_by"; "target"="target_user"; "target_user"="target_user";
    "last_editor"="last_editor"; "last_actor"="last_actor"; "created_by"="created_by"; "updated_by"="updated_by";
    "user"="user"
  }
  foreach ($k in $map.Keys) {
    if ($relName -eq $k -or $backPop -eq $k) {
      $stem = $map[$k]; $cands.Add(($stem + "_id")); $cands.Add(($stem + "_user_id"))
    }
  }
  $cands.Add("user_id")   # fallback

  foreach ($c in $cands) {
    if ($fkList -contains $c) { return $c }
    $hit = $fkList | Where-Object { $_ -like ("*" + $c + "*") }
    if ($hit) { return $hit[0] }
  }
  return $null
}

# Soma relationships zote kwenye user.py (multi-line, na zisizoanza na '#')
function Find-UserRelationships([string]$text) {
  $pattern = '^[ \t]*(?!#)(\w+)\s*:\s*Mapped\[[^\]]*\]\s*=\s*relationship\(\s*["''](\w+)["''](?<params>.*?)\)'
  $matches = [regex]::Matches($text, $pattern, 'Singleline, Multiline')
  $results = @()
  foreach ($m in $matches) {
    $attr   = $m.Groups[1].Value
    $cls    = $m.Groups[2].Value
    $params = $m.Groups['params'].Value
    $bpMatch = [regex]::Match($params, 'back_populates\s*=\s*["''](\w+)["'']')
    $bpVal = $null
    if ($bpMatch.Success) { $bpVal = $bpMatch.Groups[1].Value }

    $hasFK = $false
    if ($params -match 'foreign_keys\s*=') { $hasFK = $true }

    $start  = $m.Index
    $end    = $m.Index + $m.Length

    $results += [pscustomobject]@{
      Attr    = $attr
      Class   = $cls
      Params  = $params
      BackPop = $bpVal
      HasFK   = $hasFK
      Start   = $start
      End     = $end
    }
  }
  return $results
}

try {
  $uText = Read-Text $UserFile
  $rels  = Find-UserRelationships -text $uText
  if (-not $rels -or $rels.Count -eq 0) { Write-Host "Hakuna relationships zilizopatikana kwenye user.py"; exit }

  $work = @()
  foreach ($r in $rels) {
    if ($r.HasFK) { continue }
    $clsFile = Find-ClassFile -className $r.Class
    if (-not $clsFile) { Write-Host ("[SKIP] {0}.{1} -> class file not found" -f $r.Class, $r.Attr); continue }
    $fkList = Get-UserFKs -filePath $clsFile
    if ($fkList.Count -lt 2) { continue }  # si ambiguous
    $fk = Choose-FK -relName $r.Attr -backPop $r.BackPop -fkList $fkList
    if (-not $fk) {
      Write-Host ("[WARN] {0}.{1}: cannot choose FK among [{2}]" -f $r.Class, $r.Attr, ($fkList -join ", ")) -ForegroundColor Yellow
      continue
    }
    $work += [pscustomobject]@{ Attr=$r.Attr; Class=$r.Class; FK=$fk; Start=$r.Start; End=$r.End }
  }

  if (-not $work) {
    Write-Host "Hakuna relationship ya user.py inayohitaji foreign_keys (au hakuna class yenye FKs>=2)." -ForegroundColor Green
    exit
  }

  Write-Host "Zitarekebishwa (user.py):" -ForegroundColor Cyan
  foreach ($w in $work) {
    Write-Host (" - {0}: relationship(""{1}"", foreign_keys=""{1}.{2}"")" -f $w.Attr, $w.Class, $w.FK)
  }

  if ($Apply) {
    $newText = $uText
    foreach ($w in ($work | Sort-Object Start -Descending)) {
      $block = $newText.Substring($w.Start, $w.End - $w.Start)
      if ($block -match ('relationship\(\s*"' + [regex]::Escape($w.Class) + '"\s*,'))
      {
        $block2 = [regex]::Replace(
          $block,
          ('relationship\(\s*"' + [regex]::Escape($w.Class) + '"\s*,'),
          ('relationship("'+$w.Class+'", foreign_keys="'+$w.Class+'.'+$w.FK+'", '),
          1
        )
      }
      else {
        $block2 = [regex]::Replace(
          $block,
          ('relationship\(\s*"' + [regex]::Escape($w.Class) + '"\s*'),
          ('relationship("'+$w.Class+'", foreign_keys="'+$w.Class+'.'+$w.FK+'" '),
          1
        )
      }
      $newText = $newText.Substring(0,$w.Start) + $block2 + $newText.Substring($w.End)
    }
    Backup-Once $UserFile ".autofk.bak"
    Save-Text $UserFile $newText
    Write-Host "`nDONE: user.py updated. Backup: user.py.autofk.bak" -ForegroundColor Green
  } else {
    Write-Host "`nDRY-RUN tu. Kimbiza tena na -Apply kuandika mabadiliko." -ForegroundColor Yellow
  }

} catch {
  Write-Error $_
}
