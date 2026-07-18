Add-Type -AssemblyName Microsoft.VisualBasic
Add-Type -AssemblyName System.Windows.Forms

$key = [Microsoft.VisualBasic.Interaction]::InputBox(
  "วางคีย์ AI ลงในช่องนี้ (คลิกในช่องแล้วกด Ctrl+V) จากนั้นกด OK`n`nระบบรู้จักคีย์เองอัตโนมัติ:`n  - Gemini ขึ้นต้น AIza...  (ฟรี ~20 ครั้ง/วัน — aistudio.google.com/apikey)`n  - Groq ขึ้นต้น gsk_...  (ฟรี ~1,000 ครั้ง/วัน — console.groq.com)`n  - Claude ขึ้นต้น sk-ant-...  / DeepSeek ขึ้นต้น sk-...",
  "ตั้งค่าคีย์ AI - Investing Pro", "")

if ([string]::IsNullOrWhiteSpace($key)) {
  [System.Windows.Forms.MessageBox]::Show("ยกเลิก - ยังไม่ได้ใส่คีย์","Investing Pro") | Out-Null
  exit
}
$key = $key.Trim()

# เดาเจ้าของคีย์จากรูปแบบ — ผู้ใช้ไม่ต้องรู้ชื่อ environment variable เอง
if ($key -like "AIza*")        { $var = "GEMINI_API_KEY";    $name = "Google Gemini" }
elseif ($key -like "gsk_*")    { $var = "GROQ_API_KEY";      $name = "Groq" }
elseif ($key -like "sk-ant-*") { $var = "ANTHROPIC_API_KEY"; $name = "Claude" }
elseif ($key -like "sk-*")     { $var = "DEEPSEEK_API_KEY";  $name = "DeepSeek" }
else {
  [System.Windows.Forms.MessageBox]::Show("ไม่รู้จักรูปแบบคีย์นี้`nคีย์ต้องขึ้นต้นด้วย AIza / gsk_ / sk-ant- / sk-`nลองก๊อปคีย์ใหม่อีกครั้ง","Investing Pro") | Out-Null
  exit
}

[Environment]::SetEnvironmentVariable($var, $key, "User")
Set-Item -Path ("Env:" + $var) -Value $key

Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Start-Process -FilePath "cmd.exe" -ArgumentList '/c', ("`"" + $PSScriptRoot + "\เปิดโปรแกรม.bat`"") -WindowStyle Minimized
Start-Sleep -Seconds 5
Start-Process "http://127.0.0.1:8750"
[System.Windows.Forms.MessageBox]::Show(("ตั้งคีย์ " + $name + " เรียบร้อย! เปิดโปรแกรมใหม่ให้แล้ว`nลองกดปุ่ม AI วิเคราะห์เจาะลึก บนการ์ดหุ้นได้เลย"),"สำเร็จ") | Out-Null
