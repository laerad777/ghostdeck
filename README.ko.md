# ghostdeck

macOS에서 Ulanzi D200 계열 덱으로 JPEG 영상을 재생합니다. 물리 덱이
ADB일 때 공식 Ulanzi Studio가 키를 보낼 수 있도록 유저스페이스 가상
HID를 올릴 수 있습니다.

버전 **0.1.0**. 라이선스: MIT (`LICENSE`, `NOTICE`).
English: [README.md](README.md).

ghostdeck는 Studio.app, 벤더 펌웨어, 커널 모듈을 **배포하지 않습니다**.
가젯 `functions=hid,adb` 도 설정하지 않습니다.

## 요구 사항

- macOS (0.1.0 대상; Linux는 문서만)
- Python 3.11+
- git

`ghostdeck play`/`stop`/`detect`/`status`가 `PATH`에서 실행하는 프로그램:

- `adb` (Android SDK [platform-tools](https://developer.android.com/tools/releases/platform-tools))
- `ffmpeg`
- `ffprobe` (ffmpeg에 포함; `play`가 원본 fps를 먼저 측정)
- URL 재생 시에만 `yt-dlp`

`ghostdeck build`는 기기 바이너리와 hidshim Studio 복사본을 컴파일하므로 다음이 필요합니다:

- `armv7-linux-gnueabihf-gcc` (ARM Linux 크로스 툴체인; `play`가 기기 바이너리를 처음 빌드할 때도 필요)
- Xcode 커맨드 라인 툴: `clang`, `xcrun`, `install_name_tool`, `codesign`, `ditto`
- 공식 `/Applications/Ulanzi Studio.app` 설치

ARM 기기 바이너리(`d200-zkgui-proxy`, `libd200-zkgui-preload.so`,
`d200-color-agent`)는 git 파일이 아니며 내려받지 않습니다.
`ghostdeck build`가 `device/*.c`의 proxy와 preload를
`armv7-linux-gnueabihf-gcc`로 `~/.ghostdeck/bin/`에 컴파일합니다.
`d200-color-agent`는 자동으로 컴파일되지 않습니다.

`device/build-color-agent.sh`로 직접 빌드합니다. 이 스크립트는 두 가지
전제 조건을 검사하며, 일반 macOS에서는 두 번째가 충족되지 않습니다:

1. `armv7-linux-gnueabihf-gcc`를 제공하는 ARMv7 Linux 크로스 툴체인
   (Homebrew: `brew install armv7-unknown-linux-gnueabihf` — 포뮬러 이름과
   바이너리 이름이 다릅니다);
2. **ARM Linux**용 정적 `libturbojpeg.a`와 `turbojpeg.h`.

Homebrew의 `jpeg-turbo`는 두 번째를 충족하지 **않습니다**. Mach-O arm64
아카이브를 설치하기 때문에 ARM Linux 바이너리에 링크할 수 없습니다.
스크립트는 이를 감지해 exit 1과 함께 다음을 출력합니다:

> build-color-agent.sh: .../libturbojpeg.a contains no ELF object members
> (first inspected member was 'Mach-O 64-bit object arm64'). A macOS/Homebrew
> or Windows libturbojpeg cannot be linked into an ARM Linux binary. ...

0.1.0이 제시하는 유일한 경로는 실제 ARM Linux libturbojpeg입니다(예:
Debian/Ubuntu의 `libturbojpeg0-dev`). 다음과 같이 지정합니다:

```bash
TURBOJPEG_INC=/usr/arm-linux-gnueabihf/include \
TURBOJPEG_LIB=/usr/arm-linux-gnueabihf/lib \
  device/build-color-agent.sh
```

0.1.0에는 **macOS에서 에이전트를 만드는 간편한(turnkey) 방법이 없고**
내려받는 방법도 없습니다. 미리 빌드한 `d200-color-agent`를
`~/.ghostdeck/bin/`에 별도로 준비해야 합니다. 그 파일이 없으면
`ghostdeck build`는 에이전트 단계에서 실패합니다.

## 설치

이 디렉터리가 공개 레포 루트다. 부모 랩 트리는 올리지 마라.

```bash
git init
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
ghostdeck build
```

`ghostdeck build`가 공식 Studio를 로컬 복사하고 hidshim을 넣으며,
ARM 기기 바이너리를 `~/.ghostdeck/bin`에 컴파일한다. ARM 크로스
툴체인과 Xcode 커맨드 라인 툴이 필요하다. `adb`/`ffmpeg`는 필요 없다.
`~/.ghostdeck/bin/d200-color-agent`가 미리 있어야 한다(위 참조).

`play` 전에 `adb`와 `ffmpeg`를 `PATH`에 두세요. 예 (Homebrew ffmpeg +
Google platform-tools):

```bash
export PATH="$PATH:$HOME/Library/Android/sdk/platform-tools"
```

동시 Studio 키는 공식 Studio의 **로컬 복사본**
(`~/Applications/Ulanzi Studio ADB.app`)이 필요하다. 그 복사본은
로컬에서 만든다. 공식 `/Applications/Ulanzi Studio.app`은 쓰지도,
배포하지도 않는다.

## 명령

| 명령 | 역할 |
| --- | --- |
| `ghostdeck detect` | 시리얼, VID/PID, USB 모드. 없으면 실패. 시리얼은 런타임만. |
| `ghostdeck play FILE\|URL` | ADB JPEG 재생. IOHID 실패해도 play는 계속. |
| `ghostdeck studio` | 로컬 hidshim 복사본 (`~/Applications/Ulanzi Studio ADB.app`)을 연다. 공식 `/Applications/Ulanzi Studio.app`은 쓰지 않는다. |
| `ghostdeck stop` | 재생 중지, 스톡 UI 복원, 덱의 `/tmp/ghostdeck-*` 정리. |
| `ghostdeck quit` | IOHID keeper가 있으면 종료. |
| `ghostdeck status` | USB, shim 복사본, IOHID, 재생. |

`ffmpeg`/`ffprobe`/`adb`가 없으면 `play`는 실패합니다. `yt-dlp`가 없으면 URL만
실패하고 로컬 파일은 재생됩니다.

호스트 상태는 `~/.ghostdeck/state.json`입니다. `ghostdeck studio`는
hidshim 브리지의 표준 출력·오류를 `/tmp/d200-local-bridge.log`에
덧붙이고, 다른 로그는 터미널로 나옵니다.

## 덱이 붙어 있는데 응답하지 않을 때

USB에는 ADB로 열거되는데 `adbd`가 응답하지 않는 상태가 될 수 있습니다.
그러면 `adb devices -l`은 `device`가 아닌 상태로, 보통 `offline`으로
나타냅니다.

```
<serial>      offline usb:18092032X transport_id:1
```

호스트 쪽에서는 이것을 되살릴 수 없습니다. 이 프로젝트를 개발한 덱에서
160초 대기, `adb kill-server`/`start-server` 3회, `adb reconnect`,
USB 리셋을 모두 시도했지만 어느 것도 통하지 않았습니다.
**덱의 전원을 껐다 켜거나 USB 케이블을 다시 꽂은 뒤 명령을 다시
실행하십시오.** `offline`인 덱은 없는 덱과 다른 상태이며, 케이블
문제가 아닙니다.

`ghostdeck`은 세 가지 상태를 구분합니다.

| 상태 | `detect` | `status` | `stop` |
| --- | --- | --- | --- |
| 사용 가능 | 종료 `0`, `mode=adb` | 종료 `0`, `usb=adb` | 덱을 복원 |
| 붙어 있지만 adb 전송이 응답하지 않음 | 종료 `3`, `mode=adb (offline)` | 종료 `3`, `usb=adb (offline)` | 시리얼과 상태를 알리고 거부하며, 덱에 아무것도 보내지 않음 |
| 없음 | 종료 `1`, `no device` | 종료 `0`, `usb=none` | 종료 `1`, 덱이 없다고 알림 |

종료 코드: `0` 성공, `1` 덱 없음 또는 기타 실패, `2` 파이썬 환경을 쓸 수
없음 (`hidapi`/`pyusb` 누락), `3` 덱은 붙어 있지만 adb 전송이 명령을
실행할 수 없음.

`detect`와 `status`는 보고 명령이며 `adb devices`만 읽습니다. adb
서버를 재시작하지 않고, 덱으로 식별되지 않은 기기에는 아무것도 보내지
않습니다.

## 플러그인

스크립트를 `~/.ghostdeck/plugins`에 둡니다. 0.1.0은 폴더만 문서화하며
호출 규약은 **고정하지 않습니다**. Studio 스토어 플러그인이 아닙니다.

## 동시성 (hidshim)

물리 USB는 HID와 ADB를 동시에 열거할 수 없습니다. `play`는 실제 덱을
ADB로 둡니다. 공식 Studio는 가짜 USB를 보지 않습니다.

동시 키는 **hidshim**이다. 로컬 복사본
`~/Applications/Ulanzi Studio ADB.app`의 `libhidapi.0.dylib`가 우리
심이다. 프로세스 안에서 `2207:0019` / `ulanzi`로 열거하고
`/tmp/d200-adb-bridge.sock`으로 말한다. `ghostdeck studio`가 그 복사본을
연다.

공식 `/Applications/Ulanzi Studio.app`은 쓰지도, 배포하지도 않는다.
IOHIDUserDevice는 Apple 권한 스파이크 실패이며 제품 경로가 아니다.
ADB 재생 중 하드웨어 버튼은 최선의 노력이다.

## 하지 않는 일

- `functions=hid,adb` 설정 또는 펌웨어 플래시
- Studio.app 재배포
- ARM 바이너리를 git에 커밋
- 기기 시리얼 하드코딩
