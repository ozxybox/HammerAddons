#!/bin/bash

mkdir -p build/p2ce
mkdir -p build/hammer
mkdir -p build/bin/win64

echo "Building FGD..."
python3 src/hammeraddons/unify_fgd.py e --srctools_only --collapse_bases SRCTOOLS P2 CSGO -o "build/p2ce/p2ce_postcompiler.fgd"

echo "Copying postcompiler assets..."
# This is much easier with robocopy...
# Copy only the files prefixed with comp_
find "hammer" -type f -name "comp_*" | while read -r file; do
    file_path="${file#hammer/}"
    mkdir -p "build/hammer/$(dirname "$file_path")"
    cp "$file" "build/hammer/$file_path"
done

echo "Copying VScript files (hammer/scripts)..."
mkdir -p build/hammer/scripts
cp -r hammer/scripts/* build/hammer/scripts/

echo "Copying Model Source files (hammer/modelsrc)..."
mkdir -p build/hammer/modelsrc
cp -r hammer/modelsrc/* build/hammer/modelsrc/
