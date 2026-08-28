# ตัวเปิด Investing Pro — กันเปิดซ้ำซ้อน (สาเหตุหลักที่ทำให้ "เข้าไม่ได้")
# ถ้าเซิร์ฟเวอร์รันอยู่แล้วจะเปิดแค่เบราว์เซอร์ ไม่สร้างตัวใหม่มาแย่งพอร์ต/แรม
$ErrorActionPreference = "SilentlyContinue"
$url = "http://127.0.0.1:8750"

function Test-Server {
    try { (Invoke-WebRequest -Uri $url -TimeoutSec 5 -UseBasicParsing).StatusCode -eq 200 }
    catch { $false }
}

Write-Host "กำลังตรวจสอบ Investing Pro..." -ForegroundColor Cyan

if (Test-Server) {
    Write-Host "เซิร์ฟเวอร์ทำงานอยู่แล้ว — เปิดหน้าเว็บให้เลย" -ForegroundColor Green
    Start-Process $url
    Start-Sleep -Seconds 2
    exit
}

# เก็บกวาดเฉพาะ "แอปนี้" ที่ค้างจากรอบก่อน — ห้ามฆ่า python ตัวอื่นของผู้ใช้
# (เคยพลาดไปดับ http.server / สคริปต์งานอื่นที่รันคู่กันอยู่)
$stuck = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
         Where-Object { $_.CommandLine -match 'app\.py' }
if ($stuck) {
    Write-Host ("พบ Investing Pro ค้างอยู่ " + @($stuck).Count + " ตัว — กำลังปิดเฉพาะตัวนี้...") -ForegroundColor Yellow
    $stuck | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 3
}

# เตือนถ้าแรมเหลือน้อย — สาเหตุที่ทำให้โหลดค้างบ่อย
$os = Get-CimInstance Win32_OperatingSystem
$freeGB = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
if ($freeGB -lt 0.8) {
    Write-Host ("เตือน: แรมเหลือ " + $freeGB + " GB อาจโหลดช้าหรือค้าง") -ForegroundColor Yellow
    Write-Host "แนะนำ: ปิดโปรแกรมที่ไม่ใช้ (เบราว์เซอร์แท็บเยอะ ๆ / Webull) แล้วลองใหม่" -ForegroundColor Yellow
}

$env:GEMINI_API_KEY = [Environment]::GetEnvironmentVariable("GEMINI_API_KEY", "User")
$env:GROQ_API_KEY   = [Environment]::GetEnvironmentVariable("GROQ_API_KEY", "User")

Write-Host "กำลังเปิดเซิร์ฟเวอร์..." -ForegroundColor Cyan
Start-Process -FilePath "python" -ArgumentList "app.py", "--no-browser" `
    -WorkingDirectory $PSScriptRoot -WindowStyle Hidden

# รอจนเซิร์ฟเวอร์พร้อม (สูงสุด 90 วิ — เครื่องแรมน้อยใช้เวลานานกว่าปกติ)
$ready = $false
for ($i = 0; $i -lt 45; $i++) {
    Start-Sleep -Seconds 2
    if (Test-Server) { $ready = $true; break }
}

if ($ready) {
    Write-Host "พร้อมใช้งาน! เปิดหน้าเว็บให้แล้ว" -ForegroundColor Green
    Start-Process $url
    Start-Sleep -Seconds 2
} else {
    Write-Host ""
    Write-Host "เปิดไม่สำเร็จ — เครื่องน่าจะแรมไม่พอ" -ForegroundColor Red
    Write-Host "ทางแก้: 1) ปิดโปรแกรมอื่นแล้วรันไฟล์นี้ใหม่" -ForegroundColor Yellow
    Write-Host "        2) หรือใช้เว็บออนไลน์แทน: https://investing-proped.onrender.com" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "กด Enter เพื่อปิด"
}
