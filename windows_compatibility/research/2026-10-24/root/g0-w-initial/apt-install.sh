set -eux
test "$(id -u)" = 0
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-upgrade --no-install-recommends ca-certificates curl git build-essential bison flex pkg-config binutils patchelf libopenblas0 xz-utils
