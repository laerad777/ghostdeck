#!/bin/sh
# Build device/d200-color-agent.c for the D200 (ARMv7 Linux, hard-float) and
# install it as ~/.ghostdeck/bin/d200-color-agent.
#
# Prerequisites
# -------------
#  1. An ARM cross toolchain on PATH named `armv7-linux-gnueabihf-gcc`
#     (Homebrew: `brew install armv7-unknown-linux-gnueabihf` — note the
#     formula/binary name difference).
#  2. An *ARM Linux* libturbojpeg: `turbojpeg.h` plus `libturbojpeg.a`
#     (this agent links libjpeg-turbo's tj3* API statically). Debian/Ubuntu:
#       apt-get install libturbojpeg0-dev
#     then point this script at that sysroot with
#       TURBOJPEG_INC=/usr/arm-linux-gnueabihf/include \
#       TURBOJPEG_LIB=/usr/arm-linux-gnueabihf/lib
#     A macOS/Homebrew libturbojpeg is NOT usable here: Homebrew ships a Mach-O
#     arm64 .a/.dylib, while this link needs ELF ARM. The script reads the ELF
#     header directly and refuses to produce a broken binary.
#
# Link mode: dynamic, because this agent is a dlopen() client
# ----------------------------------------------------------
# device/d200-color-agent.c opens the deck's vendor libraries
# (/lib/libmi_sys.so, /lib/libmi_divp.so, /lib/libmi_disp.so) with dlopen() and
# resolves their symbols with dlsym(). Linking that translation unit with
# `-static` makes GCC emit
#   "Using 'dlopen' in statically linked applications requires at runtime the
#    shared libraries from the glibc version used for linking"
# and that warning is the defect, not noise: a statically linked glibc binary
# does not export its own libc symbols to a dlopen()ed object, so the vendor .so
# cannot resolve what it needs against this process, and the agent still needs
# the deck to carry the shared libraries of the exact glibc the cross toolchain
# linked against. `-static` therefore buys no self-containment for an agent
# whose whole job is to load the deck's shared libraries, while adding a version
# coupling that cannot be checked from the build host. The other device helper is
# already linked dynamically (src/ghostdeck/devicebuild.py::_compile_proxy_preload
# compiles the proxy with `-ldl -pthread` and no `-static`), so the agent matches
# it. Revisit `-static` only with a *confirmed* deck libc plus
# `-Wl,--export-dynamic`, validated on the device; nothing available on this host
# can confirm that.
#
# The ABI guards cannot be skipped (finding B-107)
# -----------------------------------------------
# They read the object header directly with POSIX `od` and never consult `file`,
# and a missing inspection tool (`od`, `ar`) is a hard failure with a
# "cannot verify the object ABI" message instead of a silently skipped check.
# The same message covers the other way a guard can lie: when the archiver on
# PATH cannot read the archive at all (a BSD `ar` reading a GNU archive lists
# members it then cannot extract), the archive is *not* reported as non-ELF — the
# script says it cannot verify instead of blaming a library that is probably fine.
# The toolchain's own archiver (`<target>-ar`) is preferred over a bare `ar` for
# exactly that reason.
#
# Cross-compile proof available in this repository's CI environment
# -----------------------------------------------------------------
# This host (macOS arm64) can only run the *syntax* check, because it has no ARM
# Linux libturbojpeg:
#     armv7-linux-gnueabihf-gcc -fsyntax-only -I/opt/homebrew/include device/d200-color-agent.c
# `-I/opt/homebrew/include` provides `turbojpeg.h` for the syntax check only.
# A real, runnable agent requires an ARM Linux static library as described above.
#
# Usage:  device/build-color-agent.sh [output-path]
#         device/build-color-agent.sh --check-abi FILE
# Exit:   0 on success, non-zero with an explicit message when a prerequisite is missing.
#
# `--check-abi` inspects one object (or one .a archive) and exits. It needs no
# toolchain, so the ABI guard can be exercised on its own:
#     device/build-color-agent.sh --check-abi /path/to/object.o

set -eu

CC="${CC:-armv7-linux-gnueabihf-gcc}"
SRC_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SRC="$SRC_DIR/d200-color-agent.c"
OUT="${1:-${HOME:-/tmp}/.ghostdeck/bin/d200-color-agent}"

die() {
    printf '%s\n' "build-color-agent.sh: $*" >&2
    exit 1
}

# --- object ABI inspection, independent of `file` (finding B-107) ----------
# The guards used to be gated on the `file` utility being installed, so on a host
# without it they silently did not run and a wrong-ABI object could still be linked.
# The ELF header is read directly with POSIX `od`, and a missing inspection tool
# is a hard failure instead of a skipped check.
#
#   offset  0 : 7f 45 4c 46   ELF magic
#   offset  4 : EI_CLASS      (01 = 32-bit ELF)
#   offset  5 : EI_DATA       (01 = little-endian)
#   offset 16 : e_type        (2 bytes, little-endian)
#   offset 18 : e_machine     (2 bytes, little-endian; 2800 = EM_ARM = ARMv7)
ELF_ARM_MACHINE=2800
ELF_ARCHIVE_MAGIC=213c617263683e0a

od_bytes() {  # od_bytes <offset> <count> <file> -> hex on stdout, empty on failure
    od -An -v -tx1 -j "$1" -N "$2" "$3" 2>/dev/null | tr -d ' \n'
}

require_od() {
    command -v od >/dev/null 2>&1 \
        || die "cannot verify the object ABI: 'od' is not on PATH (POSIX od is needed to read the ELF header). Refusing to build an object that cannot be inspected."
}

# Describes file ${1} in ABI_VERDICT
# (elf-arm|elf-aarch64|elf-other|macho|pe|unknown|unreadable) and ABI_DETAIL.
# Never calls `file`.
describe_object() {
    ABI_VERDICT=unreadable
    ABI_DETAIL=unreadable
    if [ ! -f "$1" ]; then
        ABI_DETAIL="no such file"
        return 0
    fi
    ABI_MAGIC=$(od_bytes 0 4 "$1")
    case "$ABI_MAGIC" in
        7f454c46) ;;
        cefaedfe|cffaedfe|feedface|feedfacf)
            ABI_VERDICT=macho
            ABI_DETAIL="Mach-O object (magic $ABI_MAGIC)"
            return 0
            ;;
        cafebabe|bebafeca)
            ABI_VERDICT=macho
            ABI_DETAIL="Mach-O universal binary (magic $ABI_MAGIC)"
            return 0
            ;;
        213c6172)
            ABI_VERDICT=unknown
            ABI_DETAIL="an ar archive, not an object file"
            return 0
            ;;
        "")
            ABI_DETAIL="empty or unreadable (no ELF magic)"
            return 0
            ;;
        *)
            ABI_VERDICT=unknown
            ABI_DETAIL="unrecognized object (first 4 bytes '$ABI_MAGIC')"
            return 0
            ;;
    esac
    ABI_CLASS=$(od_bytes 4 1 "$1")
    ABI_DATA=$(od_bytes 5 1 "$1")
    ABI_TYPE=$(od_bytes 16 2 "$1")
    ABI_MACHINE=$(od_bytes 18 2 "$1")
    if [ "${#ABI_CLASS}" != 2 ] || [ "${#ABI_DATA}" != 2 ] \
        || [ "${#ABI_TYPE}" != 4 ] || [ "${#ABI_MACHINE}" != 4 ]; then
        ABI_DETAIL="truncated ELF header"
        return 0
    fi
    case "$ABI_CLASS" in
        01) ABI_CLASS_NAME=32-bit ;;
        02) ABI_CLASS_NAME=64-bit ;;
        *) ABI_CLASS_NAME="class $ABI_CLASS" ;;
    esac
    case "$ABI_DATA" in
        01) ABI_DATA_NAME=little-endian ;;
        02) ABI_DATA_NAME=big-endian ;;
        *) ABI_DATA_NAME="byte order $ABI_DATA" ;;
    esac
    case "$ABI_MACHINE" in
        2800) ABI_MACHINE_NAME=ARM ;;
        b700) ABI_MACHINE_NAME=ARM64 ;;
        0300) ABI_MACHINE_NAME=i386 ;;
        3e00) ABI_MACHINE_NAME=x86_64 ;;
        *) ABI_MACHINE_NAME="unknown machine 0x$ABI_MACHINE" ;;
    esac
    case "$ABI_TYPE" in
        0100) ABI_TYPE_NAME="relocatable object" ;;
        0200) ABI_TYPE_NAME=executable ;;
        0300) ABI_TYPE_NAME="shared object" ;;
        *) ABI_TYPE_NAME="type 0x$ABI_TYPE" ;;
    esac
    ABI_DETAIL="ELF $ABI_CLASS_NAME $ABI_DATA_NAME $ABI_MACHINE_NAME $ABI_TYPE_NAME"
    case "$ABI_CLASS:$ABI_DATA:$ABI_MACHINE" in
        01:01:$ELF_ARM_MACHINE) ABI_VERDICT=elf-arm ;;
        01:01:b700|02:01:b700) ABI_VERDICT=elf-aarch64 ;;
        *) ABI_VERDICT=elf-other ;;
    esac
    return 0
}

# Fails closed unless ${1} is an ARMv7 little-endian ELF object.
require_elf_arm() {  # require_elf_arm <file> <what> [hint]
    require_od
    describe_object "$1"
    if [ "$ABI_VERDICT" = elf-arm ]; then
        return 0
    fi
    EXTRA_HINT="${3:-}"
    if [ "$ABI_VERDICT" = elf-aarch64 ]; then
        EXTRA_HINT="AArch64/ARM64 is a different machine, not a 64-bit spelling of this deck's ARMv7 target.${EXTRA_HINT:+ $EXTRA_HINT}"
    fi
    die "$2 is not an ARMv7 little-endian ELF: $ABI_DETAIL.${EXTRA_HINT:+ $EXTRA_HINT}"
}

# Fails closed unless ${1} is an archive holding at least one ARMv7 ELF member.
require_static_library_arm() {  # require_static_library_arm <path-to-.a>
    require_od
    AR_PROBE=""
    case "${CC##*/}" in
        *-gcc) AR_PROBE="${CC%-gcc}-ar" ;;
        *-cc) AR_PROBE="${CC%-cc}-ar" ;;
    esac
    if [ -z "${AR:-}" ]; then
        if [ -n "$AR_PROBE" ] && { [ -x "$AR_PROBE" ] || command -v "$AR_PROBE" >/dev/null 2>&1; }; then
            AR="$AR_PROBE"
        elif command -v ar >/dev/null 2>&1; then
            AR=ar
        else
            AR=""
        fi
    fi
    if [ -z "${AR:-}" ] || { [ ! -x "$AR" ] && ! command -v "$AR" >/dev/null 2>&1; }; then
        die "cannot verify the object ABI of $1: no usable 'ar' (AR='${AR:-}', toolchain archiver '${AR_PROBE:-<none>}'). Refusing to link a library that cannot be inspected."
    fi

    PROBE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/tjprobe.XXXXXX") \
        || die "cannot create a temp directory to inspect $1"
    trap 'rm -rf "$PROBE_DIR"' EXIT INT TERM

    # Listing the members needs no external tool: the loop below stops after 40
    # inspected members. `head` used to bound a pipeline here, and on a host without
    # it the guard inspected nothing and then blamed the library for being non-ELF.
    MEMBERS=$("$AR" t "$1" 2>/dev/null) \
        || die "cannot verify the object ABI of $1: '$AR' failed to list its members. Refusing to link a library that cannot be inspected."
    [ -n "$MEMBERS" ] \
        || die "cannot verify the object ABI of $1: '$AR' listed no members, so this archive cannot be the libturbojpeg that was asked for. Refusing to link a library that cannot be inspected."

    FIRST_MEMBER_DETAIL=""
    FOUND_MEMBER=""
    INSPECTED=0
    # `ar` may name a symbol-table member with a space (`__.SYMDEF SORTED`), and a BSD
    # `ar` reading a GNU archive lists real members with a trailing slash; such a token
    # simply fails `ar p` and is skipped. If no member can be extracted at all, the
    # archiver — not the library — is the problem, and that is a "cannot verify" hard
    # failure rather than a claim about the archive's contents.
    for MEMBER in $MEMBERS; do
        case "$MEMBER" in
            *SYMDEF*|*symbols*|/|//) continue ;;
        esac
        if [ "$INSPECTED" -ge 40 ]; then
            break
        fi
        "$AR" p "$1" "$MEMBER" >"$PROBE_DIR/m.o" 2>/dev/null || continue
        [ -s "$PROBE_DIR/m.o" ] || continue
        INSPECTED=$((INSPECTED + 1))
        describe_object "$PROBE_DIR/m.o"
        if [ -z "$FIRST_MEMBER_DETAIL" ]; then
            FIRST_MEMBER_DETAIL="$ABI_DETAIL"
        fi
        if [ "$ABI_VERDICT" = elf-arm ]; then
            FOUND_MEMBER="$MEMBER"
            break
        fi
    done
    if [ -z "$FOUND_MEMBER" ]; then
        if [ "$INSPECTED" -eq 0 ]; then
            die "cannot verify the object ABI of $1: '$AR' listed members but could not extract any of them ('$AR' cannot read this archive format). Refusing to link a library that cannot be inspected."
        fi
        die "$1 contains no ELF object members (first inspected member: ${FIRST_MEMBER_DETAIL:-nothing readable}). A macOS/Homebrew or Windows libturbojpeg cannot be linked into an ARM Linux binary. Install an ARM Linux libturbojpeg (Debian/Ubuntu: libturbojpeg0-dev) and set TURBOJPEG_LIB=/path/to/arm-linux/lib."
    fi
    printf '%s\n' "ok: $1 (first ARMv7 ELF member: $FOUND_MEMBER)"
}

case "${1:-}" in
    --check-abi)
        [ "$#" -ge 2 ] || die "'--check-abi' needs a file argument"
        require_od
        if [ -f "$2" ] && [ "$(od_bytes 0 8 "$2")" = "$ELF_ARCHIVE_MAGIC" ]; then
            require_static_library_arm "$2"
            exit 0
        fi
        require_elf_arm "$2" "the object" ""
        printf '%s\n' "ok: $2 ($ABI_DETAIL)"
        exit 0
        ;;
    --help|-h)
        printf '%s\n' "usage: ${0##*/} [output-path]"
        printf '%s\n' "       ${0##*/} --check-abi FILE"
        exit 0
        ;;
    --*)
        die "unknown option '$1'"
        ;;
esac

command -v "$CC" >/dev/null 2>&1 || die "cross compiler '$CC' not on PATH. Install an ARMv7 Linux hard-float toolchain (Homebrew: brew install armv7-unknown-linux-gnueabihf) or set CC=/path/to/armv7-linux-gnueabihf-gcc."

[ -f "$SRC" ] || die "source not found: $SRC"

# --- locate an ARM Linux libturbojpeg -------------------------------------
if [ -n "${TURBOJPEG_LIB:-}" ]; then
    LIBDIR="$TURBOJPEG_LIB"
else
    LIBDIR=""
    for candidate in /usr/arm-linux-gnueabihf/lib /usr/lib/arm-linux-gnueabihf \
                     /usr/local/lib/arm-linux-gnueabihf /opt/homebrew/lib /usr/local/lib; do
        if [ -f "$candidate/libturbojpeg.a" ]; then
            LIBDIR="$candidate"
            break
        fi
    done
fi
[ -n "$LIBDIR" ] || die "no libturbojpeg.a found. Install the ARM Linux development package (Debian/Ubuntu: apt-get install libturbojpeg0-dev) and set TURBOJPEG_LIB=/path/to/arm-linux/lib."

if [ -n "${TURBOJPEG_INC:-}" ]; then
    INCDIR="$TURBOJPEG_INC"
else
    INCDIR=""
    for candidate in "$LIBDIR/../include" /usr/arm-linux-gnueabihf/include \
                     /opt/homebrew/include /usr/local/include /usr/include; do
        if [ -f "$candidate/turbojpeg.h" ]; then
            INCDIR="$candidate"
            break
        fi
    done
fi
[ -n "$INCDIR" ] || die "turbojpeg.h not found. Set TURBOJPEG_INC=/path/to/arm-linux/include (Debian/Ubuntu package libturbojpeg0-dev provides it)."

# --- reject a host (non-ELF-ARM) static library ---------------------------
# This is the trap on a macOS/Homebrew host: libturbojpeg.a exists but is a
# Mach-O arm64 archive, so the ARM cross link fails with undefined tj3* symbols
# or produces an unusable binary. Fail loudly with the real reason instead.
#
# The archive is accepted only when at least one real member is an ARMv7 ELF
# object. Mach-O archives list a `__.SYMDEF SORTED` symbol-table member, so a
# single-member probe is not enough. The guard cannot be skipped: see
# require_static_library_arm().
require_static_library_arm "$LIBDIR/libturbojpeg.a"

# --- build ----------------------------------------------------------------
OUT_DIR=$(dirname -- "$OUT")
mkdir -p "$OUT_DIR" || die "cannot create output directory: $OUT_DIR"

printf '%s\n' "building $OUT"
printf '%s\n' "  compiler : $CC"
printf '%s\n' "  includes : $INCDIR"
printf '%s\n' "  library  : $LIBDIR/libturbojpeg.a"

# No `-static` here: see the "Link mode" note at the top of this script. A statically
# linked glibc does not export its symbols to the vendor .so files this agent dlopen()s.
"$CC" -O2 -Wall -Wextra \
    "$SRC" \
    -I"$INCDIR" \
    -I"$SRC_DIR" \
    -o "$OUT" \
    "$LIBDIR/libturbojpeg.a" \
    -lpthread -lm -ldl \
    || die "cross build failed. Confirm the ARM Linux static libturbojpeg matches this toolchain's target (see the header of this script)."

chmod +x "$OUT" 2>/dev/null || true

# --- verify the artifact is really an ARMv7 Linux ELF ---------------------
require_elf_arm "$OUT" "the built artifact" "Refusing to install it as a device binary."
printf '%s\n' "ok: $OUT ($ABI_DETAIL)"
