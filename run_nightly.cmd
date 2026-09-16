@echo off
rem Bear Camp nightly collection - invoked by Windows Task Scheduler.
rem Headless: no Claude session, no MCP. Runs against the last sanitized
rem workbook (data\master_safe.xlsx); the Monday assembly refreshes that.
setlocal
cd /d "C:\Users\mfish\Desktop\claude\projects\company-hub\bear-camp\bearcamp-revenue"
if not exist "data\logs" mkdir "data\logs"
set PYTHONIOENCODING=utf-8
echo.>> "data\logs\nightly.log"
echo ===== %DATE% %TIME% =====>> "data\logs\nightly.log"
"C:\Users\mfish\AppData\Local\Programs\Python\Python312-arm64\python.exe" collect_nightly.py >> "data\logs\nightly.log" 2>&1
echo exit code %ERRORLEVEL%>> "data\logs\nightly.log"
rem Publish the client dashboard document to the Fisher Family Hub (Firestore).
rem Skips itself with a log line if the service-account key is not installed.
"C:\Users\mfish\AppData\Local\Programs\Python\Python312-arm64\python.exe" publish_hub.py >> "data\logs\nightly.log" 2>&1
echo publish exit code %ERRORLEVEL%>> "data\logs\nightly.log"
endlocal
