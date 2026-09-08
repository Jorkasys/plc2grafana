@echo off
rem Запуск plc2grafana в один клик.
rem При первом запуске создаётся виртуальное окружение и ставятся зависимости.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Создаём виртуальное окружение...
  py -3 -m venv .venv || python -m venv .venv || goto :nopython
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
)

".venv\Scripts\python.exe" run.py %*
goto :eof

:nopython
echo Не найден Python 3.11 или новее. Установите его с python.org и запустите снова.
pause
goto :eof

:fail
echo Не удалось установить зависимости.
pause
