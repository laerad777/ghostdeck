# ghostdeck

[![ci](https://github.com/laerad777/ghostdeck/actions/workflows/ci.yml/badge.svg)](https://github.com/laerad777/ghostdeck/actions/workflows/ci.yml)

macOS에서 Ulanzi D200으로 JPEG를 재생합니다. 덱이 ADB일 때 Studio 키는
유저스페이스 가상 HID가 아니라 `ghostdeck studio`가 띄운 **로컬 hidshim
복사본**을 탑니다.

버전 **0.1.0**. MIT (`LICENSE`, `NOTICE`). English: [README.md](README.md).

이건 **체크아웃**이지 PyPI 패키지가 아닙니다. 휠만 설치하면 `vendor/`,
`device/`, `reference/`를 못 봅니다. 클론한 뒤 editable로 설치하십시오.

공식 Studio.app, 벤더 펌웨어, 커널 모듈은 배포하지 않습니다. 가젯
`functions=hid,adb`도 쓰지 않습니다. 시리얼은 런타임에만 읽고 git에 넣지
않습니다.

## 요구 사항

- macOS (0.1.0)
- Python 3.11+
- `PATH`의 `adb`, `ffmpeg`, `ffprobe` (URL이면 `yt-dlp`)
- `hidapi`, `pyusb` (`device` extra)
- Studio 키를 쓰려면 공식 `/Applications/Ulanzi Studio.app`. 없어도 `ghostdeck bridge`(또는 `ghostdeck play`)가 같은 영상 전송로를 띄웁니다. 잃는 건 버튼이지 재생이 아닙니다. `studio`는 앱이 필요하고 없으면 실패합니다.
- `~/.ghostdeck/bin/d200-color-agent` (ARM Linux 바이너리, 아래 참고)

`hidapi`나 `pyusb`가 없으면 `detect`/`status`/`play`는 설치 힌트와 함께
종료 코드 `2`입니다. `build`와 `stop`은 없어도 됩니다.

## 설치

```bash
git clone https://github.com/laerad777/ghostdeck.git
cd ghostdeck
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[device]"
```

`adb`와 `ffmpeg`를 `PATH`에 두십시오. Homebrew ffmpeg와 Android
platform-tools면 됩니다.

```bash
export PATH="$PATH:$HOME/Library/Android/sdk/platform-tools"
```

### 디바이스 에이전트

`d200-color-agent`는 git에 없습니다. 태그 릴리스에서 GitHub Actions가 ARMv7
Linux 바이너리를 만들어 `d200-color-agent`로 붙입니다. 설치:

```bash
mkdir -p ~/.ghostdeck/bin
curl -L -o ~/.ghostdeck/bin/d200-color-agent \
  https://github.com/laerad777/ghostdeck/releases/latest/download/d200-color-agent
chmod +x ~/.ghostdeck/bin/d200-color-agent
```

또는 `GHOSTDECK_AGENT_SOURCE`로 로컬 파일을 가리키십시오. `ghostdeck build`는
바이너리가 없으면 그 URL을 출력합니다. `device/build-color-agent.sh`는 ARMv7
Linux 크로스 gcc와 ARM Linux `libturbojpeg.a`가 필요합니다 (Homebrew jpeg-turbo는
Mach-O라 거절됩니다).

`ghostdeck build`는 zkgui 프록시/프리로드와 hidshim Studio 복사본을
컴파일합니다. Xcode CLT와 공식 Studio가 필요합니다. 에이전트 바이너리가
없으면 그 단계에서 실패합니다.

### 하드웨어 검사

`tools/hardware_verify.py`가 덱이 붙은 스모크 테스트입니다 (`GHOSTDECK_HW_TEST=1`).
호스티드 CI는 돌리지 않습니다. `d200` 라벨의 셀프호스티드 러너가 매일, 그리고
수동으로 돌립니다 (`.github/workflows/hardware.yml`). 잡은 2초 540×960
testsrc를 만들어 play→stop까지 검사합니다. 실제 파일을 쓰려면
`GHOSTDECK_HW_MEDIA`를 두십시오.

## 사용

```bash
ghostdeck studio          # hidshim 복사본 + 브리지. 먼저 실행 (공식 Studio 필요)
ghostdeck bridge          # 브리지만. Studio 없이
ghostdeck play video.mp4  # ADB JPEG 재생
ghostdeck stop            # 플레이어만 중지. Studio가 켜져 있으면 유지
ghostdeck gui             # 브라우저. 열린 영상 또는 파일을 덱에서 재생
```

`~/Applications/Ulanzi Studio ADB.app`을 직접 열지 마십시오. 브리지 없이
그 복사본의 심은 장치를 보지 못합니다.

## 명령

| 명령 | 하는 일 |
| --- | --- |
| `ghostdeck studio` | 로컬 hidshim 복사본과 `play`가 붙는 브리지를 시작합니다. 공식 앱이 필요하며 그 앱은 건드리지 않습니다. |
| `ghostdeck bridge` | 브리지만 시작합니다. 복사본도 공식 앱도 필요 없습니다. 영상 전송로는 그것들과 별개입니다. |
| `ghostdeck play FILE\|URL` | 실행 중인 브리지로 재생합니다. 브리지가 없으면 거부하지만, 공식 앱이 없는 호스트에서는 직접 브리지를 띄웁니다. |
| `ghostdeck stop` | 플레이어를 중지합니다. 브리지가 꺼져 있으면 스톡 UI를 복원하고 `/tmp/ghostdeck-*`를 지웁니다. 브리지가 살아 있으면 덱은 ADB로 남겨 Studio 키를 유지합니다. |
| `ghostdeck detect` | 시리얼, VID/PID, USB 모드. 덱이 없으면 실패. |
| `ghostdeck status` | USB 모드, 심 복사본, 재생 여부. |
| `ghostdeck gui` | 작은 브라우저. 열린 영상 또는 로컬 파일(파일 / 드롭 / Cmd-O)을 덱에서 재생. 같은 유튜브 영상의 광고는 처음부터 다시 돌리지 않습니다. 호스트 플레이어가 아닙니다. |

`play`는 런처입니다. 플레이어가 짧은 유예 시간을 넘기면 바로 돌아옵니다.
루프는 `stop`까지 계속됩니다.

## stop과 Studio

주인이 셋이고, `ghostdeck` 명령인 것은 하나입니다.

| 소유자 | 소유 대상 | 해제 |
| --- | --- | --- |
| `ghostdeck stop` | 플레이어. 스톡 UI와 `/tmp/ghostdeck-*`는 브리지가 **꺼져 있을 때만** | `ghostdeck stop` |
| 브리지 (`ghostdeck studio`) | 스테이징된 `/tmp/d200-color-agent`, 프레임버퍼 블랙아웃, ADB 유지 | 브리지 / hidshim 복사본 종료 |

브리지를 멈추는 `ghostdeck` 명령은 없습니다. Studio가 열린 채로 `stop`이
`0`인데 덱이 ADB인 것은 정상입니다. HID로 되돌리려면 hidshim 복사본을 끈
다음 `ghostdeck stop`을 실행하십시오.

## 종료 코드

| 코드 | 의미 |
| --- | --- |
| `0` | 성공 (`status`는 덱이 없어도 `0`) |
| `1` | 덱 없음, 또는 다른 실패 |
| `2` | `hidapi` / `pyusb` 없음 |
| `3` | 덱은 ADB로 붙어 있지만 전송이 명령을 실행하지 않음 (`offline`) |

`detect`와 `status`는 `adb devices`만 읽습니다. adb 서버를 재시작하지
않습니다. D200으로 식별되지 않은 기기에는 아무것도 보내지 않습니다.

`offline`은 케이블 문제가 아닙니다. 호스트에서 재연결해도 살아나지
않습니다. 전원을 껐다 켜거나 다시 꽂은 뒤 재시도하십시오.

## 하지 않는 일

- PyPI / 자립 휠
- 0.1.0에서 Linux 호스트 지원
- `functions=hid,adb`, 펌웨어, 커널 모듈
- Studio.app 재배포
- ARM 바이너리를 git에 넣기
- 시리얼 하드코딩

## 라이선스

MIT. PR은 `Signed-off-by`(DCO)가 필요합니다.
[CONTRIBUTING.md](CONTRIBUTING.md)를 보십시오.
