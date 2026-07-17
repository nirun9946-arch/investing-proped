Add-Type -AssemblyName Microsoft.VisualBasic
Add-Type -AssemblyName System.Windows.Forms

$key = [Microsoft.VisualBasic.Interaction]::InputBox(
  "วางคีย์ Gemini API ลงในช่องนี้ (คลิกในช่องแล้วกด Ctrl+V) จากนั้นกด OK`n`nคีย์ขึ้นต้นด้วย AIza...`nยังไม่มีคีย์? สร้างฟรีที่ aistudio.google.com/apikey",
  "ตั้งค่าคีย์ AI - Investing Pro", "")

if ([string]::IsNullOrWhiteSpace($key)) {
  [System.Windows.Forms.MessageBox]::Show("ยกเลิก - ยังไม่ได้ใส่คีย์","Investing Pro") | Out-Null
  exit
}
$key = $key.Trim()
[Environment]::SetEnvironmentVariable("GEMINI_API_KEY", $key, "User")
$env:GEMINI_API_KEY = $key

Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Start-Process -FilePath "cmd.exe" -ArgumentList '/c', ("`"" + $PSScriptRoot + "\เปิดโปรแกรม.bat`"") -WindowStyle Minimized
Start-Sleep -Seconds 5
Start-Process "http://127.0.0.1:8750"
[System.Windows.Forms.MessageBox]::Show("ตั้งคีย์เรียบร้อย! เปิดโปรแกรมใหม่ให้แล้ว`nลองกดปุ่ม AI วิเคราะห์เจาะลึก บนการ์ดหุ้นได้เลย","สำเร็จ") | Out-Null
