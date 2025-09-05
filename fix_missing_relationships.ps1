# === fix_missing_relationships.ps1 ===

# Set path ya file ya model mfano user.py
$modelPath = "E:\SmartBiz_Assistance\backend\models\user.py"

# Taarifa ya relationship mpya
$targetClass = "User"
$relClass = "UserBot"
$relName = "bots"
$relLine = "    $relName = relationship(`"$relClass`", back_populates=`"$targetClass`", cascade=`"all, delete-orphan`")"

# Backup file kabla ya kuandika
$backupPath = "$modelPath.bak"
Copy-Item -Path $modelPath -Destination $backupPath -Force
Write-Host "✅ Backup created at $backupPath" -ForegroundColor DarkGreen

# Soma file
$lines = Get-Content $modelPath
$classStartIndex = ($lines | Select-String -Pattern "class\s+$targetClass\s*\(.*\)").LineNumber

if (-not $classStartIndex) {
    Write-Host "❌ Class $targetClass haikupatikana ndani ya $modelPath" -ForegroundColor Red
    exit
}

# Angalia kama relationship tayari ipo
$existing = $lines | Select-String -Pattern "$relName\s*=\s*relationship\("

if ($existing) {
    Write-Host "⚠️  Relationship '$relName' tayari ipo kwenye $targetClass" -ForegroundColor Yellow
    exit
}

# Tafuta mwisho wa class kwa indentation detection
$classEndIndex = ($lines[$classStartIndex..($lines.Length - 1)] | Where-Object { $_ -notmatch '^\s' -or $_ -match '^class' }).LineNumber[0]
if (-not $classEndIndex) {
    $classEndIndex = $lines.Length
} else {
    $classEndIndex += $classStartIndex - 1
}

# Weka line mpya ya relationship kabla ya mwisho wa class
$newLines = @()
for ($i = 0; $i -lt $lines.Count; $i++) {
    $newLines += $lines[$i]
    if ($i -eq $classEndIndex - 2) {
        $newLines += "    # Auto-added relationship"
        $newLines += $relLine
    }
}

# Andika upya file
$newLines | Set-Content $modelPath
Write-Host "✅ Relationship '$relName' added successfully to $modelPath" -ForegroundColor Green
# === fix_missing_relationships.ps1 ===

# Set path ya file ya model mfano user.py
$modelPath = "E:\SmartBiz_Assistance\backend\models\user.py"

# Taarifa ya relationship mpya
$targetClass = "User"
$relClass = "UserBot"
$relName = "bots"
$relLine = "    $relName = relationship(`"$relClass`", back_populates=`"$targetClass`", cascade=`"all, delete-orphan`")"

# Backup file kabla ya kuandika
$backupPath = "$modelPath.bak"
Copy-Item -Path $modelPath -Destination $backupPath -Force
Write-Host "✅ Backup created at $backupPath" -ForegroundColor DarkGreen

# Soma file
$lines = Get-Content $modelPath
$classStartIndex = ($lines | Select-String -Pattern "class\s+$targetClass\s*\(.*\)").LineNumber

if (-not $classStartIndex) {
    Write-Host "❌ Class $targetClass haikupatikana ndani ya $modelPath" -ForegroundColor Red
    exit
}

# Angalia kama relationship tayari ipo
$existing = $lines | Select-String -Pattern "$relName\s*=\s*relationship\("

if ($existing) {
    Write-Host "⚠️  Relationship '$relName' tayari ipo kwenye $targetClass" -ForegroundColor Yellow
    exit
}

# Tafuta mwisho wa class kwa indentation detection
$classEndIndex = ($lines[$classStartIndex..($lines.Length - 1)] | Where-Object { $_ -notmatch '^\s' -or $_ -match '^class' }).LineNumber[0]
if (-not $classEndIndex) {
    $classEndIndex = $lines.Length
} else {
    $classEndIndex += $classStartIndex - 1
}

# Weka line mpya ya relationship kabla ya mwisho wa class
$newLines = @()
for ($i = 0; $i -lt $lines.Count; $i++) {
    $newLines += $lines[$i]
    if ($i -eq $classEndIndex - 2) {
        $newLines += "    # Auto-added relationship"
        $newLines += $relLine
    }
}

# Andika upya file
$newLines | Set-Content $modelPath
Write-Host "✅ Relationship '$relName' added successfully to $modelPath" -ForegroundColor Green
