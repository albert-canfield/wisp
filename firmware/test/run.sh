#!/bin/sh
# Builds and runs the wisp-core host tests. Also checks that core_* files stay portable.
set -e
cd "$(dirname "$0")/.."
if grep -nE '#include "(esphome|esp_|freertos|lwip)|#include <(esp_|freertos|lwip)' components/wisp/core_*.h; then
  echo "core_* files must not include ESPHome or ESP-IDF headers"
  exit 1
fi
mkdir -p test/build
for t in test_core test_grid test_hive; do
  ${CXX:-c++} -std=c++17 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-omit-frame-pointer \
    -Icomponents/wisp test/$t.cpp -o test/build/$t
  test/build/$t
done
