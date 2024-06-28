mkdir build\p2ce

echo Building FGD...
python src/hammeraddons/unify_fgd.py e --srctools_only --collapse_bases SRCTOOLS P2 CSGO -o "build/p2ce/p2ce_postcompiler.fgd"

echo Copying postcompiler assets...
robocopy hammer/ build/hammer comp_* /S /PURGE

echo Copying VScript files (hammer/scripts)...
robocopy hammer/scripts build/hammer/scripts /S /PURGE

echo Copying Model Source files (hammer/modelsrc)...
robocopy hammer/modelsrc build/hammer/modelsrc /S /PURGE

REM is this required for github ci?
EXIT /B 0
