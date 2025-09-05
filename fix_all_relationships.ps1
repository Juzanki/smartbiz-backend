# === fix_all_relationships.ps1 ===
# Inarekebisha missing back_populates + relationship import kwenye models zako zote

$modelsPath = "E:\SmartBiz_Assistance\backend\models"
$files = Get-ChildItem -Path $modelsPath -Recurse -Include *.py

$relationships = @{}
$classes = @{}

foreach ($file in $files) {
    $lines = Get-Content $file.FullName
    $currentClass = ""

    foreach ($line in $lines) {
        if ($line -match 'class\s+(\w+)\(Base\)') {
            $currentClass = $matches[1]
            $classes[$currentClass] = $file.FullName
        }

        if ($line -match 'relationship\("(\w+)",\s*back_populates="(\w+)"\)') {
            $target = $matches[1]
            $backref = $matches[2]
            if (-not $relationships.ContainsKey($target)) {
                $relationships[$target] = @()
            }
            $relationships[$target] += @{
                from = $currentClass
                back = $backref
                file = $file.FullName
            }
        }
    }
}

Write-Host "`n🧠 Scanning for missing back_populates + fixing imports..." -ForegroundColor Cyan

foreach ($targetClass in $relationships.Keys) {
    if (-not $classes.ContainsKey($targetClass)) {
        Write-Host "❌ Class '$targetClass' haijapatikana!" -ForegroundColor Red
        continue
    }

    $targetFile = $classes[$targetClass]
    $lines = Get-Content $targetFile
    $existingText = $lines -join "`n"

    $hasImport = $lines | Where-Object { $_ -match 'from sqlalchemy.orm import .*relationship' }

    foreach ($rel in $relationships[$targetClass]) {
        $expected = "$($rel.back)\s*=\s*relationship"
        if ($existingText -notmatch $expected) {
            Write-Host "✍️  Adding back_populates='$($rel.back)' in class $targetClass" -ForegroundColor Yellow

            # Backup original
            Copy-Item $targetFile "$targetFile.bak" -Force

            $classIndex = ($lines | Select-String -Pattern "class\s+$targetClass\s*\(.*\)").LineNumber
            if (-not $classIndex) { continue }

            $relLine = "    $($rel.back) = relationship(`"$($rel.from)`", back_populates=`"$targetClass`", cascade=`"all, delete-orphan`")"

            $newLines = @()
            $importAdded = $false

            for ($i = 0; $i -lt $lines.Count; $i++) {
                # Add import at the top if not present
                if (-not $importAdded -and $i -eq 0 -and -not $hasImport) {
                    $newLines += "from sqlalchemy.orm import relationship"
                    $importAdded = $true
                }

                $newLines += $lines[$i]

                if ($i -eq $classIndex) {
                    $newLines += "    # Auto-added back_populates"
                    $newLines += $relLine
                }
            }

            $newLines | Set-Content $targetFile
        }
    }
}

Write-Host "`n✅ Done fixing missing relationships and imports." -ForegroundColor Green
