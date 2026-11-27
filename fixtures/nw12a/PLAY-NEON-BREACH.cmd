@echo off
setlocal
cd /d "%~dp0"
echo.
echo   NEON BREACH  /  ROOM 07
echo   1   Play the 3D film with original procedural music
echo   2   Orbit atlas: optional 3D ring scene
echo   3   Portal tracker: optional 2D particle-history view
echo   4   Hotel echo: optional 2D image-feedback view
echo.
choice /C 1234 /N /T 5 /D 1 /M "Choose 1-4 [story plays after five seconds]: "
if errorlevel 255 exit /b 10
if errorlevel 4 (start "" "%~dp0chapters\hotel-echo.exe" & exit /b)
if errorlevel 3 (start "" "%~dp0chapters\portal-particles.exe" & exit /b)
if errorlevel 2 (start "" "%~dp0chapters\orbit-atlas.exe" & exit /b)
if errorlevel 1 start "" "%~dp0neon-breach.exe"
endlocal
