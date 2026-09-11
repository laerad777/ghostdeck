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
- `PATH`의 `adb` (Android SDK [platform-tools](https://developer.android.com/tools/releases/platform-tools))
- `PATH`의 `ffmpeg`
- URL 재생 시에만 `PATH`의 `yt-dlp`

ARM 기기 에이전트(agent, proxy, preload)는 git이 아니라 **GitHub
Releases** 자산입니다. `ghostdeck play`는 없으면
`~/.ghostdeck/bin/`으로 받고 저장소 매니페스트 해시와 대조합니다.

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
ARM 기기 바이너리를 `~/.ghostdeck/bin`에 컴파일한다. `clang`과
`armv7-linux-gnueabihf-gcc`가 필요하다.

`play` 전에 `adb`와 `ffmpeg`를 `PATH`에 두세요. 예 (Homebrew ffmpeg +
Google platform-tools):

```bash
export PATH="$PATH:$HOME/Library/Android/sdk/platform-tools"
```

동시 Studio 키는 공식 Studio의 **로컬 복사본**
(`~/Applications/Ulanzi Studio ADB.app`)이 필요하다. 그 앱은 git에 넣지 않는다.

## 명령

| 명령 | 역할 |
| --- | --- |
| `ghostdeck detect` | 시리얼, VID/PID, USB 모드. 없으면 실패. 시리얼은 런타임만. |
| `ghostdeck play FILE\|URL` | ADB JPEG 재생. IOHID 실패해도 play는 계속. |
| `ghostdeck studio` | 로컬 hidshim 복사본 (`~/Applications/Ulanzi Studio ADB.app`)을 연다. 공식 `/Applications/Ulanzi Studio.app`은 안 건드린다. |
| `ghostdeck stop` | 재생 중지, 스톡 UI, `/tmp` 에이전트 삭제. |
| `ghostdeck quit` | IOHID keeper가 있으면 종료. |
| `ghostdeck status` | USB, shim 복사본, IOHID, 재생. |

`ffmpeg`/`adb`가 없으면 `play`는 실패합니다. `yt-dlp`가 없으면 URL만
실패하고 로컬 파일은 재생됩니다.

호스트 상태는 `~/.ghostdeck/state.json`입니다. 로그는 터미널만 사용합니다.

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

공식 `/Applications/Ulanzi Studio.app`은 수정·배포하지 않는다.
IOHIDUserDevice는 Apple 권한 스파이크 실패이며 제품 경로가 아니다.
ADB 재생 중 하드웨어 버튼은 최선의 노력이다.

## 하지 않는 일

- `functions=hid,adb` 설정 또는 펌웨어 플래시
- Studio.app 재배포
- ARM 바이너리를 git에 커밋
- 기기 시리얼 하드코딩
