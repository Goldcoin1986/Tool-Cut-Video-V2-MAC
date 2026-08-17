@echo off
REM Build a Windows executable (--onedir folder build — see the
REM SPLASH_ARG section below for why this isn't --onefile anymore:
REM --onefile made every launch take ~10s to self-extract before the
REM window could even show up) with PyInstaller.
REM Requires: pip install pyinstaller (in addition to requirements.txt)
REM Also requires ffmpeg.exe and ffprobe.exe to be placed in an "ffmpeg"
REM folder next to this script before building, so they get bundled.
REM
REM Run this by double-clicking it, or from an already-open Command
REM Prompt with "build.bat" — either way it now pauses at the end so
REM you can always read what happened, success or failure.

setlocal

REM Always operate from the folder this script lives in, regardless of
REM how it was launched (double-click, a shortcut with a different
REM "Start in" folder, or run from an already-open Command Prompt sitting
REM in some other directory). Without this, every relative-path check
REM below (ffmpeg\ffmpeg.exe, assets\icons\app.ico, etc.) is silently
REM checked against the WRONG folder whenever the script isn't launched
REM with this folder as the current directory — which looks identical to
REM "the file just isn't there" from the warnings alone.
cd /d "%~dp0"

set APP_NAME=Tool Cut Video V1

echo ============================================================
echo  Building %APP_NAME%.exe
echo ============================================================
echo.

REM --- 1. Make sure Python itself is reachable -----------------------
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] "python" was not found on PATH.
    echo Install Python from https://www.python.org/downloads/ and make
    echo sure to tick "Add python.exe to PATH" during setup, then re-run
    echo this script.
    goto :fail
)

REM --- 2. Make sure PyInstaller and app dependencies are installed ----
REM Calling it as "python -m PyInstaller" instead of just "pyinstaller"
REM avoids the most common silent failure: pip installs pyinstaller.exe
REM into Python's Scripts folder, which on many Windows setups isn't on
REM PATH, so the bare "pyinstaller" command does nothing and this script
REM used to print "Build complete" anyway without building anything.
python -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
    echo PyInstaller not found — installing it now...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo [ERROR] "pip install pyinstaller" failed. Check your
        echo internet connection and the pip error above, then re-run.
        goto :fail
    )
)

REM --- 2a-pre. Install a CPU-ONLY PyTorch build BEFORE requirements.txt
REM -----------------------------------------------------------------
REM resemblyzer (needed for AI dubbing's speaker diarization) depends
REM on torch, but only ever uses it for CPU inference in this app (see
REM diarization.py's Diarizer — VoiceEncoder(device="cpu"), explicitly,
REM since this app can't assume the end user even HAS an NVIDIA GPU).
REM
REM Left to a plain "pip install torch" (which is what a bare
REM "pip install -r requirements.txt" would otherwise trigger, since
REM torch has no version pinned there beyond resemblyzer's own
REM ">=1.0.1"), PyPI's default torch wheel for Windows bundles the full
REM NVIDIA CUDA runtime (cuDNN, cuBLAS, etc.) — several GB by itself —
REM even though this app never uses any of it. Installing the CPU-only
REM build from PyTorch's own package index INSTEAD, first, means when
REM the requirements.txt install below gets to resemblyzer's torch
REM requirement, it's already satisfied by this (much smaller) CPU
REM build and pip won't try to replace it with the CUDA one.
REM
REM This one step is the single biggest fix for total download/disk
REM size for this feature — expect several GB less than installing the
REM default GPU build. It's Windows/pip-specific, which is exactly
REM what build.bat already is, so it belongs here rather than in
REM requirements.txt (which install-torch behavior should stay
REM platform-agnostic for anyone using it outside this script).
echo Installing CPU-only PyTorch (needed by resemblyzer for AI dubbing)
echo — this skips several GB of NVIDIA CUDA files this app never uses...
python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
if errorlevel 1 (
    echo [WARNING] CPU-only PyTorch install failed — continuing anyway.
    echo The next step may fall back to installing the much larger
    echo default GPU build of PyTorch instead. Check your internet
    echo connection if you want to retry this specifically.
)
echo.

if exist "requirements.txt" (
    echo Installing/updating app dependencies from requirements.txt...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] "pip install -r requirements.txt" failed. Check
        echo your internet connection and the pip error above.
        goto :fail
    )
)
echo.

REM --- 2a. Force-pin setuptools to a version that still HAS
REM pkg_resources — resemblyzer (needed for AI dubbing) needs it at
REM runtime to find its own bundled model file. requirements.txt
REM already declares "setuptools>=68.0.0,<82", so a plain
REM "pip install -r requirements.txt" above should already resolve to
REM a compatible version on its own — this extra explicit install is
REM belt-and-suspenders, not strictly required, but guarantees the
REM correct version lands even if pip's resolver ever treats an
REM already-installed newer setuptools as "good enough" and skips
REM re-resolving it (setuptools 82.0, Feb 2026, removed pkg_resources
REM entirely — not just deprecated it — so "some setuptools is
REM installed" isn't good enough, it has to be a pre-82 one).
echo Making sure setuptools is a pre-82 version (needed by resemblyzer
echo for AI dubbing — newer setuptools removed pkg_resources)...
python -m pip install "setuptools<82" >nul 2>nul
echo.

REM --- 2b. Remove the obsolete "typing" backport package, if present --
REM resemblyzer (a dependency added for the AI dubbing feature — see
REM requirements.txt) lists a package literally called "typing" in its
REM own metadata (Requires-Dist: typing). That's NOT the "typing"
REM module built into Python itself (which has existed since Python
REM 3.5 and is always available) — it's a long-dead, do-nothing PyPI
REM backport package with the same name, left over from when some
REM libraries still supported Python 2. Having it installed doesn't
REM break the app when just running "python main.py", but PyInstaller
REM specifically refuses to build with it present, because it shadows
REM the real stdlib "typing" module and breaks its own dependency
REM analysis. Safe to remove unconditionally: nothing in this app or
REM its dependencies needs the PACKAGE (only the built-in module,
REM which uninstalling this can't touch). ">nul 2>nul" + ignoring the
REM exit code below is deliberate — this must be a no-op, not a
REM failure, on the (default) case where it isn't installed at all.
python -m pip show typing >nul 2>nul
if not errorlevel 1 (
    echo Removing the obsolete "typing" backport package pulled in by
    echo resemblyzer ^(needed for AI dubbing^) — it breaks PyInstaller
    echo if left installed; see the comment above this line in
    echo build.bat for why this is safe...
    python -m pip uninstall -y typing >nul 2>nul
    echo.
)

REM --- 3. Optional bundled ffmpeg -------------------------------------
REM --add-data errors out if the source file doesn't exist at all, so
REM these flags are only included when ffmpeg.exe AND ffprobe.exe are
REM actually found. Checked in the 3 layouts people actually end up
REM with in practice, in this order:
REM   1) ffmpeg\ffmpeg.exe + ffmpeg\ffprobe.exe   — the documented layout
REM   2) ffmpeg.exe + ffprobe.exe directly next to this script — the
REM      simplest thing to do if you just drag the 2 .exe files here
REM   3) ffmpeg\bin\ffmpeg.exe + ffmpeg\bin\ffprobe.exe — what you get if
REM      you unzip an official build straight from ffmpeg.org
REM      (e.g. ffmpeg-release-essentials.zip) into an "ffmpeg" folder
REM      here without moving anything out of its own bin\ subfolder
REM Whichever layout is found, both .exe files are added individually
REM (not the whole folder) with an explicit "...;ffmpeg" destination, so
REM the built .exe always ends up with them in the same flat ffmpeg\
REM location that app\core\ffmpeg_locator.py looks for at runtime —
REM regardless of which layout they started in on disk.
set FFMPEG_ARG=
set FFMPEG_FOUND=

if exist "ffmpeg\ffmpeg.exe" if exist "ffmpeg\ffprobe.exe" goto :ffmpeg_subfolder
if exist "ffmpeg.exe" if exist "ffprobe.exe" goto :ffmpeg_root
if exist "ffmpeg\bin\ffmpeg.exe" if exist "ffmpeg\bin\ffprobe.exe" goto :ffmpeg_bin
goto :ffmpeg_not_found

:ffmpeg_subfolder
set FFMPEG_ARG=--add-data "ffmpeg\ffmpeg.exe;ffmpeg" --add-data "ffmpeg\ffprobe.exe;ffmpeg"
set FFMPEG_FOUND=1
goto :ffmpeg_done

:ffmpeg_root
set FFMPEG_ARG=--add-data "ffmpeg.exe;ffmpeg" --add-data "ffprobe.exe;ffmpeg"
set FFMPEG_FOUND=1
goto :ffmpeg_done

:ffmpeg_bin
set FFMPEG_ARG=--add-data "ffmpeg\bin\ffmpeg.exe;ffmpeg" --add-data "ffmpeg\bin\ffprobe.exe;ffmpeg"
set FFMPEG_FOUND=1
goto :ffmpeg_done

:ffmpeg_not_found
echo [WARNING] Could not find both ffmpeg.exe and ffprobe.exe. Checked:
echo   - ffmpeg\ffmpeg.exe + ffmpeg\ffprobe.exe
echo   - ffmpeg.exe + ffprobe.exe ^(next to this script^)
echo   - ffmpeg\bin\ffmpeg.exe + ffmpeg\bin\ffprobe.exe
echo The built app will rely on system PATH ffmpeg instead of a bundled
echo copy. Note BOTH ffmpeg.exe and ffprobe.exe are required — having
echo only one of the two also triggers this warning.

:ffmpeg_done
echo.

REM --- 4. Optional icon -------------------------------------------------
set ICON_ARG=
set ROOT_ICON_DATA_ARG=
if exist "assets\icons\app.ico" (
    set ICON_ARG=--icon "assets\icons\app.ico"
    REM Also bundle the same file as a plain DATA file (not just the
    REM exe resource icon) so main.py can load it at RUNTIME via
    REM setWindowIcon() — see _resolve_app_icon_path() in main.py for
    REM why: using this one identical icon for the exe resource, the
    REM splash hand-off, and the live running window/taskbar entry is
    REM what fixes "logo hiện, rồi logo nhỏ hơn khác đi" looking like
    REM two different logos in a row.
    set ROOT_ICON_DATA_ARG=--add-data "assets\icons\app.ico;assets\icons"
) else (
    echo [WARNING] assets\icons\app.ico not found. Building with the
    echo default PyInstaller icon instead.
)

REM --- 4b. Startup speed (VẤN ĐỀ: exe mất ~10s mới hiện cửa sổ) --------
REM Root cause was --onefile: EVERY launch, PyInstaller's bootloader
REM has to unpack the ENTIRE bundle (including the heavy AI-dubbing
REM deps — torch/librosa/scikit-learn/resemblyzer, see step 2a-pre
REM above) to a fresh %TEMP%\_MEIxxxxxx folder before Python — let
REM alone this app's own main.py — even starts. That's a structural
REM cost of --onefile itself; no amount of import reordering or lazy
REM loading inside main.py can shorten it, since it all happens before
REM any of that code runs.
REM
REM Fix: build with --onedir instead (see the PyInstaller call below).
REM --onedir extracts everything ONCE, at build time, into
REM dist\%APP_NAME%\ as a plain folder of files sitting next to the
REM .exe — so double-clicking the .exe at runtime just launches it
REM directly, no self-extraction step, which is what actually removes
REM the ~10s wait (a splash image can only ever mask that wait, not
REM remove it — this is the real fix). The trade-off: what used to be
REM one portable .exe file is now a whole folder — distribute/zip the
REM entire "dist\%APP_NAME%" folder, not just the .exe inside it, or
REM the app won't find its own bundled files (ffmpeg, models, etc.)
REM next time it's copied somewhere else.
REM
REM A splash screen is kept anyway (small residual delay from Qt/
REM torch/etc. initializing after the .exe itself has already started)
REM — needs a real splash IMAGE file to exist at assets\splash\
REM splash.png before building. Same "warn, don't fail the whole
REM build" pattern as ICON_ARG/FFMPEG_ARG above: if missing, the build
REM still proceeds, just without a splash. PyInstaller's --splash
REM needs Tcl/Tk (Python's own "tkinter" stdlib module) available in
REM the Python used to build — ships with the standard python.org
REM Windows installer by default; if PyInstaller reports it's missing,
REM re-run the Python installer and tick "tcl/tk and IDLE".
REM IMPORTANT: main.py must call pyi_splash.close() once MainWindow is
REM actually showing, or the splash image stays stuck on top of the
REM real window forever instead of disappearing — this build already
REM has that call; if main.py is ever replaced, keep it.
set SPLASH_ARG=
if exist "assets\splash\splash.png" (
    set SPLASH_ARG=--splash "assets\splash\splash.png"
) else (
    echo [WARNING] assets\splash\splash.png not found — building
    echo without a splash screen.
    echo To add one: drop a PNG logo at assets\splash\splash.png and
    echo re-run this script.
)
echo.

REM --- 5. Bundled watermark logo assets (X / Facebook / TikTok / YouTube) --
REM app\assets\icons\*.png are read at runtime via sys._MEIPASS (see
REM app/core/watermark_composer.py) — without this they'd be missing
REM from the built .exe and the platform-icon watermark would silently
REM fall back to plain text.
set WATERMARK_ASSETS_ARG=
if exist "app\assets\icons" (
    set WATERMARK_ASSETS_ARG=--add-data "app\assets;app\assets"
) else (
    echo [WARNING] app\assets\icons not found. The built app won't be
    echo able to draw the X/Facebook/TikTok/YouTube watermark icons.
)

REM --- 5b. Bundle resemblyzer's pretrained model file ------------------
REM resemblyzer (AI dubbing's speaker diarization, see
REM app/core/diarization.py) ships its pretrained voice-encoder weights
REM as a DATA file (pretrained.pt) sitting alongside its own Python
REM code in site-packages, loaded via pkg_resources.resource_filename()
REM at runtime — NOT downloaded, and NOT something PyInstaller finds on
REM its own. PyInstaller's automatic dependency scan only follows
REM Python imports; it has no way to know a package needs a specific
REM non-Python data file bundled too unless told explicitly (same
REM reason "app\assets\icons" needs its own --add-data above). Without
REM this step, the .exe builds and runs fine right up until AI dubbing
REM actually tries to diarize a clip, where it fails with "Couldn't
REM find the voice encoder pretrained model at ...\_MEI.....\
REM resemblyzer\pretrained.pt" and silently falls back to one default
REM voice — not a crash, but not the real per-speaker feature either.
REM
REM The exact site-packages path varies per machine/venv, so this asks
REM the currently-active Python where resemblyzer actually is (rather
REM than hardcoding a path) and only adds the flag if resemblyzer (and
REM its pretrained.pt) are actually found — same "warn, don't fail the
REM whole build" pattern as the ffmpeg/icon/watermark steps above, since
REM AI dubbing is an optional feature, not something every build needs.
set RESEMBLYZER_ARG=
set RESEMBLYZER_DIR=
for /f "delims=" %%i in ('python -c "import resemblyzer, os; print(os.path.dirname(resemblyzer.__file__))" 2^>nul') do set RESEMBLYZER_DIR=%%i
if defined RESEMBLYZER_DIR (
    if exist "%RESEMBLYZER_DIR%\pretrained.pt" (
        set RESEMBLYZER_ARG=--add-data "%RESEMBLYZER_DIR%\pretrained.pt;resemblyzer"
    ) else (
        echo [WARNING] resemblyzer is installed but its pretrained.pt
        echo file wasn't found at "%RESEMBLYZER_DIR%\pretrained.pt" —
        echo AI dubbing's speaker diarization will silently fall back to
        echo one default voice in the built .exe. Try:
        echo   pip install --force-reinstall --no-cache-dir resemblyzer
        echo then re-run this script.
    )
) else (
    echo [WARNING] resemblyzer isn't installed/importable — AI dubbing's
    echo speaker diarization won't work at all in the built .exe ^(it
    echo fails gracefully, one default voice, rather than crashing^).
    echo Make sure "pip install -r requirements.txt" above succeeded.
)
echo.

echo Running PyInstaller...
echo.

python -m PyInstaller ^
    --name "%APP_NAME%" ^
    --windowed ^
    --onedir ^
    %ICON_ARG% ^
    %ROOT_ICON_DATA_ARG% ^
    %SPLASH_ARG% ^
    %FFMPEG_ARG% ^
    %WATERMARK_ASSETS_ARG% ^
    %RESEMBLYZER_ARG% ^
    main.py

if errorlevel 1 (
    echo.
    echo [ERROR] PyInstaller reported an error — see the log above for
    echo the actual reason. Scroll up to find the first "ERROR" line.
    goto :fail
)

if not exist "dist\%APP_NAME%\%APP_NAME%.exe" (
    echo.
    echo [ERROR] PyInstaller finished but dist\%APP_NAME%\%APP_NAME%.exe
    echo was not created. Scroll up through the log above for the real
    echo cause.
    goto :fail
)

echo.
echo ============================================================
echo  SUCCESS: dist\%APP_NAME%\%APP_NAME%.exe
echo ============================================================
echo.
echo Tip: this is now a --onedir build (fast startup fix) — the app
echo lives in the WHOLE "dist\%APP_NAME%" folder, not just the .exe.
echo Copy/zip/share the entire folder, not the .exe alone, or the app
echo won't find its own bundled files ^(ffmpeg, resemblyzer model,
echo icons, etc.^) once moved elsewhere.
echo.
echo Tip: pip caches every wheel it downloads (torch, librosa, etc. —
echo the AI dubbing dependencies alone can be a few GB) under
echo %%LocalAppData%%\pip\cache, separate from what's actually installed
echo or bundled into the .exe above. If you're low on disk space now
echo that the build succeeded, "pip cache purge" reclaims that —
echo optional, and only makes the NEXT install-from-scratch slower
echo (everything just gets re-downloaded), it won't affect this .exe
echo or your currently-installed packages at all.
goto :end

:fail
echo.
echo ============================================================
echo  BUILD FAILED — see the messages above for why.
echo ============================================================

:end
endlocal
pause
