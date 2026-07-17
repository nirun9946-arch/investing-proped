@echo off
chcp 65001 >nul
cd /d "%~dp0"
title ตั้งค่าคีย์ AI (Gemini ฟรี)
echo.
echo   =====================================================
echo     ตั้งค่าคีย์ AI สำหรับปุ่ม "AI วิเคราะห์เจาะลึก"
echo   =====================================================
echo.
echo   วิธีเอาคีย์ (ฟรี ไม่ต้องผูกบัตร):
echo     1. เปิดเว็บ  aistudio.google.com/apikey
echo     2. กด Create API key  ได้สตริงขึ้นต้นด้วย  AIza...
echo     3. กด Copy  แล้วกลับมาวางที่ช่องข้างล่าง
echo.
echo   -----------------------------------------------------
set /p KEY="   วางคีย์ตรงนี้ แล้วกด Enter :  "
echo   -----------------------------------------------------
if "%KEY%"=="" (
  echo.
  echo   ยกเลิก - ยังไม่ได้ใส่คีย์
  echo.
  pause
  exit /b
)
setx GEMINI_API_KEY "%KEY%" >nul
set "GEMINI_API_KEY=%KEY%"
echo.
echo   บันทึกคีย์เรียบร้อย!  กำลังเปิดโปรแกรมใหม่ให้อ่านคีย์...
echo.
taskkill /F /IM python.exe >nul 2>&1
timeout /t 2 /nobreak >nul
start "" "%~dp0เปิดโปรแกรม.bat"
timeout /t 3 /nobreak >nul
echo   เสร็จแล้ว! เบราว์เซอร์จะเปิดหน้า Investing Pro
echo   ลองกดปุ่ม  AI วิเคราะห์เจาะลึก  บนการ์ดหุ้นได้เลย
echo.
echo   (ปิดหน้าต่างนี้ได้)
echo.
pause
