#!/bin/sh
games="p2 p1 hl2 ep1 ep2 gmod csgo tf2 asw l4d l4d2 infra mesa"
game=$1
if [ $# -eq 0 ]; then
  echo Games: "${games[*]}" & echo Enter game to build. Use ALL to build every game. & read -p "" game
fi

copy_hammer_files() {
  echo "Copying Hammer files..."
  mkdir -p build/$1/postcompiler &&
  mkdir -p build/$1/hammer &&
  cp -rf hammer/modelsrc build/$1/hammer/modelsrc &&
  cp -rf hammer/scripts build/$1/hammer/scripts &&
  cp -rf instances build/$1/instances &&
  cp -rf transforms build/$1/postcompiler/transforms &&
  find ./build/$1/instances -iname "*.vmx" -delete # Yes, I know that we could use rsync with a ton of options to do this instead of using cp and then deleting unwanted files. This is FAR nicer imo.
  
  echo "Optimizing assets..."
  python3 src/hammeraddons/build_fgd_assets.py -i "build/$1/$1.fgd" -o "build/$1/hammer" -a "hammer"

  if [ $? -ne 0 ]; then
    echo "Failed copying Hammer files. Exitting." & exit 1
  fi
  return 0
}

build_game() {
  echo "Building FGD for $1..."
  mkdir -p build/$1
  python3 src/hammeraddons/unify_fgd.py exp $1 srctools -o "build/$1/$1.fgd"
  
  if [ $? -ne 0 ]; then
    echo "Building FGD for $1 has failed. Exitting." & exit 1
  fi
  return 0
}

if [ "${game^^}" = "ALL" ]; then 
  for i in $games 
    do
    build_game $i
    copy_hammer_files $i
  done
else
  for i in $games
    do
    if [ "$i" = "$game" ]; then 
      build_game $game
      copy_hammer_files $game
      exit
    fi
    echo "Unknown game. Exitting." & exit 1
  done
fi
