@echo off
echo Creating scheduled task: FlightSearch_TLV_BKK (every 4 hours)
schtasks /Create /SC HOURLY /MO 4 /TN "FlightSearch_TLV_BKK" /TR "\"%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe\" \"C:\tmp\claudeflights\search_flights.py\"" /ST 06:00 /F
if %ERRORLEVEL% EQU 0 (
    echo Task created successfully.
    echo.
    echo To run immediately:  schtasks /Run /TN "FlightSearch_TLV_BKK"
    echo To check status:     schtasks /Query /TN "FlightSearch_TLV_BKK"
    echo To delete:           schtasks /Delete /TN "FlightSearch_TLV_BKK" /F
) else (
    echo Failed to create task. Try running this script as Administrator.
)
pause
