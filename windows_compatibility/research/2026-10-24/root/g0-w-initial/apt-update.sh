set -eux
test "$(id -u)" = 0
apt-get update
apt-cache policy build-essential bison flex pkg-config patchelf libopenblas0
